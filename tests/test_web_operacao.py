"""Ajustes da liberação operacional (09/11/2026): robots.txt pela RFC 9309,
identificação do robô, painel do Google, mensagens distintas e regra de tokens.
Sem rede: todas as fontes externas são simuladas."""
import asyncio
import json
import time
import uuid

import pytest
from sqlalchemy import select

from test_web_platform import web, source  # noqa: F401 (fixtures)
from test_web_live_sites import live_web, providers, start, run_until_done, NEW, KEY, ORG, REGISTRY, OFFICIAL_HTML  # noqa: F401
from test_web_batches import user
from app.models import Company, ProviderError
from app.services.validator import Page, USER_AGENT, ROBOT_TOKEN
from app.web import live, live_sources
from app.web.live_sources import robots_rules
from app.web.store import external_control, live_jobs

BASE = 'https://www.acmeparafusos.com.br/'


def test_robot_identity_is_definitive_and_documented(web):
    assert USER_AGENT == 'DazumCNPJ/1.0 (+https://busca-cnpjs.vercel.app/robo)' and ROBOT_TOKEN == 'DazumCNPJ'
    page = web[0].get('/robo')  # pública: sem sessão
    assert page.status_code == 200 and 'text/html' in page.headers['content-type']
    assert 'User-agent: DazumCNPJ' in page.text and 'Disallow: /' in page.text
    assert "frame-ancestors 'none'" in page.headers['content-security-policy']


def test_robots_rules_follow_rfc_9309_with_large_and_invalid_content():
    noise = '<html>' + 'x' * 5000 + '</html>\n'  # linha longa e HTML: ignorados
    text = (noise + 'Sitemap: https://acme.example/sitemap.xml\n'
            'User-agent: Googlebot\nDisallow: /\n\n'
            'User-agent: *\nDisallow: /privado\nCrawl-delay: 15\n\n'
            'User-agent: DazumCNPJ\nUser-agent: OutroBot\nAllow: /\nDisallow: /restrito # comentario\n'
            + '\n'.join(f'# comentário {i}' for i in range(5000)))
    rules = robots_rules(text)
    assert rules == ['User-agent: *', 'Disallow: /privado', 'Crawl-delay: 15',
                     'User-agent: DazumCNPJ', 'Allow: /', 'Disallow: /restrito']
    from urllib.robotparser import RobotFileParser
    parser = RobotFileParser(); parser.parse(rules)
    assert not parser.can_fetch(USER_AGENT, 'https://acme.example/restrito/x')
    assert parser.can_fetch(USER_AGENT, 'https://acme.example/privado')  # grupo próprio prevalece sobre '*'
    assert robots_rules('<!doctype html><html><body>Not found</body></html>') == []


def test_large_html_robots_is_absent_rules_and_state_stays_small(monkeypatch):
    calls = []

    async def fetch_one(url, robots=False):
        calls.append((url, robots))
        if url.endswith('robots.txt'):
            return Page(url, 200, {}, '<html>' + ('<div>layout</div>' * 30000) + '</html>')
        return Page(url, 200, {}, OFFICIAL_HTML)
    monkeypatch.setattr(live_sources, 'fetch_one', fetch_one)
    company = Company(NEW, legal_name=REGISTRY['razao_social'], trade_name='ACME PARAFUSOS')
    state = dict(phase='WEBSITE', company=company.__dict__, queue=[BASE], sites=[], current_site=BASE, robots={}, created_at=time.time())
    while state['phase'] == 'WEBSITE':
        state = asyncio.run(live_sources.execute_action(NEW, state, KEY))
    assert calls == [(BASE + 'robots.txt', True), (BASE, False)]
    assert state['robots'] == {'https://www.acmeparafusos.com.br': []} and state['validation']['confirmed'] is True
    assert len(json.dumps(state)) < 20000


def test_robot_specific_disallow_is_respected_without_fetching(monkeypatch):
    calls = []

    async def fetch_one(url, robots=False):
        calls.append(url)
        return Page(url, 200, {}, 'User-agent: *\nAllow: /\n\nUser-agent: DazumCNPJ\nDisallow: /\n')
    monkeypatch.setattr(live_sources, 'fetch_one', fetch_one)
    company = Company(NEW, legal_name=REGISTRY['razao_social'], trade_name='ACME PARAFUSOS')
    state = dict(phase='WEBSITE', company=company.__dict__, queue=[BASE], sites=[], current_site=BASE, robots={}, created_at=time.time())
    while state['phase'] == 'WEBSITE':
        state = asyncio.run(live_sources.execute_action(NEW, state, KEY))
    assert calls == [BASE + 'robots.txt']
    assert state['site_failures'] == [dict(url=BASE, error='ROBOTS_BLOQUEADO')]


def test_google_panel_site_is_prioritized_and_flagged_but_never_official(live_web, monkeypatch):
    panel = 'https://www.grupoexemplo.com.br/'
    calls = providers(monkeypatch, [BASE, f'https://cnpj.biz/{NEW}'], {})
    original = live_sources.provider_json

    async def with_panel(method, url, **kwargs):
        data = await original(method, url, **kwargs)
        if 'serper' in url:
            data = dict(data, knowledgeGraph={'title': 'ACME', 'website': panel})
            data['organic'] = [{'link': panel}] + data['organic']
        return data
    monkeypatch.setattr(live_sources, 'provider_json', with_panel)
    identity, query = start(live_web)
    result = run_until_done(live_web[1], identity, query['id'])['resultado']
    assert [c[1] for c in calls if c[0] == 'website'][0] == panel + 'robots.txt'  # validado primeiro
    assert result['website_oficial'] is None
    # Grupo próprio, fora dos possíveis sites, e nunca oficial por isso.
    assert [p['url'] for p in result['indicado_google']] == [panel]
    assert not any(p.get('indicado_google') for p in result['possiveis_sites'])
    assert 'Indicado pelo painel do Google; isso não confirma a titularidade.' in result['indicado_google'][0]['ressalvas']
    exported = live_web[0].get(f"/api/consultas/{query['id']}/exportar?formato=csv").text
    assert '""categoria"": ""PAINEL_GOOGLE""' in exported and '""indicado_google"": true' in exported


def test_concluded_real_search_costs_one_token_even_without_links(live_web, monkeypatch):
    # Regra de 10/11/2026: pesquisa real concluída = 1 token; reaproveitar = 0.
    providers(monkeypatch, ['https://portal-noticias.com.br/economia/industria'], {})
    identity, query = start(live_web)
    final = run_until_done(live_web[1], identity, query['id'])
    assert final['status'] == 'CONCLUIDA' and final['token_consumido'] is True
    assert final['saldo'] == dict(disponiveis=2468, consumidos=1, reservados=0, consultas_concluidas=1)
    assert final['resultado']['empresa']['razao_social'] == REGISTRY['razao_social']
    again = live_web[1].consult(identity, NEW, uuid.uuid4().hex, True, True)
    assert again['resultado']['origem_resultado'] == 'HISTORICO' and again['token_consumido'] is False
    assert again['saldo']['consumidos'] == 1


def test_usable_result_charges_once_even_when_processed_again(live_web, monkeypatch):
    providers(monkeypatch, [BASE], {})  # site falha: possível site é utilizável
    identity, query = start(live_web)
    final = run_until_done(live_web[1], identity, query['id'])
    assert final['token_consumido'] is True and final['saldo']['consumidos'] == 1
    for _ in range(3):
        assert live.process_consultation(live_web[1], identity, query['id'], KEY)['saldo']['consumidos'] == 1


def test_branch_shows_headquarters_site_in_own_category(live_web, monkeypatch):
    from conftest import generated_cnpj
    filial, matriz = generated_cnpj(424242000016), generated_cnpj(424242000001)
    head = f'{matriz[:2]}.{matriz[2:5]}.{matriz[5:8]}/{matriz[8:12]}-{matriz[12:]}'
    html = (f'<html><head><title>ACME Parafusos</title></head><body><h1>ACME Parafusos</h1>'
            f'<footer>ACME INDUSTRIA DE PARAFUSOS LTDA · CNPJ {head}</footer></body></html>')

    async def provider_json(method, url, *, key='', payload=None):
        if 'minhareceita' in url:
            return dict(REGISTRY, cnpj=filial)
        return {'organic': [{'link': BASE}]}

    async def fetch_one(url, robots=False):
        return Page(url, 404, {}, '') if url.endswith('robots.txt') else Page(url, 200, {}, html)
    monkeypatch.setattr(live_sources, 'provider_json', provider_json)
    monkeypatch.setattr(live_sources, 'fetch_one', fetch_one)
    identity = user(live_web)
    query = live_web[1].consult(identity, filial, uuid.uuid4().hex, True, True)
    final = run_until_done(live_web[1], identity, query['id'])
    result = final['resultado']
    assert result['website_oficial'] is None and result['status'] == 'SITE_DA_MATRIZ'
    assert [s['url'] for s in result['site_matriz']] == [BASE] and result['site_matriz'][0]['tipo'] == 'SITE_DA_MATRIZ'
    assert head in result['site_matriz'][0]['evidencia'] and result['possiveis_sites'] == []
    assert final['token_consumido'] is True and final['saldo']['consumidos'] == 1
    csv_text = live_web[0].get(f"/api/consultas/{query['id']}/exportar?formato=csv").text
    assert csv_text.splitlines()[0].endswith('"Site da matriz (filial)";"Indicado pelo painel do Google"')


def test_headquarters_with_other_cnpj_is_still_rejected(monkeypatch):
    # Matriz (0001) consultada com outra inscrição na página: continua divergente.
    other = '11.222.333/0001-81'
    html = f'<html><head><title>ACME Parafusos</title></head><body><h1>ACME</h1><footer>ACME · CNPJ {other}</footer></body></html>'

    async def fetch_one(url, robots=False):
        return Page(url, 404, {}, '') if url.endswith('robots.txt') else Page(url, 200, {}, html)
    monkeypatch.setattr(live_sources, 'fetch_one', fetch_one)
    company = Company(NEW, legal_name=REGISTRY['razao_social'], trade_name='ACME PARAFUSOS')
    state = dict(phase='WEBSITE', company=company.__dict__, queue=[BASE], sites=[], current_site=BASE, robots={}, created_at=time.time())
    while state['phase'] == 'WEBSITE':
        state = asyncio.run(live_sources.execute_action(NEW, state, KEY))
    assert state['site_results'][0]['site_type'] == 'CNPJ_DIVERGENTE'


def test_batch_reuses_saved_result_free_and_charges_new_search_once(live_web, monkeypatch):
    from test_web_platform import DAIJU
    from test_web_batches import create
    from app.web import batches
    from test_web_live_sites import Clock
    providers(monkeypatch, [BASE], {BASE + 'robots.txt': Page(BASE + 'robots.txt', 404, {}, ''),
                                    BASE: Page(BASE, 200, {}, OFFICIAL_HTML)})
    clock = Clock()
    runner = lambda s, c, k, **kw: live.advance_job(s, c, k, clock=clock, **kw)
    process = live.process_consultation
    monkeypatch.setattr(live, 'process_consultation', lambda s, u, i, k: process(s, u, i, k, runner))
    batch = create(live_web, f'{DAIJU}\n{NEW}').json()
    assert batch['saldo']['reservados'] == 2
    identity = user(live_web)
    for _ in range(12):
        pending = [i for i in batch['itens'] if i['status'] in {'PENDENTE_LOTE', 'EM_PROCESSAMENTO'}]
        if not pending:
            break
        batch = batches.step_batch(live_web[1], identity, batch['id'], pending[0]['id'], True, KEY)
    assert batch['status'] == 'CONCLUIDO'
    assert [i['token_consumido'] for i in batch['itens']] == [False, True]
    assert batch['progresso']['tokens_consumidos'] == 1
    assert batch['saldo'] == dict(disponiveis=2468, consumidos=1, reservados=0, consultas_concluidas=1)


def test_real_search_failure_after_debit_rolls_back_and_resumes_once(live_web, monkeypatch):
    from test_web_live_sites import Clock
    from app.web.store import charges
    providers(monkeypatch, [BASE], {})
    store = live_web[1]
    identity, query = start(live_web)
    original = store.complete
    calls = []

    def fail_once(*args, **kwargs):
        original(*args, **kwargs)
        if not calls:
            calls.append(1)
            raise RuntimeError('falha simulada depois do débito')
    monkeypatch.setattr(store, 'complete', fail_once)
    clock = Clock()
    runner = lambda s, c, k, **kw: live.advance_job(s, c, k, clock=clock, **kw)
    with pytest.raises(RuntimeError):
        for _ in range(20):
            live.process_consultation(store, identity, query['id'], KEY, runner)
    assert store.balance(ORG)['consumidos'] == 0 and store.balance(ORG)['reservados'] == 1
    final = live.process_consultation(store, identity, query['id'], KEY, runner)
    for _ in range(3):
        final = live.process_consultation(store, identity, query['id'], KEY, runner)
    assert final['status'] == 'CONCLUIDA' and final['saldo']['consumidos'] == 1 and final['saldo']['reservados'] == 0
    with store.engine.connect() as db:
        assert db.execute(select(charges.c.consultation_id)).scalars().all() == [query['id']]


def api_query(web, cnpj=NEW):
    client, _, headers, _ = web
    return client.post('/api/consultas', json={'cnpj': cnpj}, headers=headers | {'Idempotency-Key': uuid.uuid4().hex})


def test_distinct_messages_for_scope_suspension_configuration_and_rate(live_web, monkeypatch):
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from app.web.api import create_app
    store = live_web[1]
    config = replace(live_web[3], external_enabled=True, serper_key=KEY)
    with TestClient(create_app(config, store)) as client:
        csrf = client.post('/api/login', json={'usuario': 'equipe', 'senha': 'Senha-fixture-123'},
                           headers={'Origin': config.origin}).json()['csrf']
        headers = {'Origin': config.origin, 'X-CSRF-Token': csrf}
        ask = lambda: client.post('/api/consultas', json={'cnpj': NEW}, headers=headers | {'Idempotency-Key': uuid.uuid4().hex})
        with store.transaction() as db:
            policy = json.loads(db.execute(select(external_control.c.policy)).scalar_one())
        live.configure(store, dict(policy, allowed_organizations=['outra-organizacao']), key=KEY)
        denied = ask()
        assert denied.status_code == 403 and denied.json()['erro'] == 'PESQUISA_NAO_LIBERADA'
        live.block(store)
        suspended = ask()
        assert suspended.status_code == 503 and suspended.json()['erro'] == 'PESQUISA_SUSPENSA'
        with store.transaction() as db:
            db.execute(external_control.delete())
        pending = ask()
        assert pending.status_code == 503 and pending.json()['erro'] == 'INTEGRACAO_NAO_VERIFICADA'
        assert len({denied.json()['mensagem'], suspended.json()['mensagem'], pending.json()['mensagem']}) == 3
        statuses = [ask().status_code for _ in range(config.query_per_minute)]
        limited = ask()
        assert limited.status_code == 429 and limited.json()['erro'] == 'LIMITE_CONSULTAS'
        assert store.balance(ORG)['consumidos'] == 0 and store.balance(ORG)['reservados'] == 0
