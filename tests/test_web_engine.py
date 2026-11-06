"""Motor real, HTTP mockado e livro de cotas real em SQLite temporário."""
import asyncio

import httpx
import pytest

from app.config import Settings
from app.storage.database import Database
from app.web import engine


def test_existing_engine_and_shared_quota_are_used_without_real_http(tmp_path,monkeypatch,policy):
    from conftest import generated_cnpj
    (tmp_path/'data').mkdir()
    ledger=Database(tmp_path/'data/poc.sqlite');ledger.close()
    cnpj=generated_cnpj(99997)
    monkeypatch.setattr(engine,'ROOT',tmp_path)
    monkeypatch.setattr(engine.Settings,'from_env',lambda *_:Settings(enable_serper=True,serper_key='mock-only-key'))
    monkeypatch.setattr(engine,'load_policies',lambda *_:{'serper':dict(policy)})
    original_client=httpx.AsyncClient
    calls=[]
    def response(request):
        calls.append(request)
        assert request.url.host=='google.serper.dev'
        assert request.headers['X-API-KEY']=='mock-only-key'
        return httpx.Response(200,json={'organic':[{'link':'https://cnpj.biz/'+cnpj}]})
    monkeypatch.setattr(engine.httpx,'AsyncClient',lambda **kwargs:original_client(transport=httpx.MockTransport(response),**kwargs))
    result=asyncio.run(engine.research_one(cnpj))
    assert len(calls)==1
    assert result['empresa']['cnpj']==cnpj
    assert result['website_oficial'] is None and len(result['referencias'])==1
    ledger=Database(tmp_path/'data/poc.sqlite')
    assert ledger.usage()['serper']['requisicoes']==1
    assert ledger.row(cnpj) is None  # Original company checkpoints remain separate.
    assert ledger.conn.execute('SELECT count(*) FROM cache').fetchone()[0]==1
    ledger.close()
    with pytest.raises(ValueError,match='Checkpoint já existe'):
        asyncio.run(engine.research_one(cnpj))
    assert len(calls)==1


def test_exhausted_website_budget_prevents_search_credit_usage(tmp_path,monkeypatch,policy):
    from conftest import generated_cnpj
    (tmp_path/'data').mkdir()
    ledger=Database(tmp_path/'data/poc.sqlite')
    ledger.conn.execute("INSERT INTO requests(provider,started_at,outcome) VALUES ('websites',0,'SUCESSO')")
    ledger.close()
    monkeypatch.setattr(engine,'ROOT',tmp_path)
    monkeypatch.setattr(engine.Settings,'from_env',lambda *_:Settings(enable_serper=True,serper_key='mock-only-key',website_max_requests=1))
    monkeypatch.setattr(engine,'load_policies',lambda *_:{'serper':dict(policy)})
    monkeypatch.setattr(engine.httpx,'AsyncClient',lambda **kwargs:pytest.fail('Não consumir pesquisa sem cota de validação'))
    with pytest.raises(ValueError,match='Cota esgotada'):
        asyncio.run(engine.research_one(generated_cnpj(99996)))
    ledger=Database(tmp_path/'data/poc.sqlite')
    assert ledger.usage()['websites']['requisicoes']==1 and 'serper' not in ledger.usage()
    ledger.close()
