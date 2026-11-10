"""Qualidade da identificação de sites a partir dos 5 casos reais da etapa 1a
(10/11/2026). Os links são os que a Serper devolveu na época, guardados no Neon
(fixture etapa1a_serper_links.json). Cadastro, páginas e DNS são simulados:
nenhuma chamada real à Serper, à Minha Receita ou a sites."""
import json
import time
import uuid
from pathlib import Path

import pytest

from conftest import generated_cnpj
from test_web_platform import web, source  # noqa: F401 (fixtures)
from test_web_live_sites import live_web, run_until_done, KEY  # noqa: F401
from test_web_batches import user
from app.models import Company, ProviderError
from app.services.validator import Page, connection_category
from app.web import live, live_sources
from app.web.live_sources import (web_queries, clean_name, domain_guesses, display_root, social_profile, root_site,
                                  candidate_sites, platform_site, site_reason)
from app.web.store import external_control
from sqlalchemy import select

CASES = {c['company']['cnpj']: c for c in json.loads(
    (Path(__file__).parent / 'fixtures' / 'etapa1a_serper_links.json').read_text(encoding='utf-8'))}
APPLE, ANGELONI, GIASSI, FG, MELAU = '00623904000173', '83646984000100', '83648477001691', '04743028000170', '66136840000194'
fmt = lambda c: f'{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}'


def page(url, title, footer):
    return Page(url, 200, {}, f'<html><head><title>{title}</title></head><body><h1>{title}</h1>'
                              f'<p>Bem-vindo.</p><footer>{footer}</footer></body></html>')


def replay(live_web, monkeypatch, cnpj, pages, dns=()):
    """Pesquisa completa com os links reais guardados e fontes simuladas."""
    case = CASES[cnpj]
    company = case['company']
    calls = []

    async def provider_json(method, url, *, key='', payload=None):
        calls.append(('serper' if 'serper' in url else 'minhareceita', payload['q'] if payload else url))
        if 'minhareceita' in url:
            return dict(cnpj=company['cnpj'], razao_social=company['legal_name'], nome_fantasia=company['trade_name'],
                        municipio=company['city'], uf=company['state'], descricao_situacao_cadastral=company['registration_status'])
        return {'organic': [{'link': link} for link in case['candidates']]}

    async def fetch_one(url, robots=False):
        calls.append(('website', url))
        result = pages.get(url)
        if isinstance(result, Exception):
            raise result
        if result is None:
            return Page(url, 404, {}, '') if url.endswith('/robots.txt') else Page(url, 404, {}, '')
        return result
    monkeypatch.setattr(live_sources, 'provider_json', provider_json)
    monkeypatch.setattr(live_sources, 'fetch_one', fetch_one)
    monkeypatch.setattr(live_sources, 'domain_exists', lambda host: host in set(dns))
    identity = user(live_web)
    query = live_web[1].consult(identity, cnpj, uuid.uuid4().hex, True, True)
    final = run_until_done(live_web[1], identity, query['id'], limit=40)
    return final, calls


def websites(calls):
    return [url for kind, url in calls if kind == 'website']


def test_queries_drop_articles_and_corporate_words():
    company = lambda cnpj: Company(**CASES[cnpj]['company'])
    assert clean_name('A MELAU PET') == 'MELAU PET' and clean_name('A. ANGELONI & CIA. LTDA') == 'ANGELONI'
    assert clean_name('GIASSI & CIA LTDA') == 'GIASSI' and clean_name('WEG S/A') == 'WEG' and clean_name('WEG S.A.') == 'WEG'
    assert web_queries(company(MELAU))[0].startswith('"MELAU PET" OR "MELAU PET CAETANO" JOINVILLE SC -site:')
    assert web_queries(company(ANGELONI))[0].startswith('"SUPERMERCADOS ANGELONI" OR "ANGELONI" CRICIUMA SC -site:')
    assert web_queries(company(APPLE))[0].startswith('"APPLE COMPUTER BRASIL" SAO PAULO SP -site:')
    assert 'site oficial' not in web_queries(company(FG))[0]
    assert web_queries(company(MELAU))[1].startswith('MELAU PET CAETANO JOINVILLE SC contato')
    assert domain_guesses(company(MELAU)) == ['melaupetcaetano', 'melaupet']
    assert domain_guesses(company(FG)) == ['fgprimeempreendimentos', 'fgprime']


def test_platforms_display_roots_and_profiles():
    for url in ('https://www.waze.com/live-map/x', 'https://supermercadosangeloni.pandape.com.br/', 'https://maps.apple.com/place',
                'https://www.abinee.org.br/associada/cod/3044/', 'https://leis.org/0vueg', 'https://weg.gupy.io/'):
        assert platform_site(url)
    assert not platform_site('https://sites.google.com/view/empresa') and not platform_site('https://www.angeloni.com.br/')
    assert display_root('https://institucional.giassi.com.br/noticias/page:56') == 'https://institucional.giassi.com.br/'
    assert display_root('https://www.apple.com/br/iphone/') == 'https://www.apple.com/br/'
    assert social_profile('https://www.facebook.com/giassisupermercados/posts/abc/123/') == 'https://www.facebook.com/giassisupermercados/'
    assert social_profile('https://www.instagram.com/marca/') == 'https://www.instagram.com/marca/'


def test_root_site_uses_dns_only(monkeypatch):
    monkeypatch.setattr(live_sources, 'domain_exists', lambda host: host == 'angeloni.com.br')
    assert root_site('https://institucional.angeloni.com.br/unidades/') == 'https://angeloni.com.br/'
    assert root_site('https://www.apple.com/br/') is None
    monkeypatch.setattr(live_sources, 'domain_exists', lambda host: False)
    assert root_site('https://institucional.giassi.com.br/x') is None


def test_connection_failures_have_readable_categories():
    import socket
    import ssl
    assert connection_category(TimeoutError()) == 'TEMPO_ESGOTADO'
    assert connection_category(ssl.SSLCertVerificationError()) == 'TLS_CERTIFICADO'
    assert connection_category(ConnectionRefusedError()) == 'CONEXAO_RECUSADA'
    assert connection_category(socket.gaierror()) == 'DNS'
    assert site_reason('SITE_INACESSIVEL', None, 'TLS_CERTIFICADO') == 'o site não respondeu ou recusou o acesso (certificado TLS não verificado)'


def test_apple_same_root_cnpj_confirms_official_and_skips_maps(live_web, monkeypatch):
    other = generated_cnpj(6239040003)  # outro estabelecimento, mesma raiz 00.623.904
    footer = f'Copyright © 2026 Apple Inc. Apple Computer Brasil Ltda. CNPJ {fmt(other)}. Todos os direitos reservados.'
    final, calls = replay(live_web, monkeypatch, APPLE, {'https://www.apple.com/br/': page('https://www.apple.com/br/', 'Apple (Brasil)', footer)})
    result = final['resultado']
    assert result['website_oficial']['url'] == 'https://www.apple.com/br/'
    assert result['website_oficial']['ressalvas'] == [f'Confirmado pelo CNPJ de outro estabelecimento da mesma empresa ({fmt(other)}).']
    assert websites(calls) == ['https://www.apple.com/robots.txt', 'https://www.apple.com/br/']  # Waze e ABINEE fora
    assert not any('waze' in p['url'] or 'abinee' in p['url'] for p in result['possiveis_sites'])
    assert final['token_consumido'] is True


def test_apple_other_company_cnpj_is_still_rejected_and_not_shown_as_unvisited(live_web, monkeypatch):
    footer = 'Revenda autorizada XYZ Comércio Ltda. CNPJ 11.222.333/0001-81'
    final, calls = replay(live_web, monkeypatch, APPLE, {'https://www.apple.com/br/': page('https://www.apple.com/br/', 'Apple (Brasil)', footer)})
    result = final['resultado']
    assert result['website_oficial'] is None and result['possiveis_sites'] == []
    with live_web[1].engine.connect() as db:
        from app.web.store import live_jobs
        state = json.loads(db.execute(select(live_jobs.c.state).where(live_jobs.c.cnpj == APPLE)).scalar_one())
    rejected = state['site_results'][0]
    assert rejected['website'] == 'https://www.apple.com/br/' and rejected['signals']['cnpjs_encontrados'] == ['11222333000181']


def test_angeloni_tries_domain_root_and_skips_job_portal(live_web, monkeypatch):
    footer = f'A. ANGELONI & CIA. LTDA - CNPJ {fmt(ANGELONI)} - Criciúma/SC'
    pages = {'https://institucional.angeloni.com.br/robots.txt': ProviderError('SITE_INACESSIVEL', details={'conexao': 'TLS_CERTIFICADO'}),
             'https://www.angeloni.com.br/': page('https://www.angeloni.com.br/', 'Angeloni | Supermercado', footer)}
    final, calls = replay(live_web, monkeypatch, ANGELONI, pages, dns={'www.angeloni.com.br'})
    result = final['resultado']
    assert result['website_oficial']['url'] == 'https://www.angeloni.com.br/'
    assert websites(calls) == ['https://institucional.angeloni.com.br/robots.txt', 'https://www.angeloni.com.br/robots.txt',
                               'https://www.angeloni.com.br/']
    assert [p['url'] for p in result['possiveis_sites']] == ['https://institucional.angeloni.com.br/']
    assert result['possiveis_sites'][0]['ressalvas'] == ['Validação não concluída: o site não respondeu ou recusou o acesso (certificado TLS não verificado).']
    assert not any('pandape' in p['url'] for p in result['possiveis_sites'])


def test_giassi_branch_shows_headquarters_root_and_facebook_profile(live_web, monkeypatch):
    matriz = generated_cnpj(836484770001)
    assert matriz == '83648477000105'
    footer = f'GIASSI & CIA LTDA - CNPJ {fmt(matriz)} - Joinville/SC'
    url = 'https://institucional.giassi.com.br/noticias/page:56'
    final, calls = replay(live_web, monkeypatch, GIASSI, {url: page(url, 'Giassi Supermercados', footer)}, dns={'www.giassi.com.br'})
    result = final['resultado']
    assert result['status'] == 'SITE_DA_MATRIZ' and result['website_oficial'] is None
    assert [s['url'] for s in result['site_matriz']] == ['https://institucional.giassi.com.br/']
    assert [s['url'] for s in result['redes_sociais']] == ['https://www.facebook.com/giassisupermercados/']
    assert len(websites(calls)) == 2 and not any('maps.apple' in u or 'indeed' in u for u in websites(calls))


def test_fg_prime_without_a_site_spends_no_website_access(live_web, monkeypatch):
    final, calls = replay(live_web, monkeypatch, FG, {})
    result = final['resultado']
    assert websites(calls) == [] and result['possiveis_sites'] == [] and result['website_oficial'] is None
    assert len(result['referencias']) == 4 and final['token_consumido'] is True


def test_fg_prime_name_domain_with_short_acronym_can_be_confirmed(live_web, monkeypatch):
    footer = f'FG PRIME EMPREENDIMENTOS LTDA - CNPJ {fmt(FG)} - Balneário Camboriú/SC'
    pages = {'https://www.fgprime.com.br/': page('https://www.fgprime.com.br/', 'FG Prime Empreendimentos', footer)}
    final, calls = replay(live_web, monkeypatch, FG, pages, dns={'www.fgprime.com.br'})
    assert final['resultado']['website_oficial']['url'] == 'https://www.fgprime.com.br/'
    assert websites(calls) == ['https://www.fgprime.com.br/robots.txt', 'https://www.fgprime.com.br/']


def test_melau_pet_name_domain_is_found_without_extra_search(live_web, monkeypatch):
    footer = f'A MELAU PET CAETANO LTDA · CNPJ {fmt(MELAU)} · Joinville/SC'
    pages = {'https://www.melaupetcaetano.com.br/': page('https://www.melaupetcaetano.com.br/', 'Melau Pet Caetano', footer)}
    final, calls = replay(live_web, monkeypatch, MELAU, pages, dns={'www.melaupetcaetano.com.br'})
    result = final['resultado']
    assert result['website_oficial']['url'] == 'https://www.melaupetcaetano.com.br/'
    assert [c for c in calls if c[0] == 'serper'][0][1].startswith('"MELAU PET" OR "MELAU PET CAETANO" JOINVILLE SC')
    assert len([c for c in calls if c[0] == 'serper']) == 1
    assert websites(calls) == ['https://www.melaupetcaetano.com.br/robots.txt', 'https://www.melaupetcaetano.com.br/']


def test_guessed_domain_that_fails_is_not_shown(live_web, monkeypatch):
    final, calls = replay(live_web, monkeypatch, MELAU, {}, dns={'www.melaupetcaetano.com.br'})
    assert final['resultado']['possiveis_sites'] == [] and final['resultado']['status'] == 'SEM_SITE_OFICIAL_CONFIRMADO'


def test_second_search_only_when_enabled_and_without_brand_domain(live_web, monkeypatch):
    store = live_web[1]
    final, calls = replay(live_web, monkeypatch, FG, {})
    assert len([c for c in calls if c[0] == 'serper']) == 1  # padrão: 1 busca por CNPJ
    with store.transaction() as db:
        policy = json.loads(db.execute(select(external_control.c.policy)).scalar_one())
    policy['serper']['max_searches_per_cnpj'] = 3
    assert not live.valid_policy({k: v for k, v in policy.items() if k != 'key_sha256'})
    policy['serper']['max_searches_per_cnpj'] = 2
    live.configure(store, {k: v for k, v in policy.items() if k != 'key_sha256'}, key=KEY)
    other = generated_cnpj(424299)
    CASES[other] = dict(CASES[FG], company=dict(CASES[FG]['company'], cnpj=other))
    try:
        final, calls = replay(live_web, monkeypatch, other, {})
    finally:
        CASES.pop(other)
    serper = [q for kind, q in calls if kind == 'serper']
    assert len(serper) == 2 and serper[1].startswith('FG PRIME EMPREENDIMENTOS BALNEARIO CAMBORIU SC contato')
    assert final['saldo']['consumidos'] == 2  # 1 token por pesquisa, mesmo com 2 buscas
