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
from app.services.site_rules import rank_candidates, url_reason, candidate_groups
from app.services.site_outcome import public_validation, result_status
from app.services.validator import SafeFetcher, classify, same_site, safe_url, VisibleText, tenant_identity_evidence, USER_AGENT
from app.services.references import useful_references
from app.web.results import public_result
from app.providers.base import retry_after
from app.providers.search import search_urls, SEARCH_ENDPOINT


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


async def fetch_one(url):
    settings=Settings(timeout=8,retries=0,max_bytes=300_000,website_delay=10)
    # one() fixa o IP público, verifica TLS/Host e não segue redirects. Robots e
    # redirects são etapas persistidas separadamente, sob o controle global.
    return await SafeFetcher(SingleReservation(),settings).one(url)


def payload_for(state, partial=False):
    company=Company(**state['company'])
    result=public_validation(Validation(**state.get('validation',{})))
    urls=state.get('candidates',[])
    refs=useful_references(company,urls,result.page_evidence,result.website)
    record=dict(automatic_status=result_status(result),source_updated_at=state['created_at'],
                social_candidates=candidate_groups(urls)['social_networks'])
    payload=public_result(record,company.__dict__,result.to_dict(),refs)
    payload['origem_resultado']='PESQUISA_REAL'
    payload['fontes']=[dict(tipo='CADASTRO',nome='Minha Receita — dados públicos da Receita Federal',
        url='https://minhareceita.org/'+company.cnpj,consultado_em=state['created_at']),
        dict(tipo='DESCOBERTA',nome='Serper',consultado_em=state.get('searched_at'))]
    payload['ressalvas']=['Dados cadastrais de base pública; podem estar defasados em relação à situação atual.']
    if partial:payload['ressalvas'].append('Pesquisa parcial por falha técnica. Nenhum token comercial foi consumido.')
    return payload


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
        eligible=rank_candidates(company,[u for u in candidates if not url_reason(u,cnpj)])
        state.update(candidates=candidates[:10],searched_at=datetime.now(timezone.utc).isoformat(),robots={},
            queue=[eligible[0]] if eligible else [],redirects=[],web_calls=0,validations=[],phase='WEBSITE' if eligible else 'DONE')
    elif state['phase']=='WEBSITE':
        company=Company(**state['company'])
        if state.get('web_calls',0)>=8:raise ProviderError('LIMITE_VALIDACAO')
        target=state['queue'][0]
        clean,_,_=safe_url(target)
        if url_reason(clean,cnpj):raise ProviderError('TERCEIRO')
        if state.get('validations') and not same_site(clean,state['validations'][0]['website']):raise ProviderError('REDIRECIONAMENTO_IDENTIDADE')
        parts=urlsplit(clean);origin=parts.scheme+'://'+parts.netloc
        robots=dict(state.get('robots',{}))
        if origin not in robots:
            robots_url=state.get('robots_url') or origin+'/robots.txt'
            page=await fetch_one(robots_url)
            if page.status in {429,503}:raise ProviderError('LIMITE_TEMPORARIO',details={'retry_after':max(60,retry_after(page.headers.get('retry-after')))})
            state['web_calls']=state.get('web_calls',0)+1
            if page.status in {301,302,303,307,308}:
                chain=state.get('robots_redirects',[])
                destination=safe_url(urljoin(robots_url,page.headers.get('location','')))[0]
                if len(chain)>=5 or destination in chain:raise ProviderError('LOOP_REDIRECIONAMENTO_ROBOTS')
                state.update(robots_url=destination,robots_redirects=chain+[robots_url]);return state
            if page.status not in {200,404,410}:raise ProviderError('ROBOTS_INDISPONIVEL')
            lines = page.text.splitlines() if page.status == 200 else []
            if len(lines) > 2000 or any(len(line) > 1000 for line in lines):
                raise ProviderError('ROBOTS_INDISPONIVEL')
            robots[origin]=lines;state.update(robots=robots,robots_url=None,robots_redirects=[])
            parser=RobotFileParser();parser.parse(lines)
            state['crawl_delay']=max(10,int(parser.crawl_delay(USER_AGENT) or parser.crawl_delay('*') or 0))
        else:
            parser=RobotFileParser();parser.parse(robots[origin])
            if not parser.can_fetch(USER_AGENT,clean):raise ProviderError('ROBOTS_BLOQUEADO')
            delay=parser.crawl_delay(USER_AGENT) or parser.crawl_delay('*') or 0
            state['crawl_delay']=max(10,int(delay))
            page=await fetch_one(clean)
            if page.status in {429,503}:raise ProviderError('LIMITE_TEMPORARIO',details={'retry_after':max(60,retry_after(page.headers.get('retry-after')))})
            state['web_calls']=state.get('web_calls',0)+1
            if page.status in {301,302,303,307,308}:
                destination=safe_url(urljoin(clean,page.headers.get('location','')))[0]
                chain=state.get('redirects',[])
                if not page.headers.get('location') or len(chain)>=5 or destination in [p['url'] for p in chain]:raise ProviderError('LOOP_REDIRECIONAMENTO')
                if url_reason(destination,cnpj):raise ProviderError('TERCEIRO')
                state.update(queue=[destination]+state['queue'][1:],redirects=chain+[dict(url=clean,status=page.status,destination=destination)])
                return state
            if page.status!=200:raise ProviderError('SITE_INACESSIVEL')
            page.redirect_chain=state.get('redirects',[])
            result,validations=merged_validation(company,state,classify(company,page))
            queue=state['queue'][1:]
            if not state.get('validations') and not result.confirmed and result.site_type=='CORPORATE_CANDIDATE':
                parser=VisibleText();parser.feed(page.text)
                links=[]
                for href in parser.links:
                    try:link=safe_url(urljoin(page.url,href))[0]
                    except ValueError:continue
                    if same_site(link,page.url) and link!=page.url and not url_reason(link,cnpj) and re.search(r'termos|privacidade|legal|contato|sobre',urlsplit(link).path,re.I):links.append(link)
                links=list(dict.fromkeys(links));links.sort(key=lambda u:0 if re.search(r'termos|privacidade|legal',u,re.I) else 1)
                queue=links[:2]
            if result.confirmed or result.site_type in {'THIRD_PARTY','CNPJ_DIVERGENTE'}:queue=[]
            state.update(validation=result.to_dict(),validations=validations,queue=queue,redirects=[],phase='WEBSITE' if queue else 'DONE')
    return state
