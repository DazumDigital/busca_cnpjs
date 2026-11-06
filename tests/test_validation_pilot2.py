import asyncio
import json
import socket
from pathlib import Path

import httpx
import pytest

from app.models import Company,ProviderError
from app.review_saved import from_saved,review
from app.services.validator import Page,PinnedHTTPConnection,SafeFetcher,classify_pages
from app.services.site_rules import RULES_VERSION
from app.services.importer import ImportResult
from app.new_batch import prepare_new
from app.pilot3 import small_settings

FIXTURE=json.loads((Path(__file__).parent/'fixtures/pilot2_saved_cases.json').read_text(encoding='utf-8'))
CASES=FIXTURE['cases']


@pytest.mark.parametrize('tls,port',[ (True,443),(False,80)])
def test_wire_host_omits_default_port_and_stops_false_canonical_redirect(tls,port):
    sent=[]
    class Socket:
        def sendall(self,data):sent.append(data)
        def close(self):pass
    conn=PinnedHTTPConnection('thpbrasil.com.br',port,(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',port)),10,tls)
    conn.sock=Socket()
    conn.request('GET','/sobre/')
    wire=b''.join(sent)
    assert b'Host: thpbrasil.com.br\r\n' in wire
    assert b'thpbrasil.com.br:443' not in wire and b'thpbrasil.com.br:80' not in wire
    conn.close()


def test_http_https_www_redirect_chain_is_preserved(settings):
    routes={'http://softdata.com.br/':(301,'https://softdata.com.br/'),
            'https://softdata.com.br/':(308,'https://www.softdata.com.br/')}
    class Fetcher(SafeFetcher):
        def __init__(self):self.settings=settings
        async def allowed(self,url):pass
        async def one(self,url):
            if url in routes:
                status,dest=routes[url];return Page(url,status,{'location':dest},'')
            assert url=='https://www.softdata.com.br/'
            return Page(url,200,{},'<h1>Softdata</h1>')
    page=asyncio.run(Fetcher().fetch('http://softdata.com.br/'))
    assert page.url=='https://www.softdata.com.br/' and len(page.redirect_chain)==2
    assert [r['status'] for r in page.redirect_chain]==[301,308]


def test_real_redirect_loop_is_still_rejected_with_diagnostics(settings):
    class Fetcher(SafeFetcher):
        def __init__(self):self.settings=settings
        async def allowed(self,url):pass
        async def one(self,url):return Page(url,302,{'location':url},'')
    with pytest.raises(ProviderError) as error:
        asyncio.run(Fetcher().fetch('https://softdata.com.br/'))
    assert error.value.code=='LOOP_REDIRECIONAMENTO'
    assert error.value.details['redirect_chain'][0]['destination']=='https://softdata.com.br/'


def test_www_alias_legal_evidence_can_be_used_without_domain_allowlist():
    company=Company('00055627000140','THP COMERCIO E REPRESENTACOES LTDA')
    pages=[Page('https://thpbrasil.com.br/',200,{},'<h1>THP Brasil</h1>'),
           Page('https://www.thpbrasil.com.br/termos/',200,{},
                'Este site é operado por THP COMERCIO E REPRESENTACOES LTDA, inscrita no CNPJ 00.055.627/0001-40.')]
    result=classify_pages(company,pages)
    assert result.confirmed and result.confidence=='ALTA'
    assert len(result.inspected_urls)==2
    # Conteúdo de fixture sintético; nenhum HTML real foi baixado nesta revisão.


def test_actual_rvb_saved_directory_evidence_is_rejected_without_hostname_list():
    row=next(r for r in CASES if r['cnpj']=='00060085000101')
    saved=json.loads(row['validation'])
    result=from_saved(Company(**json.loads(row['company'])),saved)
    assert result.site_type=='THIRD_PARTY'
    assert not result.confirmed and not result.website
    assert result.page_evidence[0]['title']==saved['page_evidence'][0]['title']
    # A mesma evidência rejeita qualquer domínio, não só blumenau.net.br.
    saved['website']='https://outroguia.com/perfil/'
    for fact in saved['page_evidence']:fact['url']='https://outroguia.com/perfil/'
    assert from_saved(Company(**json.loads(row['company'])),saved).site_type=='THIRD_PARTY'


def test_manual_reference_never_creates_high_confidence():
    company=Company('00055627000140','THP COMERCIO E REPRESENTACOES LTDA')
    saved={'website':'https://thpbrasil.com.br/','confirmed':True,'confidence':'ALTA',
           'page_evidence':[{'url':'https://thpbrasil.com.br/','source':'MANUAL_REFERENCE','title':'THP Brasil',
                             'content_sha256':'a'*64,'cnpjs':[company.cnpj],
                             'owner_context':company.legal_name+' '+company.cnpj}]}
    assert from_saved(company,saved) is None


def test_derived_evidence_with_foreign_cnpj_is_rejected():
    company=Company('00056678000196','SOFTDATA SOLUCOES LTDA')
    saved={'website':'https://softdata.com.br/','page_evidence':[dict(url='https://softdata.com.br/',
           source='PUBLIC_PAGE_DERIVED_EVIDENCE',content_sha256='a'*64,title='Softdata',
           cnpjs=['00000000000191'],owner_context='Outra empresa 00000000000191')]}
    result=from_saved(company,saved)
    assert result.site_type=='CNPJ_DIVERGENTE' and not result.website


def seed_cases(db):
    ids=[r['cnpj'] for r in CASES]
    db.ingest(ImportResult([(c,c) for c in ids],[],{'selecionados':3}))
    for row in CASES:
        db.update(row['cnpj'],company=json.loads(row['company']),validation=json.loads(row['validation']),
                  candidates=json.loads(row['candidates']),status=row['status'],confidence=row['confidence'],
                  attempts=row['attempts'],error=row['error'],source=row['source'])
    return ids


def test_review_actual_three_is_offline_and_preserves_original(db,tmp_path,monkeypatch):
    ids=seed_cases(db)
    before=[dict(r) for r in db.conn.execute('SELECT * FROM companies ORDER BY cnpj')]
    def forbidden(*args,**kwargs):raise AssertionError('Não construir cliente HTTP')
    monkeypatch.setattr(httpx,'AsyncClient',forbidden)
    result=review(tmp_path/'test.sqlite',tmp_path/'test.sqlite',tmp_path/'review',ids)
    rows={r['CNPJ']:r for r in result['resultados']}
    assert rows['00060085000101']['Status']=='SEM_SITE_OFICIAL_CONFIRMADO'
    assert rows['00055627000140']['Status']=='PENDENTE_VALIDACAO_SITE_OFICIAL'
    assert rows['00056678000196']['Status']=='PENDENTE_VALIDACAO_SITE_OFICIAL'
    assert all(r['Confianca']!='ALTA' for r in rows.values())
    assert result['consultas_novas']==0
    assert [dict(r) for r in db.conn.execute('SELECT * FROM companies ORDER BY cnpj')]==before


def test_new_batch_refuses_previous_ids_without_creating_output(db,tmp_path,policy,settings):
    seed_cases(db)
    path=tmp_path/'old.csv';path.write_text('CNPJ\n'+CASES[0]['cnpj'],encoding='utf-8')
    with pytest.raises(ValueError,match='inédito'):
        prepare_new(path,tmp_path/'test.sqlite',tmp_path,tmp_path/'next',{'serper':policy},small_settings(settings))
    assert not (tmp_path/'next').exists()


def test_saved_brand_evidence_stays_medium_without_ownership_statement():
    company=Company('00055627000140','THP COMERCIO E REPRESENTACOES LTDA')
    saved={'website':'https://thpbrasil.com.br/','page_evidence':[dict(url='https://thpbrasil.com.br/',
        source='PUBLIC_PAGE_DERIVED_EVIDENCE',content_sha256='a'*64,title='THP Brasil',
        cnpjs=[company.cnpj],owner_context=company.legal_name+' '+company.cnpj)]}
    result=from_saved(company,saved)
    assert result.confidence=='MEDIA' and not result.confirmed


def test_directory_rejection_preserves_derived_evidence():
    company=Company('00060085000101','RVB USINADOS LTDA')
    result=classify_pages(company,[Page('https://portalindustrial.com/rvb/',200,{},
        '<title>RVB Isolantes | Guia</title><h1>Empresa não reivindicada</h1>')])
    assert result.site_type=='THIRD_PARTY' and not result.website
    assert result.page_evidence[0]['title']=='RVB Isolantes | Guia Empresa não reivindicada'
    assert result.page_evidence[0]['rejection_reason']


def test_review_missing_id_fails_before_creating_output(db,tmp_path):
    with pytest.raises(ValueError,match='origem'):
        review(tmp_path/'test.sqlite',tmp_path/'test.sqlite',tmp_path/'review',['00055627000140'])
    assert not (tmp_path/'review').exists()


def test_new_batch_preparation_uses_only_unresearched_fixture_and_respects_budget(db,tmp_path,policy,settings):
    # Registro real já existente, usado apenas como fixture num banco temporário sem tentativas.
    # Não representa o próximo lote do usuário e nenhuma busca é executada.
    company=Company(**json.loads(CASES[0]['company']))
    db.ingest(ImportResult([(company.cnpj,company.cnpj)],[],{'selecionados':1}))
    db.update(company.cnpj,company=company.__dict__)
    path=tmp_path/'fixture.csv'
    path.write_text('CNPJ;razao_social;municipio;UF\n'+company.cnpj+';'+company.legal_name+';'+company.city+';'+company.state,encoding='utf-8')
    before=[dict(r) for r in db.conn.execute('SELECT * FROM companies')]
    plan=prepare_new(path,tmp_path/'test.sqlite',tmp_path,tmp_path/'next',{'serper':policy},settings)
    assert plan['new_unsearched'] and plan['ids']==[company.cnpj]
    assert plan['limits']['serper_new_attempts']==2 and plan['limits']['concurrency']==1
    assert plan['selection']['effective']==1
    assert [dict(r) for r in db.conn.execute('SELECT * FROM companies')]==before
