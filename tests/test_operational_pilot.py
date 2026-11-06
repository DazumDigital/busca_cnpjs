import asyncio
import csv
import importlib
import json
import time

import httpx
import pytest
from openpyxl import load_workbook

from app.config import Settings, load_policies
from app.main import main, provider_status
from app.models import Company, ProviderError, QuotaExceeded
from app.pilot import pilot_policies, pilot_settings
from app.providers.base import JsonAPI
from app.providers.brasilapi import BrasilAPI
from app.providers.search import Serper
from app.reports.exporter import export_all, make_report
from app.services.enrichment import Enrichment
from app.services.processor import Processor
from app.services.quota_manager import QuotaManager
from app.services.validator import Page, WebsiteValidator
from app.services.website_finder import WebsiteFinder
from app.storage.database import Database
from test_pipeline import seed


def test_profile_enforces_ten_serial_even_with_larger_environment(monkeypatch):
    monkeypatch.setenv("MAX_COMPANIES", "1000")
    monkeypatch.setenv("MAX_CONCURRENT_REQUESTS", "10")
    configured = pilot_settings(Settings.from_env())
    assert configured.max_companies == 10
    assert configured.concurrency == 1
    assert configured.batch_size == 10
    assert configured.retries == 1
    assert configured.website_max_requests == 120


def test_profile_caps_never_increase_documented_limits(policy):
    capped = pilot_policies({"serper": policy, "brasilapi": dict(policy, max_requests=5, per_minute=1)})
    assert capped["serper"]["max_requests"] == 40
    assert capped["serper"]["per_minute"] == 2
    assert capped["brasilapi"]["max_requests"] == 5
    assert capped["brasilapi"]["per_minute"] == 1
    assert policy["max_requests"] == 100
    assert not provider_status(pilot_settings(Settings()), capped)["serper"]["habilitado"]


def test_explicit_env_file_does_not_enable_search(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    for name in ("MAX_COMPANIES", "MAX_CONCURRENT_REQUESTS", "ENABLE_SERPER"):
        # Registrar a chave no monkeypatch mesmo se estava ausente, pois dotenv
        # escreve diretamente em os.environ durante este teste.
        monkeypatch.setenv(name, "temporary")
        monkeypatch.delenv(name, raising=False)
    profile = tmp_path / "pilot.env"
    profile.write_text("MAX_COMPANIES=10\nMAX_CONCURRENT_REQUESTS=1\nENABLE_SERPER=false\n", encoding="utf-8")
    assert main(["--env-file", str(profile), "--pilot-10", "--check-config"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["execucao"]["limite"] == 10
    assert report["execucao"]["concorrencia"] == 1
    assert not report["serper"]["habilitado"]
    assert not (tmp_path / "data").exists()


def test_pilot_rejects_limit_eleven_without_requests(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["--pilot-10", "--dry-run", "--limit", "11"]) == 2


@pytest.mark.parametrize("status,expected", [(402, "COTA_ESGOTADA"), (401, "ACESSO_NEGADO"), (403, "ACESSO_NEGADO")])
def test_terminal_provider_errors_block_next_calls_and_restart(db, settings, policy, status, expected):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(status)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            api = JsonAPI("serper", client, QuotaManager(db, {"serper": policy}), db, settings)
            for suffix in ("first", "second"):
                with pytest.raises(ProviderError) as error:
                    await api.request("POST", "https://google.serper.dev/search", payload={"q": suffix})
                assert error.value.code == expected
            restarted = JsonAPI("serper", client, QuotaManager(db, {"serper": policy}), db, settings)
            with pytest.raises(ProviderError) as error:
                await restarted.request("POST", "https://google.serper.dev/search", payload={"q": "third"})
            assert error.value.code == expected
    asyncio.run(run())
    assert len(calls) == db.usage()["serper"]["requisicoes"] == 1


def test_local_total_never_exceeded_by_ten_different_queries(db, settings, policy):
    calls = []
    quota = QuotaManager(db, {"serper": dict(policy, max_requests=2)})

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"organic": []})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            api = Serper(JsonAPI("serper", client, quota, db, settings), "MOCK")
            for i in range(10):
                if i < 2:
                    assert await api.search(str(i)) == []
                else:
                    with pytest.raises(QuotaExceeded):
                        await api.search(str(i))
    asyncio.run(run())
    assert len(calls) == 2


def test_block_reset_requires_verification_and_does_not_reset_usage(tmp_path, db, settings, policy, monkeypatch):
    monkeypatch.chdir(tmp_path)
    QuotaManager(db, {}).block("serper", "COTA_ESGOTADA")
    db.conn.execute("INSERT INTO requests(provider,started_at,outcome) VALUES ('serper',?,'FALHA_HTTP')", (time.time(),))
    args = ["--database", str(tmp_path / "test.sqlite"), "--reset-provider-block", "serper"]
    assert main(args) == 2
    assert db.get_state("provider-block:serper")
    (tmp_path / "provider_verification.json").write_text(json.dumps({"serper": policy}), encoding="utf-8")
    assert main(args) == 0
    assert not db.get_state("provider-block:serper")
    assert db.usage()["serper"]["requisicoes"] == 1


def test_missing_key_with_verified_policy_never_constructs_clients(tmp_path, monkeypatch, policy):
    import app.main as cli
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENABLE_SERPER", "true")
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.setenv("ENABLE_BRASILAPI", "false")
    path = tmp_path / "data.csv"
    path.write_text("CNPJ\n00000000000191", encoding="utf-8")
    (tmp_path / "provider_verification.json").write_text(json.dumps({"serper": policy}), encoding="utf-8")

    def forbidden(*args, **kwargs):
        raise AssertionError("Não deveria construir o cliente Serper")

    monkeypatch.setattr(cli, "Serper", forbidden)
    assert main(["--input", str(path), "--pilot-10"]) == 0
    report = json.loads((tmp_path / "data/output/relatorio_execucao.json").read_text(encoding="utf-8"))
    assert report["status"] == {"BUSCA_NAO_CONFIGURADA": 1}
    assert not report["requisicoes_nesta_execucao"]


def test_importing_integration_modules_never_calls_http(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Importação não pode acessar rede")
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    for module in ("app.providers.base", "app.providers.brasilapi", "app.providers.search"):
        importlib.reload(importlib.import_module(module))


def test_malformed_policies_rejected(tmp_path):
    path = tmp_path / "policies.json"
    for value in ([], {"serper": True}):
        path.write_text(json.dumps(value), encoding="utf-8")
        with pytest.raises(ValueError):
            load_policies(path)


def test_ten_mock_companies_serial_persist_and_export(tmp_path, db, settings, policy):
    configured = pilot_settings(settings)
    imported = seed(tmp_path, db, 10)
    ids = [c for c, _ in imported.selected]
    # Cotas altas só no transporte simulado, para testar concorrência sem pausas reais.
    quota = QuotaManager(db, {"brasilapi": policy, "serper": policy})
    active, peak, calls = {}, {}, []

    async def handler(request):
        name = "brasilapi" if request.url.host == "brasilapi.com.br" else "serper"
        active[name] = active.get(name, 0) + 1
        peak[name] = max(peak.get(name, 0), active[name])
        calls.append(name)
        try:
            await asyncio.sleep(0)
            if name == "brasilapi":
                cnpj = request.url.path.split("/")[-1]
                return httpx.Response(200, json={"cnpj": cnpj, "razao_social": f"Acme {cnpj}"})
            query = json.loads(request.content)["q"]
            cnpj = next(c for c in ids if c in query)
            return httpx.Response(200, json={"organic": [{"link": f"https://acme.com/{cnpj}"}]})
        finally:
            active[name] -= 1

    class Fetcher:
        async def fetch(self, url):
            cnpj = url.split("/")[-1]
            return Page(url, 200, {}, f"<h1>Acme {cnpj}</h1><footer>Acme {cnpj}</footer>")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            enrichment = Enrichment(BrasilAPI(JsonAPI("brasilapi", client, quota, db, configured)))
            finder = WebsiteFinder(Serper(JsonAPI("serper", client, quota, db, configured), "MOCK"),
                                   WebsiteValidator(Fetcher(), db, configured), db, configured)
            processor = Processor(db, enrichment, finder, configured)
            await processor.run(ids)
            assert processor.completed_this_run == 10
    asyncio.run(run())
    assert peak == {"brasilapi": 1, "serper": 1}
    assert calls.count("brasilapi") == calls.count("serper") == 10
    report = make_report(db, ids, imported, quota, 0, "mock", 10)
    assert report["websites_validados_alta_confianca"] == 10
    assert all(db.row(c)["status"] == "CONCLUIDO" for c in ids)
    out = tmp_path / "exports"
    export_all(out, db, ids, imported, report)
    with (out / "resultado_cnpjs.csv").open(encoding="utf-8-sig", newline="") as stream:
        assert len(list(csv.DictReader(stream, delimiter=";"))) == 10
    book = load_workbook(out / "resultado_cnpjs.xlsx")
    assert book["Resultados"].max_row == 11
    book.close()
    reopened = Database(tmp_path / "test.sqlite")
    assert all(reopened.row(c)["confidence"] == "ALTA" for c in ids)
    reopened.close()
