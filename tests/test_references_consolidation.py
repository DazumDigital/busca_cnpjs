import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from app.consolidate import consolidate
from app.models import Company
from app.review_saved import review
from app.services.importer import ImportResult
from app.services.references import useful_references
from app.services.site_rules import candidate_kind,url_reason
from app.storage.database import Database

FIXTURES=Path(__file__).parent/'fixtures'
BOS=json.loads((FIXTURES/'boschetti_saved_case.json').read_text(encoding='utf-8'))
ROW=BOS['row'];CNPJ=ROW['cnpj'];COMPANY=Company(**json.loads(ROW['company']))
FARMBY=next(u for u in json.loads(ROW['candidates']) if 'farmby' in u)
FACTS=json.loads(ROW['validation'])['page_evidence']


@pytest.mark.parametrize('url',[FARMBY,'https://outralistagem.com/empresas/sc/jabora/veterinarias/agropecuaria-boschetti',
 'https://www.misterwhat.com.br/company/66684-agropecuaria-boschetti-ltda-jabora',
 'https://www.cylex.com.br/jabora/agropecuaria-boschetti-ltda-me-11380973.html'])
def test_references_are_allowed_while_company_catalogs_are_not_official_sites(url):
    assert url_reason(url)
    refs=useful_references(COMPANY,[url],FACTS)
    assert len(refs)==1 and not refs[0]['official_website']
    assert refs[0]['confidence'] in {'MEDIA','BAIXA'}


def test_farmby_is_preserved_with_locality_discrepancy_and_no_claim_of_exact_cnpj():
    ref=useful_references(COMPANY,[FARMBY],FACTS)[0]
    assert ref['source_type']=='DIRETORIO_EMPRESARIAL'
    assert ref['identity_match']=='NOME_E_MUNICIPIO' and ref['confidence']=='BAIXA'
    assert ref['alerts'] and ref['needs_review'] and not ref['current_access_checked']


@pytest.mark.parametrize('url',[
 'https://cnpjspot.com.br/estado/sc/municipio/jabora',
 'https://parafa.com.br/jabora/farmacias-veterinarias',
 'https://outroportal.com/busca/agropecuaria-boschetti-jabora',
 'https://cnpj.biz/00000000000191',
 'https://homonimo.com/agropecuaria-boschetti/',
 'https://outraempresa.com/empresas/sc/outracidade/agropecuaria-boschetti',
 'https://diretorio.com/?cnpj=00068581000101',
 'http://127.0.0.1/00068581000101'])
def test_generic_homonymous_wrong_cnpj_and_unsafe_links_are_not_returned(url):
    assert useful_references(COMPANY,[url])==[]


def test_exact_cnpj_reference_does_not_become_official_or_require_a_download():
    ref=useful_references(COMPANY,['https://cnpj.biz/'+CNPJ])[0]
    assert ref['identity_match']=='CNPJ_EXATO' and ref['confidence']=='MEDIA'
    assert not ref['official_website'] and not ref['current_access_checked']


def test_collective_agreement_can_be_reference_without_being_corporate_website():
    fixture=json.loads((FIXTURES/'lote02_saved_cases.json').read_text(encoding='utf-8'))
    company=Company(**json.loads(fixture['cases'][1]['company']))
    bond=next(v for v in fixture['validations'] if 'bond.com.br' in v['website'])
    refs=useful_references(company,[bond['website']],bond['page_evidence'])
    assert refs[0]['source_type']=='DOCUMENTO_INSTITUCIONAL_TERCEIRO'
    assert refs[0]['confidence']=='ALTA' and not refs[0]['official_website']


def test_boschetti_review_retains_useful_reference_and_original_history(db,tmp_path,monkeypatch):
    db.ingest(ImportResult([(CNPJ,CNPJ)],[],{}))
    db.update(CNPJ,company=json.loads(ROW['company']),validation=json.loads(ROW['validation']),
        candidates=json.loads(ROW['candidates']),status=ROW['status'],confidence=ROW['confidence'],error=ROW['error'])
    db.set_state('discovery_audit:'+CNPJ,BOS['audit'])
    db.cache_put('validation','farmby-fixture',dict(json.loads(ROW['validation']),website=FARMBY),3600)
    before=dict(db.row(CNPJ))
    monkeypatch.setattr(httpx,'AsyncClient',lambda *a,**k:pytest.fail('Não construir HTTP'))
    result=review(tmp_path/'test.sqlite',tmp_path/'test.sqlite',tmp_path/'review',[CNPJ])['resultados'][0]
    assert result['Status']=='SEM_SITE_OFICIAL_CONFIRMADO' and not result['Website']
    assert FARMBY in [r['url'] for r in result['Referencias_uteis']]
    assert dict(db.row(CNPJ))==before


def manifest_fixture(tmp_path):
    rows=json.loads((FIXTURES/'consolidation20_latest.json').read_text(encoding='utf-8'))
    ids=[r['cnpj'] for r in rows]
    source=tmp_path/'latest.sqlite';db=Database(source)
    db.ingest(ImportResult([(c,c) for c in ids],[],{}))
    for r in rows:
        db.update(r['cnpj'],company=json.loads(r['company']),validation=json.loads(r['validation'] or '{}'),
            candidates=json.loads(r['candidates'] or '[]'),status=r['status'],confidence=r['confidence'],error=r['error'])
    db.cache_put('validation','farmby',dict(json.loads(ROW['validation']),website=FARMBY),3600)
    db.close()
    return source,dict(expected_ids=ids,sources=[dict(database=str(source),ids=ids,label='Revisões explícitas')],
        manual_checks=[dict(cnpj='00055627000140',url='https://thpbrasil.com.br/',origin='RELATO_DO_USUARIO',
                           evidence='Usuário relatou identificação nos termos; não revalidada aqui')])


def test_twenty_consolidation_uses_explicit_sources_and_keeps_manual_separate(tmp_path,monkeypatch):
    source,manifest=manifest_fixture(tmp_path)
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(httpx,'AsyncClient',lambda *a,**k:pytest.fail('Não construir HTTP'))
    result=consolidate(manifest,source,tmp_path/'official')
    assert result['total']==20 and sum(result['categories'].values())==20
    data=json.loads((tmp_path/'official/consolidacao20.json').read_text(encoding='utf-8'))
    rows={r['cnpj']:r for r in data['companies']}
    assert rows['00055627000140']['category']=='VERIFICACAO_MANUAL_INFORMADA'
    assert not rows['00055627000140']['official_website'] and rows['00055627000140']['manual_website']
    assert rows['00065589000106']['category']=='CONFIRMACAO_AUTOMATICA'
    assert rows[CNPJ]['category']=='SEM_SITE_OFICIAL_CONFIRMADO'
    assert FARMBY in [r['url'] for r in data['references']]
    with sqlite3.connect(tmp_path/'official/consolidacao20.sqlite') as db:
        assert db.execute('SELECT count(*) FROM consolidated_companies').fetchone()[0]==20
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before


def test_consolidation_rejects_duplicate_or_missing_coverage_before_output(tmp_path):
    source,manifest=manifest_fixture(tmp_path)
    bad=copy.deepcopy(manifest);bad['sources'].append(bad['sources'][0])
    with pytest.raises(ValueError,match='duas fontes'):consolidate(bad,source,tmp_path/'bad')
    assert not (tmp_path/'bad').exists()
    bad=copy.deepcopy(manifest);bad['sources'][0]['ids']=bad['sources'][0]['ids'][:-1]
    with pytest.raises(ValueError,match='Cobertura'):consolidate(bad,source,tmp_path/'bad')
