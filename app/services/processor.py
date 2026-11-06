import asyncio
import json
import logging

from app.models import Company, ProviderError
from app.services.site_rules import requires_reclassification
from app.services.site_outcome import public_validation, result_status

logger = logging.getLogger(__name__)


def batches(values, size):
    if size < 1:
        raise ValueError("Lote precisa ser positivo")
    for start in range(0, len(values), size):
        yield values[start:start + size]


class Processor:
    def __init__(self, db, enrichment, finder, settings):
        self.db, self.enrichment, self.finder, self.settings = db, enrichment, finder, settings
        self.semaphore = asyncio.Semaphore(settings.concurrency)
        self.completed_this_run = 0

    async def process_one(self, cnpj):
        async with self.semaphore:
            row = self.db.row(cnpj)
            errors = []
            self.db.update(cnpj, status="EM_PROCESSAMENTO", error="", attempts=row["attempts"] + 1)
            try:
                company = Company(**json.loads(row["company"])) if row["company"] else Company(cnpj)
                source = row["source"]
                if not (company.legal_name or company.trade_name) and "PLANILHA_LOCAL" not in source.split(";"):
                    try:
                        fetched = await self.enrichment.get(cnpj)
                        company = self.db.save_company(cnpj, fetched, "brasilapi")
                        source = self.db.row(cnpj)["source"]
                    except ProviderError as exc:
                        errors.append(exc.code)
                saved = json.loads(row["candidates"]) if row["candidates"] else None
                result, candidates, validation_errors = await self.finder.find(company, saved)
                errors.extend(validation_errors)
                result = public_validation(result)
                status = result_status(result)
                sources = [v for v in (source, "serper" if candidates else "", "website_publico" if result.accessible else "") if v]
                self.db.update(cnpj, validation=result.to_dict(), candidates=candidates,
                               confidence=result.confidence, status=status,
                               source=";".join(dict.fromkeys(";".join(sources).split(";"))),
                               error=";".join(errors))
            except ProviderError as exc:
                status = exc.code if exc.code in {"BUSCA_NAO_CONFIGURADA", "COTA_ESGOTADA", "LIMITE_TEMPORARIO"} else "ERRO"
                partial = json.loads(self.db.row(cnpj)["validation"] or "{}")
                self.db.update(cnpj, status=status, confidence=partial.get("confidence", "ERRO" if status == "ERRO" else "PENDENTE"),
                               error=";".join(errors + [exc.code]))
            except asyncio.CancelledError:
                self.db.update(cnpj, status="PENDENTE", error="INTERROMPIDO")
                raise
            except Exception as exc:
                # Não persistir repr/str de exceções desconhecidas: podem conter tokens ou dados brutos.
                self.db.update(cnpj, status="ERRO", confidence="ERRO", error=type(exc).__name__)
                logger.error("Falha interna do tipo %s", type(exc).__name__)
            self.completed_this_run += 1

    async def run(self, ids, retry=False):
        if len(ids) > self.settings.max_companies or len(ids) > 1000:
            raise ValueError("Limite da POC excedido")
        if any(requires_reclassification(json.loads(self.db.row(cnpj)["validation"] or "{}")) for cnpj in ids):
            raise ValueError("Resultados usam regras antigas; execute python -m app.reclassify em uma cópia antes de retomar")
        # Executar sob ProcessLock. Recupera checkpoints de uma interrupção abrupta.
        for cnpj in ids:
            row = self.db.row(cnpj)
            if row["status"] == "EM_PROCESSAMENTO" or (retry and row["status"] not in {"CONCLUIDO", "PENDENTE"}):
                self.db.update(cnpj, status="PENDENTE")
        pending = [cnpj for cnpj in ids if self.db.row(cnpj)["status"] == "PENDENTE"]
        for batch in batches(pending, self.settings.batch_size):
            await asyncio.gather(*(self.process_one(cnpj) for cnpj in batch))
            logger.info("Checkpoint: %d registros nesta execução", self.completed_this_run)
