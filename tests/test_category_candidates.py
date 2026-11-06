"""Regressão do piloto real de Moto Peças LH, Koncremais e VLK; somente offline."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
from openpyxl import load_workbook

from app.models import Company, ProviderError, Validation
from app.review_saved import review
from app.reports.exporter import export_all, export_rows, make_report
from app.services.importer import ImportResult
from app.services.processor import Processor
from app.services.quota_manager import QuotaManager
from app.services.site_outcome import public_validation, result_status
from app.services.site_rules import url_reason
from app.services.validator import Page, classify
from app.services.website_finder import WebsiteFinder

FIXTURE=json.loads((Path(__file__).parent/'fixtures/pilot3_category_saved_cases.json').read_text(encoding='utf-8'))
CASES=FIXTURE['cases']
IDS=[r['cnpj'] for r in CASES]
MOTO=CASES[0]
PARAFA=json.loads(MOTO['validation'])['website']


def seed(db):
    imported=ImportResult([(c,c) for c in IDS],[],{'selecionados':3})
    db.ingest(imported)
    for row in CASES:
        db.update(row['cnpj'],company=json.loads(row['company']),validation=json.loads(row['validation']),
                  candidates=json.loads(row['candidates']),confidence=row['confidence'],status=row['status'],
                  error=row['error'],attempts=row['attempts'],source=row['source'])
        db.set_state('discovery_audit:'+row['cnpj'],FIXTURE['audits'][row['cnpj']])
    return imported


@pytest.mark.parametrize('url',[PARAFA,'https://novoportal.com/guia-comercial/categoria/motos',
    'https://novoportal.com/business-directory/company-information.html',
    'https://motopecaslh.com.br/categoria/motos','https://novoportal.com/categorias/motos'])
def test_category_and_guide_urls_are_not_official_even_with_exact_identity(url):
    company=Company(**json.loads(MOTO['company']))
    assert url_reason(url,company.cnpj)
    result=classify(company,Page(url,200,{},f'<h1>{company.legal_name}</h1><footer>{company.legal_name} {company.cnpj}</footer>'))
    assert not result.website and not result.confirmed


def test_actual_parafa_candidate_is_filtered_before_validation(db,settings):
    seed(db)
    class Validator:
        async def validate(self,*args):raise AssertionError('Não visitar categoria')
    result,candidates,errors=asyncio.run(WebsiteFinder(None,Validator(),db,settings).find(
        Company(**json.loads(MOTO['company'])),[PARAFA]))
    assert candidates==[PARAFA] and not result.website and not errors
    assert result_status(result)=='SEM_SITE_OFICIAL_CONFIRMADO'
    assert db.get_state('discovery_audit:'+IDS[0])[PARAFA]['decision']=='REJECTED'


@pytest.mark.parametrize('confidence',['BAIXA','MEDIA'])
def test_candidate_is_retained_but_never_persisted_as_official(db,settings,confidence):
    seed(db)
    url='https://motopecaslh.com.br/'
    result=Validation(url,'motopecaslh.com.br',confidence,40,[],True,False,site_type='CORPORATE_CANDIDATE')
    class Finder:
        async def find(self,*args):return result,[url],[]
    db.update(IDS[0],validation={},status='PENDENTE')
    asyncio.run(Processor(db,None,Finder(),settings).run([IDS[0]]))
    row=db.row(IDS[0]);saved=json.loads(row['validation'])
    assert row['status']=='PENDENTE_VALIDACAO_SITE_OFICIAL' and row['confidence']==confidence
    assert not saved['website'] and not saved['domain'] and not saved['confirmed']
    assert saved['signals']['candidate_url']==url and json.loads(row['candidates'])==[url]


@pytest.mark.parametrize('mode,expected',[
    ('empty','SEM_SITE_OFICIAL_CONFIRMADO'),('third_party','SEM_SITE_OFICIAL_CONFIRMADO'),
    ('pending','PENDENTE_VALIDACAO_SITE_OFICIAL'),('error','FALHA_VALIDACAO_SITE_OFICIAL')])
def test_absence_pending_and_technical_failure_are_distinct(db,settings,mode,expected):
    seed(db)
    class Search:
        async def search(self,*args):return []
    class Validator:
        async def validate(self,company,url):
            if mode=='error':raise ProviderError('SITE_INACESSIVEL')
            return Validation(url,'motopecaslh.com.br','BAIXA',20,[],True,False,site_type='CORPORATE_CANDIDATE')
    urls=[] if mode=='empty' else [PARAFA] if mode=='third_party' else ['https://motopecaslh.com.br/']
    db.update(IDS[0],validation={},candidates=urls,status='PENDENTE')
    finder=WebsiteFinder(Search(),Validator(),db,settings)
    asyncio.run(Processor(db,None,finder,settings).run([IDS[0]]))
    row=db.row(IDS[0]);val=json.loads(row['validation'])
    assert row['status']==expected and not val['website']
    assert ('SITE_INACESSIVEL' in row['error'])==(mode=='error')
    report=make_report(db,[IDS[0]],ImportResult([(IDS[0],IDS[0])],[],{}),QuotaManager(db,{}),0,'offline',1)
    assert report['websites_encontrados']==0
    assert report['falhas_validacao']==(mode=='error')
    assert report['candidatos_pendentes']==(mode=='pending')


def test_checkpoint_and_exports_never_publish_weak_candidate(db,settings,tmp_path):
    imported=seed(db)
    url='https://motopecaslh.com.br/'
    class Validator:
        async def validate(self,company,candidate):
            return Validation(url,'motopecaslh.com.br','MEDIA',50,[],True,False,site_type='CORPORATE_CANDIDATE')
    result,_,_=asyncio.run(WebsiteFinder(None,Validator(),db,settings).find(Company(**json.loads(MOTO['company'])),[url]))
    assert result.confidence=='MEDIA'
    assert not json.loads(db.row(IDS[0])['validation'])['website']
    # Defesa na exportação também cobre registros legados, sem alterar o banco histórico.
    db.update(IDS[0],validation=json.loads(MOTO['validation']),confidence='BAIXA',status='CONCLUIDO')
    before=dict(db.row(IDS[0]))
    export_all(tmp_path/'export',db,IDS,imported,{})
    exported=export_rows(db.rows(IDS))[0]
    assert not exported['Website'] and exported['Status']=='SEM_SITE_OFICIAL_CONFIRMADO'
    assert exported['Candidato para revisão']==PARAFA
    assert dict(db.row(IDS[0]))==before
    book=load_workbook(tmp_path/'export/resultado_cnpjs.xlsx',read_only=True)
    values=list(book['Resultados'].values);headers=values[0]
    assert values[1][headers.index('Website')] is None
    assert values[1][headers.index('CNPJ')]==IDS[0]
    book.close()


def test_real_three_are_reclassified_offline_preserving_history_and_candidates(db,tmp_path,monkeypatch):
    seed(db)
    before=[dict(r) for r in db.conn.execute('SELECT * FROM companies ORDER BY cnpj')]
    def forbidden(*args,**kwargs):raise AssertionError('Não construir cliente HTTP')
    monkeypatch.setattr(httpx,'AsyncClient',forbidden)
    report=review(tmp_path/'test.sqlite',tmp_path/'test.sqlite',tmp_path/'review',IDS)
    rows={r['CNPJ']:r for r in report['resultados']}
    assert rows[IDS[0]]['Status']=='PENDENTE_VALIDACAO_SITE_OFICIAL'
    assert rows[IDS[1]]['Status']=='SEM_SITE_OFICIAL_CONFIRMADO'
    assert rows[IDS[2]]['Status']=='SEM_SITE_OFICIAL_CONFIRMADO'
    assert all(not r['Website'] for r in rows.values()) and report['consultas_novas']==0
    assert rows[IDS[0]]['Candidato_revisao']==PARAFA
    assert any('business-directory' in r['url'] for r in rows[IDS[2]]['Rejeicoes'])
    assert [dict(r) for r in db.conn.execute('SELECT * FROM companies ORDER BY cnpj')]==before


def test_confirmed_corporate_homepage_still_completes():
    company=Company(**json.loads(MOTO['company']))
    result=classify(company,Page('https://motopecaslh.com.br/',200,{},
        f'<h1>{company.legal_name}</h1><footer>{company.legal_name} {company.cnpj}</footer>'))
    public=public_validation(result)
    assert public.website and public.confirmed and result_status(public)=='CONCLUIDO'


def test_high_label_without_ownership_signals_is_only_a_pending_candidate():
    result=Validation('https://motopecaslh.com.br/','motopecaslh.com.br','ALTA',100,[],True,True,site_type='CORPORATE')
    public=public_validation(result)
    assert not public.website and not public.confirmed and public.confidence=='PENDENTE'
    assert result_status(public)=='PENDENTE_VALIDACAO_SITE_OFICIAL'


def test_technical_error_from_saved_corporate_candidate_is_not_absence(db,tmp_path):
    seed(db)
    url='https://motopecaslh.com.br/'
    db.update(IDS[0],candidates=[url],validation={},error='SITE_INACESSIVEL')
    db.set_state('discovery_audit:'+IDS[0],{url:{'decision':'ERROR','reason':'SITE_INACESSIVEL'}})
    result=review(tmp_path/'test.sqlite',tmp_path/'test.sqlite',tmp_path/'review',[IDS[0]])['resultados'][0]
    assert result['Status']=='FALHA_VALIDACAO_SITE_OFICIAL' and not result['Website']
