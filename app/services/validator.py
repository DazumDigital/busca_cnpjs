"""HTTP público com IP fixado, TLS verificado, robots.txt e classificação explicável."""
import asyncio
import http.client
import ipaddress
import re
import socket
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from app.models import Company, ProviderError, QuotaExceeded, UnsafeURL, Validation
from app.security import tls_context
from app.services.site_rules import (RULES, RULES_VERSION, THIRD_PARTIES, brand_tokens, domain_brand_match,
                                     domain_reason, extract_cnpjs, tenant_brand_match, url_reason)
from app.services.site_rules import content_directory_reason

# Identificação pública e estável do robô. O token "DazumCNPJ" é o que os sites
# usam no robots.txt para regras específicas; a página explica uso e bloqueio.
ROBOT_TOKEN = "DazumCNPJ"
ROBOT_INFO_URL = "https://busca-cnpjs.vercel.app/robo"
USER_AGENT = f"{ROBOT_TOKEN}/1.0 (+{ROBOT_INFO_URL})"


def third_party(host):
    return bool(domain_reason(host))


def safe_url(url: str):
    if not isinstance(url, str) or len(url) > 2048 or re.search(r"[\x00-\x20\x7f\\]", url):
        raise UnsafeURL("URL inválida")
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"https", "http"} or parts.username is not None or parts.password is not None:
            raise UnsafeURL("Esquema ou credenciais proibidos")
        host = (parts.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if port != (443 if parts.scheme == "https" else 80):
            raise UnsafeURL("Porta proibida")
        if len(host) > 253 or "." not in host or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")
        ):
            raise UnsafeURL("Domínio inválido")
        if host.endswith((".local", ".localhost", ".internal", ".test", ".invalid", ".example", ".onion", ".home.arpa")):
            raise UnsafeURL("Destino reservado")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise UnsafeURL("Endereços IP literais não são websites candidatos")
        clean = urlunsplit((parts.scheme, host, parts.path or "/", parts.query, ""))
        return clean, host, port
    except (ValueError, UnicodeError) as exc:
        raise UnsafeURL("URL inválida ou insegura") from exc


def public_address(value):
    ip = ipaddress.ip_address(value.split("%")[0])
    if not ip.is_global or ip.is_multicast or ip.is_unspecified:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped or ip.sixtofour or ip.teredo or ip in ipaddress.ip_network("64:ff9b::/96"):
            return False
    return True


def resolve_public(host, port, resolver=None):
    results = (resolver or socket.getaddrinfo)(host, port, type=socket.SOCK_STREAM)
    if not results or any(not public_address(row[4][0]) for row in results):
        raise UnsafeURL("DNS contém endereço não público")
    return results[0]


class PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port, target, timeout, tls):
        # HTTPConnection usa default_port para produzir Host. Em TLS, deixar 80
        # fazia Host incluir :443 e podia reativar o redirect canônico do site.
        self.default_port = 443 if tls else 80
        super().__init__(host, port, timeout=timeout)
        self.target, self.tls = target, tls

    def connect(self):
        family, socktype, proto, _, sockaddr = self.target
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(self.timeout)
            # sockaddr numérico já validado; nenhuma segunda resolução DNS.
            sock.connect(sockaddr)
            if ipaddress.ip_address(sock.getpeername()[0]) != ipaddress.ip_address(sockaddr[0]):
                raise UnsafeURL("IP remoto divergente")
            if self.tls:
                sock = tls_context().wrap_socket(sock, server_hostname=self.host)
            self.sock = sock
        except BaseException:
            sock.close()
            raise


@dataclass
class Page:
    url: str
    status: int
    headers: dict
    text: str
    redirect_chain: list[dict] = field(default_factory=list)


def same_site(left, right):
    """Somente alias www da mesma identidade; não aceita subdomínios arbitrários."""
    return (urlsplit(left).hostname or '').removeprefix('www.') == (urlsplit(right).hostname or '').removeprefix('www.')


def connection_category(exc):
    """Categoria fixa de uma falha antes da resposta HTTP, para diagnóstico."""
    import ssl
    if isinstance(exc, TimeoutError):
        return 'TEMPO_ESGOTADO'
    if isinstance(exc, ssl.SSLCertVerificationError):
        return 'TLS_CERTIFICADO'
    if isinstance(exc, ssl.SSLError):
        return 'TLS'
    if isinstance(exc, ConnectionRefusedError):
        return 'CONEXAO_RECUSADA'
    if isinstance(exc, socket.gaierror):
        return 'DNS'
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, http.client.RemoteDisconnected)):
        return 'CONEXAO_ENCERRADA'
    return 'CONEXAO'


class SafeFetcher:
    def __init__(self, quota, settings, *, truncate=False, any_text=False):
        self.quota, self.settings = quota, settings
        self.robots = {}
        self.locks = {}
        # robots.txt (RFC 9309): lê só o início de arquivos grandes e aceita
        # qualquer tipo de conteúdo. Páginas continuam com o limite estrito.
        self.truncate, self.any_text = truncate, any_text

    def _one(self, url):
        clean, host, port = safe_url(url)
        target = resolve_public(host, port)
        conn = PinnedHTTPConnection(host, port, target, self.settings.timeout, clean.startswith("https:"))
        deadline = time.monotonic() + self.settings.timeout
        try:
            parsed = urlsplit(clean)
            path = urlunsplit(("", "", parsed.path, parsed.query, ""))
            conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.8",
                                               "Accept-Encoding": "identity", "Connection": "close"})
            response = conn.getresponse()
            headers = {k.lower(): v for k, v in response.getheaders()}
            if response.status != 200:
                return Page(clean, response.status, headers, "")
            if headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                raise ProviderError("CONTEUDO_NAO_SUPORTADO")
            content_type = headers.get("content-type", "").lower()
            if not self.any_text and not any(t in content_type for t in ("text/html", "application/xhtml+xml", "text/plain")):
                raise ProviderError("CONTEUDO_NAO_SUPORTADO")
            body = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Tempo de leitura excedido")
                if conn.sock:
                    conn.sock.settimeout(remaining)
                block = response.read1(min(16384, self.settings.max_bytes + 1 - len(body)))
                if not block:
                    break
                body.extend(block)
                if len(body) > self.settings.max_bytes:
                    if self.truncate:
                        del body[self.settings.max_bytes:]
                        break
                    raise ProviderError("CONTEUDO_MUITO_GRANDE")
            charset = re.search(r"charset=[\"']?([a-zA-Z0-9_-]+)", content_type)
            try:
                text = body.decode(charset.group(1) if charset else "utf-8", errors="replace")
            except LookupError:
                text = body.decode("utf-8", errors="replace")
            return Page(clean, response.status, headers, text)
        finally:
            conn.close()

    async def one(self, url):
        clean, host, _ = safe_url(url)
        if url_reason(clean):
            raise ProviderError("TERCEIRO", "Domínio de terceiros")
        lock = self.locks.setdefault(host, asyncio.Lock())
        async with lock:
            key = f"website-next:{host}"
            pause = self.quota.db.get_state(key, 0) - time.time()
            if pause > 0:
                await asyncio.sleep(pause)
            reservation = await self.quota.reserve("websites")
            self.quota.db.set_state(key, time.time() + self.settings.website_delay)
            try:
                result = await asyncio.to_thread(self._one, clean)
            except (OSError, http.client.HTTPException, ValueError, ProviderError) as exc:
                self.quota.finish(reservation, "FALHA_VALIDACAO")
                if isinstance(exc, (UnsafeURL, ProviderError)):
                    raise
                # Só a categoria da falha (sem mensagem bruta, URL ou IP).
                raise ProviderError("SITE_INACESSIVEL", details={'conexao': connection_category(exc)}) from None
            self.quota.finish(reservation, "SUCESSO" if result.status < 400 else "FALHA_HTTP", result.status)
            if result.status in (429, 503):
                from app.providers.base import retry_after
                self.quota.cooldown("websites", max(60, retry_after(result.headers.get("retry-after"))))
            return result

    async def allowed(self, url):
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            lock = self.locks.setdefault("robots:" + origin, asyncio.Lock())
            async with lock:
                if origin not in self.robots:
                    current = origin + "/robots.txt"
                    visited = set()
                    for _ in range(6):
                        current, _, _ = safe_url(current)
                        if current in visited:
                            raise ProviderError('LOOP_REDIRECIONAMENTO_ROBOTS')
                        visited.add(current)
                        page = await self.one(current)
                        if page.status in {301, 302, 303, 307, 308}:
                            if not page.headers.get('location'):
                                raise ProviderError('REDIRECIONAMENTO_INVALIDO')
                            current = urljoin(current, page.headers['location'])
                            continue
                        parser = RobotFileParser()
                        if page.status in (404, 410):
                            parser.parse([])
                        elif page.status == 200:
                            parser.parse(page.text.splitlines())
                        else:
                            raise ProviderError("ROBOTS_INDISPONIVEL")
                        self.robots[origin] = parser
                        break
                    else:
                        raise ProviderError("REDIRECIONAMENTOS_EXCESSIVOS")
        parser = self.robots[origin]
        if not parser.can_fetch(USER_AGENT, url):
            raise ProviderError("ROBOTS_BLOQUEADO")
        crawl_delay = parser.crawl_delay(USER_AGENT) or parser.crawl_delay("*")
        if crawl_delay:
            key = f"website-next:{parts.hostname}"
            self.quota.db.set_state(key, max(self.quota.db.get_state(key, 0), time.time() + crawl_delay))

    async def fetch(self, url):
        visited = set()
        chain = []
        for _ in range(6):
            url, host, _ = safe_url(url)
            if url_reason(url):
                raise ProviderError("TERCEIRO")
            if url in visited:
                raise ProviderError("LOOP_REDIRECIONAMENTO", details={'redirect_chain':chain})
            visited.add(url)
            await self.allowed(url)
            page = await self.one(url)
            if page.status in {301, 302, 303, 307, 308}:
                if not page.headers.get("location"):
                    raise ProviderError("REDIRECIONAMENTO_INVALIDO")
                destination, _, _ = safe_url(urljoin(url, page.headers["location"]))
                if url_reason(destination):
                    raise ProviderError('TERCEIRO', details={'redirect_chain':chain})
                chain.append(dict(url=url,status=page.status,destination=destination))
                url = destination
                continue
            if page.status != 200:
                raise ProviderError("SITE_INACESSIVEL", f"HTTP {page.status}")
            page.redirect_chain = chain
            return page
        raise ProviderError("REDIRECIONAMENTOS_EXCESSIVOS", details={'redirect_chain':chain})


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []
        self.title_parts = []
        self.heading_parts = []
        self.footer_parts = []
        self.links = []
        self.in_title = self.in_heading = self.in_footer = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        if tag == "title": self.in_title += 1
        if tag == "h1": self.in_heading += 1
        if tag == "footer": self.in_footer += 1
        if tag == "a" and not self.hidden:
            href = dict(attrs).get("href")
            if href: self.links.append(href)

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)
        if tag == "title": self.in_title = max(0, self.in_title - 1)
        if tag == "h1": self.in_heading = max(0, self.in_heading - 1)
        if tag == "footer": self.in_footer = max(0, self.in_footer - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)
            if self.in_title: self.title_parts.append(data)
            if self.in_heading: self.heading_parts.append(data)
            if self.in_footer: self.footer_parts.append(data)


def fold(text):
    return re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()).strip()


def name_tokens(text):
    stop = {"ltda", "limitada", "sa", "s", "a", "e", "de", "da", "do", "dos", "das", "me", "eireli", "comercio", "servicos", "empresa"}
    return [t for t in fold(text).split() if len(t) >= 3 and t not in stop]


def institutional_path(url):
    path = urlsplit(url).path.rstrip('/') or '/'
    return path == '/' or bool(re.search(r'termos|condicoes|privacidade|legal|contato|sobre|quem somos',fold(path)))


def owned_institutional_fact(company, fact):
    """Revalida a declaração derivada, sem confiar em score ou indicação manual."""
    if fact.get('source') != 'PUBLIC_PAGE_DERIVED_EVIDENCE' or not re.fullmatch(r'[0-9a-f]{64}',fact.get('content_sha256','')):
        return False
    try:
        clean, _, _ = safe_url(fact.get('url',''))
    except ValueError:
        return False
    if url_reason(clean,company.cnpj) or not institutional_path(clean):
        return False
    context = fact.get('owner_context','')
    numbers = set(fact.get('cnpjs',[])) | extract_cnpjs(context)
    text = ' '+fold(context)+' '
    names = [name_tokens(n) for n in (company.legal_name,company.trade_name)]
    name = any(tokens and all(' '+t+' ' in text for t in tokens) for tokens in names)
    explicit = re.search(r'operad[ao] por|mantid[ao] por|pertence a|propriedade d[aeo]|inscrit[ao].{0,80}cnpj',text)
    labelled = ' razao social ' in text and ' cnpj ' in text
    return numbers == {company.cnpj} and company.cnpj in extract_cnpjs(context) and name and bool(explicit or labelled)


def tenant_identity_evidence(company, host, facts):
    if not tenant_brand_match(company,host):
        return []
    paths = set()
    for fact in facts:
        if same_site(fact.get('url',''),'https://'+host+'/') and owned_institutional_fact(company,fact):
            paths.add(urlsplit(fact['url']).path.rstrip('/') or '/')
    # Duas páginas institucionais distintas, incluindo uma página legal, no
    # mesmo tenant. Queries e outra loja no mesmo serviço não multiplicam prova.
    if len(paths) < 2 or not any(re.search(r'termos|condicoes|privacidade|legal',fold(p)) for p in paths):
        return []
    return sorted(paths)


def classify(company: Company, page: Page) -> Validation:
    return classify_pages(company, [page])


def rejected(reason, *, kind="THIRD_PARTY"):
    return Validation(evidence=[reason], rules_version=RULES_VERSION, site_type=kind)


def classify_pages(company, pages):
    if not pages or pages[0].status != 200:
        return Validation(evidence=["Página inicial inacessível ou ausente"], confidence="BAIXA", rules_version=RULES_VERSION)
    first, host, _ = safe_url(pages[0].url)
    reason = url_reason(first, company.cnpj)
    if reason:
        return rejected(reason, kind="CNPJ_DIVERGENTE" if reason.startswith("CNPJ_DIVERGENTE") else "THIRD_PARTY")
    parsers, texts, inspected = [], [], []
    for page in pages:
        clean, page_host, _ = safe_url(page.url)
        if not same_site(clean, first) or page.status != 200 or url_reason(clean, company.cnpj):
            continue
        parser = VisibleText()
        parser.feed(page.text)
        visible = " ".join(parser.parts)
        heading = fold(" ".join(parser.title_parts + parser.heading_parts) or visible[:240])
        content = fold(visible)
        directory_reason = content_directory_reason(heading, content)
        if directory_reason:
            import hashlib
            result = rejected(directory_reason)
            result.inspected_urls = inspected + [clean]
            result.page_evidence = [dict(url=clean, title=" ".join(parser.title_parts + parser.heading_parts)[:300],
                content_sha256=hashlib.sha256(visible.encode()).hexdigest(), cnpjs=sorted(extract_cnpjs(visible)),
                owner_context='', source='PUBLIC_PAGE_DERIVED_EVIDENCE', rejection_reason=directory_reason,
                redirect_chain=page.redirect_chain, reviewed_at_utc=datetime.now(timezone.utc).isoformat())]
            return result
        parsers.append((parser, clean))
        texts.append(visible)
        inspected.append(clean)
    if not inspected:
        return Validation(evidence=["Página inacessível"], confidence="BAIXA", rules_version=RULES_VERSION)
    visible = " ".join(texts)
    text = " " + fold(visible) + " "
    numbers = extract_cnpjs(visible)
    exact = company.cnpj in numbers
    if numbers and not exact:
        return rejected("CNPJ_DIVERGENTE_CONTEUDO: inscrição diferente; filial não substitui o CNPJ consultado", kind="CNPJ_DIVERGENTE")
    names = [name_tokens(n) for n in (company.trade_name, company.legal_name)]
    name_match = any(tokens and all(f" {t} " in text for t in tokens) for tokens in names)
    brand_domain = domain_brand_match(company, host)
    heading_text = " " + fold(" ".join(parsers[0][0].title_parts + parsers[0][0].heading_parts)) + " "
    heading_brand = any(f" {token} " in heading_text for token in brand_tokens(company))
    legal_owner, navigation = False, set()
    for parser, clean in parsers:
        footer_numbers = extract_cnpjs(" ".join(parser.footer_parts))
        footer_text = " " + fold(" ".join(parser.footer_parts)) + " "
        footer_brand = any(f" {token} " in footer_text for token in brand_tokens(company))
        if company.cnpj in footer_numbers and len(footer_numbers) == 1 and footer_brand:
            legal_owner = True
        legal_path = re.search(r"termos|condicoes|privacidade|legal", fold(urlsplit(clean).path))
        own_language = re.search(r"operad[ao] por|mantid[ao] por|pertence a|propriedade d[aeo]|inscrit[ao].{0,80}cnpj", fold(" ".join(parser.parts)))
        if legal_path and own_language and company.cnpj in extract_cnpjs(" ".join(parser.parts)):
            # CNPJ de outra inscrição na mesma declaração não confirma a pessoa jurídica alvo.
            if extract_cnpjs(" ".join(parser.parts)) == {company.cnpj}:
                legal_owner = True
        for link in parser.links:
            try:
                target, _, _ = safe_url(urljoin(clean, link))
            except UnsafeURL:
                continue
            if not same_site(target, first):
                continue
            path = fold(urlsplit(target).path)
            for group in ("sobre", "contato", "produtos", "servicos"):
                if group in path.split(): navigation.add(group)
    corporate_nav = len(navigation) >= 2
    import hashlib
    page_evidence = []
    for parser, url in parsers:
        content = " ".join(parser.parts)
        cnpjs = sorted(extract_cnpjs(content))
        footer = " ".join(parser.footer_parts)
        legal = bool(re.search(r"termos|condicoes|privacidade|legal", fold(urlsplit(url).path)))
        owner_text = content if legal else footer
        match = next((m for m in re.finditer(r"[0-9]{2}\.?[0-9]{3}\.?[0-9]{3}/?[0-9]{4}-?[0-9]{2}", owner_text)
                      if re.sub(r"[^0-9]", "", m.group()) == company.cnpj), None)
        excerpt = owner_text[max(0, match.start()-220):match.end()+220] if match else ""
        page_evidence.append(dict(url=url, content_sha256=hashlib.sha256(content.encode()).hexdigest(),
                                  title=" ".join(parser.title_parts + parser.heading_parts)[:300],
                                  cnpjs=cnpjs, exact_cnpj=company.cnpj in cnpjs,
                                  owner_context=excerpt[:500], source="PUBLIC_PAGE_DERIVED_EVIDENCE",
                                  owner_context_kind='LEGAL_BODY' if legal else 'FOOTER',
                                  reviewed_at_utc=datetime.now(timezone.utc).isoformat(),
                                  redirect_chain=next((p.redirect_chain for p in pages if p.url==url),[])))
    tenant_paths = tenant_identity_evidence(company,host,page_evidence)
    tenant_identity = bool(tenant_paths) and numbers == {company.cnpj}
    signals = dict(exact_cnpj=exact, name_match=name_match, brand_domain=brand_domain,
                   heading_brand=heading_brand, legal_owner=legal_owner, corporate_navigation=corporate_nav,
                   tenant_identity_confirmed=tenant_identity,tenant_evidence_paths=tenant_paths)
    points = {'exact_cnpj':25,'name_match':15,'brand_domain':15,'heading_brand':15,'legal_owner':20,'corporate_navigation':10}
    score = min(100,10+sum(value for key,value in points.items() if signals[key])+ (15 if tenant_identity and not brand_domain else 0))
    evidence = ['Página acessível; menção de nome/CNPJ não comprova propriedade']
    evidence.extend(k for k in points if signals[k])
    if tenant_identity:evidence.append('Identidade do tenant sustentada em páginas institucionais distintas do mesmo host')
    strong = exact and name_match and legal_owner and heading_brand and (brand_domain or tenant_identity)
    plausible = score >= 45 and ((name_match and (brand_domain or heading_brand)) or (brand_domain and heading_brand and corporate_nav))
    level = 'ALTA' if strong else 'MEDIA' if plausible else 'BAIXA'
    if level == 'MEDIA':evidence.append('REVISÃO MANUAL: candidato plausível sem confirmação suficiente do CNPJ/titularidade')
    return Validation(first, host.removeprefix("www."), level, score, evidence, True, strong,
                      RULES_VERSION, "CORPORATE" if strong else "CORPORATE_CANDIDATE", signals, inspected, page_evidence)


def validation_cache_key(company, clean):
    import hashlib
    import json
    from dataclasses import asdict
    return hashlib.sha256(json.dumps([asdict(company), clean, RULES_VERSION], sort_keys=True).encode()).hexdigest()


class WebsiteValidator:
    def __init__(self, fetcher, db, settings):
        self.fetcher, self.db, self.settings = fetcher, db, settings

    async def validate(self, company, url):
        clean, host, _ = safe_url(url)
        reason = url_reason(clean, company.cnpj)
        if reason:
            return rejected(reason)
        # Cache ligado à identidade inteira para não reaproveitar pontuação de outra empresa.
        key = validation_cache_key(company, clean)
        cached = self.db.cache_get("validation", key)
        if cached is not None and cached.get("rules_version") == RULES_VERSION:
            return Validation(**cached)
        page = await self.fetcher.fetch(clean)
        pages = [page]
        initial = classify(company, page)
        # Somente numa execução online já autorizada: até dois links institucionais
        # explícitos da mesma origem; SafeFetcher aplica SSRF, robots e cotas.
        if not initial.confirmed and initial.site_type == "CORPORATE_CANDIDATE":
            parser = VisibleText()
            parser.feed(page.text)
            links = list(dict.fromkeys(urljoin(page.url, href) for href in parser.links))
            links.sort(key=lambda u: 0 if re.search(r"termos|privacidade|legal", fold(urlsplit(u).path)) else 1)
            used = 0
            for link in links:
                try:
                    normalized, linked_host, _ = safe_url(link)
                except UnsafeURL:
                    continue
                if not same_site(normalized, page.url) or normalized == page.url or url_reason(normalized, company.cnpj):
                    continue
                if not re.search(r"termos|privacidade|legal|contato|sobre", fold(urlsplit(normalized).path)):
                    continue
                used += 1
                try:
                    extra = await self.fetcher.fetch(normalized)
                except QuotaExceeded:
                    raise
                except (ProviderError, UnsafeURL):
                    if used >= 2:
                        break
                    continue
                pages.append(extra)
                if classify_pages(company, pages).confirmed or used >= 2:
                    break
        result = classify_pages(company, pages)
        self.db.cache_put("validation", key, result.to_dict(), self.settings.cache_ttl)
        return result
