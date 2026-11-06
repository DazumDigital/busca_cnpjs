from app.models import ProviderError, QuotaExceeded, UnsafeURL, Validation
from app.services.validator import safe_url
from app.services.site_rules import RULES_VERSION, candidate_kind, candidate_priority, rank_candidates, url_reason
from app.services.search_queries import queries_for
from app.services.site_outcome import public_validation


def result_quality(result):
    level = {"ALTA": 3, "MEDIA": 2, "BAIXA": 1}.get(result.confidence, 0)
    return level, result.score


class WebsiteFinder:
    def __init__(self, search, validator, db, settings):
        self.search, self.validator, self.db, self.settings = search, validator, db, settings

    async def find(self, company, saved_candidates=None):
        if self.search is None and not saved_candidates:
            raise ProviderError("BUSCA_NAO_CONFIGURADA")
        candidates = list(saved_candidates or [])
        best, errors, attempted = Validation(), [], set()
        rejected_urls = {}
        audit = {}

        def record(url, **details):
            audit[url] = dict(details, source_kind=candidate_kind(url), decision_scope='WEBSITE_OFICIAL', rules_version=RULES_VERSION)
            self.db.set_state("discovery_audit:" + company.cnpj, audit)
        successful_validations = 0

        async def evaluate():
            nonlocal best, successful_validations
            eligible = []
            for url in candidates:
                try:
                    normalized, _, _ = safe_url(url)
                except UnsafeURL:
                    rejected_urls[url] = "URL_INSEGURA"
                    record(url, decision="REJECTED", reason="URL_INSEGURA")
                    continue
                reason = url_reason(normalized, company.cnpj)
                if reason:
                    rejected_urls[url] = reason
                    record(url, decision="REJECTED", reason=reason)
                else:
                    eligible.append(normalized)
            for url in rank_candidates(company, eligible):
                if len(attempted) >= self.settings.candidates:
                    break
                if url in attempted:
                    continue
                attempted.add(url)
                record(url, decision="VALIDATING", priority=candidate_priority(company, url))
                try:
                    result = await self.validator.validate(company, url)
                    successful_validations += 1
                except QuotaExceeded:
                    record(url, decision="ERROR", reason="COTA_ESGOTADA")
                    raise
                except (UnsafeURL, ProviderError) as exc:
                    errors.append(exc.code if isinstance(exc, ProviderError) else "URL_INSEGURA")
                    record(url, decision="ERROR", reason=errors[-1],
                           details=exc.details if isinstance(exc,ProviderError) else {})
                    continue
                record(url, decision="VALIDATED", confidence=result.confidence, site_type=result.site_type,
                       confirmed=result.confirmed, priority=candidate_priority(company, url))
                if result.site_type in {"THIRD_PARTY", "CNPJ_DIVERGENTE"}:
                    rejected_urls[url] = "; ".join(result.evidence)
                    continue
                if result.website and result_quality(result) > result_quality(best):
                    best = result
                    public = public_validation(best)
                    self.db.update(company.cnpj, validation=public.to_dict(), confidence=public.confidence)
                if best.confirmed:
                    break

        await evaluate()
        if best.confirmed or best.confidence == "MEDIA":
            return best, candidates, errors
        if self.search is not None and len(attempted) < self.settings.candidates:
            for query in queries_for(company)[:min(self.settings.queries, 2)]:
                urls = await self.search.search(query)
                for url in urls:
                    try:
                        clean, host, _ = safe_url(url)
                    except UnsafeURL:
                        continue
                    if clean not in candidates:
                        candidates.append(clean)
                # Checkpoint antes de visitar os sites. Armazena só links permitidos pela política.
                previous = self.db.row(company.cnpj)["source"]
                source = ";".join(dict.fromkeys(v for v in (previous + ";serper").split(";") if v))
                self.db.update(company.cnpj, candidates=candidates, source=source)
                await evaluate()
                if best.confirmed or best.confidence == "MEDIA" or len(attempted) >= self.settings.candidates:
                    break
        eligible_unconfirmed = [u for u in candidates if u not in rejected_urls and not url_reason(u, company.cnpj)]
        failed = [u for u in eligible_unconfirmed if audit.get(u,{}).get('decision') == 'ERROR']
        if not best.website and failed:
            return Validation(confidence="PENDENTE", rules_version=RULES_VERSION, site_type="VALIDATION_ERROR",
                              signals={'technical_errors':list(errors), 'candidate_url':failed[0]},
                              evidence=["Falha técnica ao validar candidato; vínculo não avaliado"]+errors), candidates, errors
        if errors and not successful_validations:
            raise ProviderError("VALIDACAO_FALHOU", ", ".join(sorted(set(errors))))
        if not best.website and (rejected_urls or eligible_unconfirmed):
            best = Validation(evidence=["SEM_SITE_OFICIAL_CONFIRMADO"] + [f"{url}: {reason}" for url, reason in rejected_urls.items()],
                              confidence="PENDENTE" if eligible_unconfirmed else "NAO_ENCONTRADO",
                              signals={'candidate_url':rank_candidates(company,eligible_unconfirmed)[0]} if eligible_unconfirmed else {},
                              rules_version=RULES_VERSION, site_type="CORPORATE_PENDING" if eligible_unconfirmed else "THIRD_PARTY_ONLY")
        return best, candidates, errors
