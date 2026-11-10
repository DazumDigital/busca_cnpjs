import asyncio
import concurrent.futures
import json
import os
import time
from threading import Barrier
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
ORG='dazumdigital'
KEY='fixture-private-key'


@pytest.fixture
def live_setup(web,policy):
    p=dict(policy,max_requests=100,min_interval=0)
    rules=dict(serper=p,websites=p,minhareceita=p,allowed_organizations=[ORG])
    live.configure(web[1],rules,key=KEY)
    return web


async def simulated(cnpj,state,key):
    assert key=='fixture-private-key'
    state=dict(state)
    if state['phase']=='CADASTRO':state.update(company=Company(cnpj,legal_name='EMPRESA FIXTURE LTDA',city='Blumenau',state='SC').__dict__,phase='BUSCA')
    # Uma referência com o CNPJ exato torna o resultado utilizável (1 token).
    else:state.update(phase='DONE',candidates=[f'https://cnpj.biz/{cnpj}'])
    return state


def runner(store,cnpj,key,*,organization=ORG):return live.advance_job(store,cnpj,key,executor=simulated,organization=organization)


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
    run=lambda s,c,k,**kw:live.advance_job(s,c,k,executor=failed,**kw)
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

    result = live.advance_job(store, NEW, 'fixture-private-key', clock=lambda: now, executor=expired_worker,organization=ORG)
    assert result['status'] == 'UNCERTAIN'
    with store.engine.connect() as db:
        assert db.execute(select(external_control.c.lease_until)).scalar_one() == newer_lease
        assert db.execute(select(live_jobs.c.status)).scalar_one() == 'UNCERTAIN'
        assert db.execute(select(external_requests.c.outcome)).scalar_one() == 'RESERVADA'


def test_no_key_expired_proof_and_exhaustion_block_before_network(live_setup):
    store=live_setup[1]
    with pytest.raises(WebError):live.advance_job(store,NEW,'',executor=simulated,organization=ORG)
    with store.transaction() as db:
        row=db.execute(select(external_control)).mappings().one();p=json.loads(row['policy']);p['minhareceita']['max_requests']=1
        db.execute(insert(external_requests).values(id='used',provider='minhareceita',started_at=time.time(),outcome='SUCESSO',operation='used'))
        db.execute(update(external_control).values(policy=json.dumps(p)))
    with pytest.raises(WebError,match='COTA_EXTERNA_ESGOTADA'):runner(store,NEW,'fixture-private-key')
    with store.engine.connect() as db:assert db.execute(select(func.count()).select_from(external_requests)).scalar()==1


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
    def run(_):return live.advance_job(store,NEW,'fixture-private-key',executor=slow,organization=ORG)
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
    async def fetch(url, robots=False):
        calls.append(url)
        return Page(url,200,{},'User-agent: *\nDisallow: /secret\nCrawl-delay: 30')
    monkeypatch.setattr(live_sources,'fetch_one',fetch)
    state=dict(phase='WEBSITE',queue=['https://corporativa.com.br/secret'],company=Company(NEW,legal_name='Corporativa').__dict__,robots={},created_at=time.time())
    next=asyncio.run(execute_action(NEW,state,'key'))
    assert len(calls)==1 and next['crawl_delay']==30
    # Robots bloqueando o candidato encerra só a validação desse site: a
    # consulta conclui com o candidato pendente, sem acessar a página proibida.
    final=asyncio.run(execute_action(NEW,next,'key'))
    assert len(calls)==1 and final['phase']=='DONE'
    assert final['site_failures']==[{'url':'https://corporativa.com.br/secret','error':'ROBOTS_BLOQUEADO'}]
    assert final['validation']['site_type']=='VALIDATION_ERROR'


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


def test_scoped_policy_rechecked_after_admission(live_setup):
    store = live_setup[1]
    live.ensure_ready(store, KEY, ORG, (NEW,))
    with store.transaction() as db:
        policy = json.loads(db.execute(select(external_control.c.policy)).scalar_one())
    policy['allowed_cnpjs'] = [generated_cnpj(12345)]
    live.configure(store, policy, key=KEY)
    # Causas distintas: escopo não liberado (403) e configuração/chave pendente (503).
    for key, org, cnpj, code in [(KEY, ORG, NEW, 'PESQUISA_NAO_LIBERADA'),
                                 ('wrong-key', ORG, policy['allowed_cnpjs'][0], 'INTEGRACAO_NAO_VERIFICADA'),
                                 (KEY, 'another-organization', policy['allowed_cnpjs'][0], 'PESQUISA_NAO_LIBERADA')]:
        with pytest.raises(WebError, match=code):
            runner(store, cnpj, key, organization=org)
    with store.engine.connect() as db:
        assert db.execute(select(func.count()).select_from(external_requests)).scalar_one() == 0


@pytest.mark.parametrize('use_batch', [False, True])
def test_hard_quota_releases_commercial_reservation(live_setup, monkeypatch, use_batch):
    from app.web import batches
    web = live_setup
    store = web[1]
    identity = user(web)
    with store.transaction() as db:
        policy = json.loads(db.execute(select(external_control.c.policy)).scalar_one())
        policy['serper']['confirmed_free_remaining'] = 1
        db.execute(insert(external_requests).values(id='used', provider='serper', started_at=time.time(),
            outcome='RESERVADA', operation='used'))
    live.configure(store, policy, key=KEY)
    if use_batch:
        batch = create(web, NEW).json()
        original = live.process_consultation
        monkeypatch.setattr(live, 'process_consultation', lambda s,u,i,k: original(s,u,i,k,runner))
        result = batches.step_batch(store, identity, batch['id'], batch['itens'][0]['id'], True, KEY)
        assert result['itens'][0]['erro'] == 'COTA_EXTERNA_ESGOTADA'
    else:
        query = store.consult(identity, NEW, uuid.uuid4().hex, True, True)
        result = live.process_consultation(store, identity, query['id'], KEY, runner)
        assert result['erro'] == 'COTA_EXTERNA_ESGOTADA' and result['status'] == 'FALHOU'
    assert result['saldo']['consumidos'] == 0 and result['saldo']['reservados'] == 0
    with store.engine.connect() as db:
        assert db.execute(select(func.count()).select_from(external_requests)).scalar_one() == 1


@pytest.mark.parametrize(('window', 'seconds'), [('per_minute', 60), ('per_hour', 3600), ('per_day', 86400)])
def test_serper_windows_include_reserved_calls(live_setup, window, seconds):
    store = live_setup[1]
    now = time.time()
    with store.transaction() as db:
        policy = json.loads(db.execute(select(external_control.c.policy)).scalar_one())
        policy['serper'][window] = 1
        db.execute(insert(external_requests).values(id='used', provider='serper', started_at=now-1,
            outcome='RESERVADA', operation='used'))
    live.configure(store, policy, key=KEY)
    result = live.advance_job(store, NEW, KEY, executor=simulated, clock=lambda:now, organization=ORG)
    assert result['status'] == 'WAIT' and result['retry_after'] == seconds-1


def test_postgresql_locks_reserve_only_one_serper_call(policy):
    from sqlalchemy.engine import make_url
    url = os.getenv('WEB_QUOTA_TEST_DATABASE_URL')
    if not url:
        pytest.skip('Banco PostgreSQL local descartável não configurado')
    parsed = make_url(url)
    assert parsed.host == '127.0.0.1' and parsed.port == 55445 and parsed.database == 'quota_qa'
    # A fresh schema makes re-runs independent without resetting any ledger.
    schema = 'quota_' + uuid.uuid4().hex
    with Store(url).transaction() as db:
        db.exec_driver_sql(f'CREATE SCHEMA {schema}')
    url = parsed.update_query_dict({'options':f'-csearch_path={schema}'}).render_as_string(hide_password=False)
    store = Store(url)
    store.initialize()
    caps = dict(policy, max_requests=100, min_interval=0)
    live.configure(store, dict(serper=dict(caps, confirmed_free_remaining=1),
        minhareceita=caps, websites=caps, allowed_organizations=[ORG]), key=KEY)
    ids = [generated_cnpj(700000 + i) for i in range(4)]
    from app.web.auth import create_user
    from app.web.store import users
    create_user(store, 'offline-qa', 'offline-pass-fixture', organization=ORG)
    with store.engine.connect() as db:
        user_id = db.execute(select(users.c.id).where(users.c.username=='offline-qa')).scalar_one()
    identity = {'id':user_id,'organization_id':ORG}
    from app.web import batches
    batch = batches.create_batch(store, identity, '\n'.join(ids), uuid.uuid4().hex)
    for cnpj in ids:
        assert runner(store, cnpj, KEY)['status'] == 'READY'
    # Independent connections contend for the last unit under SELECT FOR UPDATE.
    barrier = Barrier(len(ids))
    def next_step(item):
        independent = Store(url)
        barrier.wait(timeout=10)
        return live.process_consultation(independent, identity, item['id'], KEY, runner)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(ids)) as pool:
        list(pool.map(next_step, batch['itens']))
    for item in batch['itens']:
        live.process_consultation(Store(url), identity, item['id'], KEY, runner)
    result = batches.get_batch(Store(url), identity, batch['id'])
    assert result['progresso']['concluidos'] == 1 and result['progresso']['erros'] == 3
    assert result['saldo'] == {'disponiveis':2468,'consumidos':1,'reservados':0,'consultas_concluidas':1}
    with store.engine.connect() as db:
        assert db.execute(select(func.count()).select_from(external_requests)
            .where(external_requests.c.provider == 'serper')).scalar_one() == 1
        assert db.execute(select(func.count()).select_from(charges)).scalar_one() == 1


@pytest.mark.parametrize('error', ['COTA_ESGOTADA', 'ACESSO_NEGADO', 'LIMITE_TEMPORARIO'])
def test_serper_rejections_block_or_pause_without_resending(live_setup, error):
    store = live_setup[1]
    assert runner(store, NEW, KEY)['status'] == 'READY'

    async def rejected(cnpj, state, key):
        assert state['phase'] == 'BUSCA'
        raise ProviderError(error, details={'retry_after':60} if error=='LIMITE_TEMPORARIO' else {})

    first = live.advance_job(store, NEW, KEY, executor=rejected, organization=ORG)
    other = generated_cnpj(999998)
    if error == 'LIMITE_TEMPORARIO':
        # 429: o provedor recusou antes de processar. O progresso é mantido e a
        # mesma etapa aguarda o cooldown, sem reenviar durante a espera.
        assert first['status'] == 'WAIT' and first['retry_after'] >= 60
        assert runner(store, NEW, KEY)['status'] == 'WAIT'
        assert runner(store, other, KEY)['status'] == 'WAIT'
    else:
        assert first['status'] == 'FAILED'
        with pytest.raises(WebError, match='PESQUISA_SUSPENSA'):
            runner(store, other, KEY)
    with store.engine.connect() as db:
        assert db.execute(select(func.count()).select_from(external_requests)
            .where(external_requests.c.provider == 'serper')).scalar_one() == 1
