import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import Settings, policy_reason
from app.main import provider_status
from app.models import ProviderError, QuotaExceeded
from app.providers.base import JsonAPI, retry_after
from app.providers.brasilapi import BrasilAPI
from app.providers.search import Serper
from app.services.quota_manager import QuotaManager


class FakeClock:
    def __init__(self):
        import time
        self.now = time.time()
        self.waits = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def test_policy_defaults_and_flags(policy, monkeypatch):
    assert policy_reason({})
    assert not policy_reason(policy, search=True)
    for flag in ("verified", "no_card", "no_auto_charge", "no_paid_credits", "storage_permitted", "batch_permitted"):
        assert policy_reason(dict(policy, **{flag: False}), search=True)
    assert policy_reason(dict(policy, expires_on="2000-01-01"))
    assert policy_reason(dict(policy, confirmed_free_remaining=0), search=True)
    assert policy_reason(dict(policy, balance_verified_at="2000-01-01T00:00:00+00:00"), search=True)
    state = provider_status(Settings(enable_serper=True), {"serper": policy})
    assert not state["serper"]["habilitado"]
    monkeypatch.setenv("ENABLE_PAID_APIS", "true")
    with pytest.raises(ValueError, match="custo zero"):
        Settings.from_env()


@pytest.mark.parametrize("variable,value", [("ENABLE_AUTOMATIC_TOPUP", "true"), ("MAX_ADDITIONAL_COST_USD", "1"),
                                            ("MAX_ADDITIONAL_COST_USD", "NaN"), ("MAX_COMPANIES", "1001")])
def test_zero_cost_config(variable, value, monkeypatch):
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError):
        Settings.from_env()


def test_concurrent_quota_and_restart(db, policy):
    p = dict(policy, max_requests=3)
    quota = QuotaManager(db, {"mock": p})

    async def run():
        results = await asyncio.gather(*(quota.reserve("mock") for _ in range(6)), return_exceptions=True)
        assert sum(isinstance(v, int) for v in results) == 3
        assert sum(isinstance(v, QuotaExceeded) for v in results) == 3
        restarted = QuotaManager(db, {"mock": p})
        with pytest.raises(QuotaExceeded):
            await restarted.reserve("mock")
    asyncio.run(run())


def test_snapshot_balance_and_rate_windows(db, policy):
    clock = FakeClock()
    p = dict(policy, confirmed_free_remaining=2, per_minute=1)
    quota = QuotaManager(db, {"mock": p}, sleep=clock.sleep, clock=clock)

    async def run():
        await quota.reserve("mock")
        await quota.reserve("mock")
        assert sum(clock.waits) >= 60
        with pytest.raises(QuotaExceeded):
            await quota.reserve("mock")
    asyncio.run(run())


@pytest.mark.parametrize("status", [429, 500, 503])
def test_http_retry_after_cache_and_reduced_storage(db, settings, policy, status):
    responses = []
    clock = FakeClock()
    quota = QuotaManager(db, {"brasilapi": policy}, clock=clock, sleep=clock.sleep)

    def handler(request):
        responses.append(request)
        if len(responses) == 1:
            return httpx.Response(status, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"cnpj": "00000000000191", "razao_social": "Empresa Teste",
                                        "email": "dado-que-nao-deve-ser-armazenado"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = BrasilAPI(JsonAPI("brasilapi", client, quota, db, settings, sleep=clock.sleep))
            assert (await provider.lookup("00000000000191")).legal_name == "Empresa Teste"
            await provider.lookup("00000000000191")
        assert len(responses) == 2
        assert sum(clock.waits) >= 3
        assert db.usage()["brasilapi"]["falhas"] == 1
        assert "dado-que" not in db.conn.execute("SELECT value FROM cache").fetchone()[0]
    asyncio.run(run())


@pytest.mark.parametrize("kind,expected", [("timeout", "ERRO_REDE"), ("404", "CNPJ_NAO_ENCONTRADO"),
                                          ("invalid", "RESPOSTA_INVALIDA"), ("wrongcnpj", "RESPOSTA_INVALIDA"),
                                          ("429", "LIMITE_TEMPORARIO"), ("403", "ACESSO_NEGADO")])
def test_provider_failures(db, settings, policy, kind, expected):
    clock = FakeClock()
    quota = QuotaManager(db, {"brasilapi": policy}, clock=clock, sleep=clock.sleep)
    calls = []

    def handler(request):
        calls.append(1)
        if kind == "timeout":
            raise httpx.ReadTimeout("mock")
        if kind == "invalid":
            return httpx.Response(200, json={})
        if kind == "wrongcnpj":
            return httpx.Response(200, json={"cnpj": "123", "razao_social": "Outro"})
        return httpx.Response(int(kind))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = BrasilAPI(JsonAPI("brasilapi", client, quota, db, settings, sleep=clock.sleep))
            with pytest.raises(ProviderError) as error:
                await provider.lookup("00000000000191")
            assert error.value.code == expected
        assert len(calls) == (3 if kind in {"timeout", "429"} else 1)
    asyncio.run(run())


def test_quota_blocks_retry(db, settings, policy):
    calls = []
    clock = FakeClock()
    quota = QuotaManager(db, {"brasilapi": dict(policy, max_requests=1)}, clock=clock, sleep=clock.sleep)

    def handler(request):
        calls.append(1)
        return httpx.Response(503)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            api = JsonAPI("brasilapi", client, quota, db, settings, sleep=clock.sleep)
            with pytest.raises(QuotaExceeded):
                await api.request("GET", "https://brasilapi.com.br/mock")
        assert len(calls) == 1
    asyncio.run(run())


def test_serper_client_never_stores_snippets_or_key(db, settings, policy):
    quota = QuotaManager(db, {"serper": policy})

    def handler(request):
        assert request.headers["X-API-KEY"] == "MOCK_SECRET"
        return httpx.Response(200, json={"organic": [{"link": "https://empresa.com/", "snippet": "NAO_PERSISTIR"}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = Serper(JsonAPI("serper", client, quota, db, settings), "MOCK_SECRET")
            assert await provider.search("empresa") == ["https://empresa.com/"]
        dump = "\n".join(db.conn.iterdump())
        assert "MOCK_SECRET" not in dump
        assert "NAO_PERSISTIR" not in dump
    asyncio.run(run())


def test_retry_after_date():
    from email.utils import format_datetime
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60))
    assert 58 <= retry_after(future) <= 60
    assert retry_after("garbage") == 0
    assert retry_after("-5") == 0
