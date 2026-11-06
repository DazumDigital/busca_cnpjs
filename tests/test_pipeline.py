import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from openpyxl import load_workbook

from app.main import approve_stage, execute, main
from app.models import Company, ProviderError
from app.providers.base import JsonAPI
from app.providers.brasilapi import BrasilAPI
from app.providers.search import Serper
from app.reports.exporter import export_all, make_report
from app.services.enrichment import Enrichment
from app.services.importer import import_file
from app.services.processor import Processor, batches
from app.services.quota_manager import QuotaManager
from app.services.validator import Page, WebsiteValidator
from app.services.website_finder import WebsiteFinder
from app.storage.database import Database
from app.storage.lock import ProcessLock
from conftest import generated_cnpj


def seed(tmp_path, db, count=1):
    path = tmp_path / "data.csv"
    path.write_text("CNPJ\n" + "\n".join(generated_cnpj(i) for i in range(1, count + 1)), encoding="utf-8")
    result = import_file(path)
    db.ingest(result)
    return result


def test_batches():
    assert list(batches(list(range(105)), 50)) == [list(range(50)), list(range(50, 100)), list(range(100, 105))]


def test_persistence_and_audit_on_reimport(tmp_path, db):
    imported = seed(tmp_path, db)
    cnpj = imported.selected[0][0]
    db.update(cnpj, company=asdict(Company(cnpj, "Acme")), status="CONCLUIDO")
    db.ingest(imported)
    assert db.row(cnpj)["status"] == "CONCLUIDO"
    assert db.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 2
    reopened = Database(tmp_path / "test.sqlite")
    assert json.loads(reopened.row(cnpj)["company"])["legal_name"] == "Acme"
    reopened.close()


def test_database_hard_cap(tmp_path, db):
    seed(tmp_path, db, 1000)
    path = tmp_path / "extra.csv"
    path.write_text("CNPJ\n" + generated_cnpj(1001), encoding="utf-8")
    with pytest.raises(ValueError, match="1000"):
        db.ingest(import_file(path))
    assert db.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1000


def test_mock_full_flow_and_resume_without_repeat(tmp_path, db, settings, policy):
    imported = seed(tmp_path, db, 2)
    ids = [c for c, _ in imported.selected]
    quota = QuotaManager(db, {"brasilapi": policy, "serper": policy})
    calls, pages = [], []

    def handler(request):
        calls.append(str(request.url))
        if request.url.host == "brasilapi.com.br":
            cnpj = request.url.path.split("/")[-1]
            return httpx.Response(200, json={"cnpj": cnpj, "razao_social": f"Acme {cnpj}", "municipio": "São Paulo", "uf": "SP"})
        cnpj = next(c for c in ids if c in json.loads(request.content)["q"])
        return httpx.Response(200, json={"organic": [{"link": f"https://acme.com/{cnpj}"}]})

    class Fetcher:
        async def fetch(self, url):
            pages.append(url)
            cnpj = url.split("/")[-1]
            return Page(url, 200, {}, f"<h1>Acme {cnpj}</h1><footer>Acme {cnpj} São Paulo SP</footer>")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            enrichment = Enrichment(BrasilAPI(JsonAPI("brasilapi", client, quota, db, settings)))
            finder = WebsiteFinder(Serper(JsonAPI("serper", client, quota, db, settings), "MOCK"),
                                   WebsiteValidator(Fetcher(), db, settings), db, settings)
            processor = Processor(db, enrichment, finder, settings)
            await processor.run(ids)
            assert processor.completed_this_run == 2
            before = len(calls)
            await Processor(db, enrichment, finder, settings).run(ids)
            assert len(calls) == before == 4
            assert len(pages) == 2
            assert all(db.row(c)["confidence"] == "ALTA" for c in ids)
    asyncio.run(run())
    report = make_report(db, ids, imported, quota, 0, "mock", 2)
    assert report["websites_validados_alta_confianca"] == 2
    assert report["taxa_cobertura"] == 1
    out = tmp_path / "export"
    export_all(out, db, ids, imported, report)
    assert json.loads((out / "relatorio_execucao.json").read_text(encoding="utf-8"))["modo"] == "mock"
    book = load_workbook(out / "resultado_cnpjs.xlsx")
    assert book["Resultados"]["A2"].value == ids[0]
    assert book["Resultados"]["A2"].data_type == "s"
    book.close()


def test_resume_after_interruption_preserves_company(tmp_path, db, settings):
    imported = seed(tmp_path, db)
    cnpj = imported.selected[0][0]
    db.update(cnpj, status="EM_PROCESSAMENTO", company=asdict(Company(cnpj, "Acme")), attempts=1)

    class NoCompanyCalls:
        async def get(self, cnpj): raise AssertionError("Não deve consultar de novo")

    finder = WebsiteFinder(None, None, db, settings)
    processor = Processor(db, NoCompanyCalls(), finder, settings)
    asyncio.run(processor.run([cnpj]))
    assert db.row(cnpj)["status"] == "BUSCA_NAO_CONFIGURADA"
    assert db.row(cnpj)["attempts"] == 2
    assert json.loads(db.row(cnpj)["company"])["legal_name"] == "Acme"


def test_cli_dry_run_is_offline_and_separate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    input_file = tmp_path / "data.csv"
    input_file.write_text("CNPJ\n00000000000191\ninvalid\n00000000000191", encoding="utf-8")
    monkeypatch.setenv("ENABLE_BRASILAPI", "true")
    monkeypatch.setenv("ENABLE_SERPER", "true")
    assert main(["--dry-run", "--input", str(input_file), "--limit", "1000"]) == 0
    assert not (tmp_path / "data/poc.sqlite").exists()
    report = json.loads((tmp_path / "data/output/dry-run/relatorio_execucao.json").read_text(encoding="utf-8"))
    assert report["requisicoes_nesta_execucao"] == {}
    assert report["processados_nesta_execucao"] == 0
    assert report["status"] == {"PENDENTE": 1}
    assert report["custo_adicional_efetivo_usd"] == 0


def test_real_with_unverified_providers_is_offline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    file = tmp_path / "data.csv"
    file.write_text("CNPJ\n00000000000191", encoding="utf-8")
    assert main(["--input", str(file), "--limit", "10"]) == 0
    report = json.loads((tmp_path / "data/output/relatorio_execucao.json").read_text(encoding="utf-8"))
    assert report["status"] == {"BUSCA_NAO_CONFIGURADA": 1}
    assert not report["requisicoes_nesta_execucao"]


def test_large_stage_blocked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["--limit", "1000"]) == 2
    db = Database(tmp_path / "data/poc.sqlite")
    with pytest.raises(ValueError):
        approve_stage(db, 10)
    db.close()


def test_formula_injection_sanitized(tmp_path, db, settings):
    from app.reports.exporter import safe_cell
    assert safe_cell(" =HYPERLINK(\"x\")").startswith("'")
    assert safe_cell("+cmd").startswith("'")
    assert safe_cell("normal") == "normal"
    imported = seed(tmp_path, db)
    cnpj = imported.selected[0][0]
    db.update(cnpj, company=asdict(Company(cnpj, '=HYPERLINK("x")')))
    quota = QuotaManager(db, {})
    report = make_report(db, [cnpj], imported, quota, 0, "mock", 0)
    export_all(tmp_path, db, [cnpj], imported, report)
    book = load_workbook(tmp_path / "resultado_cnpjs.xlsx")
    assert book["Resultados"]["C2"].data_type == "s"
    assert book["Resultados"]["C2"].value.startswith("'")
    book.close()


def test_process_lock(tmp_path):
    path = tmp_path / "process.lock"
    with ProcessLock(path):
        with pytest.raises(ValueError):
            with ProcessLock(path):
                pass
    with ProcessLock(path):
        pass
