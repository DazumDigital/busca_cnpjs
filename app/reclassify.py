"""Reclassificação estritamente local: não carrega .env e não constrói clientes HTTP."""
import argparse
import json
import sqlite3
import time
from collections import Counter
from contextlib import closing
from pathlib import Path

from app.models import Company, Validation
from app.reports.exporter import export_all, timestamp, write_csv
from app.services.importer import ImportResult
from app.services.site_rules import RULES_VERSION, candidate_priority, domain_brand_match, rank_candidates, url_reason
from app.services.validator import safe_url, validation_cache_key
from app.services.site_outcome import official_website
from app.storage.database import Database


def reclassify(source, output, limit=10):
    source, output = Path(source).resolve(), Path(output).resolve()
    if not 1 <= limit <= 10:
        raise ValueError("Reclassificação limitada aos dez CNPJs da POC")
    target = output / "reclassificacao.sqlite"
    if source == target or target.exists():
        raise ValueError("Use uma pasta de saída nova; o banco original e reclassificações anteriores não serão sobrescritos")
    if output.exists() and any(output.iterdir()):
        raise ValueError("A pasta de saída deve estar vazia para preservar os arquivos existentes")
    if not source.is_file():
        raise ValueError("Banco de origem não encontrado")
    output.mkdir(parents=True, exist_ok=True)
    # Backup consistente, inclusive se houver WAL; a conexão de origem é somente leitura.
    with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(target)) as copy:
            original.backup(copy)
    db = Database(target)
    started = time.time()
    changes, audits = [], []
    try:
        db.conn.executescript("""
            CREATE TABLE IF NOT EXISTS classification_history (
              id INTEGER PRIMARY KEY, cnpj TEXT, previous_record TEXT, rules_version TEXT, reviewed_at REAL);
            CREATE TABLE IF NOT EXISTS candidate_reviews (
              cnpj TEXT, url TEXT, decision TEXT, reason TEXT, priority INTEGER,
              rules_version TEXT, reviewed_at REAL);
        """)
        originals = [dict(r) for r in db.conn.execute("SELECT * FROM companies ORDER BY position LIMIT ?", (limit,))]
        ids = [r["cnpj"] for r in originals]
        usage_before = db.usage()
        for row in originals:
            company = Company(**json.loads(row["company"] or json.dumps({"cnpj": row["cnpj"]})))
            previous = json.loads(row["validation"] or "{}")
            candidates = list(dict.fromkeys(json.loads(row["candidates"] or "[]") + ([previous["website"]] if previous.get("website") else [])))
            eligible = []
            for url in candidates:
                try:
                    clean, _, _ = safe_url(url)
                    reason = url_reason(clean, company.cnpj)
                except ValueError:
                    reason = "URL_INSEGURA"
                audit = dict(CNPJ=company.cnpj, URL=url, Decisao="REJEITADO" if reason else "CANDIDATO_PENDENTE",
                             Motivo=reason or "Vínculo exige evidência local da versão atual", Prioridade=0 if reason else candidate_priority(company, clean))
                audits.append(audit)
                if not reason:
                    eligible.append(clean)
            eligible = rank_candidates(company, eligible)
            best = None
            for url in eligible:
                cached = db.cache_get("validation", validation_cache_key(company, url))
                if cached and cached.get("rules_version") == RULES_VERSION:
                    # Não reutilizar score/ALTA sem os novos sinais estruturados.
                    if official_website(cached) == url:
                        best = Validation(**cached)
                        break
            if best:
                status, priority_url = "CONCLUIDO", best.website
            elif eligible:
                priority_url = eligible[0]
                best = Validation(confidence="PENDENTE", rules_version=RULES_VERSION, site_type="CORPORATE_PENDING",
                                  evidence=["Candidato corporativo priorizado: " + priority_url,
                                            "Cache antigo contém apenas score; não comprova titularidade",
                                            "Conteúdo público/termos não foram armazenados; nenhuma nova consulta foi feita"],
                                  signals={"candidate_url": priority_url,
                                           "brand_domain": domain_brand_match(company, safe_url(priority_url)[1])})
                status = "PENDENTE_VALIDACAO_SITE_OFICIAL"
            else:
                priority_url = ""
                best = Validation(rules_version=RULES_VERSION, site_type="THIRD_PARTY_ONLY",
                                  evidence=["SEM_SITE_OFICIAL_CONFIRMADO: candidatos de terceiros, inscrições divergentes ou documentos"])
                status = "SEM_SITE_OFICIAL_CONFIRMADO"
            with db.transaction():
                db.conn.execute("INSERT INTO classification_history(cnpj,previous_record,rules_version,reviewed_at) VALUES (?,?,?,?)",
                                (company.cnpj, json.dumps(row, ensure_ascii=False), RULES_VERSION, started))
                for a in [a for a in audits if a["CNPJ"] == company.cnpj]:
                    db.conn.execute("INSERT INTO candidate_reviews VALUES (?,?,?,?,?,?,?)",
                                    (company.cnpj, a["URL"], a["Decisao"], a["Motivo"], a["Prioridade"], RULES_VERSION, started))
                db.update(company.cnpj, validation=best.to_dict(), confidence=best.confidence, status=status, error="")
            changes.append(dict(CNPJ=company.cnpj, Empresa=company.legal_name or company.trade_name,
                                Website_anterior=previous.get("website", ""), Confianca_anterior=row["confidence"],
                                Website_reclassificado=best.website, Confianca_reclassificada=best.confidence,
                                Status=status, Candidato_prioritario=priority_url,
                                Candidatos_rejeitados=sum(a["Decisao"] == "REJEITADO" for a in audits if a["CNPJ"] == company.cnpj)))
        assert db.usage() == usage_before, "Reclassificar não pode alterar contadores"
        counts = dict(importados=len(ids), validos=len(ids), invalidos=0, duplicados=0, fora_limite=0, selecionados=len(ids))
        last_import = db.conn.execute("SELECT MAX(id) FROM imports").fetchone()[0]
        audit = [dict(r) for r in db.conn.execute("""SELECT line AS linha,original,cnpj,kind AS classificacao
                                                   FROM audit WHERE import_id=? ORDER BY line""", (last_import,))]
        imported = ImportResult([(r["cnpj"], r["original"]) for r in originals], audit, counts)
        rows = db.rows(ids)
        report = dict(modo="RECLASSIFICACAO_OFFLINE", inicio_utc=timestamp(started), fim_utc=timestamp(time.time()),
                      banco_original=str(source), banco_copia=str(target), regras=RULES_VERSION, empresas=len(ids),
                      alta_anteriores=sum(r["confidence"] == "ALTA" for r in originals),
                      alta_atuais=sum(r["confidence"] == "ALTA" for r in rows),
                      status=dict(Counter(r["status"] for r in rows)),
                      requisicoes_novas=0, requisicoes_anteriores_preservadas=usage_before,
                      evidencias_legadas_reutilizadas_como_confirmacao=False,
                      mudancas=changes, auditoria_candidatos=audits)
        export_all(output, db, ids, imported, report)
        if changes:
            write_csv(output / "comparativo_reclassificacao.csv", changes, list(changes[0]))
        if audits:
            write_csv(output / "auditoria_candidatos.csv", audits, list(audits[0]))
        return report
    finally:
        db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Reclassifica candidatos existentes sem rede, credenciais ou consumo de cotas")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="Pasta nova; banco de origem nunca é sobrescrito")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)
    try:
        report = reclassify(args.database, args.output, args.limit)
        print(json.dumps({k: v for k, v in report.items() if k not in {"mudancas", "auditoria_candidatos"}}, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f"Reclassificação não concluída: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
