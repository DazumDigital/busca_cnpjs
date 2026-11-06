import asyncio
import json
import ssl
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest

from app.main import approve_stage
from app.models import Company, ProviderError, QuotaExceeded, Validation
from app.reports.exporter import make_report
from app.security import tls_context
from app.services.enrichment import Enrichment
from app.services.processor import Processor
from app.services.quota_manager import QuotaManager
from app.services.validator import Page, SafeFetcher
from app.services.website_finder import WebsiteFinder
from test_pipeline import seed


def test_tls_verifies_certificates_without_keylog(monkeypatch):
    monkeypatch.setenv("SSLKEYLOGFILE", "this-must-never-be-created.log")
    context = tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.keylog_filename is None


def test_partial_candidate_survives_quota_exhaustion(tmp_path, db, settings):
    imported = seed(tmp_path, db)
    cnpj = imported.selected[0][0]
    db.update(cnpj, company=asdict(Company(cnpj, "Acme Digital")))

    class Search:
        calls = 0
        async def search(self, query):
            self.calls += 1
            if self.calls > 1:
                raise QuotaExceeded()
            return ["https://acme.com/", "https://acme.com/contato/"]

    class Validator:
        async def validate(self, company, url):
            if url.endswith('/contato/'):
                raise QuotaExceeded()
            return Validation(url, "acme.com", "MEDIA", 40, ["Nome"], True, False)

    processor = Processor(db, Enrichment(None), WebsiteFinder(Search(), Validator(), db, settings), settings)
    asyncio.run(processor.run([cnpj]))
    row = db.row(cnpj)
    assert row["status"] == "COTA_ESGOTADA"
    assert row["confidence"] == "MEDIA"
    validation = json.loads(row["validation"])
    assert not validation["website"]
    assert validation['signals']['candidate_url'] == "https://acme.com/"


def test_cancellation_keeps_business_checkpoint(tmp_path, db, settings):
    imported = seed(tmp_path, db)
    cnpj = imported.selected[0][0]

    class Data:
        async def get(self, cnpj):
            return Company(cnpj, "Acme Digital")

    class Finder:
        async def find(self, *args):
            raise asyncio.CancelledError()

    processor = Processor(db, Data(), Finder(), settings)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(processor.run([cnpj]))
    assert db.row(cnpj)["status"] == "PENDENTE"
    assert json.loads(db.row(cnpj)["company"])["legal_name"] == "Acme Digital"


def test_expired_policy_is_blocked_at_request_time(db, policy):
    quota = QuotaManager(db, {"brasilapi": dict(policy, expires_on="2000-01-01")})

    async def run():
        with pytest.raises(ProviderError) as exc:
            await quota.reserve("brasilapi")
        assert exc.value.code == "INTEGRACAO_NAO_VERIFICADA"
    asyncio.run(run())
    assert not db.usage()


@pytest.mark.parametrize("window,seconds", [("per_minute", 60), ("per_hour", 3600), ("per_day", 86400)])
def test_rate_windows(window, seconds, db, policy):
    from test_providers_quotas import FakeClock
    clock = FakeClock()
    quota = QuotaManager(db, {"brasilapi": dict(policy, **{window: 1})}, sleep=clock.sleep, clock=clock)

    async def run():
        await quota.reserve("brasilapi")
        await quota.reserve("brasilapi")
    asyncio.run(run())
    assert sum(clock.waits) >= seconds


def test_stage_approval_uses_completed_run_and_persists(tmp_path, db, settings):
    imported = seed(tmp_path, db, 10)
    ids = [c for c, _ in imported.selected]
    for c in ids:
        db.update(c, status="CONCLUIDO", attempts=1)
    db.conn.execute("INSERT INTO requests(provider,started_at,outcome) VALUES ('mock',?,'SUCESSO')", (time.time(),))
    report = make_report(db, ids, imported, QuotaManager(db, {}), 0, "real", 10)
    db.conn.execute("INSERT INTO runs(started_at,finished_at,mode,stage,report) VALUES (0,1,'real',10,?)",
                    (json.dumps(report),))
    approve_stage(db, 10)
    assert db.get_state("approved-stage:10")


def test_robots_missing_allows_and_redirect_loop_blocks(settings):
    class Fetcher(SafeFetcher):
        async def one(self, url):
            if url.endswith("/robots.txt"):
                return Page(url, 404, {}, "")
            return Page(url, 302, {"location": url}, "")

    async def run():
        fetcher = Fetcher(None, settings)
        await fetcher.allowed("https://acme.com/")
        with pytest.raises(ProviderError) as exc:
            await fetcher.fetch("https://acme.com/")
        assert exc.value.code == "LOOP_REDIRECIONAMENTO"
    asyncio.run(run())


def test_identical_concurrent_api_requests_use_one_reservation(db, settings, policy):
    import httpx
    from app.providers.base import JsonAPI
    calls = []
    quota = QuotaManager(db, {"serper": policy})

    async def handler(request):
        calls.append(1)
        await asyncio.sleep(0)
        return httpx.Response(200, json={"value": "ok"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            api = JsonAPI("serper", client, quota, db, settings)
            result = await asyncio.gather(*(api.request("GET", "https://google.serper.dev/mock") for _ in range(4)))
            assert result == [{"value": "ok"}] * 4
    asyncio.run(run())
    assert len(calls) == db.usage()["serper"]["requisicoes"] == 1
