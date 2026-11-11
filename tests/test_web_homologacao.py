"""Homologação de 10/11/2026.

Resultados aprovados pelo responsável na conferência visual da revalidação,
consultas que nunca ficam presas com token reservado, nova tentativa segura do
cadastro gratuito e robustez de histórico, limite de login e exportação.
"""
import io
import json
import time
import uuid
from pathlib import Path

import pytest
from openpyxl import load_workbook
from sqlalchemy import func, insert, select, update

from test_web_platform import web, source, query, DAIJU
from test_web_batches import user
from test_web_live import live_setup, simulated, runner, NEW, ORG, KEY
from app.models import ProviderError
from app.web import batches as service
from app.web import live
from app.web.api import client_address, company_name
from app.web.exports import export
from app.web.live_sources import payload_for
from app.web.store import WebError, consultations, external_requests, organizations

STATES = json.loads((Path(__file__).parent / 'fixtures/homologacao_revalidacao_states.json').read_text(encoding='utf-8'))
GROUPS = ('website_oficial', 'site_matriz', 'possiveis_sites', 'indicado_google', 'redes_sociais', 'referencias')
DIRECTORIES = ('econodata', 'cnpj.biz', 'buscasim', 'srwatson', 'atlaspublico', 'portaldatransparencia', 'waze', 'pandape', 'indeed')

# Resultado de produção conferido e aprovado pelo responsável (10/11/2026):
# prioriza o site oficial, mas preserva possíveis sites e referências úteis.
APPROVED = {
    '00623904000173': dict(status='CONCLUIDO', website_oficial=['https://www.apple.com/br/']),
    '83646984000100': dict(status='FALHA_VALIDACAO_SITE_OFICIAL',
                           possiveis_sites=['https://institucional.angeloni.com.br/', 'https://www.angeloni.com.br/']),
    '83648477001691': dict(status='SITE_DA_MATRIZ', site_matriz=['https://institucional.giassi.com.br/'],
                           possiveis_sites=['https://www.giassi.com.br/'],
                           redes_sociais=['https://www.facebook.com/giassisupermercados/'],
                           referencias=['https://portaldatransparencia.gov.br/cartoes/cpgf/pessoa-juridica/83648477001691']),
    '04743028000170': dict(status='SEM_SITE_OFICIAL_CONFIRMADO', referencias=[
        'https://www.econodata.com.br/consulta-empresa/04743028000170-fg-prime-empreendimentos-ltda',
        'https://srwatson.io/empresa/fg-prime-empreendimentos-ltda-04743028000170',
        'https://buscasim.com.br/cnpj/04743028000170-FG-PRIME-EMPREENDIMENTOS-LTDA',
        'https://atlaspublico.com.br/empresa/04743028000170']),
    '66136840000194': dict(status='CONCLUIDO', website_oficial=['https://www.melaupetcaetano.com.br/'],
                           referencias=['https://www.econodata.com.br/consulta-empresa/66136840000194-a-melau-pet-caetano-ltda']),
}


def urls(payload, group):
    value = payload.get(group)
    if isinstance(value, dict):
        return [value['url']]
    return [item['url'] for item in value or []]


@pytest.mark.parametrize('cnpj', sorted(APPROVED))
def test_approved_production_results_are_reproduced(cnpj):
    payload = payload_for(json.loads(json.dumps(STATES[cnpj])))
    expected = APPROVED[cnpj]
    assert payload['status'] == expected['status']
    for group in GROUPS:
        assert urls(payload, group) == expected.get(group, []), group
    # Diretórios e plataformas nunca viram site oficial, da matriz ou possível site.
    for group in ('website_oficial', 'site_matriz', 'possiveis_sites', 'indicado_google'):
        assert not any(name in url for url in urls(payload, group) for name in DIRECTORIES)


def test_apple_official_site_states_the_same_company_establishment():
    payload = payload_for(json.loads(json.dumps(STATES['00623904000173'])))
    assert '00.623.904/0003-35' in payload['website_oficial']['ressalvas'][0]


def test_pending_query_is_settled_when_external_search_is_off(live_setup):
    client, store, headers, config = live_setup
    assert config.external_enabled is False
    identity = user(live_setup)
    pending = store.consult(identity, NEW, str(uuid.uuid4()), True, True)
    assert pending['status'] == 'NA_FILA' and pending['saldo']['reservados'] == 1
    for _ in range(2):
        data = client.post(f"/api/consultas/{pending['id']}/processar", headers=headers).json()
        assert data['status'] == 'FALHOU' and data['saldo']['reservados'] == 0 and data['saldo']['consumidos'] == 0
    assert client.post(f"/api/consultas/{pending['id']}/processar", headers=headers).status_code == 200
    with store.engine.connect() as db:
        assert db.execute(select(func.count()).select_from(external_requests)).scalar() == 0


def test_pending_query_of_saved_company_completes_free_when_external_is_off(web):
    client, store, headers, _ = web
    identity = user(web)
    identifier = str(uuid.uuid4())
    with store.transaction() as db:
        db.execute(insert(consultations).values(id=identifier, organization_id=ORG, user_id=identity['id'],
            idempotency_key='fixture-pending-key-1', cnpj=DAIJU, status='NA_FILA', created_at=time.time()))
        db.execute(update(organizations).values(reserved=organizations.c.reserved + 1))
    data = client.post(f'/api/consultas/{identifier}/processar', headers=headers).json()
    assert data['status'] == 'CONCLUIDA' and data['token_consumido'] is False
    assert data['saldo']['reservados'] == 0 and data['saldo']['consumidos'] == 0
    assert data['resultado']['origem_resultado'] == 'HISTORICO'


def test_processing_requires_csrf_and_owner_even_when_external_is_off(live_setup):
    client, store, headers, _ = live_setup
    pending = store.consult(user(live_setup), NEW, str(uuid.uuid4()), True, True)
    assert client.post(f"/api/consultas/{pending['id']}/processar", headers={'Origin': headers['Origin']}).status_code == 403
    assert client.post(f'/api/consultas/{uuid.uuid4()}/processar', headers=headers).status_code == 404
    assert store.balance(ORG)['reservados'] == 1


@pytest.mark.parametrize('code', ['PESQUISA_SUSPENSA', 'INTEGRACAO_NAO_VERIFICADA', 'PESQUISA_NAO_LIBERADA'])
def test_policy_refusal_closes_query_without_charge_and_keeps_progress(live_setup, code):
    store = live_setup[1]
    identity = user(live_setup)
    first = store.consult(identity, NEW, str(uuid.uuid4()), True, True)
    assert live.process_consultation(store, identity, first['id'], KEY, runner)['status'] == 'NA_FILA'

    def refused(*args, **kwargs):
        raise WebError(code, 503)
    closed = live.process_consultation(store, identity, first['id'], KEY, refused)
    assert closed['status'] == 'FALHOU' and closed['erro'] == code
    assert closed['saldo']['reservados'] == 0 and closed['saldo']['consumidos'] == 0
    # Uma nova consulta continua de onde parou: o cadastro não é repetido.
    second = store.consult(identity, NEW, str(uuid.uuid4()), True, True)
    final = live.process_consultation(store, identity, second['id'], KEY, runner)
    assert final['status'] == 'CONCLUIDA' and final['saldo']['consumidos'] == 1 and final['saldo']['reservados'] == 0
    with store.engine.connect() as db:
        assert db.execute(select(func.count()).select_from(external_requests)).scalar() == 2


def test_blocked_policy_fails_batch_item_and_returns_reservation(live_setup):
    store = live_setup[1]
    identity = user(live_setup)
    batch = service.create_batch(store, identity, NEW, str(uuid.uuid4()))
    assert batch['saldo']['reservados'] == 1
    live.block(store)
    data = service.step_batch(store, identity, batch['id'], batch['itens'][0]['id'], True, KEY)
    assert data['itens'][0]['status'] == 'FALHOU' and data['itens'][0]['erro'] == 'PESQUISA_SUSPENSA'
    assert data['saldo']['reservados'] == 0 and data['saldo']['consumidos'] == 0
    with store.engine.connect() as db:
        assert db.execute(select(func.count()).select_from(external_requests)).scalar() == 0


def test_transient_registry_failure_is_retried_once_by_a_new_query(live_setup):
    store = live_setup[1]
    identity = user(live_setup)
    calls = []

    async def flaky(cnpj, state, key):
        calls.append(state['phase'])
        if len(calls) == 1:
            raise ProviderError('ERRO_PROVEDOR')
        return await simulated(cnpj, state, key)
    run = lambda s, c, k, **kw: live.advance_job(s, c, k, executor=flaky, **kw)
    first = store.consult(identity, NEW, str(uuid.uuid4()), True, True)
    failed = live.process_consultation(store, identity, first['id'], KEY, run)
    assert failed['status'] == 'FALHOU' and failed['saldo']['reservados'] == 0 and failed['saldo']['consumidos'] == 0
    # Reabrir a consulta que falhou não tenta de novo.
    live.process_consultation(store, identity, first['id'], KEY, run)
    assert calls == ['CADASTRO']
    second = store.consult(identity, NEW, str(uuid.uuid4()), True, True)
    live.process_consultation(store, identity, second['id'], KEY, run)
    final = live.process_consultation(store, identity, second['id'], KEY, run)
    assert final['status'] == 'CONCLUIDA' and final['saldo']['consumidos'] == 1 and final['saldo']['reservados'] == 0
    assert calls == ['CADASTRO', 'CADASTRO', 'BUSCA']


@pytest.mark.parametrize('phase,code', [('BUSCA', 'ERRO_PROVEDOR'), ('CADASTRO', 'CNPJ_NAO_ENCONTRADO'), ('CADASTRO', 'COTA_ESGOTADA')])
def test_paid_or_permanent_failures_are_not_retried_automatically(live_setup, phase, code):
    store = live_setup[1]
    identity = user(live_setup)
    calls = []

    async def fails(cnpj, state, key):
        calls.append(state['phase'])
        if state['phase'] == phase:
            raise ProviderError(code)
        return await simulated(cnpj, state, key)
    run = lambda s, c, k, **kw: live.advance_job(s, c, k, executor=fails, **kw)
    first = store.consult(identity, NEW, str(uuid.uuid4()), True, True)
    while live.process_consultation(store, identity, first['id'], KEY, run)['status'] == 'NA_FILA':
        pass
    attempts = len(calls)
    live.configure(store, dict(json.loads(json.dumps(_policy(store))), blocked=False), key=KEY)
    second = store.consult(identity, NEW, str(uuid.uuid4()), True, True)
    result = live.process_consultation(store, identity, second['id'], KEY, run)
    assert result['status'] == 'FALHOU' and result['saldo']['consumidos'] == 0 and result['saldo']['reservados'] == 0
    assert len(calls) == attempts


def _policy(store):
    from app.web.store import external_control
    with store.engine.connect() as db:
        policy = json.loads(db.execute(select(external_control.c.policy)).scalar_one())
    policy.pop('key_sha256', None)
    policy.pop('blocked', None)
    return policy


def test_history_shows_company_name_and_handles_bad_payloads(web):
    assert query(web, DAIJU).status_code == 200
    entries = web[0].get('/api/consultas').json()
    assert entries[0]['cnpj'] == DAIJU and entries[0]['empresa']
    assert company_name(None) == company_name('not json') == company_name('[]') == ''


class FakeRequest:
    def __init__(self, headers, host='10.0.0.1'):
        self.headers = headers
        self.client = type('Client', (), {'host': host})()


def test_login_rate_limit_uses_the_platform_client_address(monkeypatch):
    monkeypatch.delenv('VERCEL', raising=False)
    assert client_address(FakeRequest({'x-forwarded-for': '203.0.113.9'})) == '10.0.0.1'
    monkeypatch.setenv('VERCEL', '1')
    assert client_address(FakeRequest({'x-forwarded-for': '203.0.113.9'})) == '203.0.113.9'
    assert client_address(FakeRequest({'x-forwarded-for': '203.0.113.9, 10.1.1.1'})) == '203.0.113.9'
    assert client_address(FakeRequest({'x-forwarded-for': 'not-an-ip'})) == '10.0.0.1'
    assert client_address(FakeRequest({})) == '10.0.0.1'


def test_xlsx_export_survives_control_characters_and_long_evidence():
    item = {'cnpj': DAIJU, 'status': 'CONCLUIDA', 'resultado': {'empresa': {'razao_social': 'EMPRESA\x0bX\x01'},
            'referencias': [{'url': 'https://example.com/a', 'evidencia': 'x' * 40000 + '\x0c'}]}}
    content, _, _ = export([item], 'xlsx')
    book = load_workbook(io.BytesIO(content))
    assert book['Empresas']['B2'].value == 'EMPRESAX'
    assert book['Empresas']['A2'].value == DAIJU and book['Empresas']['A2'].data_type == 's'
    assert len(book['Links']['F2'].value) <= 32000 and len(book['Empresas']['L2'].value) <= 32000
