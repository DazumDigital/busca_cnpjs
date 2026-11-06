"""Regressão dos três resultados reais do primeiro bloco dos sete restantes."""
import asyncio
import copy
import json
import time
from pathlib import Path

import httpx
import pytest
from openpyxl import load_workbook

from app.models import Company
from app.new_batch import prepare_new
from app.pilot3 import main as pilot_main
from app.review_saved import from_saved,review
from app.services.importer import ImportResult
from app.services.site_rules import candidate_groups,candidate_kind,content_directory_reason,url_reason
from app.services.website_finder import WebsiteFinder

FIXTURE=json.loads((Path(__file__).parent/'fixtures/lote01_saved_cases.json').read_text(encoding='utf-8'))
CASES=FIXTURE['cases']
IDS=[r['cnpj'] for r in CASES]
NEXT=['00065589000106','00065768000143','00066424000159']
YELP=next(u for u in json.loads(CASES[0]['candidates']) if 'yelp.com' in u)
CNPJSPOT=next(u for u in json.loads(CASES[1]['candidates']) if 'cnpjspot.com.br' in u)


def seed(db):
    db.ingest(ImportResult([(c,c) for c in IDS],[],{'selecionados':3}))
    for r in CASES:
        db.update(r['cnpj'],company=json.loads(r['company']),validation=json.loads(r['validation']),
                  candidates=json.loads(r['candidates']),confidence=r['confidence'],status=r['status'],
                  attempts=r['attempts'],error=r['error'],source=r['source'])
        db.set_state('discovery_audit:'+r['cnpj'],FIXTURE['audits'][r['cnpj']])


@pytest.mark.parametrize('url',[YELP,'https://www.yelp.com.br/biz/duale','https://mobile.yelp.com/biz/duale',
                               CNPJSPOT,'https://cnpjspot.com.br/'])
def test_directory_domains_are_rejected_before_visits(db,settings,url):
    seed(db)
    assert url_reason(url) and candidate_kind(url)=='BUSINESS_DIRECTORY'
    class Validator:
        async def validate(self,*args):raise AssertionError('Não visitar diretórios')
    result,urls,errors=asyncio.run(WebsiteFinder(None,Validator(),db,settings).find(Company(**json.loads(CASES[0]['company'])),[url]))
    assert urls==[url] and not errors and not result.website
    assert result.site_type=='THIRD_PARTY_ONLY'
    assert db.get_state('discovery_audit:'+IDS[0])[url]['source_kind']=='BUSINESS_DIRECTORY'


def test_social_network_links_are_separate_without_implying_verified_profiles():
    urls=json.loads(CASES[0]['candidates'])
    groups=candidate_groups(urls)
    assert len(groups['social_networks'])==3
    assert all('instagram.com' in u or 'facebook.com' in u for u in groups['social_networks'])
    assert YELP in groups['business_directories'] and YELP not in groups['social_networks']
    assert all(url_reason(u) for u in groups['social_networks'])
    assert candidate_kind('https://instagram.com.outrodominio.com/perfil')=='UNVERIFIED_CANDIDATE'
    assert candidate_kind('https://yelp.com.outrodominio.com/')=='UNVERIFIED_CANDIDATE'


def test_actual_listing_title_is_rejected_even_on_unlisted_hostname():
    saved=copy.deepcopy(json.loads(CASES[1]['validation']))
    saved['website']=CNPJSPOT
    company=Company(**json.loads(CASES[1]['company']))
    title=saved['page_evidence'][0]['title']
    assert content_directory_reason(title)
    assert len(saved['page_evidence'][0]['cnpjs'])>1
    assert company.cnpj in saved['page_evidence'][0]['cnpjs']
    assert from_saved(company,saved).site_type=='THIRD_PARTY'
    saved['website']='https://outralistagem.com/lista/'
    for fact in saved['page_evidence']:fact['url']=saved['website']
    assert from_saved(company,saved).site_type=='THIRD_PARTY'


@pytest.mark.parametrize('url',[
    'https://leis.org/municipais/sc/chapeco/lei/1983/170',
    'https://api.mziq.com/mzfilemanager/v2/d/arquivo',
    'https://ndl.ethernet.edu.et/bitstreams/arquivo/download',
    'https://theanarchistlibrary.org/library/artigo',
    'https://www.academia.edu/26472939/ICHoLS_XIII_Conference_Handbook'])
def test_explicit_documents_do_not_sustain_corporate_validation_errors(url):
    assert url_reason(url) and candidate_kind(url)=='THIRD_PARTY'


def test_review_actual_three_keeps_history_social_links_and_listing_evidence(db,tmp_path,monkeypatch):
    seed(db)
    before=[dict(r) for r in db.conn.execute('SELECT * FROM companies ORDER BY cnpj')]
    usage=db.usage()
    def forbidden(*args,**kwargs):raise AssertionError('Não construir cliente HTTP')
    monkeypatch.setattr(httpx,'AsyncClient',forbidden)
    report=review(tmp_path/'test.sqlite',tmp_path/'test.sqlite',tmp_path/'review',IDS)
    assert report['consultas_novas']==0
    assert all(r['Status']=='SEM_SITE_OFICIAL_CONFIRMADO' and not r['Website'] for r in report['resultados'])
    duale=report['resultados'][0];mirelle=report['resultados'][1]
    assert len(duale['Redes_sociais_nao_verificadas'])==3 and YELP in duale['Diretorios_descartados']
    assert CNPJSPOT in mirelle['Diretorios_descartados']
    assert duale['Candidato_revisao']==YELP and mirelle['Candidato_revisao']==CNPJSPOT
    assert [dict(r) for r in db.conn.execute('SELECT * FROM companies ORDER BY cnpj')]==before
    assert db.usage()==usage
    book=load_workbook(tmp_path/'review/resultado_cnpjs.xlsx',read_only=True)
    rows=list(book['Resultados'].values);headers=rows[0]
    assert all(not row[headers.index('Website')] for row in rows[1:])
    assert 'instagram.com/dualemodas/' in rows[1][headers.index('Redes sociais (não verificadas)')]
    assert 'yelp.com/biz/' in rows[1][headers.index('Diretórios e listagens (não oficiais)')]
    book.close()


def test_prepare_next_three_and_precheck_are_offline_with_shared_budget(db,tmp_path,policy,settings,monkeypatch,capsys):
    seed(db)
    stamp=time.time()-3900
    db.conn.executemany('INSERT INTO requests(provider,started_at,outcome) VALUES (?,?,?)',
        [('serper',stamp,'SUCESSO')]*26+[('websites',stamp,'SUCESSO')]*84)
    before=[dict(r) for r in db.conn.execute('SELECT * FROM companies ORDER BY cnpj')]
    usage=db.usage()
    source=tmp_path/'test.sqlite';out=tmp_path/'lote02'
    path=tmp_path/'lote02.csv'
    path.write_text('CNPJ;razao_social;municipio\n00065589000106;DAIJU COMERCIO DE MOVEIS E COLCHOES LTDA;Palhoca\n'
        '00065768000143;MODESC INDUSTRIA E COMERCIO DE MOVEIS LTDA;Chapeco\n'
        '00066424000159;FARMACOMED SERVICOS LTDA;Florianopolis\n',encoding='utf-8')
    policies={'serper':dict(policy,max_requests=34,per_day=34)}
    policy_file=tmp_path/'policies.json';policy_file.write_text(json.dumps(policies),encoding='utf-8')
    def forbidden(*args,**kwargs):raise AssertionError('Não construir cliente HTTP')
    monkeypatch.setattr(httpx,'AsyncClient',forbidden)
    plan=prepare_new(path,source,tmp_path,out,policies,settings,3)
    assert plan['ids']==NEXT and plan['initial_usage']=={'serper':26,'websites':84}
    assert plan['limits']['serper_new_attempts']==6 and plan['limits']['concurrency']==1
    assert plan['budget']['serper']['restantes_locais']==8
    assert plan['budget']['websites']['restantes_locais']==36
    assert pilot_main(['--check','--plan',str(out/'plan.json'),'--policies',str(policy_file),'--env-file',str(tmp_path/'absent.env')])==0
    output=json.loads(capsys.readouterr().out)
    assert output['chamadas_externas']==0 and output['ids']==NEXT
    assert db.usage()==usage and [dict(r) for r in db.conn.execute('SELECT * FROM companies ORDER BY cnpj')]==before
