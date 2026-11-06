import asyncio
import csv
import json
import time
from dataclasses import asdict, replace

import pytest
from openpyxl import Workbook, load_workbook

from app.config import Settings
from app.main import main
from app.models import Company, Validation
from app.reports.exporter import export_all, make_report
from app.services.importer import ImportResult, import_file
from app.services.processor import Processor
from app.services.quota_manager import QuotaManager
from app.services.validator import Page, WebsiteValidator, classify
from app.services.website_finder import WebsiteFinder, queries_for
from app.storage.database import Database
from conftest import generated_cnpj


def local_csv(tmp_path, name="companies.csv", rows=None, headers=None):
    path = tmp_path / name
    headers = headers or ["CNPJ", "Razão social", "Nome fantasia", "Município", "UF", "Situação cadastral"]
    rows = rows if rows is not None else [[generated_cnpj(1), "Acme Digital Ltda", "Acme Digital", "São Paulo", "sp", "ATIVA"]]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(headers)
        writer.writerows(rows)
    return path


def test_csv_additional_columns_are_imported(tmp_path):
    result = import_file(local_csv(tmp_path), 10)
    company = result.companies[generated_cnpj(1)]
    assert company == Company(generated_cnpj(1), "Acme Digital Ltda", "Acme Digital", "São Paulo", "SP", "ATIVA")
    assert result.business_columns["legal_name"] == "Razão social"
    assert result.selected == [(generated_cnpj(1), generated_cnpj(1))]


@pytest.mark.parametrize("header", ["razao_social", "RAZÃO SOCIAL", "Nome Empresarial", "Razão Social / Nome Empresarial"])
def test_header_variations(tmp_path, header):
    path = local_csv(tmp_path, headers=["CNPJ", header, "fantasia", "cidade", "Estado", "situação"],
                     rows=[[generated_cnpj(1), "Empresa", "Marca", "Recife", "pe", "ATIVA"]])
    company = import_file(path).companies[generated_cnpj(1)]
    assert company.legal_name == "Empresa"
    assert company.trade_name == "Marca"
    assert company.city == "Recife"
    assert company.state == "PE"
    assert company.registration_status == "ATIVA"


def test_xlsx_business_data_and_numeric_cnpj_zeros(tmp_path):
    path = tmp_path / "companies.xlsx"
    book = Workbook()
    book.active.append(["CNPJ", "Razão social", "nome_fantasia", "Município", "UF", "situação cadastral"])
    book.active.append([191, "Empresa Local", "Marca Local", "Curitiba", "pr", "ATIVA"])
    book.save(path)
    book.close()
    imported = import_file(path, 10)
    company = imported.companies["00000000000191"]
    assert imported.selected == [("00000000000191", "191")]
    assert company.legal_name == "Empresa Local"
    assert company.state == "PR"


def test_xlsx_business_formulas_are_not_used_as_names(tmp_path):
    path = tmp_path / "formulas.xlsx"
    book = Workbook()
    book.active.append(["CNPJ", "Razão social", "Nome fantasia"])
    book.active.append([191, '=HYPERLINK("https://invalid")', "Marca Local"])
    book.save(path)
    book.close()
    company = import_file(path).companies["00000000000191"]
    assert company.legal_name == ""
    assert company.trade_name == "Marca Local"


def test_cnpj_only_and_old_import_result_are_compatible(tmp_path, db):
    path = local_csv(tmp_path, headers=["CNPJ"], rows=[[generated_cnpj(1)]])
    result = import_file(path)
    assert result.companies == result.business_columns == {}
    old_result = ImportResult(result.selected, result.audit, result.counts)
    db.ingest(old_result)
    assert not db.row(generated_cnpj(1))["company"]
    assert db.company_sources(generated_cnpj(1)) == {}


def test_duplicates_invalids_and_pilot_limit_preserve_first_row(tmp_path):
    rows = [[generated_cnpj(i), f"Empresa {i}"] for i in range(1, 12)]
    rows.insert(1, [generated_cnpj(1), "Não substituir pelo duplicado"])
    rows.insert(0, ["invalid", "Ignorar"])
    result = import_file(local_csv(tmp_path, headers=["CNPJ", "Razão social"], rows=rows), 10)
    assert len(result.selected) == len(result.companies) == 10
    assert result.companies[generated_cnpj(1)].legal_name == "Empresa 1"
    assert generated_cnpj(11) not in result.companies
    assert result.counts["duplicados"] == result.counts["invalidos"] == result.counts["fora_limite"] == 1


def test_ambiguous_business_headers_rejected(tmp_path):
    path = local_csv(tmp_path, headers=["CNPJ", "Razão social", "Nome empresarial"],
                     rows=[[generated_cnpj(1), "Empresa A", "Empresa B"]])
    with pytest.raises(ValueError, match="ambíguas"):
        import_file(path)


def test_local_fields_persist_with_per_field_sources_and_export(tmp_path, db):
    imported = import_file(local_csv(tmp_path), 10)
    db.ingest(imported)
    cnpj = generated_cnpj(1)
    row = db.row(cnpj)
    assert row["source"] == "PLANILHA_LOCAL"
    assert row["confidence"] == row["status"] == "PENDENTE"
    assert db.company_sources(cnpj) == dict.fromkeys(
        ["legal_name", "trade_name", "city", "state", "registration_status"], "PLANILHA_LOCAL")
    reopened = Database(tmp_path / "test.sqlite")
    assert json.loads(reopened.row(cnpj)["company"])["legal_name"] == "Acme Digital Ltda"
    reopened.close()
    quota = QuotaManager(db, {})
    report = make_report(db, [cnpj], imported, quota, time.time(), "dry-run", 0)
    assert report["empresas_com_dados_da_planilha"] == 1
    assert report["websites_validados_alta_confianca"] == 0
    out = tmp_path / "export"
    export_all(out, db, [cnpj], imported, report)
    with (out / "resultado_cnpjs.csv").open(encoding="utf-8-sig", newline="") as stream:
        exported = next(csv.DictReader(stream, delimiter=";"))
    assert exported["Razão social"] == "Acme Digital Ltda"
    assert exported["Nome fantasia"] == "Acme Digital"
    assert exported["Município"] == "São Paulo"
    assert exported["Situação cadastral"] == "ATIVA"
    assert exported["Fonte"] == "PLANILHA_LOCAL"
    assert not exported["Website"]
    book = load_workbook(out / "resultado_cnpjs.xlsx")
    assert book["Resultados"]["C2"].value == "Acme Digital Ltda"
    assert book["Resultados"]["A2"].value == cnpj
    assert book["Resultados"]["A2"].data_type == "s"
    book.close()


def test_reimport_blanks_and_identical_values_preserve_data_and_result(tmp_path, db):
    path = local_csv(tmp_path)
    db.ingest(import_file(path))
    cnpj = generated_cnpj(1)
    db.update(cnpj, validation=Validation("https://acme.com/", "acme.com", "ALTA", 90, ["mock"], True, True).to_dict(),
              confidence="ALTA", status="CONCLUIDO", attempts=1)
    blank = local_csv(tmp_path, name="blank.csv", rows=[[cnpj, "", None, "   ", "", "N/A"]])
    db.ingest(import_file(blank))
    db.ingest(import_file(path))
    row = db.row(cnpj)
    company = json.loads(row["company"])
    assert company["legal_name"] == "Acme Digital Ltda"
    assert company["registration_status"] == "ATIVA"
    assert row["status"] == "CONCLUIDO"
    assert row["confidence"] == "ALTA"
    assert row["attempts"] == 1


def test_changed_identity_requires_new_validation_without_resetting_usage(tmp_path, db):
    db.ingest(import_file(local_csv(tmp_path)))
    cnpj = generated_cnpj(1)
    db.update(cnpj, validation=Validation("https://acme.com/", "acme.com", "ALTA", 90, [], True, True).to_dict(),
              confidence="ALTA", status="CONCLUIDO", attempts=2)
    db.conn.execute("INSERT INTO requests(provider,started_at,outcome) VALUES ('serper',?,'SUCESSO')", (time.time(),))
    new = local_csv(tmp_path, name="new.csv", headers=["CNPJ", "Razão social"], rows=[[cnpj, "Nova Empresa"]])
    db.ingest(import_file(new))
    row = db.row(cnpj)
    assert row["status"] == row["confidence"] == "PENDENTE"
    assert row["validation"] is None
    assert json.loads(row["candidates"]) == ["https://acme.com/"]
    assert json.loads(row["company"])["trade_name"] == "Acme Digital"
    assert row["attempts"] == 2
    assert db.usage()["serper"]["requisicoes"] == 1


def test_additive_migration_preserves_legacy_rows_requests_and_state(tmp_path, db):
    imported = import_file(local_csv(tmp_path, headers=["CNPJ"], rows=[[generated_cnpj(1)]]))
    db.ingest(imported)
    cnpj = generated_cnpj(1)
    db.update(cnpj, company=asdict(Company(cnpj, "Legado")), source="brasilapi", status="CONCLUIDO", attempts=3)
    db.conn.execute("DROP TABLE company_field_sources")
    db.conn.execute("INSERT INTO requests(provider,started_at,outcome) VALUES ('serper',?,'SUCESSO')", (time.time(),))
    db.set_state("approved-stage:10", True)
    previous = dict(db.row(cnpj))
    migrated = Database(tmp_path / "test.sqlite")
    assert dict(migrated.row(cnpj)) == previous
    assert migrated.usage()["serper"]["requisicoes"] == 1
    assert migrated.get_state("approved-stage:10") is True
    assert migrated.company_sources(cnpj) == {}
    migrated.close()


def test_queries_use_trade_legal_and_location_before_cnpj():
    queries = queries_for(Company(generated_cnpj(1), "Razão Empresarial Ltda", "Marca Local", "Recife", "PE"))
    assert queries[0].startswith('"Marca Local" Recife PE site oficial')
    assert 'Razão Empresarial' in queries[1] and 'Recife' in queries[1]
    assert len(queries) == 2 and all(generated_cnpj(1) not in q for q in queries)
    assert queries_for(Company(generated_cnpj(1)))[0].startswith(f'"{generated_cnpj(1)}" site oficial')


def test_legal_only_and_identical_names_do_not_waste_alternative():
    only = Company(generated_cnpj(1), "Acme", city="Recife")
    same = Company(generated_cnpj(1), "Acme", "ACME", "Recife")
    for company in (only, same):
        queries = queries_for(company)
        assert len(queries) == 2
        assert company.cnpj not in queries[-1]


def test_finder_uses_max_two_queries_even_when_settings_allow_three(tmp_path, db, settings):
    imported = import_file(local_csv(tmp_path))
    db.ingest(imported)
    company = imported.companies[generated_cnpj(1)]

    class Search:
        calls = []
        async def search(self, query):
            self.calls.append(query)
            return []

    search = Search()
    finder = WebsiteFinder(search, None, db, replace(settings, queries=3))
    result, candidates, errors = asyncio.run(finder.find(company))
    assert search.calls == queries_for(company)[:2]
    assert len(search.calls) == 2
    assert result.confidence == "NAO_ENCONTRADO"


@pytest.mark.parametrize("trade_only", [False, True])
def test_local_data_skips_business_api_and_strong_evidence_stops_search(tmp_path, db, settings, trade_only):
    cnpj = generated_cnpj(1)
    path = local_csv(tmp_path, rows=[[cnpj, "" if trade_only else "Acme Digital Ltda", "Acme Digital", "São Paulo", "SP", "ATIVA"]])
    db.ingest(import_file(path))

    class ForbiddenEnrichment:
        async def get(self, cnpj):
            raise AssertionError("BrasilAPI não pode ser chamada para dados da planilha")

    class Search:
        calls = []
        async def search(self, query):
            self.calls.append(query)
            return ["https://acme.com/"]

    class Fetcher:
        async def fetch(self, url):
            return Page(url, 200, {}, f"<h1>Acme Digital</h1><footer>Acme Digital {cnpj} São Paulo SP</footer>")

    search = Search()
    finder = WebsiteFinder(search, WebsiteValidator(Fetcher(), db, settings), db, settings)
    asyncio.run(Processor(db, ForbiddenEnrichment(), finder, settings).run([cnpj]))
    assert len(search.calls) == 1
    assert "Acme Digital" in search.calls[0] and "São Paulo SP" in search.calls[0]
    assert db.row(cnpj)["confidence"] == "ALTA"
    assert "PLANILHA_LOCAL" in db.row(cnpj)["source"]
    assert "brasilapi" not in db.row(cnpj)["source"]


def test_sheet_identity_alone_never_confirms_a_website(tmp_path):
    company = next(iter(import_file(local_csv(tmp_path)).companies.values()))
    result = classify(company, Page("https://acme.com/", 200, {}, "Outra empresa sem identificação"))
    assert not result.confirmed
    assert result.confidence == "BAIXA"


def test_resume_keeps_local_data_and_never_calls_business_provider(tmp_path, db, settings):
    imported = import_file(local_csv(tmp_path))
    db.ingest(imported)
    cnpj = generated_cnpj(1)

    class ForbiddenEnrichment:
        async def get(self, cnpj):
            raise AssertionError("Dados locais não podem provocar consulta cadastral")

    class InterruptedFinder:
        async def find(self, *args):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(Processor(db, ForbiddenEnrichment(), InterruptedFinder(), settings).run([cnpj]))
    assert db.row(cnpj)["status"] == "PENDENTE"

    class Finder:
        async def find(self, company, saved):
            assert company.trade_name == "Acme Digital"
            return Validation(), [], []

    asyncio.run(Processor(db, ForbiddenEnrichment(), Finder(), settings).run([cnpj]))
    assert db.row(cnpj)["status"] == "SEM_SITE_OFICIAL_CONFIRMADO"
    assert db.row(cnpj)["attempts"] == 2
    assert db.company_sources(cnpj)["trade_name"] == "PLANILHA_LOCAL"


def test_dry_run_with_business_data_is_offline_and_identified_as_local(tmp_path, monkeypatch):
    import app.main as cli
    path = local_csv(tmp_path)
    monkeypatch.chdir(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("Dry-run não pode construir cliente HTTP")

    monkeypatch.setattr(cli.httpx, "AsyncClient", forbidden)
    assert main(["--input", str(path), "--dry-run", "--pilot-10"]) == 0
    report = json.loads((tmp_path / "data/output/dry-run/relatorio_execucao.json").read_text(encoding="utf-8"))
    assert report["modo"] == "dry-run"
    assert report["empresas_com_dados_da_planilha"] == 1
    assert report["empresas_identificadas"] == 1
    assert report["requisicoes_nesta_execucao"] == {}
    assert report["websites_encontrados"] == 0
    assert report["status"] == {"PENDENTE": 1}
