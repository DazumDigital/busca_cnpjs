import concurrent.futures
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, insert, update, func

from app.web.api import create_app
from app.web.auth import create_user
from app.web.config import WebSettings, ROOT
from app.web.migrate import import_consolidation
from app.web.store import Store, WebError, organizations, companies, consultations, charges, imports, import_records, users
from app.models import Company, Validation
from app.services.references import useful_references
from app.services.site_rules import candidate_groups

DAIJU='00065589000106'; THP='00055627000140'; SOFTDATA='00056678000196'; BOSCHETTI='00068581000101'; KOBARG='00064909000103'


@pytest.fixture
def source(tmp_path):
    rows=json.loads((Path(__file__).parent/'fixtures/consolidation20_latest.json').read_text(encoding='utf-8'))
    path=tmp_path/'source.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE consolidated_companies(cnpj TEXT,record TEXT); CREATE TABLE source_records(cnpj TEXT,record TEXT);'
                         'CREATE TABLE useful_references(cnpj TEXT,record TEXT); CREATE TABLE manual_checks(cnpj TEXT,record TEXT)')
        for row in rows:
            company=Company(**json.loads(row['company'])); validation=json.loads(row['validation'] or '{}')
            candidates=json.loads(row['candidates'] or '[]')
            record=dict(cnpj=row['cnpj'], automatic_status=row['status'], social_candidates=candidate_groups(candidates)['social_networks'], source_updated_at=row['updated_at'])
            db.execute('INSERT INTO consolidated_companies VALUES (?,?)',(row['cnpj'],json.dumps(record)))
            db.execute('INSERT INTO source_records VALUES (?,?)',(row['cnpj'],json.dumps({'record':row})))
            additional=json.loads((Path(__file__).parent/'fixtures/boschetti_saved_case.json').read_text(encoding='utf-8'))
            facts=json.loads(additional['row']['validation'])['page_evidence'] if row['cnpj']==BOSCHETTI else []
            refs=useful_references(company,candidates,validation.get('page_evidence',[])+facts)
            for ref in refs:db.execute('INSERT INTO useful_references VALUES (?,?)',(row['cnpj'],json.dumps(ref)))
        db.execute('INSERT INTO manual_checks VALUES (?,?)',(THP,json.dumps(dict(url='https://thpbrasil.com.br/',evidence='Verificação informada pelo usuário.'))))
    return path


@pytest.fixture
def web(tmp_path, source):
    config=WebSettings(database_url='sqlite:///'+(tmp_path/'web.sqlite').as_posix(),origin='http://testserver')
    store=Store(config.database_url); store.initialize(); create_user(store,'equipe','Senha-fixture-123')
    import_consolidation(store,source)
    app=create_app(config,store)
    with TestClient(app,raise_server_exceptions=True) as client:
        response=client.post('/api/login',json=dict(usuario='equipe',senha='Senha-fixture-123'),headers={'Origin':config.origin})
        assert response.status_code==200
        headers={'Origin':config.origin,'X-CSRF-Token':response.json()['csrf']}
        yield client,store,headers,config


def query(web, cnpj=DAIJU, key=None, **values):
    client,_,headers,_=web
    return client.post('/api/consultas',json=dict(cnpj=cnpj,**values),headers=headers|{'Idempotency-Key':key or str(uuid.uuid4())})


def test_initial_balance_import_and_restart(web, source):
    client,store,_,config=web
    assert client.get('/api/saldo').json()['disponiveis']==2469
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    assert import_consolidation(store,source)['importacao_nova'] is False
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before
    query(web)  # resultado salvo: reaproveitado sem custo
    fresh=Store(config.database_url); fresh.initialize()
    assert fresh.balance('dazumdigital')['disponiveis']==2469
    with fresh.engine.connect() as db:
        assert db.execute(select(func.count()).select_from(companies)).scalar()==20
        assert db.execute(select(func.count()).select_from(imports)).scalar()==1
        assert db.execute(select(func.count()).select_from(import_records)).scalar()==20


@pytest.mark.parametrize('cnpj,field',[(DAIJU,'website_oficial'),(THP,'verificacao_manual'),(SOFTDATA,'possiveis_sites'),(BOSCHETTI,'referencias')])
def test_real_classifications(web,cnpj,field):
    response=query(web,cnpj)
    assert response.status_code==200,response.text
    result=response.json()['resultado']
    assert result[field]
    assert response.json()['saldo']['consumidos']==0 and response.json()['token_consumido'] is False
    if cnpj!=DAIJU:assert result['website_oficial'] is None
    if cnpj==BOSCHETTI:
        farm=next(r for r in result['referencias'] if 'farmby' in r['url'])
        assert farm['confianca']=='BAIXA' and farm['ressalvas']


def test_saved_results_are_reused_without_token(web):
    # Regra de 10/11/2026: reaproveitar um resultado já salvo não é pesquisa
    # nova e não custa token; a tela informa isso explicitamente.
    for cnpj in (KOBARG, DAIJU, KOBARG):
        data=query(web,cnpj).json()
        assert data['status']=='CONCLUIDA' and data['resultado']['origem_resultado']=='HISTORICO'
        assert data['token_consumido'] is False and data['saldo']['consumidos']==0 and data['saldo']['reservados']==0
        assert any('reaproveitado sem nova busca' in note for note in data['resultado']['ressalvas'])
    with web[1].engine.connect() as db:
        assert db.execute(select(func.count()).select_from(charges)).scalar()==0
    # Reaproveitar funciona mesmo sem saldo livre.
    with web[1].transaction() as db:db.execute(update(organizations).values(initial_tokens=0))
    assert query(web,DAIJU).status_code==200


@pytest.mark.parametrize('cnpj',['123','00000000000000','00065589000107','00065589000106<script>','0065589000106','１２３４５６７８９０１２３４'])
def test_invalid_not_charged(web,cnpj):
    assert query(web,cnpj).status_code==422
    assert web[1].balance('dazumdigital')['consumidos']==0


def test_formatted_cnpj_and_cached_different_operation(web):
    first=query(web,'00.065.589/0001-06').json(); second=query(web,DAIJU).json()
    assert first['cnpj']==second['cnpj']==DAIJU and first['id']!=second['id']
    assert second['saldo']['consumidos']==0


def test_idempotent_duplicate_retry_reload(web):
    key=str(uuid.uuid4())
    first=query(web,key=key).json(); second=query(web,key=key).json()
    assert first['id']==second['id']
    assert second['saldo']['consumidos']==0
    assert web[0].get('/api/consultas/'+first['id']).json()['saldo']['consumidos']==0
    assert query(web,THP,key=key).status_code==409
    with web[1].engine.connect() as db:
        assert db.execute(select(func.count()).select_from(consultations)).scalar()==1


def test_concurrent_duplicates_and_users(web):
    _,store,_,_=web
    with store.engine.connect() as db: user=dict(db.execute(select(users)).mappings().one())
    key=str(uuid.uuid4())
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda _:store.consult(user,DAIJU,key),range(12)))
    assert len({r['id'] for r in results})==1
    create_user(store,'colega','Outra-senha-123')
    with store.engine.connect() as db: colleague=dict(db.execute(select(users).where(users.c.username=='colega')).mappings().one())
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda i:store.consult(user if i%2 else colleague,DAIJU,str(uuid.uuid4())),range(12)))
    assert len({r['id'] for r in results})==12 and all(r['status']=='CONCLUIDA' for r in results)
    assert store.balance('dazumdigital')=={'disponiveis':2469,'consumidos':0,'reservados':0,'consultas_concluidas':0}
    with store.engine.connect() as db: assert db.execute(select(func.count()).select_from(charges)).scalar()==0


def test_zero_balance_concurrent(web):
    # Cinco pesquisas novas disputam o último token: só uma reserva.
    from conftest import generated_cnpj
    _,store,_,_=web
    with store.transaction() as db:
        db.execute(update(organizations).values(initial_tokens=1))
        user=dict(db.execute(select(users)).mappings().one())
    def run(i):
        try:return store.consult(user,generated_cnpj(880000+i),str(uuid.uuid4()),True,True)['status']
        except WebError as exc:return exc.code
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool: answers=list(pool.map(run,range(5)))
    assert answers.count('NA_FILA')==1 and answers.count('SALDO_INSUFICIENTE')==4
    assert store.balance('dazumdigital')['reservados']==1 and store.balance('dazumdigital')['disponiveis']==0
    assert query(web).status_code==200  # resultado salvo continua acessível sem saldo


def test_internal_failure_rollback_no_charge(web,monkeypatch,caplog):
    store=web[1]; original=store.complete
    def fail(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError('secret-provider-api-key-not-public')
    monkeypatch.setattr(store,'complete',fail)
    response=query(web)
    assert response.status_code==500
    assert 'secret' not in response.text
    assert 'secret-provider-api-key-not-public' not in caplog.text
    assert store.balance('dazumdigital')['consumidos']==0
    with store.engine.connect() as db:
        assert db.execute(select(func.count()).select_from(consultations)).scalar()==0
        assert db.execute(select(func.count()).select_from(charges)).scalar()==0


def test_external_disabled_and_unknown_without_charge(web):
    # A fixture CNPJ outside the historical20; no external calls permitted.
    from conftest import generated_cnpj
    cnpj=generated_cnpj(99999)
    assert query(web,cnpj,autorizar_pesquisa_externa=True).status_code==404
    assert web[1].balance('dazumdigital')['consumidos']==0


def test_durable_queue_success_failure_cancel(web):
    from conftest import generated_cnpj
    _,store,_,_=web
    with store.engine.connect() as db:user=dict(db.execute(select(users)).mappings().one())
    first=store.consult(user,generated_cnpj(99999),str(uuid.uuid4()),True,True)
    assert first['status']=='NA_FILA' and first['saldo']['reservados']==1 and first['saldo']['consumidos']==0
    job=store.claim('dazumdigital'); assert store.claim('dazumdigital') is None
    store.finish_job(job['id']); store.finish_job(job['id'])
    assert store.balance('dazumdigital')['reservados']==0
    assert store.balance('dazumdigital')['consumidos']==0
    first=store.consult(user,generated_cnpj(99998),str(uuid.uuid4()),True,True)
    payload=query(web,DAIJU).json()['resultado']
    payload['empresa']['cnpj']=generated_cnpj(99998)
    store.claim('dazumdigital'); store.finish_job(first['id'],payload); store.finish_job(first['id'],payload)
    # Só a pesquisa real concluída custa: reaproveitar DAIJU acima foi grátis e
    # repetir finish_job não duplica o desconto.
    assert store.balance('dazumdigital')['consumidos']==1
    assert store.balance('dazumdigital')['reservados']==0


def test_csrf_unauthorized_host_and_cookie(web):
    client,_,headers,config=web
    assert client.post('/api/consultas',json={'cnpj':DAIJU}).status_code==403
    assert client.post('/api/logout',headers=headers|{'Origin':'https://evil.example'}).status_code==403
    assert client.get('/api/saldo',headers={'Host':'evil.example'}).status_code==400
    response=client.post('/api/logout',headers=headers);assert response.status_code==200
    assert client.get('/api/saldo').status_code==401
    assert client.get('/api/consultas').status_code==401


def test_login_errors_rate_limit_and_no_technical_leak(web):
    client,_,_,config=web
    for _ in range(5):
        response=client.post('/api/login',json={'usuario':'missing','senha':'no'},headers={'Origin':config.origin})
        assert response.status_code==401
    assert client.post('/api/login',json={'usuario':'missing','senha':'no'},headers={'Origin':config.origin}).status_code==429
    data=query(web,BOSCHETTI).json()
    public=json.dumps(data).lower()
    assert all(x not in public for x in ['serper','api_key','database_url','source_hash','c:\\','provider_verification'])


def test_security_headers_assets_and_size(web):
    client,_,_,_=web
    response=client.get('/')
    assert response.status_code==200
    assert "script-src 'self'" in response.headers['Content-Security-Policy']
    assert client.get('/assets/app.js').status_code==200
    assert client.get('/assets/core.mjs').status_code==200
    assert client.get('/.env').status_code==404
    assert client.get('/api/health').json()=={'status':'ok'}
    assert client.post('/api/login',content='x'*5000).status_code==413


def test_scope_prevents_cross_organization_access(web):
    client,store,_,config=web
    identifier=query(web).json()['id']
    store.initialize('outra'); create_user(store,'outra','Outra-conta-123','outra')
    client.post('/api/login',json={'usuario':'outra','senha':'Outra-conta-123'},headers={'Origin':config.origin})
    assert client.get('/api/consultas/'+identifier).status_code==404
    assert client.get('/api/consultas').json()==[]
    assert client.get('/api/saldo').json()['disponiveis']==2469


@pytest.mark.parametrize('kwargs',[
    dict(database_url='sqlite:///var/platform.sqlite',environment='production',origin='https://internal.example.com'),
    dict(database_url='sqlite:///'+(ROOT/'data/poc.sqlite').as_posix()),
    dict(database_url='postgresql+psycopg://a:b@remote/db',environment='production',origin='https://internal.example.com'),
    dict(origin='http://public.example.com'), dict(origin='https://public.example.com/path'),
])
def test_unsafe_config_rejected(kwargs):
    with pytest.raises(ValueError):WebSettings(**kwargs)


def test_safe_serverless_entrypoint_without_database():
    with TestClient(create_app(WebSettings(origin='http://testserver'))) as client:
        assert client.get('/').status_code==200
        assert client.get('/api/health').status_code==503


def test_legacy_database_rejected(tmp_path):
    path=tmp_path/'legacy.sqlite'
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE requests(id INTEGER)')
    with pytest.raises(ValueError):Store('sqlite:///'+path.as_posix()).initialize()


def test_anonymous_session_discovery_is_public_without_database():
    with TestClient(create_app(WebSettings(origin='http://testserver'))) as client:
        assert client.get('/api/sessao').json()=={'autenticado':False}
        assert client.get('/api/saldo').status_code==401


def test_conflicting_tls_options_rejected():
    with pytest.raises(ValueError):
        WebSettings(database_url='postgresql+psycopg://a:b@remote/db?sslmode=verify-full&sslmode=disable')


def test_engine_preflight_blocks_unverified_provider_without_http(monkeypatch):
    import asyncio
    from app.web import engine
    from app.config import Settings
    monkeypatch.setattr(engine.Settings,'from_env',lambda *_:Settings())
    monkeypatch.setattr(engine,'load_policies',lambda *_:{})
    monkeypatch.setattr(engine.httpx,'AsyncClient',lambda *a,**kw:pytest.fail('HTTP não deveria ser construído'))
    with pytest.raises(ValueError,match='não autorizada'):
        asyncio.run(engine.research_one(DAIJU))


def test_web_worker_keeps_small_pilot_limits():
    from app.web.engine import external_settings
    from app.config import Settings
    limited=external_settings(Settings())
    assert limited.website_max_requests==120 and limited.website_rpm==6
    assert limited.website_delay>=5 and limited.concurrency==1
    assert limited.retries==0 and limited.queries==1 and limited.candidates==1
    assert not limited.enable_brasilapi
    assert external_settings(Settings(website_max_requests=7,website_rpm=2)).website_max_requests==7


def test_native_login_post_fails_safely_without_echoing_credentials(web):
    client,store,_,config=web
    response=client.post('/api/login',data={'username':'equipe','password':'Senha-fixture-123'},headers={'Origin':config.origin})
    assert response.status_code==422
    assert 'Senha-fixture-123' not in response.text and 'password' not in response.text
    assert '?' not in str(response.request.url)
    assert store.balance('dazumdigital')['consumidos']==0
