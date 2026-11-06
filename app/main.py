import argparse
import asyncio
import json
import logging
import time
from dataclasses import replace
from pathlib import Path

import httpx

from app.config import Settings, load_policies, policy_reason
from app.providers.base import JsonAPI
from app.providers.brasilapi import BrasilAPI
from app.providers.search import Serper
from app.pilot import pilot_policies, pilot_settings
from app.reports.exporter import export_all, make_report
from app.security import tls_context
from app.services.enrichment import Enrichment
from app.services.importer import import_file
from app.services.processor import Processor
from app.services.site_rules import requires_reclassification
from app.services.quota_manager import QuotaManager
from app.services.validator import SafeFetcher, WebsiteValidator
from app.services.website_finder import WebsiteFinder
from app.storage.database import Database
from app.storage.lock import ProcessLock


def provider_status(settings, policies):
    result = {}
    for name, enabled in (("brasilapi", settings.enable_brasilapi), ("serper", settings.enable_serper)):
        reason = policy_reason(policies.get(name, {}), search=name == "serper")
        if not enabled:
            reason = "desabilitado no ambiente; " + reason
        if name == "serper" and not settings.serper_key:
            reason = "chave ausente; " + reason
        result[name] = {"habilitado": enabled and not reason, "pendencia": reason,
                        "limite_local": policies.get(name, {}).get("max_requests", 0)}
    return result


def approve_stage(db, stage):
    row = db.conn.execute("SELECT report FROM runs WHERE mode='real' AND stage=? AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1",
                          (stage,)).fetchone()
    if not row:
        raise ValueError("Nenhuma execução real dessa etapa disponível para revisão")
    report = json.loads(row[0])
    blocked = {"PENDENTE", "EM_PROCESSAMENTO", "ERRO", "BUSCA_NAO_CONFIGURADA", "COTA_ESGOTADA", "LIMITE_TEMPORARIO"}
    successes = sum(v["sucessos"] for v in report["requisicoes_acumuladas"].values())
    if (report["interrompido"] or report["escopo_selecionado"] < stage
            or report["processados_acumulados"] < stage or not successes
            or any(report["status"].get(s, 0) for s in blocked)):
        raise ValueError("A etapa exige lote completo processado, chamadas bem-sucedidas e ausência de bloqueios/erros")
    db.set_state(f"approved-stage:{stage}", {"at": time.time()})


async def execute(args, settings, policies, db):
    limit = args.limit if args.limit is not None else settings.max_companies
    if not 1 <= limit <= min(1000, settings.max_companies):
        raise ValueError("Limite acima do permitido pela POC/configuração")
    if not args.dry_run:
        if limit > 10 and not db.get_state("approved-stage:10"):
            raise ValueError("Execute e revise a etapa de 10; depois use --approve-stage 10")
        if limit > 100 and not db.get_state("approved-stage:100"):
            raise ValueError("Execute e revise a etapa de 100; depois use --approve-stage 100")
    imported = import_file(args.input, limit, args.cnpj_column)
    db.ingest(imported)
    ids = [cnpj for cnpj, _ in imported.selected]
    if any(requires_reclassification(json.loads(db.row(cnpj)["validation"] or "{}")) for cnpj in ids):
        raise ValueError("Resultados antigos exigem reclassificação offline; use python -m app.reclassify com saída em outra pasta")
    started_at = time.time()
    status = provider_status(settings, policies)
    active_policies = {name: policies[name] for name, state in status.items() if state["habilitado"]}
    active_policies["websites"] = dict(max_requests=settings.website_max_requests, per_minute=settings.website_rpm,
                                       per_hour=settings.website_rpm * 60, per_day=settings.website_max_requests)
    quota = QuotaManager(db, active_policies)
    mode = "dry-run" if args.dry_run else "real"
    stage = 10 if limit <= 10 else 100 if limit <= 100 else 1000
    run_id = db.conn.execute("INSERT INTO runs(started_at,mode,stage) VALUES (?,?,?)", (started_at, mode, stage)).lastrowid
    processor, interrupted = None, False
    try:
        if not args.dry_run:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, verify=tls_context(),
                                         headers={"User-Agent": "DazumCNPJ-POC/0.1"}) as client:
                company_provider = BrasilAPI(JsonAPI("brasilapi", client, quota, db, settings)) if status["brasilapi"]["habilitado"] else None
                search_provider = Serper(JsonAPI("serper", client, quota, db, settings), settings.serper_key) if status["serper"]["habilitado"] else None
                validator = WebsiteValidator(SafeFetcher(quota, settings), db, settings)
                finder = WebsiteFinder(search_provider, validator, db, settings)
                processor = Processor(db, Enrichment(company_provider), finder, settings)
                await processor.run(ids, retry=args.retry)
    except asyncio.CancelledError:
        interrupted = True
        raise
    finally:
        report = make_report(db, ids, imported, quota, started_at, mode,
                             processor.completed_this_run if processor else 0, interrupted)
        report["integracoes"] = status
        report["etapa"] = stage
        out = args.output or Path("data/output") / ("dry-run" if args.dry_run else "")
        export_all(out, db, ids, imported, report)
        db.conn.execute("UPDATE runs SET finished_at=?,report=? WHERE id=?", (time.time(), json.dumps(report), run_id))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"Arquivos exportados em: {out.resolve()}")
    return report


def parser():
    cli = argparse.ArgumentParser(description="POC Dazum: até 1000 CNPJs; orçamento adicional zero")
    cli.add_argument("--input", type=Path, default=Path("data/input/cnpjs.csv"))
    cli.add_argument("--env-file", type=Path, default=Path(".env"), help="Arquivo local de ambiente; variáveis já existentes têm prioridade")
    cli.add_argument("--pilot-10", action="store_true", help="Impõe máximo 10 empresas, concorrência 1 e cotas conservadoras")
    cli.add_argument("--limit", type=int)
    cli.add_argument("--batch-size", type=int)
    cli.add_argument("--cnpj-column")
    cli.add_argument("--dry-run", action="store_true", help="Importa/exporta sem nenhuma chamada externa")
    cli.add_argument("--retry", action="store_true", help="Retoma também falhas e bloqueios; mantém checkpoints e cotas")
    cli.add_argument("--database", type=Path)
    cli.add_argument("--output", type=Path)
    cli.add_argument("--policies", type=Path, default=Path("provider_verification.json"))
    cli.add_argument("--check-config", action="store_true", help="Inspeciona bloqueios sem rede")
    cli.add_argument("--approve-stage", type=int, choices=(10, 100), help="Registra revisão humana da etapa anterior; não faz chamadas")
    cli.add_argument("--reset-provider-block", choices=("brasilapi", "serper"), help="Remove somente bloqueio persistente após revalidação; não zera cotas nem faz chamadas")
    return cli


def main(argv=None):
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env(args.env_file)
        if args.batch_size is not None:
            settings = replace(settings, batch_size=args.batch_size)
        policies = load_policies(args.policies)
        if args.pilot_10:
            settings = pilot_settings(settings)
            policies = pilot_policies(policies)
        if args.check_config:
            state = provider_status(settings, policies)
            state["execucao"] = dict(pilot_10=args.pilot_10, limite=settings.max_companies,
                                     concorrencia=settings.concurrency, lote=settings.batch_size,
                                     tentativas_por_consulta=settings.retries + 1,
                                     pesquisas_por_empresa=settings.queries,
                                     teto_websites=settings.website_max_requests)
            print(json.dumps(state, ensure_ascii=False, indent=2))
            return 0
        database_path = args.database or Path("data/dry-run.sqlite" if args.dry_run else "data/poc.sqlite")
        with ProcessLock(database_path.with_suffix(".run.lock")):
            db = Database(database_path)
            try:
                if args.reset_provider_block:
                    reason = policy_reason(policies.get(args.reset_provider_block, {}), search=args.reset_provider_block == "serper")
                    if reason:
                        raise ValueError("Revalide conta/saldo/permissões antes de remover o bloqueio: " + reason)
                    db.set_state(f"provider-block:{args.reset_provider_block}", None)
                    print("Bloqueio removido; histórico e cotas preservados")
                elif args.approve_stage:
                    approve_stage(db, args.approve_stage)
                    print(f"Revisão da etapa {args.approve_stage} registrada")
                else:
                    asyncio.run(execute(args, settings, policies, db))
            finally:
                db.close()
        return 0
    except KeyboardInterrupt:
        print("Interrompido; checkpoints e relatório parcial preservados")
        return 130
    except (ValueError, OSError) as exc:
        print(f"Falha de configuração/arquivo: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
