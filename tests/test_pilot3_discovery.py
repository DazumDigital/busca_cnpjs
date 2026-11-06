import asyncio
import hashlib
import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.models import Company, Validation
from app.pilot3 import budget_status, execute_plan, prepare, read_plan, small_settings
from app.services.search_queries import SEARCH_EXCLUSIONS, queries_for
from app.services.validator import Page, WebsiteValidator, classify
from app.services.website_finder import WebsiteFinder
from test_official_sites import REAL, THP, seed_real

IDS = ['00055627000140','00056678000196','00060085000101']


@pytest.fixture
def configured(policy, settings, db):
    seed_real(db)
    stamp=time.time()-3900
    db.conn.executemany("INSERT INTO requests(provider,started_at,outcome) VALUES (?,?,?)",
                        [('serper',stamp,'SUCESSO')]*10+[('websites',stamp,'SUCESSO')]*41)
    p=dict(policy,max_requests=20,per_minute=2,per_hour=20,per_day=20,confirmed_free_remaining=2500,
           balance_verified_at=(datetime.now(timezone.utc)-timedelta(seconds=4000)).isoformat())
    return small_settings(replace(settings,enable_serper=True,serper_key='MOCK')), {'serper':p}


@pytest.mark.parametrize('cnpj', IDS)
def test_nominal_queries_use_city_and_exclusions_without_registration_search(cnpj):
    company=Company(**next(r['company'] for r in REAL if r['company']['cnpj']==cnpj))
    queries=queries_for(company)
    assert len(queries)==2
    assert all(company.city in q for q in queries)
    assert all(company.cnpj not in q for q in queries)
    assert all('-site:opencnpj.com' in q and '-site:cnpja.com' in q for q in queries)
    assert all('-cnpj' not in q.split() for q in queries)
    assert 'LTDA' not in queries[0]
    assert 'site oficial' in queries[0] and 'contato institucional' in queries[1]


def test_cnpj_is_fallback_only_when_names_are_absent():
    queries=queries_for(Company(IDS[0]))
    assert len(queries)==1 and IDS[0] in queries[0]


def test_thp_brand_navigation_can_be_medium_without_false_high():
    company=Company(**THP['company'])
    html='<h1>THP Brasil</h1><a href="/sobre/">Sobre</a><a href="/contato/">Contato</a>'
    result=classify(company,Page('https://thpbrasil.com.br/',200,{},html))
    assert result.confidence=='MEDIA' and not result.confirmed
    assert not result.signals['exact_cnpj']
    assert result.page_evidence[0]['title']=='THP Brasil'
    assert result.page_evidence[0]['cnpjs']==[]


def test_documentary_evidence_keeps_exact_identity_and_source_url(db, settings):
    company=Company(**THP['company'])
    html=f'<h1>THP Brasil</h1><footer>{company.legal_name} CNPJ {company.cnpj}</footer>'

    class Fetcher:
        async def fetch(self,url): return Page(url,200,{},html)

    result=asyncio.run(WebsiteValidator(Fetcher(),db,settings).validate(company,'https://thpbrasil.com.br/sobre/'))
    assert result.confirmed
    evidence=result.page_evidence[0]
    assert evidence['url']=='https://thpbrasil.com.br/sobre/'
    assert evidence['exact_cnpj'] and company.cnpj in evidence['owner_context']
    assert len(evidence['content_sha256'])==64
    cache=json.loads(db.conn.execute("SELECT value FROM cache WHERE provider='validation'").fetchone()[0])
    assert cache['page_evidence']==result.page_evidence


def test_existing_medium_candidate_avoids_new_serper_query(db, settings):
    seed_real(db,[THP])
    db.conn.execute('UPDATE companies SET validation=NULL')
    company=Company(**THP['company'])

    class Search:
        async def search(self,query): raise AssertionError('Não consumir busca adicional para MEDIA')

    class Fetcher:
        async def fetch(self,url):
            return Page(url,200,{},'<h1>THP Brasil</h1><a href="/sobre/">Sobre</a><a href="/contato/">Contato</a>')

    result,_,_=asyncio.run(WebsiteFinder(Search(),WebsiteValidator(Fetcher(),db,settings),db,settings).find(company,THP['candidates']))
    assert result.confidence=='MEDIA' and not result.confirmed
    audit=db.get_state('discovery_audit:'+company.cnpj)
    assert audit['https://opencnpj.com/00055627000140']['decision']=='REJECTED'
    assert audit['https://thpbrasil.com.br/sobre/']['confidence']=='MEDIA'


def test_medium_candidate_is_not_replaced_by_high_numeric_score_with_low_confidence(db,settings):
    seed_real(db,[THP])
    company=Company(**THP['company'])

    class Search:
        async def search(self,query): raise AssertionError('MEDIA existente não exige nova busca')

    class Validator:
        async def validate(self,company,url):
            if 'thpbrasil' in url:
                return Validation(url,'thpbrasil.com.br','MEDIA',50,[],True,False)
            return Validation(url,'outrosite.com','BAIXA',90,[],True,False)

    result,_,_=asyncio.run(WebsiteFinder(Search(),Validator(),db,settings).find(company,
                            ['https://outrosite.com/','https://thpbrasil.com.br/sobre/']))
    assert result.domain=='thpbrasil.com.br' and result.confidence=='MEDIA'


def test_remaining_budget_distinguishes_snapshot_estimate_and_local_total(db, configured, tmp_path):
    settings,policies=configured
    budget=budget_status(tmp_path/'test.sqlite',policies,settings)
    assert budget['serper']['usadas']==10 and budget['serper']['restantes_locais']==10
    assert budget['serper']['saldo_informado_no_manifesto']==2500
    assert budget['serper']['estimativa_apos_tentativas_desta_aplicacao']==2490
    assert budget['serper']['saldo_atual_provedor']=='NAO_CONSULTADO'
    assert budget['websites']['restantes_locais']==79


def test_prepare_check_and_dry_run_are_offline_and_preserve_source(db, configured, tmp_path, monkeypatch):
    settings,policies=configured
    original=[dict(r) for r in db.conn.execute('SELECT * FROM companies ORDER BY position')]
    usage=db.usage()

    def forbidden(*args,**kwargs): raise AssertionError('Não construir cliente HTTP')
    monkeypatch.setattr(httpx,'AsyncClient',forbidden)
    plan=prepare(tmp_path/'test.sqlite',tmp_path/'pilot3',IDS,policies,settings)
    loaded=read_plan(tmp_path/'pilot3/plan.json')
    assert loaded['ids']==IDS and plan['limits']['companies']==3
    assert len(plan['candidate_analysis'][0]['corporate_candidates'])==2
    assert all(not r['corporate_candidates'] for r in plan['candidate_analysis'][1:])
    assert plan['limits']['retries']==0 and plan['limits']['serper_new_attempts']==6
    report=asyncio.run(execute_plan(plan,settings,policies,dry_run=True))
    assert report['escopo_selecionado']==3 and report['requisicoes_nesta_execucao']=={}
    assert db.usage()==usage
    assert [dict(r) for r in db.conn.execute('SELECT * FROM companies ORDER BY position')]==original


def test_four_companies_cannot_be_prepared(db, configured, tmp_path):
    settings,policies=configured
    with pytest.raises(ValueError,match='três'):
        prepare(tmp_path/'test.sqlite',tmp_path/'bad',IDS+['00055600000157'],policies,settings)
    assert not (tmp_path/'bad').exists()


def test_copied_checkpoint_cannot_reset_source_budget(db, configured, tmp_path, monkeypatch):
    settings,policies=configured
    plan=prepare(tmp_path/'test.sqlite',tmp_path/'pilot3',IDS,policies,settings)
    db.conn.executemany("INSERT INTO requests(provider,started_at,outcome) VALUES ('serper',?,'SUCESSO')",[(time.time(),)]*6)

    def forbidden(*args,**kwargs): raise AssertionError('Orçamento esgotado não pode construir cliente')
    monkeypatch.setattr(httpx,'AsyncClient',forbidden)
    with pytest.raises(ValueError,match='esgotado'):
        asyncio.run(execute_plan(plan,settings,policies,dry_run=False))
    assert db.usage()['serper']['requisicoes']==16
    assert policies['serper']['max_requests']==20


def test_unverified_or_unconfigured_search_blocks_run_without_requests(db, configured, tmp_path, monkeypatch):
    settings,policies=configured
    plan=prepare(tmp_path/'test.sqlite',tmp_path/'pilot3',IDS,policies,settings)
    before=db.usage()

    def forbidden(*args,**kwargs): raise AssertionError('Provedor não liberado')
    monkeypatch.setattr(httpx,'AsyncClient',forbidden)
    with pytest.raises(ValueError,match='não liberada'):
        asyncio.run(execute_plan(plan,replace(settings,enable_serper=False),policies,dry_run=False))
    assert db.usage()==before


def test_mock_run_uses_shared_quota_and_resumes_without_repeating_queries(db, configured, tmp_path, monkeypatch):
    settings,policies=configured
    plan=prepare(tmp_path/'test.sqlite',tmp_path/'pilot3',IDS,policies,settings)
    original=[dict(r) for r in db.conn.execute('SELECT * FROM companies ORDER BY position')]
    calls=[]
    companies={r['company']['cnpj']:Company(**r['company']) for r in REAL if r['company']['cnpj'] in IDS}
    domains={'00055627000140':'thpbrasil.com.br','00056678000196':'softdata.com.br','00060085000101':'rvb.com.br'}

    def handler(request):
        query=json.loads(request.content)['q']
        calls.append(query)
        cnpj=IDS[1] if 'SOFTDATA' in query else IDS[2]
        return httpx.Response(200,json={'organic':[{'link':'https://'+domains[cnpj]+'/'}]})

    real_client=httpx.AsyncClient
    def mock_client(**kwargs):
        return real_client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx,'AsyncClient',mock_client)

    class Fetcher:
        def __init__(self,quota,settings): self.quota=quota
        async def fetch(self,url):
            n=await self.quota.reserve('websites')
            self.quota.finish(n,'SUCESSO',200)
            cnpj=next(c for c,d in domains.items() if d in url)
            company=companies[cnpj]
            return Page(url,200,{},f'<h1>{company.legal_name}</h1><footer>{company.legal_name} {cnpj}</footer>')

    monkeypatch.setattr('app.services.validator.SafeFetcher',Fetcher)
    report=asyncio.run(execute_plan(plan,settings,policies,dry_run=False))
    assert report['escopo_selecionado']==3 and report['websites_validados_alta_confianca']==3
    assert len(calls)==2  # THP reutiliza candidatos; as outras duas fazem uma busca cada.
    assert db.usage()['serper']['requisicoes']==12
    assert db.usage()['websites']['requisicoes']==44
    asyncio.run(execute_plan(plan,settings,policies,dry_run=False))
    assert len(calls)==2 and db.usage()['serper']['requisicoes']==12
    assert [dict(r) for r in db.conn.execute('SELECT * FROM companies ORDER BY position')]==original
    assert policies['serper']['max_requests']==20
