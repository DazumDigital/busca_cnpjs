import concurrent.futures
import csv
import io
import json
import uuid
import pytest
from sqlalchemy import select, func, update
from openpyxl import load_workbook
from test_web_platform import web, source, DAIJU, THP, BOSCHETTI, SOFTDATA
from conftest import generated_cnpj
from app.web import batches as service
from app.web.batch_input import validated_input, analyze_input
from app.web.exports import export
from app.web.store import users, companies, charges, organizations, batches, consultations, Store, WebError
from app.web.auth import create_user


def user(web):
    with web[1].engine.connect() as db:
        return dict(db.execute(select(users).where(users.c.username == 'equipe')).mappings().one())


def cnps(web, n):
    with web[1].engine.connect() as db:
        return db.execute(select(companies.c.cnpj).order_by(companies.c.cnpj).limit(n)).scalars().all()


def create(web, values, key=None):
    return web[0].post('/api/lotes', json={'cnpjs': values}, headers=web[2] | {'Idempotency-Key':key or str(uuid.uuid4())})


def step(web, batch, item):
    return web[0].post(f"/api/lotes/{batch['id']}/itens/{item['id']}/processar", headers=web[2])


@pytest.mark.parametrize('n', [1, 5, 10, 15])
def test_batches_process_exactly_once_and_persist(web, n):
    key = str(uuid.uuid4())
    response = create(web, '\n'.join(cnps(web,n)), key)
    assert response.status_code == 200, response.text
    batch = response.json()
    assert batch['progresso']['total'] == n
    assert batch['saldo']['disponiveis'] == 2469 - n
    assert batch['saldo']['consumidos'] == 0 and batch['saldo']['reservados'] == n
    assert create(web, '\n'.join(cnps(web,n)), key).json()['id'] == batch['id']
    assert web[0].get('/api/lotes/'+batch['id']).json()['progresso']['pendentes'] == n
    for i, item in enumerate(batch['itens'], 1):
        result = step(web,batch,item).json()
        assert result['progresso']['concluidos'] == i
        assert result['saldo']['consumidos'] == i
        assert step(web,batch,item).json()['saldo']['consumidos'] == i
        fresh = Store(web[3].database_url)
        assert service.get_batch(fresh,user(web),batch['id'])['progresso']['concluidos'] == i
    assert result['status'] == 'CONCLUIDO' and result['saldo']['reservados'] == 0
    with web[1].engine.connect() as db:
        assert db.execute(select(func.count()).select_from(charges)).scalar() == n


def test_sixteen_rejected_before_reservation(web):
    assert create(web, '\n'.join(cnps(web,16))).status_code == 422
    assert web[1].balance('dazumdigital')['reservados'] == 0
    with web[1].engine.connect() as db:
        assert db.execute(select(func.count()).select_from(batches)).scalar() == 0


def test_parser_counts_and_duplicates_without_extra_tokens(web):
    value = f'{DAIJU};00.065.589/0001-06,123\n{THP}\n<script>'
    parsed = analyze_input(value)
    assert (parsed['identificados'],parsed['validos'],parsed['invalidos'],parsed['duplicados']) == (5,2,2,1)
    batch = create(web,value).json()
    assert batch['progresso']['total'] == 2 and batch['saldo']['reservados'] == 2
    assert create(web,'123;invalid').status_code == 422


@pytest.mark.parametrize('value', ['', '１２３４５６７８９０１２３４', '<script>00065589000106', '1'*3501, '\n; ,'])
def test_invalid_inputs_are_rejected(value):
    with pytest.raises(WebError):validated_input(value)


def test_unknown_company_is_not_charged_or_researched(web):
    batch = create(web, DAIJU+';'+generated_cnpj(999999)).json()
    for item in batch['itens']:result=step(web,batch,item).json()
    assert result['status'] == 'CONCLUIDO_COM_ERROS'
    assert result['progresso']['concluidos'] == 1 and result['progresso']['erros'] == 1
    assert result['saldo']['consumidos'] == 1 and result['saldo']['reservados'] == 0
    assert result['itens'][1]['erro'] == 'EMPRESA_NAO_PESQUISADA'
    assert result['pesquisa_externa'] is False


def test_insufficient_balance_is_atomic(web):
    with web[1].transaction() as db:db.execute(update(organizations).values(initial_tokens=10))
    assert create(web,'\n'.join(cnps(web,15))).status_code == 402
    assert web[1].balance('dazumdigital')['reservados'] == 0


def test_network_retry_same_item_does_not_advance(web):
    batch = create(web,DAIJU+';'+THP).json()
    first = step(web,batch,batch['itens'][0]).json()
    again = step(web,batch,batch['itens'][0]).json()
    assert first['progresso'] == again['progresso']
    assert again['progresso']['pendentes'] == 1 and again['saldo']['consumidos'] == 1


def test_failure_rolls_back_and_resumes_without_double_charge(web, monkeypatch):
    batch = create(web,DAIJU).json();original=web[1].complete
    def fail(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError('private-provider-secret')
    monkeypatch.setattr(web[1],'complete',fail)
    response=step(web,batch,batch['itens'][0])
    assert response.status_code==500 and 'private' not in response.text
    assert web[1].balance('dazumdigital')['consumidos']==0
    assert web[1].balance('dazumdigital')['reservados']==1
    monkeypatch.setattr(web[1],'complete',original)
    assert step(web,batch,batch['itens'][0]).json()['saldo']['consumidos']==1


def test_concurrent_creations_and_steps(web):
    store=web[1]; identity=user(web);key=str(uuid.uuid4())
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results=list(pool.map(lambda _:service.create_batch(store,identity,DAIJU+';'+THP,key), range(12)))
    assert len({b['id'] for b in results})==1
    assert store.balance('dazumdigital')['reservados']==2
    batch=results[0]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _:service.step_batch(store,identity,batch['id'],batch['itens'][0]['id']),range(12)))
    assert store.balance('dazumdigital')['consumidos']==1
    assert service.get_batch(store,identity,batch['id'])['progresso']['pendentes']==1


def test_concurrent_reservations_cannot_exceed_balance(web):
    store=web[1];identity=user(web)
    with store.transaction() as db:db.execute(update(organizations).values(initial_tokens=1))
    def run(_):
        try:return service.create_batch(store,identity,DAIJU,str(uuid.uuid4()))['status']
        except WebError as e:return e.code
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(run,range(6)))
    assert results.count('PENDENTE')==1 and results.count('SALDO_INSUFICIENTE')==5


def test_cancel_is_idempotent_and_preserves_completed_items(web):
    batch=create(web,DAIJU+';'+THP).json();step(web,batch,batch['itens'][0])
    endpoint='/api/lotes/'+batch['id']+'/cancelar'
    first=web[0].post(endpoint,headers=web[2]).json();second=web[0].post(endpoint,headers=web[2]).json()
    assert first['progresso']==second['progresso']
    assert second['saldo']['consumidos']==1 and second['saldo']['reservados']==0


def test_mutations_require_origin_csrf_and_owner(web):
    batch=create(web,DAIJU).json();client,store,headers,config=web
    assert client.post('/api/lotes',json={'cnpjs':DAIJU}).status_code==403
    assert client.post('/api/lotes',json={'cnpjs':DAIJU},headers=headers).status_code==422
    create_user(store,'colleague','Strong-fixture-password')
    session=client.post('/api/login',json={'usuario':'colleague','senha':'Strong-fixture-password'},headers={'Origin':config.origin}).json()
    other={'Origin':config.origin,'X-CSRF-Token':session['csrf']}
    assert client.get('/api/lotes/'+batch['id']).status_code==404
    assert client.get('/api/lotes/'+batch['id']+'/exportar').status_code==404
    assert client.get('/api/consultas/'+batch['itens'][0]['id']).status_code==404
    assert client.get('/api/lotes').json()==[]
    assert client.post(f"/api/lotes/{batch['id']}/itens/{batch['itens'][0]['id']}/processar",headers=other).status_code==404
    assert client.post('/api/lotes/'+batch['id']+'/cancelar',headers=other).status_code==404


def test_idempotency_key_cannot_change_input(web):
    key=str(uuid.uuid4());batch=create(web,DAIJU,key).json()
    assert create(web,THP,key).status_code==409
    assert web[1].balance('dazumdigital')['reservados']==1


@pytest.mark.parametrize('format',['csv','xlsx'])
def test_export_separates_official_manual_and_references_without_charging(web,format):
    batch=create(web,DAIJU+';'+THP+';'+BOSCHETTI+';'+SOFTDATA).json()
    for item in batch['itens']:step(web,batch,item)
    before=web[1].balance('dazumdigital')
    response=web[0].get('/api/lotes/'+batch['id']+'/exportar?formato='+format)
    assert response.status_code==200
    assert web[1].balance('dazumdigital')==before
    if format=='csv':
        rows=list(csv.reader(io.StringIO(response.content.decode('utf-8-sig')),delimiter=';'))
        assert rows[1][0]=="'"+DAIJU and 'lojavirtualnuvem' in rows[1][6]
        assert rows[2][6]=='' and 'thpbrasil' in rows[2][7]
        assert rows[3][6]=='' and 'farmby' in rows[3][10].lower()
    else:
        book=load_workbook(io.BytesIO(response.content));sheet=book['Empresas']
        assert sheet['A2'].value==DAIJU and sheet['A2'].data_type=='s'
        assert sheet['G3'].value is None and 'thpbrasil' in sheet['H3'].value
        assert 'Links' in book.sheetnames
    item=batch['itens'][0]
    assert web[0].get('/api/consultas/'+item['id']+'/exportar?formato='+format).status_code==200


@pytest.mark.parametrize('dangerous',['=HYPERLINK("https://evil")',' +cmd','@SUM(A1)','\t=1+1'])
def test_exports_prevent_formula_execution(dangerous):
    item={'cnpj':DAIJU,'status':'CONCLUIDA','resultado':{'empresa':{'razao_social':dangerous}}}
    content,_,_=export([item],'csv')
    row=list(csv.reader(io.StringIO(content.decode('utf-8-sig')),delimiter=';'))[1]
    assert row[1].startswith("'")
    content,_,_=export([item],'xlsx')
    cell=load_workbook(io.BytesIO(content)).active['B2']
    assert cell.data_type=='s' and cell.value==dangerous


def test_corrupt_or_mismatched_saved_company_does_not_charge(web):
    with web[1].transaction() as db:
        db.execute(update(companies).where(companies.c.cnpj==DAIJU).values(payload='{"empresa":{"cnpj":"wrong"}}'))
    batch=create(web,DAIJU).json()
    result=step(web,batch,batch['itens'][0]).json()
    assert result['progresso']['erros']==1 and result['saldo']['consumidos']==0 and result['saldo']['reservados']==0
