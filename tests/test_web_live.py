import asyncio
import concurrent.futures
import json
import time
import uuid
from datetime import datetime, timezone
from dataclasses import replace
import pytest
from sqlalchemy import insert, select, update, func
from test_web_platform import web, source
from test_web_batches import user, create, step
from conftest import generated_cnpj
from app.models import Company, ProviderError
from app.web import live
from app.web.live_sources import execute_action
from app.web.store import external_control, external_requests, live_jobs, charges, WebError, Store

NEW=generated_cnpj(999999)


@pytest.fixture
def live_setup(web,policy):
    p=dict(policy,max_requests=100,min_interval=0)
    rules=dict(serper=p,websites=p,minhareceita=p)
    with web[1].transaction() as db:db.execute(insert(external_control).values(id='global',policy=json.dumps(rules),lease_until=0))
    return web


async def simulated(cnpj,state,key):
    assert key=='fixture-private-key'
    state=dict(state)
    if state['phase']=='CADASTRO':state.update(company=Company(cnpj,legal_name='EMPRESA FIXTURE LTDA',city='Blumenau',state='SC').__dict__,phase='BUSCA')
    else:state.update(phase='DONE',candidates=[])
    return state


def runner(store,cnpj,key):return live.advance_job(store,cnpj,key,executor=simulated)


def test_real_flow_is_staged_saved_and_charged_once(live_setup):
    web=live_setup;store=web[1];identity=user(web)
    query=store.consult(identity,NEW,str(uuid.uuid4()),True,True)
    assert query['status']=='NA_FILA' and query['saldo']['reservados']==1
    first=live.process_consultation(store,identity,query['id'],'fixture-private-key',runner)
    assert first['status']=='NA_FILA' and first['saldo']['consumidos']==0
    final=live.process_consultation(store,identity,query['id'],'fixture-private-key',runner)
    assert final['status']=='CONCLUIDA' and final['saldo']['consumidos']==1 and final['saldo']['reservados']==0
    assert final['resultado']['empresa']['cnpj']==NEW and final['resultado']['origem_resultado']=='PESQUISA_REAL'
    assert final['resultado']['fontes'][0]['nome'].startswith('Minha Receita')
    again=live.process_consultation(store,identity,query['id'],'fixture-private-key',runner)
    assert again['saldo']['consumidos']==1
    cached=store.consult(identity,NEW,str(uuid.uuid4()))
    assert cached['resultado']['origem_resultado']=='HISTORICO'
    with store.engine.connect() as db:assert db.execute(select(func.count()).select_from(external_requests)).scalar()==2


def test_failure_never_charges_or_automatically_repeats(live_setup):
    web=live_setup;store=web[1];identity=user(web);calls=[]
    async def failed(*args):calls.append(1);raise ProviderError('ERRO_REDE')
    run=lambda s,c,k:live.advance_job(s,c,k,executor=failed)
    query=store.consult(identity,NEW,str(uuid.uuid4()),True,True)
    result=live.process_consultation(store,identity,query['id'],'fixture-private-key',run)
    assert result['status']=='FALHOU' and result['saldo']['consumidos']==0 and result['saldo']['reservados']==0
    live.process_consultation(store,identity,query['id'],'fixture-private-key',run)
    second=store.consult(identity,NEW,str(uuid.uuid4()),True,True)
    live.process_consultation(store,identity,second['id'],'fixture-private-key',run)
    assert len(calls)==1


def test_interrupted_request_is_not_resent(live_setup):
    store=live_setup[1]
    with store.transaction() as db:
        db.execute(insert(live_jobs).values(cnpj=NEW,id=str(uuid.uuid4()),status='IN_FLIGHT',state=json.dumps({'phase':'CADASTRO','created_at':time.time()}),step=0,lease_until=time.time()-1,updated_at=time.time()))
    result=runner(store,NEW,'fixture-private-key')
    assert result['status']=='UNCERTAIN' and result['error']=='ETAPA_INTERROMPIDA'
    with store.engine.connect() as db:assert db.execute(select(func.count()).select_from(external_requests)).scalar()==0


def test_late_worker_preserves_newer_lease(live_setup):
    store = live_setup[1]
    now = time.time()
    newer_lease = now + 90

    async def expired_worker(cnpj, state, key):
        with store.transaction() as db:
            db.execute(update(external_control).values(lease_until=newer_lease))
        return await simulated(cnpj, state, key)

    result = live.advance_job(store, NEW, 'fixture-private-key', clock=lambda: now, executor=expired_worker)
    assert result['status'] == 'UNCERTAIN'
    with store.engine.connect() as db:
        assert db.execute(select(external_control.c.lease_until)).scalar_one() == newer_lease
        assert db.execute(select(live_jobs.c.status)).scalar_one() == 'UNCERTAIN'
        assert db.execute(select(external_requests.c.outcome)).scalar_one() == 'RESERVADA'


def test_no_key_expired_proof_and_exhaustion_block_before_network(live_setup):
    store=live_setup[1]
    with pytest.raises(WebError):live.advance_job(store,NEW,'',executor=simulated)
    with store.transaction() as db:
        row=db.execute(select(external_control)).mappings().one();p=json.loads(row['policy']);p['minhareceita']['max_requests']=0
        db.execute(update(external_control).values(policy=json.dumps(p)))
    with pytest.raises(WebError,match='COTA_EXTERNA_ESGOTADA'):runner(store,NEW,'fixture-private-key')
    with store.engine.connect() as db:assert db.execute(select(func.count()).select_from(external_requests)).scalar()==0


def test_provider_quota_wait_is_returned_without_sleep_or_call(live_setup):
    store=live_setup[1]
    with store.transaction() as db:
        row=db.execute(select(external_control)).mappings().one();p=json.loads(row['policy']);p['minhareceita']['per_minute']=1
        db.execute(update(external_control).values(policy=json.dumps(p)))
        db.execute(insert(external_requests).values(id='prior',provider='minhareceita',started_at=time.time(),outcome='SUCESSO',operation='prior'))
    result=runner(store,NEW,'fixture-private-key')
    assert result['status']=='WAIT' and 1<=result['retry_after']<=60


def test_concurrent_duplicates_use_single_global_job(live_setup):
    store=live_setup[1];calls=[]
    async def slow(cnpj,state,key):
        calls.append(state['phase']);time.sleep(.1)
        return await simulated(cnpj,state,key)
    def run(_):return live.advance_job(store,NEW,'fixture-private-key',executor=slow)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(run,range(6)))
    assert calls.count('CADASTRO')==1 and calls.count('BUSCA')<=1
    assert all(r['status'] in {'READY','WAIT','DONE'} for r in results)


def test_company_metadata_requires_exact_cnpj(monkeypatch):
    from app.web import live_sources
    async def wrong(*args,**kwargs):return {'cnpj':generated_cnpj(1234),'razao_social':'Outra Empresa'}
    monkeypatch.setattr(live_sources,'provider_json',wrong)
    with pytest.raises(ProviderError,match='CADASTRO_DIVERGENTE'):asyncio.run(execute_action(NEW,{'phase':'CADASTRO'},'key'))


def test_robots_and_redirect_are_separate_bounded_steps(monkeypatch):
    from app.web import live_sources
    from app.services.validator import Page
    calls=[]
    async def fetch(url):
        calls.append(url)
        return Page(url,200,{},'User-agent: *\nDisallow: /secret\nCrawl-delay: 30')
    monkeypatch.setattr(live_sources,'fetch_one',fetch)
    state=dict(phase='WEBSITE',queue=['https://corporativa.com.br/secret'],company=Company(NEW,legal_name='Corporativa').__dict__,robots={},created_at=time.time())
    next=asyncio.run(execute_action(NEW,state,'key'))
    assert len(calls)==1 and next['crawl_delay']==30
    with pytest.raises(ProviderError,match='ROBOTS_BLOQUEADO'):asyncio.run(execute_action(NEW,next,'key'))
    assert len(calls)==1


@pytest.mark.parametrize('n',[1,5,10,15])
def test_batches_of_new_companies_use_progressive_steps(live_setup,monkeypatch,n):
    from app.web import batches as batch_service
    web=live_setup;original=live.process_consultation
    monkeypatch.setattr(live,'process_consultation',lambda s,u,i,k:original(s,u,i,k,runner))
    ids=[generated_cnpj(500000+i) for i in range(n)]
    batch=create(web,'\n'.join(ids)).json()
    for item in batch['itens']:
        halfway=batch_service.step_batch(web[1],user(web),batch['id'],item['id'],True,'fixture-private-key')
        final=batch_service.step_batch(web[1],user(web),batch['id'],item['id'],True,'fixture-private-key')
    assert final['progresso']['concluidos']==n and final['saldo']['consumidos']==n and final['saldo']['reservados']==0
    with web[1].engine.connect() as db:assert db.execute(select(func.count()).select_from(external_requests)).scalar()==n*2


def test_single_api_starts_new_company_and_resumes(live_setup,monkeypatch):
    from app.web.api import create_app
    from fastapi.testclient import TestClient
    web=live_setup;original=live.process_consultation
    monkeypatch.setattr(live,'process_consultation',lambda s,u,i,k:original(s,u,i,k,runner))
    config=replace(web[3],external_enabled=True,serper_key='fixture-private-key')
    with TestClient(create_app(config,web[1])) as client:
        session=client.post('/api/login',json={'usuario':'equipe','senha':'Senha-fixture-123'},headers={'Origin':config.origin}).json()
        headers={'Origin':config.origin,'X-CSRF-Token':session['csrf'],'Idempotency-Key':str(uuid.uuid4())}
        first=client.post('/api/consultas',json={'cnpj':NEW},headers=headers)
        assert first.status_code==200 and first.json()['status']=='NA_FILA'
        endpoint='/api/consultas/'+first.json()['id']+'/processar'
        assert client.post(endpoint,headers=headers).json()['saldo']['consumidos']==0
        assert client.post(endpoint,headers=headers).json()['saldo']['consumidos']==1
        assert client.post(endpoint,headers=headers).json()['saldo']['consumidos']==1


def test_test_only_policy_cannot_research_from_team_account(live_setup):
    web=live_setup
    with web[1].transaction() as db:
        row=db.execute(select(external_control)).mappings().one();p=json.loads(row['policy']);p['allowed_organizations']=['isolated-qa']
        db.execute(update(external_control).values(policy=json.dumps(p)))
    with pytest.raises(WebError):live.ensure_ready(web[1],'fixture-private-key','dazumdigital')
    assert live.ensure_ready(web[1],'fixture-private-key','isolated-qa')
