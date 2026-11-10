"""Uma chamada externa por etapa; reaproveita o HTTP protegido e a classificação da POC."""
import asyncio
import json
import re
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from app.config import Settings
from app.models import Company, Validation, ProviderError
from app.security import tls_context
from app.services.importer import normalize_cnpj
from app.services.search_queries import queries_for
from app.services.site_rules import RULES_VERSION, rank_candidates, url_reason, candidate_groups, domain_brand_match, tenant_brand_match
from app.services.site_outcome import public_validation, result_status
from app.services.validator import SafeFetcher, classify, same_site, safe_url, VisibleText, tenant_identity_evidence, USER_AGENT, ROBOT_TOKEN
from app.services.references import useful_references
from app.web.results import public_result, link
from app.providers.base import retry_after
from app.providers.search import search_urls, SEARCH_ENDPOINT

# Orçamento por consulta: até 3 sites candidatos distintos e 8 acessos (robots,
# redirects e páginas). Cada acesso continua sendo uma etapa HTTP persistida.
MAX_SITES = 3
MAX_WEB_CALLS = 8
# Falhas do site candidato não invalidam o cadastro nem a busca já obtidos.
SITE_REASONS = {
    'ROBOTS_BLOQUEADO': 'o robots.txt do site não permite a verificação automática',
    'ROBOTS_INDISPONIVEL': 'o robots.txt do site não pôde ser lido',
    'SITE_INACESSIVEL': 'o site não respondeu ou recusou o acesso',
    'LIMITE_TEMPORARIO': 'o site limitou temporariamente os acessos',
    'CONTEUDO_MUITO_GRANDE': 'a página excede o tamanho máximo analisado',
    'CONTEUDO_NAO_SUPORTADO': 'o formato da página não é suportado',
    'URL_INSEGURA': 'o endereço foi bloqueado pela proteção de rede',
    'LIMITE_VALIDACAO': 'o limite de acessos de validação desta consulta foi atingido',
    'REDIRECIONAMENTO_IDENTIDADE': 'a página redireciona para outro domínio',
}
for _code in ('LOOP_REDIRECIONAMENTO', 'LOOP_REDIRECIONAMENTO_ROBOTS', 'REDIRECIONAMENTOS_EXCESSIVOS', 'REDIRECIONAMENTO_INVALIDO'):
    SITE_REASONS[_code] = 'o site apresenta redirecionamentos inválidos ou em excesso'


def site_reason(code, http_status=None):
    reason = SITE_REASONS.get(code, 'falha técnica na verificação do site')
    return reason + (f' (HTTP {http_status})' if type(http_status) is int else '')


def brand_site(company, url):
    """Domínio ou subdomínio (tenant) com a marca. Só indício: nunca confirma vínculo."""
    host = urlsplit(url).hostname or ''
    return domain_brand_match(company, host) or tenant_brand_match(company, host)


async def provider_json(method, url, *, key='', payload=None):
    headers={'User-Agent':USER_AGENT}
    if key:headers['X-API-KEY']=key
    async with httpx.AsyncClient(trust_env=False,verify=tls_context(),follow_redirects=False,timeout=8) as client:
        response=await client.request(method,url,json=payload,headers=headers)
    if response.status_code!=200:
        raise ProviderError('COTA_ESGOTADA' if response.status_code==402 else 'ACESSO_NEGADO' if response.status_code in {401,403} else 'LIMITE_TEMPORARIO' if response.status_code==429 else 'CNPJ_NAO_ENCONTRADO' if response.status_code==404 else 'ERRO_PROVEDOR',
            details={'retry_after':max(60,retry_after(response.headers.get('retry-after')))} if response.status_code in {429,503} else {})
    if len(response.content)>1_000_000:raise ProviderError('RESPOSTA_INVALIDA')
    return response.json()


class SingleReservation:
    """A reserva global já foi persistida no PostgreSQL antes de entrar no HTTP."""
    def __init__(self):self.db=self;self.used=False
    def get_state(self,*args):return 0
    def set_state(self,*args):pass
    async def reserve(self,provider):
        if self.used:raise ProviderError('ETAPA_EXCEDIDA')
        self.used=True;return 1
    def finish(self,*args):pass
    def cooldown(self,*args):pass


ROBOTS_MAX_BYTES = 500 * 1024  # RFC 9309 §2.5: analisar ao menos 500 KiB
ROBOTS_LINE = re.compile(r'^\s*(user-agent|allow|disallow|crawl-delay)\s*:\s*([^#]*?)\s*(?:#.*)?$', re.I)


async def fetch_one(url, robots=False):
    # Páginas: até 1 MB, como na POC. robots.txt: só os primeiros 500 KiB, de
    # qualquer tipo de conteúdo, sem falhar por tamanho (RFC 9309).
    settings=Settings(timeout=8,retries=0,max_bytes=ROBOTS_MAX_BYTES if robots else 1_000_000,website_delay=10)
    # one() fixa o IP público, verifica TLS/Host e não segue redirects. Robots e
    # redirects são etapas persistidas separadamente, sob o controle global.
    return await SafeFetcher(SingleReservation(),settings,truncate=robots,any_text=robots).one(url)


def robots_rules(text):
    """Grupos do robots.txt que se aplicam ao robô ('*' ou o token DazumCNPJ).
    Linhas inválidas, longas demais ou HTML são ignoradas, como prevê a RFC 9309;
    só as regras aplicáveis ficam no estado persistido."""
    token=ROBOT_TOKEN.lower()
    def applies(agent):
        agent=agent.split('/')[0].strip().lower()
        return agent=='*' or (bool(agent) and agent in token)
    kept,agents,relevant,in_agents=[],[],False,False
    for raw in text.splitlines():
        match=ROBOTS_LINE.match(raw) if len(raw)<=2048 else None
        if not match:
            continue
        field,value=match.group(1).lower(),match.group(2).strip()
        if field=='user-agent':
            if not in_agents:
                agents,relevant,in_agents=[],False,True
            if applies(value):
                agents.append(value);relevant=True
            continue
        if in_agents:
            in_agents=False
            if relevant:kept.extend('User-agent: '+agent for agent in agents)
        if relevant:
            kept.append({'allow':'Allow','disallow':'Disallow','crawl-delay':'Crawl-delay'}[field]+': '+value)
    return kept[:5000]


def payload_for(state, partial=False):
    company=Company(**state['company'])
    result=public_validation(Validation(**state.get('validation',{})))
    urls=state.get('candidates',[])
    refs=useful_references(company,urls,result.page_evidence,result.website)
    record=dict(automatic_status=result_status(result),source_updated_at=state['created_at'],
                social_candidates=candidate_groups(urls)['social_networks'])
    payload=public_result(record,company.__dict__,result.to_dict(),refs)
    payload['origem_resultado']='PESQUISA_REAL'
    for item in payload['possiveis_sites']:
        failed=next((f for f in state.get('site_failures',[]) if same_site(f['url'],item['url'])),None)
        if failed:item['ressalvas']=['Validação não concluída: '+site_reason(failed['error'],failed.get('http_status'))+'.']
    payload['possiveis_sites'].extend(other_sites(company,state,payload))
    mark_google_panel(payload,state)
    payload['fontes']=[dict(tipo='CADASTRO',nome='Minha Receita — dados públicos da Receita Federal',
        url='https://minhareceita.org/'+company.cnpj,consultado_em=state['created_at'])]
    if state.get('searched_at'):
        payload['fontes'].append(dict(tipo='DESCOBERTA',nome='Serper (pesquisa Google)',consultado_em=state['searched_at']))
    if state.get('web_calls'):
        payload['fontes'].append(dict(tipo='VALIDACAO',nome='Páginas públicas dos sites candidatos, respeitando robots.txt',
            acessos=state['web_calls']))
    payload['ressalvas']=['Dados cadastrais de base pública; podem estar defasados em relação à situação atual.']
    if partial:payload['ressalvas'].append('Pesquisa parcial por falha técnica. Nenhum token comercial foi consumido.')
    return payload


def candidate_sites(company,cnpj,urls,limit=MAX_SITES,first=None):
    """Um endereço por site: primeiro o indicado pelo painel do Google, depois
    domínios compatíveis com a marca. Ordem de validação, não de confiança."""
    sites=[]
    ranked=rank_candidates(company,[u for u in urls if not url_reason(u,cnpj)])
    if first and first in ranked:
        ranked=[first]+[u for u in ranked if u!=first]
    for url in ranked:
        if not any(same_site(url,site) for site in sites):sites.append(url)
    return sites[:limit]


def preferred_site(company,url,state):
    """Candidato que pode aparecer como possível site mesmo sem validação."""
    panel=state.get('google_panel_site')
    return brand_site(company,url) or bool(panel and same_site(url,panel))


def mark_google_panel(payload,state):
    panel=state.get('google_panel_site')
    if not panel:
        return
    for item in [payload.get('website_oficial')]+payload['possiveis_sites']+payload['referencias']+payload['redes_sociais']:
        if item and same_site(item['url'],panel):
            item['indicado_google']=True
            if item.get('tipo')=='POSSIVEL_SITE':
                item['ressalvas']=list(item.get('ressalvas') or [])+['Indicado pelo painel do Google; isso não confirma a titularidade.']


def other_sites(company,state,payload):
    """Demais candidatos plausíveis, sempre como possíveis sites e nunca como oficiais."""
    shown=[i['url'] for i in payload['possiveis_sites']]+([payload['website_oficial']['url']] if payload['website_oficial'] else [])
    rejected=[v.get('website','') for v in state.get('site_results',[]) if v.get('site_type') in {'THIRD_PARTY','CNPJ_DIVERGENTE'}]
    visited={v.get('website',''):v for v in state.get('site_results',[]) if v.get('website')}
    extra=[]
    for url in candidate_sites(company,company.cnpj,state.get('candidates',[]),limit=4,first=state.get('google_panel_site')):
        if any(same_site(url,u) for u in shown+rejected if u):continue
        failed=next((f for f in state.get('site_failures',[]) if same_site(f['url'],url)),None)
        seen=next((v for u,v in visited.items() if same_site(u,url)),None)
        # Páginas de terceiros sem a marca (associações, catálogos) não viram
        # "possível site" só por terem falhado ou não terem sido visitadas.
        if seen and (seen.get('confidence')=='MEDIA' or preferred_site(company,url,state)):
            item=link(seen['website'],'POSSIVEL_SITE',seen.get('confidence') if seen.get('confidence')=='MEDIA' else 'BAIXA',
                      'Página acessada; as evidências não confirmam a titularidade.')
        elif not preferred_site(company,url,state):
            continue
        elif failed:
            item=link(url,'POSSIVEL_SITE','PENDENTE','Encontrado na pesquisa; a validação técnica não pôde ser concluída.',
                      ressalvas=['Validação não concluída: '+site_reason(failed['error'],failed.get('http_status'))+'.'])
        elif not seen:
            item=link(url,'POSSIVEL_SITE','NAO_CONFIRMADO','Domínio compatível com o nome da empresa; não visitado nesta consulta.')
        else:
            continue
        if item:extra.append(item)
    return extra[:3]


def merged_validation(company, state, current):
    values=state.get('validations',[])+[current.to_dict()]
    facts=[f for value in values for f in value.get('page_evidence',[])]
    first=Validation(**values[0])
    if first.website and any(not same_site(f['url'],first.website) for f in facts):
        raise ProviderError('REDIRECIONAMENTO_IDENTIDADE')
    if current.site_type in {'THIRD_PARTY','CNPJ_DIVERGENTE'}:
        return current,values
    signals=dict(first.signals)
    for flag in ['exact_cnpj','name_match','legal_owner']:
        signals[flag]=any(value.get('signals',{}).get(flag) is True for value in values)
    numbers={cnpj for f in facts for cnpj in f.get('cnpjs',[])}
    if numbers and numbers!={company.cnpj}:
        return Validation(site_type='CNPJ_DIVERGENTE',evidence=['Inscrições divergentes nas páginas institucionais']),values
    paths=tenant_identity_evidence(company,urlsplit(first.website).hostname or '',facts)
    signals.update(tenant_identity_confirmed=bool(paths),tenant_evidence_paths=paths)
    strong=all(signals.get(k) is True for k in ['exact_cnpj','name_match','legal_owner','heading_brand']) and (signals.get('brand_domain') is True or bool(paths))
    combined=replace(first,confirmed=strong,confidence='ALTA' if strong else first.confidence,
        site_type='CORPORATE' if strong else first.site_type,signals=signals,page_evidence=facts,
        inspected_urls=[f['url'] for f in facts])
    return combined,values


def action_for(state):
    phase=state['phase']
    if phase=='CADASTRO':return 'minhareceita'
    if phase=='BUSCA':return 'serper'
    if phase=='WEBSITE':return 'websites'
    return None


async def execute_action(cnpj, state, key):
    state=dict(state)
    if state['phase']=='CADASTRO':
        data=await provider_json('GET','https://minhareceita.org/'+cnpj)
        if normalize_cnpj(data.get('cnpj'))!=cnpj or not isinstance(data.get('razao_social'),str) or not data['razao_social'].strip():raise ProviderError('CADASTRO_DIVERGENTE')
        def field(name):
            value=data.get(name)
            if isinstance(value,(dict,list,bool)):raise ProviderError('RESPOSTA_INVALIDA')
            return str(value or '')[:300]
        company=Company(cnpj,field('razao_social'),field('nome_fantasia'),field('municipio'),field('uf'),field('descricao_situacao_cadastral'))
        state.update(company=company.__dict__,phase='BUSCA')
    elif state['phase']=='BUSCA':
        company=Company(**state['company'])
        data=await provider_json('POST',SEARCH_ENDPOINT,key=key,
            payload={'q':queries_for(company)[0],'gl':'br','hl':'pt-br','num':10})
        if not isinstance(data.get('organic'),list):raise ProviderError('RESPOSTA_INVALIDA')
        urls=search_urls(data)
        candidates=[]
        for url in urls:
            try:clean,_,_=safe_url(url)
            except (ValueError,TypeError):continue
            if clean not in candidates:candidates.append(clean)
        # Só a URL do painel do Google é guardada (atribuição), nunca o painel.
        graph=data.get('knowledgeGraph')
        panel=None
        if isinstance(graph,dict) and isinstance(graph.get('website'),str):
            try:panel=safe_url(graph['website'])[0]
            except (ValueError,TypeError):panel=None
        state['google_panel_site']=panel
        sites=candidate_sites(company,cnpj,candidates,first=panel)
        state.update(candidates=candidates[:10],searched_at=datetime.now(timezone.utc).isoformat(),robots={},
            queue=sites[:1],sites=sites[1:],current_site=sites[0] if sites else None,site_results=[],site_failures=[],
            redirects=[],web_calls=0,validations=[],phase='WEBSITE' if sites else 'DONE')
    elif state['phase']=='WEBSITE':
        company=Company(**state['company'])
        state.setdefault('current_site',(state.get('queue') or [None])[0])
        if state.get('web_calls',0)>=MAX_WEB_CALLS:
            return close_site(state,'LIMITE_VALIDACAO')
        if not state.get('queue'):
            return close_site(state)
        try:
            await website_step(company,cnpj,state)
        except ProviderError as exc:
            status=exc.details.get('http_status')
            page_failed(state,exc.code,status if type(status) is int else None)
        except ValueError:
            # UnsafeURL é ValueError; nenhuma mensagem bruta (URL/IP) é persistida.
            page_failed(state,'URL_INSEGURA')
        except OSError:
            page_failed(state,'SITE_INACESSIVEL')
        if state['phase']=='WEBSITE' and state.get('web_calls',0)>=MAX_WEB_CALLS:
            # Encerra já, para não reservar uma etapa que não poderia acessar a rede.
            close_site(state,'LIMITE_VALIDACAO')
    return state


def quality(value):
    return ({'ALTA':3,'MEDIA':2,'BAIXA':1}.get(value.get('confidence'),0),value.get('score',0))


def best_site(state):
    usable=[v for v in state.get('site_results',[]) if v.get('website') and v.get('site_type') not in {'THIRD_PARTY','CNPJ_DIVERGENTE'}]
    return max(usable,key=quality) if usable else None


def close_site(state,failure=None,http_status=None):
    """Encerra o site atual, registrando a validação obtida ou a falha técnica."""
    if state.get('site_validation'):
        state['site_results']=state.get('site_results',[])+[state['site_validation']]
    elif failure and state.get('current_site'):
        record=dict(url=state['current_site'],error=failure)
        if http_status:record['http_status']=http_status
        state['site_failures']=state.get('site_failures',[])+[record]
    state.update(site_validation=None,validations=[],queue=[],redirects=[],robots_url=None,robots_redirects=[])
    best=best_site(state)
    remaining=list(state.get('sites',[]))
    if (best and (best.get('confirmed') or best.get('confidence')=='MEDIA')) or not remaining or state.get('web_calls',0)>=MAX_WEB_CALLS:
        return finish_websites(state)
    state.update(current_site=remaining[0],queue=remaining[:1],sites=remaining[1:])
    return state


def stop_websites(state,code='LIMITE_VALIDACAO'):
    """Encerra a validação sem rede (ex.: cota de websites esgotada), preservando
    cadastro, busca e evidências já obtidos."""
    state=dict(state)
    if not state.get('site_validation') and state.get('current_site'):
        state['site_failures']=state.get('site_failures',[])+[dict(url=state['current_site'],error=code)]
    return finish_websites(state)


def finish_websites(state):
    if state.get('site_validation'):
        state['site_results']=state.get('site_results',[])+[state['site_validation']]
    best=best_site(state)
    failures=state.get('site_failures',[])
    company=Company(**state['company'])
    branded=[f for f in failures if preferred_site(company,f['url'],state)]
    if best:
        final=best
    elif branded:
        final=Validation(confidence='PENDENTE',rules_version=RULES_VERSION,site_type='VALIDATION_ERROR',
            signals={'technical_errors':[f['error'] for f in failures],'candidate_url':branded[0]['url']},
            evidence=['Falha técnica ao validar candidato; vínculo não avaliado']+[f['error'] for f in failures]).to_dict()
    elif failures:
        # Só páginas sem a marca falharam: não há candidato a site oficial a revisar.
        final=Validation(rules_version=RULES_VERSION,evidence=['SEM_SITE_OFICIAL_CONFIRMADO']+[f['error'] for f in failures],
            signals={'technical_errors':[f['error'] for f in failures]}).to_dict()
    elif state.get('site_results'):
        final=state['site_results'][0]
    else:
        final=Validation().to_dict()
    state.update(validation=final,phase='DONE',queue=[],sites=[],site_validation=None,validations=[],redirects=[])
    return state


def page_failed(state,code,http_status=None):
    """Página secundária falha sem perder o site; a inicial encerra só o candidato."""
    if code=='TERCEIRO' and not state.get('validations'):
        rejected=Validation(website=state.get('current_site') or '',site_type='THIRD_PARTY',rules_version=RULES_VERSION,
                            evidence=['Redireciona para domínio de terceiro'])
        state['site_validation']=rejected.to_dict()
        return close_site(state)
    if state.get('validations'):
        failure=dict(url=(state.get('queue') or [''])[0],error=code)
        if http_status:failure['http_status']=http_status
        state['page_failures']=state.get('page_failures',[])+[failure]
        state.update(queue=state['queue'][1:],redirects=[],robots_url=None,robots_redirects=[])
        return state if state['queue'] else close_site(state)
    return close_site(state,code,http_status)


async def website_step(company,cnpj,state):
    """Um único acesso HTTP. Atualiza o estado no lugar; erros ficam com o chamador."""
    target=state['queue'][0]
    clean,_,_=safe_url(target)
    if url_reason(clean,cnpj):raise ProviderError('TERCEIRO')
    if state.get('validations') and not same_site(clean,state['validations'][0]['website']):raise ProviderError('REDIRECIONAMENTO_IDENTIDADE')
    parts=urlsplit(clean);origin=parts.scheme+'://'+parts.netloc
    robots=dict(state.get('robots',{}))
    if origin not in robots:
        robots_url=state.get('robots_url') or origin+'/robots.txt'
        # O acesso conta mesmo se falhar: a reserva técnica já foi gravada.
        state['web_calls']=state.get('web_calls',0)+1
        page=await fetch_one(robots_url,robots=True)
        if page.status in {429,503}:raise ProviderError('LIMITE_TEMPORARIO',details={'retry_after':max(60,retry_after(page.headers.get('retry-after')))})
        if page.status in {301,302,303,307,308}:
            chain=state.get('robots_redirects',[])
            destination=safe_url(urljoin(robots_url,page.headers.get('location','')))[0]
            if len(chain)>=5 or destination in chain:raise ProviderError('LOOP_REDIRECIONAMENTO_ROBOTS')
            state.update(robots_url=destination,robots_redirects=chain+[robots_url]);return state
        # RFC 9309 §2.3.1.3: 4xx (exceto 429, tratado acima) significa robots.txt
        # indisponível e não proíbe o acesso; 5xx significa inacessível.
        if not (page.status==200 or 400<=page.status<500):
            raise ProviderError('ROBOTS_INDISPONIVEL',details={'http_status':page.status})
        if page.status!=200:
            state['robots_status']=dict(state.get('robots_status',{}),**{origin:page.status})
        lines = robots_rules(page.text) if page.status == 200 else []
        robots[origin]=lines;state.update(robots=robots,robots_url=None,robots_redirects=[])
        parser=RobotFileParser();parser.parse(lines)
        state['crawl_delay']=max(10,int(parser.crawl_delay(USER_AGENT) or parser.crawl_delay('*') or 0))
        return state
    parser=RobotFileParser();parser.parse(robots[origin])
    if not parser.can_fetch(USER_AGENT,clean):raise ProviderError('ROBOTS_BLOQUEADO')
    delay=parser.crawl_delay(USER_AGENT) or parser.crawl_delay('*') or 0
    state['crawl_delay']=max(10,int(delay))
    state['web_calls']=state.get('web_calls',0)+1
    page=await fetch_one(clean)
    if page.status in {429,503}:raise ProviderError('LIMITE_TEMPORARIO',details={'retry_after':max(60,retry_after(page.headers.get('retry-after')))})
    if page.status in {301,302,303,307,308}:
        destination=safe_url(urljoin(clean,page.headers.get('location','')))[0]
        chain=state.get('redirects',[])
        if not page.headers.get('location') or len(chain)>=5 or destination in [p['url'] for p in chain]:raise ProviderError('LOOP_REDIRECIONAMENTO')
        if url_reason(destination,cnpj):raise ProviderError('TERCEIRO')
        state.update(queue=[destination]+state['queue'][1:],redirects=chain+[dict(url=clean,status=page.status,destination=destination)])
        return state
    if page.status!=200:raise ProviderError('SITE_INACESSIVEL',details={'http_status':page.status})
    page.redirect_chain=state.get('redirects',[])
    result,validations=merged_validation(company,state,classify(company,page))
    queue=state['queue'][1:]
    if not state.get('validations') and not result.confirmed and result.site_type=='CORPORATE_CANDIDATE':
        visible=VisibleText();visible.feed(page.text)
        links=[]
        for href in visible.links:
            try:address=safe_url(urljoin(page.url,href))[0]
            except ValueError:continue
            if same_site(address,page.url) and address!=page.url and not url_reason(address,cnpj) and re.search(r'termos|privacidade|legal|contato|sobre',urlsplit(address).path,re.I):links.append(address)
        links=list(dict.fromkeys(links));links.sort(key=lambda u:0 if re.search(r'termos|privacidade|legal',u,re.I) else 1)
        queue=links[:2]
    if result.confirmed or result.site_type in {'THIRD_PARTY','CNPJ_DIVERGENTE'}:queue=[]
    state.update(site_validation=result.to_dict(),validations=validations,queue=queue,redirects=[])
    return state if queue else close_site(state)
