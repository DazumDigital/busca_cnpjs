import asyncio
import time
from datetime import datetime

from app.config import policy_reason
from app.models import ProviderError, QuotaExceeded


class QuotaManager:
    """Reserva antes de enviar; falhas e chamadas interrompidas também consomem o teto local."""
    def __init__(self, db, policies, sleep=asyncio.sleep, clock=time.time):
        self.db, self.policies, self.sleep, self.clock = db, policies, sleep, clock

    async def reserve(self, provider):
        p = self.policies[provider]
        while True:
            if provider != "websites" and policy_reason(p, search=provider == "serper"):
                raise ProviderError("INTEGRACAO_NAO_VERIFICADA")
            blocked = self.db.get_state(f"provider-block:{provider}")
            if blocked:
                raise ProviderError(blocked["code"])
            now = self.clock()
            wait = 0
            with self.db.transaction():
                rows = self.db.conn.execute(
                    "SELECT started_at FROM requests WHERE provider=? ORDER BY started_at", (provider,)
                ).fetchall()
                total = len(rows)
                cap = p["max_requests"]
                balance = p.get("confirmed_free_remaining")
                if balance is not None:
                    # O saldo confirmado é um snapshot; descontar só reservas após o snapshot.
                    snapshot = datetime.fromisoformat(p["balance_verified_at"]).timestamp()
                    used_since = sum(r[0] >= snapshot for r in rows)
                    if used_since >= balance:
                        raise QuotaExceeded()
                if total >= cap:
                    raise QuotaExceeded()
                for key, seconds in (("per_minute", 60), ("per_hour", 3600), ("per_day", 86400)):
                    recent = [r[0] for r in rows if r[0] > now - seconds]
                    if len(recent) >= p[key]:
                        wait = max(wait, recent[-p[key]] + seconds - now)
                wait = max(wait, self.db.get_state(f"cooldown:{provider}", 0) - now)
                if wait <= 0:
                    return self.db.conn.execute(
                        "INSERT INTO requests(provider,started_at,outcome) VALUES (?,?,'RESERVADA')",
                        (provider, now)).lastrowid
            # Sem reservar a cota durante a pausa; reavaliar em uma transação depois.
            await self.sleep(min(wait + 0.01, 60))

    def finish(self, reservation, outcome, status=None):
        self.db.conn.execute("UPDATE requests SET outcome=?,http_status=? WHERE id=?",
                             (outcome, status, reservation))

    def block(self, provider, code):
        if code not in {"COTA_ESGOTADA", "ACESSO_NEGADO"}:
            raise ValueError("Bloqueio de provedor desconhecido")
        self.db.set_state(f"provider-block:{provider}", {"code": code, "at": self.clock()})

    def cooldown(self, provider, seconds):
        until = max(self.db.get_state(f"cooldown:{provider}", 0), self.clock() + seconds)
        self.db.set_state(f"cooldown:{provider}", until)

    def summary(self):
        usage = self.db.usage()
        result = {}
        for name, p in self.policies.items():
            count = usage.get(name, {}).get("requisicoes", 0)
            result[name] = dict(requisicoes_reservadas=count, limite_local_total=p["max_requests"],
                                limite_minuto=p["per_minute"], limite_hora=p["per_hour"],
                                limite_dia=p["per_day"],
                                saldo_snapshot_provedor=p.get("confirmed_free_remaining"),
                                saldo_snapshot_data=p.get("balance_verified_at"),
                                saldo_atual_provedor="NAO_CONSULTADO",
                                fracao_teto_local=count / p["max_requests"] if p["max_requests"] else None)
        return result
