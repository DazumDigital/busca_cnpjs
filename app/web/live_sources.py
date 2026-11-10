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
from app.services.search_queries import SEARCH_EXCLUSIONS
from app.services.site_rules import RULES_VERSION, rank_candidates, url_reason, candidate_groups, domain_brand_match, tenant_brand_match, extract_cnpjs, folded
from app.services.site_outcome import public_validation, result_status
from app.services.validator import SafeFetcher, classify, same_site, safe_url, VisibleText, tenant_identity_evidence, USER_AGENT, ROBOT_TOKEN, resolve_public
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


CONNECTION_REASONS = {'TEMPO_ESGOTADO': 'tempo esgotado', 'TLS_CERTIFICADO': 'certificado TLS não verificado',
                      'TLS': 'falha na conexão segura', 'CONEXAO_RECUSADA': 'conexão recusada', 'DNS': 'domínio não encontrado',
                      'CONEXAO_ENCERRADA': 'conexão encerrada pelo site', 'CONEXAO': 'falha de conexão'}


def site_reason(code, http_status=None, connection=None):
    reason = SITE_REASONS.get(code, 'falha técnica na verificação do site')
    if type(http_status) is int:
        return reason + f' (HTTP {http_status})'
    return reason + (f' ({CONNECTION_REASONS[connection]})' if connection in CONNECTION_REASONS else '')


def failure_reason(failure):
    return site_reason(failure['error'], failure.get('http_status'), failure.get('conexao'))


# Plataformas e agregadores que nunca são o site da empresa: mapas, vagas,
# jurídico, catálogos, notícias, associações e portais públicos. Podem virar
# referência, mas não gastam acessos de validação nem aparecem como possível
# site. Lista da plataforma web; as regras da POC e os históricos não mudam.
PLATFORM_DOMAINS = frozenset({
    'waze.com', 'maps.apple.com', 'maps.google.com', 'maps.app.goo.gl', 'goo.gl', 'g.page', 'bing.com',
    'tripadvisor.com', 'tripadvisor.com.br', 'yelp.com', 'foursquare.com', 'moovitapp.com', 'ifood.com.br',
    'pandape.com.br', 'infojobs.com.br', 'gupy.io', 'indeed.com', 'vagas.com.br', 'catho.com.br', 'solides.com.br',
    'solides.jobs', 'kenoby.com', 'hunterhero.io', '99jobs.com', 'trabalhabrasil.com.br', 'empregos.com.br',
    'glassdoor.com', 'glassdoor.com.br', 'recrutei.com.br', 'jobs.quickin.io', 'abler.com.br',
    'jusbrasil.com.br', 'escavador.com', 'leis.org', 'rapidlei.com', 'advdinamico.com.br', 'srwatson.io',
    'buscasim.com.br', 'atlaspublico.com.br', 'econodata.com.br', 'casadosdados.com.br', 'cnpj.biz', 'cnpj.info',
    'consultasocio.com', 'empresaqui.com.br', 'guiamais.com.br', 'apontador.com.br', 'solutudo.com.br',
    'telelistas.net', 'cnpja.com', 'opencnpj.com', 'reclameaqui.com.br', 'mercadolivre.com.br', 'olx.com.br',
    'wikipedia.org', 'tiendeo.com.br', 'redalyc.org', 'scielo.br', 'publicnow.com', 'mziq.com', 'nsctotal.com.br',
    'globo.com', 'uol.com.br', 'estadao.com.br', 'exame.com', 'infomoney.com.br', 'terra.com.br', 'r7.com',
    'abinee.org.br', 'core-sp.org.br', 'portaldatransparencia.gov.br',
})
ARTICLES = frozenset({'a', 'o', 'as', 'os'})
CORPORATE_WORDS = frozenset({'ltda', 'limitada', 'eireli', 'me', 'epp', 'sa', 'cia', 'companhia'})
CONNECTORS = frozenset({'e', 'de', 'da', 'do', 'das', 'dos'})


def platform_site(url):
    host = (urlsplit(url).hostname or '').lower().rstrip('.')
    return any(host == domain or host.endswith('.' + domain) for domain in PLATFORM_DOMAINS)


def clean_name(name):
    """Nome para a busca: sem artigo inicial, pontuação e termos societários
    ("A MELAU PET" → "MELAU PET"; "A. ANGELONI & CIA. LTDA" → "ANGELONI")."""
    text = re.sub(r'\bS\s*/\s*A\b|\bS\.\s*A\.?', ' ', name or '', flags=re.I)
    words = re.sub(r"[&.,;:/\\()\"'\-]+", ' ', text).split()
    while words and words[0].lower() in ARTICLES:
        words = words[1:]
    return ' '.join(word for word in words if word.lower() not in CORPORATE_WORDS)


def web_queries(company):
    """Busca principal com os nomes limpos (fantasia OU razão social) e a
    localidade; alternativa com o nome mais completo e "contato"."""
    names = list(dict.fromkeys(n for n in (clean_name(company.trade_name), clean_name(company.legal_name)) if n))
    exclusions = ' '.join('-site:' + host for host in SEARCH_EXCLUSIONS)
    if not names:
        return [f'"{company.cnpj}" {exclusions}']
    location = ' '.join(v for v in (' '.join(company.city.split()), company.state) if v)
    primary = ' OR '.join(f'"{name}"' for name in names[:2])
    alternative = f'{max(names, key=len)} {location} contato'
    return [' '.join(f'{primary} {location} {exclusions}'.split()), ' '.join(f'{alternative} {exclusions}'.split())]


def domain_guesses(company):
    """Rótulos .com.br deduzidos do nome (ex.: "melaupetcaetano"). Só servem para
    tentar o domínio; a confirmação continua exigindo o CNPJ na página."""
    labels = []
    for name in (clean_name(company.legal_name), clean_name(company.trade_name)):
        words = [w for w in folded(name).split() if w not in CONNECTORS]
        if words:
            labels += [''.join(words), ''.join(words[:2])]
    return [label for label in dict.fromkeys(labels) if 4 <= len(label) <= 40 and label.isascii() and label.isalnum()][:3]


def domain_exists(host):
    """DNS com endereço público (mesma proteção SSRF do acesso). Não acessa o site."""
    try:
        resolve_public(host, 443)
        return True
    except (OSError, ValueError, UnicodeError):
        return False


def guessed_sites(company, limit=2):
    found = []
    for label in domain_guesses(company):
        for host in (f'www.{label}.com.br', f'{label}.com.br'):
            if domain_exists(host):
                found.append(f'https://{host}/')
                break
        if len(found) >= limit:
            break
    return found


def root_site(url):
    """Domínio principal de um subdomínio da marca (institucional.angeloni.com.br
    → www.angeloni.com.br), se existir no DNS."""
    labels = (urlsplit(url).hostname or '').lower().rstrip('.').split('.')
    size = 3 if len(labels) >= 3 and labels[-1] in {'br', 'uk', 'au', 'nz'} and labels[-2] in {
        'com', 'net', 'org', 'co', 'ind', 'edu', 'gov'} else 2
    if len(labels) <= size or labels[:-size] == ['www']:
        return None
    root = '.'.join(labels[-size:])
    return next((f'https://{host}/' for host in ('www.' + root, root) if domain_exists(host)), None)


def display_root(url):
    """Endereço exibido: a raiz do site, preservando prefixo de país/idioma (/br/)."""
    parts = urlsplit(url)
    segments = [s for s in parts.path.split('/') if s]
    prefix = f'/{segments[0]}/' if segments and re.fullmatch(r'[a-z]{2}(?:-[a-z]{2})?', segments[0], re.I) else '/'
    return f'{parts.scheme}://{parts.netloc}{prefix}'


def social_profile(url):
    """Post ou página interna de rede social → perfil (facebook.com/marca/)."""
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    segments = [s for s in parts.path.split('/') if s]
    if host.endswith('facebook.com') and len(segments) > 1 and segments[1] in {'posts', 'videos', 'photos', 'reviews', 'about', 'events'}:
        return f'https://www.facebook.com/{segments[0]}/'
    return url


def format_cnpj(number):
    return f'{number[:2]}.{number[2:5]}.{number[5:8]}/{number[8:12]}-{number[12:]}'


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
    final=Validation(**state.get('validation',{}))
    matriz=final if final.site_type=='MATRIZ_SITE' else None
    if matriz:
        # O site da matriz tem categoria própria e nunca vira site oficial da filial.
        final=Validation(rules_version=RULES_VERSION,evidence=['SITE_DA_MATRIZ'])
    result=public_validation(final)
    urls=state.get('candidates',[])
    refs=useful_references(company,urls,result.page_evidence,result.website)
    record=dict(automatic_status=result_status(result),source_updated_at=state['created_at'],
                social_candidates=candidate_groups(urls)['social_networks'])
    payload=public_result(record,company.__dict__,result.to_dict(),refs)
    payload['origem_resultado']='PESQUISA_REAL'
    payload['site_matriz']=[]
    if matriz:
        head=matriz.signals.get('matriz_cnpj','')
        item=link(display_root(matriz.website),'SITE_DA_MATRIZ','MEDIA',
            f'O site mostra o CNPJ da matriz ({format_cnpj(head)}); a filial consultada não aparece nas páginas verificadas.')
        if item:
            payload['site_matriz']=[item];payload['status']='SITE_DA_MATRIZ'
    if payload['website_oficial'] and final.signals.get('mesma_raiz'):
        payload['website_oficial']['ressalvas']=[
            f'Confirmado pelo CNPJ de outro estabelecimento da mesma empresa ({format_cnpj(final.signals["mesma_raiz"])}).']
    for item in payload['possiveis_sites']:
        failed=next((f for f in state.get('site_failures',[]) if same_site(f['url'],item['url'])),None)
        if failed:item['ressalvas']=['Validação não concluída: '+failure_reason(failed)+'.']
    payload['possiveis_sites'].extend(other_sites(company,state,payload))
    # Exibe a raiz do site (não a página interna) e o perfil social (não o post).
    unique=[]
    for item in payload['possiveis_sites']:
        root=display_root(item['url'])
        if not any(same_site(root,u['url']) and display_root(u['url'])==root for u in unique):
            unique.append(dict(item,url=root,dominio=urlsplit(root).hostname))
    payload['possiveis_sites']=unique
    socials=[]
    for item in payload['redes_sociais']:
        profile=social_profile(item['url'])
        if profile not in [s['url'] for s in socials]:socials.append(dict(item,url=profile))
    payload['redes_sociais']=socials
    mark_google_panel(payload,state)
    # Grupo próprio para o site indicado pelo painel do Google (nunca oficial por isso).
    payload['indicado_google']=[i for i in payload['possiveis_sites'] if i.get('indicado_google')]
    payload['possiveis_sites']=[i for i in payload['possiveis_sites'] if not i.get('indicado_google')]
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


def candidate_sites(company,cnpj,urls,limit=MAX_SITES,first=None,extra=()):
    """Ordem de validação (não de confiança), um endereço por site:
    painel do Google, domínios e subdomínios da marca, sites adicionados pela
    descoberta (raiz do domínio, palpites pelo nome) e, só sem nenhum desses,
    um único candidato sem a marca. Plataformas e terceiros ficam de fora."""
    eligible=[u for u in urls if not url_reason(u,cnpj) and not platform_site(u)]
    ranked=rank_candidates(company,eligible)
    if first and first in ranked:
        ranked=[first]+[u for u in ranked if u!=first]
    sites=[]
    def add(url):
        if url and not any(same_site(url,site) for site in sites):sites.append(url)
    for url in ranked:
        if url==first or brand_site(company,url):add(url)
    for url in extra:
        if not url_reason(url,cnpj) and not platform_site(url):add(url)
    if not sites and ranked:
        add(ranked[0])
    return sites[:limit]


def discovery(company,cnpj,candidates,panel=None):
    """Acrescenta, sem chamar a Serper, a raiz dos subdomínios da marca e, se a
    busca não trouxe nenhum domínio da marca, até 2 domínios deduzidos do nome
    que existam no DNS. Devolve (sites, adicionados, palpites)."""
    found=candidate_sites(company,cnpj,candidates,first=panel)
    branded=[u for u in found if u==panel or brand_site(company,u)]
    roots=[r for r in (root_site(u) for u in branded if domain_brand_match(company,urlsplit(u).hostname or '')) if r]
    guesses=[] if branded else guessed_sites(company)
    added=list(dict.fromkeys(roots+guesses))
    return candidate_sites(company,cnpj,candidates,first=panel,extra=added),added,guesses


def matriz_cnpj(company,page):
    """Para uma filial: CNPJ da matriz se a página, no domínio ou subdomínio da
    marca, só mostra inscrições da mesma raiz e entre elas a da matriz (0001).
    Indica o site do grupo; não confirma a filial."""
    if company.cnpj[8:12]=='0001' or not brand_site(company,page.url):
        return None
    numbers=page_cnpjs(page)
    root=company.cnpj[:8]
    if not numbers or any(number[:8]!=root for number in numbers):
        return None
    return next((number for number in sorted(numbers) if number[8:12]=='0001'),None)


def name_domain(company,url):
    """Domínio cujo rótulo é o próprio nome da empresa (ex.: fgprime.com.br para
    "FG PRIME"), inclusive com sigla curta que a regra de marca ignora."""
    labels=(urlsplit(url).hostname or '').lower().removeprefix('www.').split('.')
    return bool(labels) and labels[0] in domain_guesses(company)


def with_name_domain(company,url,result):
    """Considera o domínio-nome como domínio da marca; a confirmação continua
    exigindo CNPJ exato, nome, titularidade e marca no título."""
    if result.site_type not in {'CORPORATE','CORPORATE_CANDIDATE'} or result.signals.get('brand_domain') or not name_domain(company,url):
        return result
    signals=dict(result.signals,brand_domain=True,dominio_do_nome=True)
    strong=all(signals.get(k) is True for k in ('exact_cnpj','name_match','legal_owner','heading_brand'))
    return replace(result,signals=signals,confirmed=strong,confidence='ALTA' if strong else result.confidence,
                   site_type='CORPORATE' if strong else result.site_type,
                   evidence=list(result.evidence)+['Domínio formado pelo nome da empresa'])


def page_cnpjs(page):
    visible=VisibleText();visible.feed(page.text)
    return extract_cnpjs(' '.join(visible.parts))


def same_company_validation(company,page):
    """Matriz consultada e página só com outro estabelecimento da mesma raiz
    (ex.: 00.623.904/0003-xx): a raiz identifica a mesma pessoa jurídica, então
    reavalia a página com esse CNPJ e exige as mesmas evidências fortes."""
    numbers=page_cnpjs(page)
    root=company.cnpj[:8]
    if company.cnpj[8:12]!='0001' or not numbers or any(number[:8]!=root for number in numbers):
        return None
    other=sorted(numbers)[0]
    result=classify(replace(company,cnpj=other),page)
    if result.site_type not in {'CORPORATE','CORPORATE_CANDIDATE'}:
        return None
    return replace(result,signals=dict(result.signals,mesma_raiz=other),evidence=list(result.evidence)+[
        f'CNPJ de outro estabelecimento da mesma empresa ({format_cnpj(other)})'])


def preferred_site(company,url,state):
    """Candidato que pode aparecer como possível site mesmo sem validação."""
    panel=state.get('google_panel_site')
    return brand_site(company,url) or bool(panel and same_site(url,panel))


def mark_google_panel(payload,state):
    panel=state.get('google_panel_site')
    if not panel:
        return
    for item in [payload.get('website_oficial')]+payload.get('site_matriz',[])+payload['possiveis_sites']+payload['referencias']+payload['redes_sociais']:
        if item and same_site(item['url'],panel):
            item['indicado_google']=True
            if item.get('tipo')=='POSSIVEL_SITE':
                item['ressalvas']=list(item.get('ressalvas') or [])+['Indicado pelo painel do Google; isso não confirma a titularidade.']


def other_sites(company,state,payload):
    """Demais candidatos plausíveis, sempre como possíveis sites e nunca como oficiais."""
    shown=[i['url'] for i in payload['possiveis_sites']+payload.get('site_matriz',[])]+([payload['website_oficial']['url']] if payload['website_oficial'] else [])
    rejected=[v.get('website','') for v in state.get('site_results',[]) if v.get('site_type') in {'THIRD_PARTY','CNPJ_DIVERGENTE'}]
    visited={v.get('website',''):v for v in state.get('site_results',[]) if v.get('website')}
    guesses=state.get('guessed_sites',[])
    extra=[]
    for url in candidate_sites(company,company.cnpj,state.get('candidates',[]),limit=4,first=state.get('google_panel_site'),
                               extra=state.get('added_sites',[])):
        if any(same_site(url,u) for u in shown+rejected if u):continue
        failed=next((f for f in state.get('site_failures',[]) if same_site(f['url'],url)),None)
        seen=next((v for u,v in visited.items() if same_site(u,url)),None)
        # Domínio deduzido do nome só aparece se a página foi acessada.
        if any(same_site(url,g) for g in guesses) and not seen:continue
        # Páginas de terceiros sem a marca (associações, catálogos) não viram
        # "possível site" só por terem falhado ou não terem sido visitadas.
        if seen and (seen.get('confidence')=='MEDIA' or preferred_site(company,url,state)):
            item=link(seen['website'],'POSSIVEL_SITE',seen.get('confidence') if seen.get('confidence')=='MEDIA' else 'BAIXA',
                      'Página acessada; as evidências não confirmam a titularidade.')
        elif not preferred_site(company,url,state):
            continue
        elif failed:
            item=link(url,'POSSIVEL_SITE','PENDENTE','Encontrado na pesquisa; a validação técnica não pôde ser concluída.',
                      ressalvas=['Validação não concluída: '+failure_reason(failed)+'.'])
        elif not seen:
            item=link(url,'POSSIVEL_SITE','NAO_CONFIRMADO','Domínio compatível com o nome da empresa; não visitado nesta consulta.')
        else:
            continue
        if item and seen and any(same_site(url,g) for g in guesses):
            item['evidencia']='Domínio deduzido do nome da empresa; página acessada, titularidade não confirmada.'
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
        search_round=state.get('search_round',0)
        data=await provider_json('POST',SEARCH_ENDPOINT,key=key,
            payload={'q':web_queries(company)[min(search_round,1)],'gl':'br','hl':'pt-br','num':10})
        if not isinstance(data.get('organic'),list):raise ProviderError('RESPOSTA_INVALIDA')
        urls=search_urls(data)
        candidates=list(state.get('candidates',[])) if search_round else []
        for url in urls:
            try:clean,_,_=safe_url(url)
            except (ValueError,TypeError):continue
            if clean not in candidates:candidates.append(clean)
        # Só a URL do painel do Google é guardada (atribuição), nunca o painel.
        graph=data.get('knowledgeGraph')
        panel=state.get('google_panel_site') if search_round else None
        if not panel and isinstance(graph,dict) and isinstance(graph.get('website'),str):
            try:panel=safe_url(graph['website'])[0]
            except (ValueError,TypeError):panel=None
        state.update(google_panel_site=panel,searched_at=datetime.now(timezone.utc).isoformat(),searches=search_round+1)
        found=candidate_sites(company,cnpj,candidates,first=panel)
        branded=any(u==panel or brand_site(company,u) for u in found)
        # Segunda busca só se a política permitir (padrão: 1) e a primeira não
        # trouxe nenhum domínio da marca. Cada busca é uma etapa e uma reserva.
        if not branded and search_round+1<state.get('max_searches',1):
            state.update(candidates=candidates[:20],search_round=search_round+1)
            return state
        sites,added,guesses=discovery(company,cnpj,candidates,panel)
        state.update(candidates=candidates[:20],added_sites=added,guessed_sites=guesses,robots={},
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
            connection=exc.details.get('conexao')
            page_failed(state,exc.code,status if type(status) is int else None,
                        connection if connection in CONNECTION_REASONS else None)
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


def close_site(state,failure=None,http_status=None,connection=None):
    """Encerra o site atual, registrando a validação obtida ou a falha técnica."""
    if state.get('site_validation'):
        state['site_results']=state.get('site_results',[])+[state['site_validation']]
    elif failure and state.get('current_site'):
        record=dict(url=state['current_site'],error=failure)
        if http_status:record['http_status']=http_status
        if connection:record['conexao']=connection
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
    # Falha num domínio só deduzido do nome não vira candidato a revisar.
    branded=[f for f in failures if preferred_site(company,f['url'],state)
             and not any(same_site(f['url'],g) for g in state.get('guessed_sites',[]))]
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


def page_failed(state,code,http_status=None,connection=None):
    """Página secundária falha sem perder o site; a inicial encerra só o candidato."""
    if code=='TERCEIRO' and not state.get('validations'):
        rejected=Validation(website=state.get('current_site') or '',site_type='THIRD_PARTY',rules_version=RULES_VERSION,
                            evidence=['Redireciona para domínio de terceiro'])
        state['site_validation']=rejected.to_dict()
        return close_site(state)
    if state.get('validations'):
        failure=dict(url=(state.get('queue') or [''])[0],error=code)
        if http_status:failure['http_status']=http_status
        if connection:failure['conexao']=connection
        state['page_failures']=state.get('page_failures',[])+[failure]
        state.update(queue=state['queue'][1:],redirects=[],robots_url=None,robots_redirects=[])
        return state if state['queue'] else close_site(state)
    return close_site(state,code,http_status,connection)


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
    if result.site_type=='CNPJ_DIVERGENTE':
        same=same_company_validation(company,page)
        head=None if same else matriz_cnpj(company,page)
        if same:
            # Matriz consultada; página com outro estabelecimento da mesma raiz.
            result=same
        elif head:
            # Aponta para a página inicial do site, mesmo que o CNPJ esteja numa página interna.
            site=next((v['website'] for v in validations if v.get('website')),'') or safe_url(page.url)[0]
            result=Validation(website=site,domain=(urlsplit(site).hostname or '').removeprefix('www.'),
                confidence='MEDIA',score=50,accessible=True,rules_version=RULES_VERSION,site_type='MATRIZ_SITE',
                evidence=['CNPJ da matriz na página; a filial consultada não aparece'],signals={'matriz_cnpj':head})
        else:
            # Recusado, mas registrado como visitado e com as inscrições vistas.
            result=replace(result,website=result.website or safe_url(page.url)[0],
                signals=dict(result.signals,cnpjs_encontrados=sorted(page_cnpjs(page))[:5]))
        validations=validations[:-1]+[result.to_dict()]
    named=with_name_domain(company,page.url,result)
    if named is not result:
        result=named;validations=validations[:-1]+[result.to_dict()]
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
    if result.confirmed or result.site_type in {'THIRD_PARTY','CNPJ_DIVERGENTE','MATRIZ_SITE'}:queue=[]
    state.update(site_validation=result.to_dict(),validations=validations,queue=queue,redirects=[])
    return state if queue else close_site(state)
