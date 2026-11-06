import asyncio
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest

from app.models import Company, Validation
from app.reclassify import reclassify
from app.services.importer import ImportResult
from app.services.processor import Processor
from app.services.site_rules import RULES_VERSION, extract_cnpjs, rank_candidates, url_reason
from app.services.validator import Page, WebsiteValidator, classify, classify_pages, validation_cache_key
from app.services.website_finder import WebsiteFinder

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/poc10_real_candidates.json').read_text(encoding='utf-8'))
REAL = FIXTURE['companies']
THP = next(r for r in REAL if r['company']['cnpj'] == '00055627000140')


def seed_real(db, rows=REAL):
    ids = [r['company']['cnpj'] for r in rows]
    imported = ImportResult([(c, c) for c in ids], [], dict(importados=len(ids), validos=len(ids),
                            invalidos=0, duplicados=0, fora_limite=0, selecionados=len(ids)))
    db.ingest(imported)
    for item in rows:
        cnpj = item['company']['cnpj']
        db.update(cnpj, company=item['company'], validation=item['previous'], confidence=item['previous_confidence'],
                  status='CONCLUIDO', candidates=item['candidates'], source='PLANILHA_LOCAL;serper;website_publico', attempts=1)
    return ids


@pytest.mark.parametrize('item', REAL, ids=lambda r: r['company']['cnpj'])
def test_every_real_selected_third_party_is_rejected(item):
    company = Company(**item['company'])
    url = item['previous']['website']
    # HTML SINTÉTICO adversarial: não é cópia da página real. Mesmo incluindo
    # marca e CNPJ em heading/footer, um diretório não pode receber ALTA.
    html = f'<h1>{company.legal_name}</h1><footer>{company.legal_name} {company.cnpj}</footer>'
    result = classify(company, Page(url, 200, {}, html))
    assert not result.confirmed and not result.website
    assert result.confidence == 'NAO_ENCONTRADO'
    assert url_reason(url, company.cnpj)


def test_thp_ranked_first_and_not_replaced_by_opencnpj(db, settings):
    seed_real(db, [THP])
    company = Company(**THP['company'])
    assert 'thpbrasil.com.br' in rank_candidates(company, THP['candidates'])[0]
    calls = []

    class Fetcher:
        async def fetch(self, url):
            calls.append(url)
            assert 'thpbrasil.com.br' in url
            # Evidência simulada para testar o requisito; não é HTML baixado.
            return Page(url, 200, {}, f'<h1>THP Brasil</h1><footer>{company.legal_name} CNPJ {company.cnpj}</footer>')

    finder = WebsiteFinder(None, WebsiteValidator(Fetcher(), db, settings), db, settings)
    result, _, _ = asyncio.run(finder.find(company, list(reversed(THP['candidates']))))
    assert result.confirmed
    assert result.domain == 'thpbrasil.com.br'
    assert len(calls) == 1
    assert result.signals['legal_owner'] is True


def test_thp_cnpj_on_linked_legal_page_confirms_with_other_ownership_signals(db, settings):
    company = Company(**THP['company'])
    calls = []

    class Fetcher:
        async def fetch(self, url):
            calls.append(url)
            if url.endswith('/termos/'):
                return Page(url, 200, {}, f'<h1>Termos de uso</h1>Este site é operado por {company.legal_name}, inscrita no CNPJ 00.055.627/0001-40.')
            return Page(url, 200, {}, '<h1>THP Brasil</h1><a href="/termos/">Termos</a><a href="/contato/">Contato</a>')

    result = asyncio.run(WebsiteValidator(Fetcher(), db, settings).validate(company, 'https://thpbrasil.com.br/sobre/'))
    assert result.confirmed and result.domain == 'thpbrasil.com.br'
    assert calls == ['https://thpbrasil.com.br/sobre/', 'https://thpbrasil.com.br/termos/']
    assert result.rules_version == RULES_VERSION


def test_name_and_cnpj_mentions_alone_never_prove_ownership():
    company = Company(**THP['company'])
    result = classify(company, Page('https://thpbrasil.com.br/sobre/', 200, {}, f'{company.legal_name} {company.cnpj}'))
    assert not result.confirmed
    assert result.confidence != 'ALTA'
    assert result.signals['legal_owner'] is False


def test_unknown_directory_content_is_rejected_despite_exact_identity():
    company = Company(**THP['company'])
    result = classify(company, Page('https://outroportal.com/', 200, {},
                      f'<h1>{company.legal_name}</h1>{company.cnpj} Dados da Receita Federal; outras empresas; quadro societário.'))
    assert result.site_type == 'THIRD_PARTY'
    assert not result.website and not result.confirmed


@pytest.mark.parametrize('url', ['https://novoportal.com/consulta-cnpj/00055627000140',
                                'https://novoportal.com/empresa/thp-00055627000140',
                                'https://novoportal.com/associado/thp/',
                                'https://novoportal.com/documento.pdf',
                                'https://sub.opencnpj.com/00055627000140'])
def test_extensible_url_patterns_and_subdomains_reject_third_parties(url):
    assert url_reason(url, '00055627000140')


def test_filial_cannot_be_selected_for_matriz_even_when_name_matches():
    company = Company('00058645000185', 'LANCHONETE SICHA & GUICA LTDA')
    foreign = '00058645000266'
    assert extract_cnpjs(foreign) == {foreign}
    assert url_reason('https://sicha.com/' + foreign, company.cnpj) == 'CNPJ_DIVERGENTE_URL'
    result = classify(company, Page('https://sicha.com/', 200, {}, f'<h1>{company.legal_name}</h1><footer>{company.legal_name} {foreign}</footer>'))
    assert not result.confirmed and not result.website
    assert result.site_type == 'CNPJ_DIVERGENTE'


def test_embedded_or_partial_cnpj_and_repeated_digits_do_not_count():
    assert extract_cnpjs('1000556270001409') == set()
    assert extract_cnpjs('00055627') == set()
    assert extract_cnpjs('00000000000000') == set()
    assert extract_cnpjs('00.055.627/0001-40') == {'00055627000140'}


def test_cnpj_in_body_but_different_owner_in_footer_cannot_confirm():
    company = Company(**THP['company'])
    result = classify(company, Page('https://thpbrasil.com.br/', 200, {},
                      f'<h1>THP Brasil</h1>{company.legal_name} {company.cnpj}<footer>Outra empresa 00.000.000/0001-91</footer>'))
    assert not result.confirmed
    assert not result.signals['legal_owner']


def test_opaque_domain_and_corporate_navigation_do_not_bypass_brand_requirement():
    company = Company(**THP['company'])
    result = classify(company, Page('https://portaldesconhecido.com/', 200, {},
                      f'<h1>THP</h1><a href="/sobre/">Sobre</a><a href="/contato/">Contato</a><footer>{company.legal_name} {company.cnpj}</footer>'))
    assert result.signals['corporate_navigation']
    assert not result.confirmed


def test_brand_in_third_party_subdomain_does_not_prove_ownership():
    company = Company(**THP['company'])
    result = classify(company, Page('https://thp.portaldesconhecido.com/', 200, {},
                      f'<h1>THP Brasil</h1><footer>{company.legal_name} {company.cnpj}</footer>'))
    assert not result.signals['brand_domain']
    assert not result.confirmed


def test_legacy_high_cache_is_not_reused_or_deleted(db, settings):
    company = Company(**THP['company'])
    url = 'https://thpbrasil.com.br/sobre/'
    legacy_key = hashlib.sha256(json.dumps([asdict(company), url], sort_keys=True).encode()).hexdigest()
    legacy = dict(THP['previous'], website=url, domain='thpbrasil.com.br')
    db.cache_put('validation', legacy_key, legacy, 3600)
    calls = []

    class Fetcher:
        async def fetch(self, url):
            calls.append(url)
            return Page(url, 200, {}, f'{company.legal_name} {company.cnpj}')

    validator = WebsiteValidator(Fetcher(), db, settings)
    result = asyncio.run(validator.validate(company, url))
    assert len(calls) == 1 and not result.confirmed
    assert db.cache_get('validation', legacy_key)['confidence'] == 'ALTA'
    assert db.cache_get('validation', validation_cache_key(company, url))['rules_version'] == RULES_VERSION


def test_only_directories_do_not_consume_candidate_budget_or_trigger_fetch(db, settings):
    item = REAL[0]
    seed_real(db, [item])
    company = Company(**item['company'])

    class Validator:
        async def validate(self, *args):
            raise AssertionError('Diretórios não podem ser visitados')

    class Enrichment:
        async def get(self, *args):
            raise AssertionError('Não consultar dados empresariais')

    db.update(company.cnpj, status='PENDENTE')
    db.conn.execute('UPDATE companies SET validation=NULL WHERE cnpj=?', (company.cnpj,))
    finder = WebsiteFinder(None, Validator(), db, settings)
    asyncio.run(Processor(db, Enrichment(), finder, settings).run([company.cnpj]))
    assert db.row(company.cnpj)['status'] == 'SEM_SITE_OFICIAL_CONFIRMADO'
    assert not json.loads(db.row(company.cnpj)['validation'])['website']


def test_legal_followups_are_bounded_even_when_all_fail(db, settings):
    company = Company(**THP['company'])
    calls = []

    class Fetcher:
        async def fetch(self, url):
            from app.models import ProviderError
            calls.append(url)
            if len(calls) > 1:
                raise ProviderError('SITE_INACESSIVEL')
            return Page(url, 200, {}, '<h1>THP Brasil</h1>' + ''.join(f'<a href="/termos/{i}">Termos</a>' for i in range(10)))

    asyncio.run(WebsiteValidator(Fetcher(), db, settings).validate(company, 'https://thpbrasil.com.br/'))
    assert len(calls) == 3


def test_offline_reclassification_preserves_original_and_consumption(tmp_path, db, monkeypatch):
    seed_real(db)
    db.conn.execute("INSERT INTO requests(provider,started_at,outcome) VALUES ('serper',1,'SUCESSO')")
    original_rows = [dict(r) for r in db.conn.execute('SELECT * FROM companies ORDER BY position')]
    original_cache = [dict(r) for r in db.conn.execute('SELECT * FROM cache')]

    def forbidden(*args, **kwargs):
        raise AssertionError('Reclassificação não pode criar clientes HTTP')

    monkeypatch.setattr(httpx, 'AsyncClient', forbidden)
    result = reclassify(tmp_path / 'test.sqlite', tmp_path / 'reclassified')
    assert result['requisicoes_novas'] == 0
    assert result['alta_anteriores'] == 5
    assert result['alta_atuais'] == 0
    assert result['status'] == {'SEM_SITE_OFICIAL_CONFIRMADO': 9, 'PENDENTE_VALIDACAO_SITE_OFICIAL': 1}
    thp = next(r for r in result['mudancas'] if r['CNPJ'] == '00055627000140')
    assert 'thpbrasil.com.br' in thp['Candidato_prioritario']
    assert not thp['Website_reclassificado']
    assert [dict(r) for r in db.conn.execute('SELECT * FROM companies ORDER BY position')] == original_rows
    assert [dict(r) for r in db.conn.execute('SELECT * FROM cache')] == original_cache
    from app.storage.database import Database
    copy = Database(tmp_path / 'reclassified/reclassificacao.sqlite')
    assert copy.usage() == db.usage()
    assert copy.conn.execute('SELECT count(*) FROM classification_history').fetchone()[0] == 10
    assert copy.conn.execute('SELECT count(*) FROM candidate_reviews').fetchone()[0] > 50
    copy.close()
    with pytest.raises(ValueError):
        reclassify(tmp_path / 'test.sqlite', tmp_path / 'reclassified')


def test_output_cannot_overwrite_existing_export_folder(tmp_path, db):
    seed_real(db)
    out = tmp_path / 'exports'
    out.mkdir()
    original = out / 'resultado_cnpjs.csv'
    original.write_text('resultado original', encoding='utf-8')
    with pytest.raises(ValueError, match='vazia'):
        reclassify(tmp_path / 'test.sqlite', out)
    assert original.read_text(encoding='utf-8') == 'resultado original'


@pytest.mark.parametrize('pages', [[], [Page('https://thpbrasil.com.br/', 503, {}, '')]])
def test_missing_or_inaccessible_main_page_does_not_confirm(pages):
    assert not classify_pages(Company(**THP['company']), pages).confirmed


def test_legacy_checkpoint_cannot_bypass_new_rules_or_trigger_requests(db, settings):
    seed_real(db)

    class Forbidden:
        async def get(self, *args):
            raise AssertionError('Não consultar APIs')
        async def find(self, *args):
            raise AssertionError('Não pesquisar')

    with pytest.raises(ValueError, match='regras antigas'):
        asyncio.run(Processor(db, Forbidden(), Forbidden(), settings).run([THP['company']['cnpj']]))
    assert db.row(THP['company']['cnpj'])['confidence'] == 'ALTA'  # original intacto, uso bloqueado
    assert db.usage() == {}
