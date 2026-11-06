import json
import math
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv


def boolean(name: str, default=False) -> bool:
    raw = os.getenv(name, str(default)).lower()
    if raw not in {"true", "false"}:
        raise ValueError(f"{name} deve ser true ou false")
    return raw == "true"


@dataclass(frozen=True)
class Settings:
    max_companies: int = 1000
    batch_size: int = 50
    concurrency: int = 2
    timeout: float = 15
    retries: int = 2
    cache_ttl: float = 168 * 3600
    candidates: int = 3
    queries: int = 2
    website_max_requests: int = 12000
    website_rpm: int = 20
    max_bytes: int = 1000000
    website_delay: float = 2
    enable_brasilapi: bool = False
    enable_serper: bool = False
    serper_key: str = field(default="", repr=False)

    def __post_init__(self):
        if not 1 <= self.max_companies <= 1000:
            raise ValueError("MAX_COMPANIES deve estar entre 1 e 1000")
        if not 1 <= self.batch_size <= 1000 or not 1 <= self.concurrency <= 10:
            raise ValueError("Lote deve estar entre 1 e 1000; concorrência entre 1 e 10")
        if not 0 <= self.retries <= 5 or not 1 <= self.candidates <= 10 or not 1 <= self.queries <= 3:
            raise ValueError("Retentativas 0..5, candidatos 1..10 e pesquisas 1..3")
        if any(not math.isfinite(v) or v <= 0 for v in
               (self.timeout, self.cache_ttl, self.website_delay)):
            raise ValueError("Timeout, TTL e intervalo devem ser positivos e finitos")
        if self.website_max_requests < 0 or self.website_rpm < 1 or self.max_bytes < 1024:
            raise ValueError("Limites de website inválidos")

    @classmethod
    def from_env(cls, env_file=Path(".env")):
        load_dotenv(env_file, override=False)
        cost = float(os.getenv("MAX_ADDITIONAL_COST_USD", "0"))
        if cost != 0 or boolean("ENABLE_PAID_APIS") or boolean("ENABLE_AUTOMATIC_TOPUP"):
            raise ValueError("Esta POC exige custo zero, APIs pagas e recarga desativadas")
        return cls(
            max_companies=int(os.getenv("MAX_COMPANIES", "1000")),
            batch_size=int(os.getenv("BATCH_SIZE", "50")),
            concurrency=int(os.getenv("MAX_CONCURRENT_REQUESTS", "2")),
            timeout=float(os.getenv("HTTP_TIMEOUT", "15")),
            retries=int(os.getenv("HTTP_RETRIES", "2")),
            cache_ttl=float(os.getenv("CACHE_TTL_HOURS", "168")) * 3600,
            candidates=int(os.getenv("MAX_CANDIDATES", "3")),
            queries=int(os.getenv("SEARCH_QUERIES", "2")),
            website_max_requests=int(os.getenv("WEBSITE_MAX_REQUESTS", "12000")),
            website_rpm=int(os.getenv("WEBSITE_REQUESTS_PER_MINUTE", "20")),
            max_bytes=int(os.getenv("WEBSITE_MAX_BYTES", "1000000")),
            website_delay=float(os.getenv("WEBSITE_DELAY_SECONDS", "2")),
            enable_brasilapi=boolean("ENABLE_BRASILAPI"),
            enable_serper=boolean("ENABLE_SERPER"),
            serper_key=os.getenv("SERPER_API_KEY", ""),
        )


def load_policies(path: Path) -> dict:
    policies = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(policies, dict) or any(not isinstance(v, dict) for v in policies.values()):
        raise ValueError("Manifesto de provedores deve ser um objeto JSON com objetos por provedor")
    return policies


def policy_reason(policy: dict, *, search=False) -> str:
    flags = ("verified", "free_only", "no_card", "no_auto_charge", "no_paid_credits",
             "storage_permitted", "batch_permitted")
    if any(policy.get(flag) is not True for flag in flags):
        return "gratuidade, uso em lote ou armazenamento não verificados"
    try:
        today = datetime.now(timezone.utc).date()
        if not date.fromisoformat(policy["reviewed_on"]) <= today <= date.fromisoformat(policy["expires_on"]):
            return "verificação expirada ou futura"
        if not policy.get("evidence", "").strip():
            return "evidências ausentes"
        for key in ("max_requests", "per_minute", "per_hour", "per_day"):
            if type(policy[key]) is not int or policy[key] <= 0:
                return "cotas não verificadas"
        if search:
            if type(policy.get("confirmed_free_remaining")) is not int or policy["confirmed_free_remaining"] <= 0:
                return "saldo gratuito não confirmado"
            at = datetime.fromisoformat(policy["balance_verified_at"])
            if at.tzinfo is None or not 0 <= (datetime.now(timezone.utc) - at).total_seconds() <= 86400:
                return "saldo deve ter confirmação UTC com até 24 horas"
    except (KeyError, ValueError, TypeError):
        return "verificação incompleta"
    return ""
