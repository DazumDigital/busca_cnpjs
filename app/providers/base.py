import asyncio
import hashlib
import json
import random
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from app.models import Company, ProviderError


class CompanyProvider(ABC):
    @abstractmethod
    async def lookup(self, cnpj: str) -> Company: ...


class SearchProvider(ABC):
    @abstractmethod
    async def search(self, query: str) -> list[str]: ...


def retry_after(value):
    if value is None:
        return 0
    try:
        delay = float(value)
        return max(0, delay) if delay < float("inf") else 0
    except ValueError:
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


class JsonAPI:
    def __init__(self, name, client, quota, db, settings, sleep=asyncio.sleep):
        self.name, self.client, self.quota = name, client, quota
        self.db, self.settings, self.sleep = db, settings, sleep
        self.locks = {}

    async def request(self, method, url, *, payload=None, headers=None, project=lambda x: x):
        key = hashlib.sha256(json.dumps([method, url, payload], sort_keys=True).encode()).hexdigest()
        async with self.locks.setdefault(key, asyncio.Lock()):
            cached = self.db.cache_get(self.name, key)
            if cached is not None:
                return cached
            return await self._fetch(method, url, key, payload, headers, project)

    async def _fetch(self, method, url, key, payload, headers, project):
        for attempt in range(self.settings.retries + 1):
            reservation = await self.quota.reserve(self.name)
            delay = 0
            try:
                # Endpoints fixos; redirecionamento de API não é seguido (nem a chave enviada).
                response = await self.client.request(method, url, json=payload, headers=headers,
                                                     timeout=self.settings.timeout, follow_redirects=False)
            except httpx.TransportError:
                self.quota.finish(reservation, "FALHA_REDE")
                if attempt == self.settings.retries:
                    raise ProviderError("ERRO_REDE", "Timeout ou falha de conexão") from None
            else:
                code = response.status_code
                if code == 200:
                    try:
                        value = project(response.json())
                    except (ValueError, TypeError, KeyError, AttributeError):
                        self.quota.finish(reservation, "RESPOSTA_INVALIDA", code)
                        raise ProviderError("RESPOSTA_INVALIDA", "Resposta incompleta ou incompatível") from None
                    self.quota.finish(reservation, "SUCESSO", code)
                    self.db.cache_put(self.name, key, value, self.settings.cache_ttl)
                    return value
                self.quota.finish(reservation, "FALHA_HTTP", code)
                if code == 404:
                    raise ProviderError("CNPJ_NAO_ENCONTRADO")
                if code in (401, 403):
                    self.quota.block(self.name, "ACESSO_NEGADO")
                    raise ProviderError("ACESSO_NEGADO")
                if code in (402,):
                    self.quota.block(self.name, "COTA_ESGOTADA")
                    raise ProviderError("COTA_ESGOTADA")
                if code not in (429, 500, 502, 503, 504):
                    raise ProviderError("ERRO_HTTP", f"HTTP {code}")
                delay = retry_after(response.headers.get("Retry-After"))
                pause = max(delay, 2 ** attempt + random.uniform(0, 1))
                self.quota.cooldown(self.name, pause)
                if attempt == self.settings.retries:
                    raise ProviderError("LIMITE_TEMPORARIO" if code == 429 else "ERRO_PROVEDOR", f"HTTP {code}")
            # A próxima reserva respeita o cooldown global, inclusive em outras tarefas.
            if delay == 0:
                await self.sleep(2 ** attempt + random.uniform(0, 1))
