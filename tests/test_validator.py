import asyncio
import socket
from types import SimpleNamespace

import pytest

from app.models import Company, ProviderError, UnsafeURL
from app.services.validator import (Page, PinnedHTTPConnection, SafeFetcher, classify, public_address,
                                    resolve_public, safe_url)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://[::1]/", "http://localhost/", "http://host.local/",
    "http://host.internal/", "file:///etc/passwd", "ftp://empresa.com/", "https://user:pass@empresa.com/",
    "https://empresa.com:8443/", "http://2130706433/", "https://empresa.com\\@127.0.0.1/",
    "https://empresa.com/\r\nHeader:1", "https://evil%00.com/", "http://169.254.169.254/",
])
def test_unsafe_urls(url):
    with pytest.raises(UnsafeURL):
        safe_url(url)


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.0.1", "169.254.169.254",
                                "0.0.0.0", "100.64.0.1", "::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1",
                                "64:ff9b::7f00:1", "2002:7f00:1::", "224.0.0.1"])
def test_rejects_nonpublic_and_translations(ip):
    assert not public_address(ip)


def test_dns_all_answers_must_be_public():
    def resolver(host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ("93.184.216.34", "10.0.0.1")]
    with pytest.raises(UnsafeURL):
        resolve_public("empresa.com", 443, resolver)


def test_pinned_connection_never_resolves_twice(monkeypatch):
    connected = []

    class FakeSocket:
        def settimeout(self, timeout): pass
        def connect(self, target): connected.append(target)
        def getpeername(self): return ("93.184.216.34", 80)
        def close(self): pass

    monkeypatch.setattr(socket, "socket", lambda *args: FakeSocket())
    conn = PinnedHTTPConnection("empresa.com", 80,
                                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)), 10, False)
    conn.connect()
    assert connected == [("93.184.216.34", 80)]
    conn.close()


def test_redirect_to_private_is_blocked(settings):
    class FakeFetcher(SafeFetcher):
        def __init__(self): self.settings = settings
        async def allowed(self, url): pass
        async def one(self, url):
            assert url == "https://empresa.com/"
            return Page(url, 302, {"location": "http://127.0.0.1/admin"}, "")

    async def run():
        with pytest.raises(UnsafeURL):
            await FakeFetcher().fetch("https://empresa.com/")
    asyncio.run(run())


def test_public_redirect_and_robots_each_origin(settings):
    visited = []

    class FakeFetcher(SafeFetcher):
        def __init__(self): self.settings = settings
        async def allowed(self, url): visited.append(url)
        async def one(self, url):
            if url == "http://empresa.com/":
                return Page(url, 301, {"location": "https://empresa.com/"}, "")
            return Page(url, 200, {}, "Empresa")

    page = asyncio.run(FakeFetcher().fetch("http://empresa.com/"))
    assert page.url == "https://empresa.com/"
    assert visited == ["http://empresa.com/", "https://empresa.com/"]


def test_robots_denies(settings):
    class FakeFetcher(SafeFetcher):
        def __init__(self):
            super().__init__(None, settings)
        async def one(self, url):
            return Page(url, 200, {}, "User-agent: *\nDisallow: /")

    async def run():
        with pytest.raises(ProviderError) as error:
            await FakeFetcher().allowed("https://empresa.com/")
        assert error.value.code == "ROBOTS_BLOQUEADO"
    asyncio.run(run())


@pytest.mark.parametrize("html,level,confirmed", [
    ("<h1>Acme Digital</h1><footer>Acme Digital 00.000.000/0001-91 São Paulo SP</footer>", "ALTA", True),
    ("Acme Digital 00.000.000/0001-91 São Paulo SP", "MEDIA", False),
    ("Acme Digital São Paulo SP", "BAIXA", False),
    ("Outra empresa", "BAIXA", False),
    ("<script>Acme Digital 00.000.000/0001-91</script>Outra empresa", "BAIXA", False),
    ("Consulta CNPJ: Acme Digital 00.000.000/0001-91", "NAO_ENCONTRADO", False),
    ("Acme Digital 1000000000001919", "BAIXA", False),
])
def test_evidence_classification(html, level, confirmed):
    company = Company("00000000000191", "Acme Digital Ltda", "Acme Digital", "São Paulo", "SP")
    result = classify(company, Page("https://acme.com/", 200, {}, html))
    assert result.confidence == level
    assert result.confirmed is confirmed


def test_directory_is_excluded():
    result = classify(Company("00000000000191", "Acme Digital"),
                      Page("https://cnpj.biz/00000000000191", 200, {}, "Acme Digital 00000000000191"))
    assert result.confidence == "NAO_ENCONTRADO"
    assert not result.website
