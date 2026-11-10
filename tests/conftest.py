import socket
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.storage.database import Database


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    original_connect = socket.socket.connect
    original_socketpair = socket.socketpair
    def denied(*args, **kwargs):
        raise AssertionError("Teste tentou acessar rede real")
    def internal_socketpair(*args, **kwargs):
        # asyncio no Windows usa um par local de sockets como canal interno.
        # Liberar somente a criação síncrona desse par pelo código da stdlib.
        monkeypatch.setattr(socket.socket, "connect", original_connect)
        try:
            return original_socketpair(*args, **kwargs)
        finally:
            monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket, "socketpair", internal_socketpair)


@pytest.fixture(autouse=True)
def no_dns_discovery(monkeypatch):
    # A descoberta de sites consulta o DNS (raiz do domínio e palpites pelo nome).
    # Nos testes nenhum domínio "existe", salvo quando o teste simula explicitamente.
    from app.web import live_sources
    monkeypatch.setattr(live_sources, "domain_exists", lambda host: False)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.sqlite")
    yield database
    database.close()


@pytest.fixture
def settings():
    return Settings(retries=2, website_delay=0.01)


@pytest.fixture
def policy():
    now = datetime.now(timezone.utc)
    return dict(verified=True, reviewed_on=now.date().isoformat(),
                expires_on=(now + timedelta(days=1)).date().isoformat(), free_only=True,
                no_card=True, no_auto_charge=True, no_paid_credits=True, storage_permitted=True,
                batch_permitted=True, evidence="Mock: nenhuma conta real", max_requests=100,
                per_minute=100, per_hour=100, per_day=100,
                confirmed_free_remaining=100, balance_verified_at=now.isoformat())


def generated_cnpj(number):
    base = str(number).zfill(12)
    for weights in ((5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2), (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)):
        rest = sum(int(a) * b for a, b in zip(base, weights)) % 11
        base += str(0 if rest < 2 else 11 - rest)
    return base
