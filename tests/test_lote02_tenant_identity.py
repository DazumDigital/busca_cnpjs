"""Regressões reais do Bloco 2 e cenários sintéticos para a regra geral de tenants."""
import asyncio
import copy
import json
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from openpyxl import load_workbook

from app.models import Company
from app.new_batch import prepare_new
from app.pilot3 import execute_plan
from app.review_saved import from_saved,review
from app.services.importer import ImportResult
from app.services.site_outcome import official_website
from app.services.site_rules import content_directory_reason,url_reason
from app.services.validator import Page,classify_pages

FIXTURE=json.loads((Path(__file__).parent/'fixtures/lote02_saved_cases.json').read_text(encoding='utf-8'))
CASES=FIXTURE['cases'];IDS=[r['cnpj'] for r in CASES]
COMPANY=Company(**json.loads(CASES[0]['company']))
STORE='https://daijucomercio.lojavirtualnuvem.com.br/'
SAVED=next(v for v in FIXTURE['validations'] if v['website']==STORE)
BOND=next(v for v in FIXTURE['validations'] if 'convencoes.bond.com.br' in v['website'])


def seed(db):
    db.ingest(ImportResult([(c,c) for c in IDS],[],{'selecionados':3}))
    for r in CASES:
        db.update(r['cnpj'],company=json.loads(r['company']),validation=json.loads(r['validation']),
                  candidates=json.loads(r['candidates']),confidence=r['confidence'],status=r['status'],
                  attempts=r['attempts'],error=r['error'],source=r['source'])
        db.set_state('discovery_audit:'+r['cnpj'],FIXTURE['audits'][r['cnpj']])
    for i,v in enumerate(FIXTURE['validations']):db.cache_put('validation','real-fixture-'+str(i),v,3600)


def test_actual_daiju_evidence_confirms_tenant_without_forcing_registered_domain_match():
    result=from_saved(COMPANY,SAVED)
    assert result.confidence=='ALTA' and result.confirmed
    assert not result.signals['brand_domain']
    assert result.signals['tenant_identity_confirmed']
    assert len(result.signals['tenant_evidence_paths'])==3
    assert official_website(result.to_dict())==STORE
    assert result.page_evidence==SAVED['page_evidence']


def test_same_evidence_on_other_hosting_service_uses_general_rule():
    saved=copy.deepcopy(SAVED)
    synthetic='https://daijucomercio.hospedagemdelojas.net/'
    saved['website']=synthetic
    for fact in saved['page_evidence']:fact['url']=fact['url'].replace(STORE,synthetic)
    assert from_saved(COMPANY,saved).confirmed


@pytest.mark.parametrize('mutation',['single_page','query_duplicates','other_tenant','no_cnpj','foreign_cnpj','manual_source','no_legal_page','no_declaration'])
def test_partial_or_mixed_tenant_evidence_cannot_create_high_confidence(mutation):
    saved=copy.deepcopy(SAVED)
    facts=saved['page_evidence']
    if mutation=='single_page':saved['page_evidence']=facts[:1]
    if mutation=='query_duplicates':
        for i,f in enumerate(facts):f['url']=STORE+'?page='+str(i)
    if mutation=='other_tenant':
        for f in facts[1:]:f['url']=f['url'].replace('daijucomercio.','outraloja.')
    if mutation=='no_cnpj':
        for f in facts:f.update(cnpjs=[],owner_context='')
    if mutation=='foreign_cnpj':
        for f in facts:
            f['cnpjs']=['00000000000191'];f['owner_context']=f['owner_context'].replace('00.065.589/0001-06','00.000.000/0001-91')
    if mutation=='manual_source':
        for f in facts:f['source']='MANUAL_REFERENCE'
    if mutation=='no_legal_page':saved['page_evidence']=[facts[0],facts[2]]
    if mutation=='no_declaration':
        for f in facts:f['owner_context']=COMPANY.legal_name+' '+COMPANY.cnpj
    result=from_saved(COMPANY,saved)
    assert result is None or (not result.confirmed and result.confidence!='ALTA' and not official_website(result.to_dict()))


def synthetic_pages():
    # HTML sintético para testar o parser, não é conteúdo obtido nesta revisão.
    footer=f'<footer>Razão Social: {COMPANY.legal_name} CNPJ: {COMPANY.cnpj}</footer>'
    return [Page(STORE,200,{},'<h1>Daiju Comercio</h1>'+footer),
            Page(STORE+'politica-de-privacidade/',200,{},'<h1>Daiju Comercio - Política de Privacidade</h1>'+footer)]


def test_online_classifier_requires_multiple_institutional_pages_for_tenant():
    pages=synthetic_pages()
    assert not classify_pages(COMPANY,pages[:1]).confirmed
    result=classify_pages(COMPANY,pages)
    assert result.confirmed and official_website(result.to_dict())==STORE
    pages[1].url=pages[1].url.replace('daijucomercio.','outraloja.')
    assert not classify_pages(COMPANY,pages).confirmed


def test_body_mentions_without_footer_or_ownership_context_do_not_confirm_tenant():
    pages=synthetic_pages()
    for p in pages:p.text=p.text.replace('<footer>','<p>').replace('</footer>','</p>')
    assert not classify_pages(COMPANY,pages).confirmed


def test_collective_agreement_is_rejected_by_path_and_saved_title_without_bond_blacklist():
    company=Company(**json.loads(CASES[1]['company']))
    assert url_reason(BOND['website'])
    assert content_directory_reason(BOND['page_evidence'][0]['title'])
    result=from_saved(company,BOND)
    assert result.site_type=='THIRD_PARTY' and not result.website
    other=copy.deepcopy(BOND);other['website']='https://outroportal.com/noticias/documento/'
    other['page_evidence'][0]['url']=other['website']
    assert from_saved(company,other).site_type=='THIRD_PARTY'
    assert not url_reason('https://bond.com.br/')


def test_real_three_review_is_offline_and_keeps_modesc_social_links_and_originals(db,tmp_path,monkeypatch):
    seed(db)
    before=[dict(r) for r in db.conn.execute('SELECT * FROM companies ORDER BY cnpj')]
    def forbidden(*args,**kwargs):raise AssertionError('Não construir cliente HTTP')
    monkeypatch.setattr(httpx,'AsyncClient',forbidden)
    report=review(tmp_path/'test.sqlite',tmp_path/'test.sqlite',tmp_path/'review',IDS)
    rows=report['resultados']
    assert rows[0]['Status']=='CONCLUIDO' and rows[0]['Confianca']=='ALTA' and rows[0]['Website']==STORE
    assert rows[1]['Status']==rows[2]['Status']=='SEM_SITE_OFICIAL_CONFIRMADO'
    assert not rows[1]['Website'] and not rows[2]['Website']
    assert len(rows[1]['Redes_sociais_nao_verificadas'])==4
    assert report['consultas_novas']==0
    assert [dict(r) for r in db.conn.execute('SELECT * FROM companies ORDER BY cnpj')]==before
    book=load_workbook(tmp_path/'review/resultado_cnpjs.xlsx',read_only=True)
    manual=list(book['Revisão manual'].values)
    assert len(manual)==3 and all(r[0]!=IDS[0] for r in manual[1:])
    book.close()


def test_last_pilot_stops_after_two_new_search_reservations_even_with_global_room(db,tmp_path,policy,settings,monkeypatch):
    seed(db)
    stamp=time.time()-3900
    db.conn.executemany("INSERT INTO requests(provider,started_at,outcome) VALUES ('serper',?,'SUCESSO')",[(stamp,)]*30)
    path=tmp_path/'last.csv'
    path.write_text('CNPJ;razao_social;municipio\n00068581000101;AGROPECUARIA BOSCHETTI LTDA;Jabora\n',encoding='utf-8')
    policies={'serper':dict(policy,max_requests=34,per_day=34)}
    active=replace(settings,enable_serper=True,serper_key='MOCK')
    plan=prepare_new(path,tmp_path/'test.sqlite',tmp_path,tmp_path/'last',policies,active,1)
    assert plan['limits']['serper_new_attempts']==2
    assert plan['limits']['companies']==1
    db.conn.executemany("INSERT INTO requests(provider,started_at,outcome) VALUES ('serper',?,'SUCESSO')",[(stamp,)]*2)
    def forbidden(*args,**kwargs):raise AssertionError('Teto de duas buscas deve bloquear antes do cliente')
    monkeypatch.setattr(httpx,'AsyncClient',forbidden)
    with pytest.raises(ValueError,match='esgotado'):
        asyncio.run(execute_plan(plan,active,policies,dry_run=False))
    assert db.usage()['serper']['requisicoes']==32
