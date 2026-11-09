"""Fluxo real ponta a ponta com o executor de produção e provedores simulados.

Nenhum teste acessa a rede: Minha Receita, Serper e os sites candidatos são
substituídos por respostas fixas. Cobre site oficial, candidato, referência,
falhas de validação, orçamento de acessos, pausas por limite e liberação.
"""
import asyncio
import json
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, func

from conftest import generated_cnpj
from test_web_platform import web, source  # noqa: F401 (fixtures)
from test_web_batches import user, create
from app.config import policy_reason
from app.models import Company, ProviderError
from app.services.validator import Page
from app.web import live, live_sources, manage
from app.web.store import companies, external_requests, live_jobs, external_control, organizations

NEW = generated_cnpj(424242)
ORG = 'dazumdigital'
KEY = 'fixture-private-key'
FORMATTED = f'{NEW[:2]}.{NEW[2:5]}.{NEW[5:8]}/{NEW[8:12]}-{NEW[12:]}'
REGISTRY = dict(cnpj=NEW, razao_social='ACME INDUSTRIA DE PARAFUSOS LTDA', nome_fantasia='ACME PARAFUSOS',
                municipio='BLUMENAU', uf='SC', descricao_situacao_cadastral='ATIVA')
OFFICIAL_HTML = f'''<html><head><title>ACME Parafusos | Fixadores industriais</title></head><body>
<h1>ACME Parafusos</h1><p>A ACME Parafusos fabrica fixadores em Blumenau.</p>
<a href="/sobre">Sobre</a><a href="/contato">Contato</a>
<footer>ACME INDUSTRIA DE PARAFUSOS LTDA · CNPJ {FORMATTED} · Blumenau/SC</footer></body></html>'''
CANDIDATE_HTML = '''<html><head><title>ACME Parafusos</title></head><body><h1>ACME Parafusos</h1>
<p>Fixadores industriais em Blumenau.</p><a href="/sobre">Sobre</a><a href="/contato">Contato</a></body></html>'''


@pytest.fixture
def live_web(web, policy):
    caps = dict(policy, max_requests=100, min_interval=0)
    live.configure(web[1], dict(serper=caps, websites=caps, minhareceita=caps, allowed_organizations=[ORG]), key=KEY)
    return web


class Clock:
    """Relógio determinístico: cada leitura avança 30 s, sem dormir nos testes."""
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        self.now += 30
        return self.now


def providers(monkeypatch, organic, pages, calls=None):
    calls = [] if calls is None else calls

    async def provider_json(method, url, *, key='', payload=None):
        calls.append(('serper' if 'serper' in url else 'minhareceita', url))
        if 'minhareceita' in url:
            return dict(REGISTRY)
        assert key == KEY and payload['gl'] == 'br'
        return {'organic': [{'link': link} for link in organic]}

    async def fetch_one(url):
        calls.append(('website', url))
        page = pages.get(url)
        if page is None:
            raise ProviderError('SITE_INACESSIVEL')
        if isinstance(page, Exception):
            raise page
        return page

    monkeypatch.setattr(live_sources, 'provider_json', provider_json)
    monkeypatch.setattr(live_sources, 'fetch_one', fetch_one)
    return calls


def run_until_done(store, identity, query_id, limit=20):
    clock = Clock()
    runner = lambda s, c, k, **kw: live.advance_job(s, c, k, clock=clock, **kw)
    for _ in range(limit):
        data = live.process_consultation(store, identity, query_id, KEY, runner)
        if data['status'] not in {'NA_FILA', 'EM_PROCESSAMENTO'}:
            return data
    raise AssertionError('Pesquisa não terminou dentro do limite de etapas')


def start(web):
    identity = user(web)
    return identity, web[1].consult(identity, NEW, uuid.uuid4().hex, True, True)


def test_official_site_reference_and_social_are_separated(live_web, monkeypatch):
    calls = providers(monkeypatch,
        ['https://www.acmeparafusos.com.br/', f'https://cnpj.biz/{NEW}', 'https://www.facebook.com/acmeparafusos',
         'https://portal-noticias.com.br/economia/industria-local'],
        {'https://www.acmeparafusos.com.br/robots.txt': Page('https://www.acmeparafusos.com.br/robots.txt', 200, {}, 'User-agent: *\nAllow: /'),
         'https://www.acmeparafusos.com.br/': Page('https://www.acmeparafusos.com.br/', 200, {}, OFFICIAL_HTML)})
    identity, query = start(live_web)
    final = run_until_done(live_web[1], identity, query['id'])
    result = final['resultado']
    assert final['status'] == 'CONCLUIDA' and final['saldo']['consumidos'] == 1 and final['saldo']['reservados'] == 0
    assert result['empresa'] == dict(cnpj=NEW, razao_social=REGISTRY['razao_social'], nome_fantasia='ACME PARAFUSOS',
        municipio='BLUMENAU', uf='SC', situacao_cadastral='ATIVA', pesquisado_em=result['empresa']['pesquisado_em'])
    assert result['website_oficial']['url'] == 'https://www.acmeparafusos.com.br/'
    assert result['website_oficial']['tipo'] == 'CONFIRMADO_AUTOMATICAMENTE' and result['status'] == 'CONCLUIDO'
    assert [r['url'] for r in result['referencias']] == [f'https://cnpj.biz/{NEW}']
    assert result['referencias'][0]['tipo'] == 'DIRETORIO_EMPRESARIAL'
    assert [s['url'] for s in result['redes_sociais']] == ['https://www.facebook.com/acmeparafusos']
    # Notícia sem marca no domínio não vira possível site; diretório nunca vira oficial.
    assert result['possiveis_sites'] == []
    assert [f['tipo'] for f in result['fontes']] == ['CADASTRO', 'DESCOBERTA', 'VALIDACAO']
    assert [c[0] for c in calls] == ['minhareceita', 'serper', 'website', 'website']
    with live_web[1].engine.connect() as db:
        saved = json.loads(db.execute(select(companies.c.payload).where(companies.c.cnpj == NEW)).scalar_one())
        assert saved['website_oficial']['url'] == 'https://www.acmeparafusos.com.br/'
        assert db.execute(select(func.count()).select_from(external_requests)).scalar_one() == 4
    # Segunda leitura reaproveita o armazenado como histórico, sem nova chamada.
    again = live_web[1].consult(identity, NEW, uuid.uuid4().hex, True, True)
    assert again['resultado']['origem_resultado'] == 'HISTORICO' and len(calls) == 4


def test_first_site_failure_tries_next_candidate(live_web, monkeypatch):
    second = 'https://acme-parafusos.ind.br/'
    providers(monkeypatch, ['https://www.acmeparafusos.com.br/', second],
        {second + 'robots.txt': Page(second + 'robots.txt', 404, {}, ''),
         second: Page(second, 200, {}, OFFICIAL_HTML)})
    identity, query = start(live_web)
    result = run_until_done(live_web[1], identity, query['id'])['resultado']
    assert result['website_oficial']['url'] == second
    pending = result['possiveis_sites']
    assert [p['url'] for p in pending] == ['https://www.acmeparafusos.com.br/']
    assert pending[0]['ressalvas'] == ['Validação não concluída: o site não respondeu ou recusou o acesso.']


def test_unreachable_sites_still_conclude_with_pending_candidate(live_web, monkeypatch):
    providers(monkeypatch, ['https://www.acmeparafusos.com.br/', f'https://cnpj.biz/{NEW}'], {})
    identity, query = start(live_web)
    final = run_until_done(live_web[1], identity, query['id'])
    result = final['resultado']
    # Cadastro e busca concluídos: resultado entregue e salvo, falha visível.
    assert final['status'] == 'CONCLUIDA' and final['saldo']['consumidos'] == 1
    assert result['website_oficial'] is None and result['status'] == 'FALHA_VALIDACAO_SITE_OFICIAL'
    assert result['possiveis_sites'][0]['url'] == 'https://www.acmeparafusos.com.br/'
    assert 'não respondeu' in result['possiveis_sites'][0]['ressalvas'][0]
    assert len(result['referencias']) == 1


def test_unconfirmed_page_is_candidate_never_official(live_web, monkeypatch):
    base = 'https://www.acmeparafusos.com.br/'
    providers(monkeypatch, [base],
        {base + 'robots.txt': Page(base + 'robots.txt', 200, {}, ''),
         base: Page(base, 200, {}, CANDIDATE_HTML),
         base + 'sobre': Page(base + 'sobre', 200, {}, CANDIDATE_HTML),
         base + 'contato': Page(base + 'contato', 404, {}, '')})
    identity, query = start(live_web)
    result = run_until_done(live_web[1], identity, query['id'])['resultado']
    assert result['website_oficial'] is None
    assert result['possiveis_sites'][0]['url'] == base and result['possiveis_sites'][0]['tipo'] == 'POSSIVEL_SITE'
    assert result['status'] == 'PENDENTE_VALIDACAO_SITE_OFICIAL'


def test_only_directories_found_is_a_concluded_empty_search(live_web, monkeypatch):
    calls = providers(monkeypatch, [f'https://cnpj.biz/{NEW}', 'https://www.econodata.com.br/empresas/sc/acme'], {})
    identity, query = start(live_web)
    final = run_until_done(live_web[1], identity, query['id'])
    result = final['resultado']
    assert final['status'] == 'CONCLUIDA' and result['website_oficial'] is None and result['possiveis_sites'] == []
    assert [r['url'] for r in result['referencias']] == [f'https://cnpj.biz/{NEW}']
    assert not [c for c in calls if c[0] == 'website']


def test_registry_not_found_fails_without_charge_and_keeps_reason(live_web, monkeypatch):
    async def missing(*args, **kwargs):
        raise ProviderError('CNPJ_NAO_ENCONTRADO')
    monkeypatch.setattr(live_sources, 'provider_json', missing)
    identity, query = start(live_web)
    final = run_until_done(live_web[1], identity, query['id'])
    assert final['status'] == 'FALHOU' and final['erro'] == 'CNPJ_NAO_ENCONTRADO'
    assert final['saldo']['consumidos'] == 0 and final['saldo']['reservados'] == 0
    reopened = live_web[0].get('/api/consultas/' + query['id']).json()
    assert reopened['erro'] == 'CNPJ_NAO_ENCONTRADO' and reopened['resultado'] is None


def test_exhausted_website_quota_concludes_without_losing_paid_search(live_web, monkeypatch):
    calls = providers(monkeypatch, ['https://www.acmeparafusos.com.br/', f'https://cnpj.biz/{NEW}'], {})
    store = live_web[1]
    with store.transaction() as db:
        policy = json.loads(db.execute(select(external_control.c.policy)).scalar_one())
    policy['websites']['max_requests'] = 1
    with store.transaction() as db:
        db.execute(external_requests.insert().values(id='old-web', provider='websites', started_at=time.time() - 10,
                                                     outcome='SUCESSO', operation='old-web'))
    live.configure(store, policy, key=KEY)
    identity, query = start(live_web)
    final = run_until_done(store, identity, query['id'])
    assert final['status'] == 'CONCLUIDA' and final['saldo']['consumidos'] == 1
    assert final['resultado']['status'] == 'FALHA_VALIDACAO_SITE_OFICIAL'
    assert 'limite de acessos' in final['resultado']['possiveis_sites'][0]['ressalvas'][0]
    assert [c[0] for c in calls] == ['minhareceita', 'serper']
    with store.engine.connect() as db:
        assert db.execute(select(func.count()).select_from(external_requests)
                          .where(external_requests.c.provider == 'websites')).scalar_one() == 1


def test_robots_4xx_means_no_robots_and_http_status_is_recorded(live_web, monkeypatch):
    # RFC 9309: robots.txt 403 não proíbe; o bloqueio real aparece na página (HTTP 403).
    base = 'https://www.acmeparafusos.com.br/'
    calls = providers(monkeypatch, [base], {base + 'robots.txt': Page(base + 'robots.txt', 403, {}, ''),
                                            base: Page(base, 403, {}, '')})
    identity, query = start(live_web)
    result = run_until_done(live_web[1], identity, query['id'])['resultado']
    assert [c[1] for c in calls if c[0] == 'website'] == [base + 'robots.txt', base]
    assert result['possiveis_sites'][0]['ressalvas'] == ['Validação não concluída: o site não respondeu ou recusou o acesso (HTTP 403).']
    with live_web[1].engine.connect() as db:
        state = json.loads(db.execute(select(live_jobs.c.state)).scalar_one())
    assert state['site_failures'] == [dict(url=base, error='SITE_INACESSIVEL', http_status=403)]
    assert state['robots_status'] == {'https://www.acmeparafusos.com.br': 403}


def test_robots_5xx_stops_site_with_status(monkeypatch):
    base = 'https://www.acmeparafusos.com.br/'
    calls = []

    async def fetch_one(url):
        calls.append(url)
        return Page(url, 500, {}, '')
    monkeypatch.setattr(live_sources, 'fetch_one', fetch_one)
    company = Company(NEW, legal_name=REGISTRY['razao_social'], trade_name='ACME PARAFUSOS')
    state = dict(phase='WEBSITE', company=company.__dict__, queue=[base], sites=[], current_site=base, robots={}, created_at=time.time())
    final = asyncio.run(live_sources.execute_action(NEW, state, KEY))
    assert calls == [base + 'robots.txt'] and final['phase'] == 'DONE'
    assert final['site_failures'] == [dict(url=base, error='ROBOTS_INDISPONIVEL', http_status=500)]


def test_unbranded_third_party_pages_are_not_possible_sites(live_web, monkeypatch):
    # Caso real do piloto WEG: página de associação de outra pessoa jurídica e
    # portal de vagas. Só domínio ou subdomínio com a marca vira possível site.
    providers(monkeypatch, ['https://www.acmeparafusos.com.br/', 'https://acme.gupy.io/',
                            'https://www.iecee.org/members/member-bodies/acme-drives-controls-ltda'], {})
    identity, query = start(live_web)
    result = run_until_done(live_web[1], identity, query['id'])['resultado']
    assert [p['url'] for p in result['possiveis_sites']] == ['https://www.acmeparafusos.com.br/', 'https://acme.gupy.io/']
    assert result['status'] == 'FALHA_VALIDACAO_SITE_OFICIAL'


def test_only_unbranded_failures_mean_no_official_candidate(live_web, monkeypatch):
    providers(monkeypatch, ['https://www.iecee.org/members/member-bodies/acme-drives-controls-ltda'], {})
    identity, query = start(live_web)
    final = run_until_done(live_web[1], identity, query['id'])
    assert final['status'] == 'CONCLUIDA'
    assert final['resultado']['possiveis_sites'] == [] and final['resultado']['status'] == 'SEM_SITE_OFICIAL_CONFIRMADO'


def test_revalidate_sites_reuses_candidates_without_paid_calls(live_web, monkeypatch, tmp_path):
    from app.web.auth import create_user
    from app.web.store import users
    base = 'https://www.acmeparafusos.com.br/'
    pages = {}
    calls = providers(monkeypatch, [base, f'https://cnpj.biz/{NEW}'], pages)
    store = live_web[1]
    identity, query = start(live_web)
    first = run_until_done(store, identity, query['id'])
    assert first['resultado']['website_oficial'] is None
    with pytest.raises(ValueError):
        live.restart_websites(store, generated_cnpj(5))
    # Uma organização de QA nova, sem o resultado anterior em cache.
    manage.create_organization(store, 'qa-revalidacao', 1)
    create_user(store, 'qa.revalidacao', 'Senha-fixture-123', organization='qa-revalidacao')
    with store.transaction() as db:
        policy = json.loads(db.execute(select(external_control.c.policy)).scalar_one())
    live.configure(store, dict(policy, allowed_organizations=['qa-revalidacao']), key=KEY)
    restarted = live.restart_websites(store, NEW)
    assert restarted == dict(sites=[base], rodadas_anteriores=1)
    pages.update({base + 'robots.txt': Page(base + 'robots.txt', 404, {}, ''), base: Page(base, 200, {}, OFFICIAL_HTML)})
    with store.engine.connect() as db:
        qa = dict(db.execute(select(users).where(users.c.username == 'qa.revalidacao')).mappings().one())
    second = store.consult(qa, NEW, uuid.uuid4().hex, True, True)
    final = run_until_done(store, qa, second['id'])
    assert final['resultado']['website_oficial']['url'] == base and final['saldo']['consumidos'] == 1
    assert [c[0] for c in calls].count('serper') == 1 and [c[0] for c in calls].count('minhareceita') == 1
    with store.engine.connect() as db:
        state = json.loads(db.execute(select(live_jobs.c.state)).scalar_one())
        providers_used = [r[0] for r in db.execute(select(external_requests.c.provider))]
    assert len(state['website_runs']) == 1 and state['website_runs'][0]['site_type'] == 'VALIDATION_ERROR'
    assert providers_used.count('serper') == 1 and providers_used.count('minhareceita') == 1
    # A primeira organização mantém o resultado da primeira rodada, sem exclusão.
    with store.engine.connect() as db:
        assert db.execute(select(func.count()).select_from(companies).where(companies.c.cnpj == NEW)).scalar_one() == 2
    monkeypatch.setenv('WEB_DATABASE_URL', live_web[3].database_url)
    assert manage.main(['--env-file', str(tmp_path / 'absent.env'), 'revalidate-sites', NEW]) == 2


def test_website_budget_is_bounded(monkeypatch):
    calls = []

    async def fetch_one(url):
        calls.append(url)
        if url.endswith('robots.txt'):
            return Page(url, 200, {}, '')
        return Page(url, 302, {'location': url.rstrip('/') + f'/p{len(calls)}'}, '')
    monkeypatch.setattr(live_sources, 'fetch_one', fetch_one)
    company = Company(NEW, legal_name=REGISTRY['razao_social'], trade_name='ACME PARAFUSOS', city='BLUMENAU', state='SC')
    sites = ['https://acmeparafusos.com.br/', 'https://acme-parafusos.ind.br/', 'https://acmefix.com/']
    state = dict(phase='WEBSITE', company=company.__dict__, queue=sites[:1], sites=sites[1:], current_site=sites[0],
                 robots={}, created_at=time.time(), candidates=sites)
    for _ in range(30):
        if state['phase'] == 'DONE':
            break
        state = asyncio.run(live_sources.execute_action(NEW, state, KEY))
    assert state['phase'] == 'DONE' and len(calls) <= live_sources.MAX_WEB_CALLS == state['web_calls']
    assert {f['error'] for f in state['site_failures']} <= {'LOOP_REDIRECIONAMENTO', 'LIMITE_VALIDACAO'}
    assert state['validation']['site_type'] == 'VALIDATION_ERROR'


def test_temporary_limit_pauses_then_resumes_with_bounded_retries(live_web):
    store = live_web[1]
    clock = Clock()
    attempts = []

    async def flaky(cnpj, state, key):
        state = dict(state)
        if state['phase'] == 'CADASTRO':
            state.update(company=Company(cnpj, legal_name='ACME LTDA').__dict__, phase='BUSCA')
            return state
        attempts.append(1)
        if len(attempts) == 1:
            raise ProviderError('LIMITE_TEMPORARIO', details={'retry_after': 120})
        state.update(phase='DONE', candidates=[])
        return state

    run = lambda: live.advance_job(store, NEW, KEY, executor=flaky, clock=clock, organization=ORG)
    assert run()['status'] == 'READY'
    paused = run()
    assert paused['status'] == 'WAIT' and paused['retry_after'] >= 120
    assert run()['status'] == 'WAIT' and len(attempts) == 1  # cooldown respeitado, sem reenvio
    clock.now += 600
    assert run()['status'] == 'DONE' and len(attempts) == 2

    other = generated_cnpj(424243)

    async def always_limited(cnpj, state, key):
        if state['phase'] == 'CADASTRO':
            return dict(state, company=Company(cnpj, legal_name='ACME LTDA').__dict__, phase='BUSCA')
        raise ProviderError('LIMITE_TEMPORARIO', details={'retry_after': 60})
    statuses = []
    for _ in range(12):
        clock.now += 600
        statuses.append(live.advance_job(store, other, KEY, executor=always_limited, clock=clock, organization=ORG)['status'])
        if statuses[-1] == 'FAILED':
            break
    assert statuses.count('WAIT') == live.MAX_TEMPORARY_RETRIES and statuses[-1] == 'FAILED'


def test_release_job_requires_failed_or_uncertain_and_new_operation(live_web):
    store = live_web[1]
    now = time.time()
    with store.transaction() as db:
        db.execute(live_jobs.insert().values(cnpj=NEW, id='job-1', status='UNCERTAIN', step=3, lease_until=0,
            state=json.dumps({'phase': 'BUSCA', 'created_at': now, 'temporary_retries': 2}), error='ETAPA_INTERROMPIDA', updated_at=now))
    released = live.release_job(store, NEW)
    assert released == dict(anterior='UNCERTAIN', erro='ETAPA_INTERROMPIDA', fase='BUSCA')
    with store.engine.connect() as db:
        job = db.execute(select(live_jobs)).mappings().one()
    assert job['status'] == 'READY' and job['step'] == 4 and job['error'] is None
    assert 'temporary_retries' not in json.loads(job['state'])
    with pytest.raises(ValueError):
        live.release_job(store, NEW)  # READY não é liberável
    with pytest.raises(ValueError):
        live.release_job(store, generated_cnpj(1))


def test_balance_validity_is_configurable_with_safe_cap(policy):
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    assert policy_reason(dict(policy, balance_verified_at=old), search=True)
    assert policy_reason(dict(policy, balance_verified_at=old, balance_valid_hours=168), search=True) == ''
    for invalid in (0, 721, '168', 1.5, True):
        assert policy_reason(dict(policy, balance_valid_hours=invalid), search=True)


def test_admin_commands_never_reset_balances_and_hide_key(live_web, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv('WEB_DATABASE_URL', live_web[3].database_url)
    monkeypatch.setenv('SERPER_API_KEY', KEY)
    env = ['--env-file', str(tmp_path / 'absent.env')]
    assert manage.main(env + ['create-org', 'qa-serper-weg', '--tokens', '1']) == 0
    assert manage.main(env + ['create-org', ORG, '--tokens', '5']) == 0
    assert 'preservado' in capsys.readouterr().out
    with live_web[1].engine.connect() as db:
        rows = dict(db.execute(select(organizations.c.id, organizations.c.initial_tokens)).all())
    assert rows == {ORG: 2469, 'qa-serper-weg': 1}
    assert manage.main(env + ['external-status']) == 0
    status = capsys.readouterr().out
    assert 'key_sha256' not in status and KEY not in status and '"politica"' in status
    assert manage.main(env + ['retry-job', NEW]) == 2  # exige --confirmed-reviewed
    assert manage.main(env + ['block-external']) == 0
    with pytest.raises(Exception, match='INTEGRACAO_NAO_VERIFICADA'):
        live.ensure_ready(live_web[1], KEY, ORG)
    with live_web[1].engine.connect() as db:
        assert json.loads(db.execute(select(external_control.c.policy)).scalar_one())['blocked'] is True


def test_batch_shows_partial_results_with_operational_errors(live_web, monkeypatch):
    from app.web import batches
    providers(monkeypatch, ['https://www.acmeparafusos.com.br/'],
        {'https://www.acmeparafusos.com.br/robots.txt': Page('https://www.acmeparafusos.com.br/robots.txt', 200, {}, ''),
         'https://www.acmeparafusos.com.br/': Page('https://www.acmeparafusos.com.br/', 200, {}, OFFICIAL_HTML)})
    missing = generated_cnpj(424244)
    original = live_sources.provider_json

    async def registry(method, url, **kwargs):
        if url.endswith(missing):
            raise ProviderError('CNPJ_NAO_ENCONTRADO')
        return await original(method, url, **kwargs)
    monkeypatch.setattr(live_sources, 'provider_json', registry)
    clock = Clock()
    runner = lambda s, c, k, **kw: live.advance_job(s, c, k, clock=clock, **kw)
    process = live.process_consultation
    monkeypatch.setattr(live, 'process_consultation', lambda s, u, i, k: process(s, u, i, k, runner))
    batch = create(live_web, f'{NEW}\n{missing}').json()
    identity = user(live_web)
    for item in batch['itens']:
        for _ in range(10):
            data = batches.step_batch(live_web[1], identity, batch['id'], item['id'], True, KEY)
            if next(i for i in data['itens'] if i['id'] == item['id'])['status'] not in {'PENDENTE_LOTE', 'EM_PROCESSAMENTO'}:
                break
    assert data['progresso'] == dict(total=2, concluidos=1, em_andamento=0, pendentes=0, erros=1, tokens_consumidos=1)
    assert [i['erro'] for i in data['itens']] == [None, 'CNPJ_NAO_ENCONTRADO']
    assert data['saldo']['consumidos'] == 1 and data['saldo']['reservados'] == 0
    exported = live_web[0].get(f"/api/lotes/{batch['id']}/exportar?formato=csv")
    assert exported.status_code == 200 and 'acmeparafusos.com.br' in exported.text
