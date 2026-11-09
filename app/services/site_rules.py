"""Regras locais extensíveis; importar este módulo não acessa a rede."""
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit

from app.services.importer import valid_cnpj

_raw = Path(__file__).with_name("third_party_rules.json").read_bytes()
RULES = json.loads(_raw)
RULES_VERSION = RULES["version"] + ":validation8:" + hashlib.sha256(_raw).hexdigest()[:12]
THIRD_PARTIES = frozenset(RULES["domains"] + RULES.get('social_domains',[]) + RULES.get('directory_domains',[]))
CNPJ_RE = re.compile(r"(?<![a-zA-Z0-9])[0-9]{2}\.?[0-9]{3}\.?[0-9]{3}/?[0-9]{4}-?[0-9]{2}(?![a-zA-Z0-9])")


def extract_cnpjs(text):
    values = {re.sub(r"[^0-9]", "", m.group()) for m in CNPJ_RE.finditer(text)}
    return {v for v in values if valid_cnpj(v)}


def domain_reason(host):
    host = host.lower().rstrip(".")
    if any(host == d or host.endswith("." + d) for d in THIRD_PARTIES):
        return "DOMINIO_TERCEIRO"
    if any(host.endswith(suffix) for suffix in RULES["host_suffixes"]):
        return "FONTE_INSTITUCIONAL_DE_TERCEIRO"
    return ""


def url_reason(url, cnpj=None):
    parts = urlsplit(url)
    path_query = unquote(parts.path + ("?" + parts.query if parts.query else "")).lower()
    numbers = extract_cnpjs(path_query)
    if cnpj and numbers and cnpj not in numbers:
        return "CNPJ_DIVERGENTE_URL"
    reason = domain_reason(parts.hostname or "")
    if reason:
        return reason
    if any(re.search(pattern, path_query) for pattern in RULES["url_patterns"]):
        return "PADRAO_URL_DE_TERCEIRO_OU_DOCUMENTO"
    return ""


def candidate_kind(url):
    """Categoria de origem; não confirma vínculo nem torna o domínio corporativo."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or '').lower().rstrip('.')
        if parts.scheme not in {'http','https'} or not host:
            return 'UNSAFE_URL'
    except ValueError:
        return 'UNSAFE_URL'
    for key, kind in (('social_domains','SOCIAL_NETWORK'),('directory_domains','BUSINESS_DIRECTORY')):
        if any(host == d or host.endswith('.'+d) for d in RULES.get(key,[])):
            return kind
    reason = url_reason(url)
    path = unquote(parts.path).lower()
    if reason and re.search(r'(?:^|/)(?:guia-comercial|guia-de-empresas|business-directory|categorias?|consulta-cnpj|cnpj)(?:/|$)',path):
        return 'BUSINESS_DIRECTORY'
    return 'THIRD_PARTY' if reason else 'UNVERIFIED_CANDIDATE'


def candidate_groups(urls):
    groups = {'social_networks':[], 'business_directories':[]}
    for url in dict.fromkeys(urls):
        kind = candidate_kind(url)
        if kind == 'SOCIAL_NETWORK':groups['social_networks'].append(url)
        elif kind == 'BUSINESS_DIRECTORY':groups['business_directories'].append(url)
    return groups


def folded(text):
    return re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()).strip()


def brand_tokens(company):
    generic = {"ltda", "limitada", "eireli", "comercio", "comercial", "representacoes", "servicos", "empresa",
               "industria", "industrial", "import", "importacao", "exportacao", "confeccoes", "solucoes", "editora"}
    names = [company.trade_name, company.legal_name]
    return {t for name in names for t in folded(name).split() if len(t) >= 3 and not t.isdigit() and t not in generic}


def domain_brand_match(company, host):
    # Aproximação conservadora para os sufixos usuais; uma marca em subdomínio
    # de um portal não basta (thp.portal.com continua sendo portal.com).
    labels = host.lower().rstrip(".").split(".")
    if len(labels) < 2:
        return False
    index = -3 if len(labels) >= 3 and labels[-1] in {"br", "uk", "au", "nz"} and labels[-2] in {
        "com", "net", "org", "co", "ind", "edu", "gov"} else -2
    label = labels[index]
    return any(label.startswith(token) for token in brand_tokens(company))


def tenant_brand_match(company, host):
    """Indício estrutural de marca no subdomínio; nunca confirma titularidade sozinho."""
    labels = host.lower().rstrip('.').removeprefix('www.').split('.')
    registered = 3 if len(labels) >= 3 and labels[-1] in {'br','uk','au','nz'} and labels[-2] in {
        'com','net','org','co','ind','edu','gov'} else 2
    tenant = labels[:-registered]
    return len(tenant) == 1 and any(tenant[0].startswith(t) for t in brand_tokens(company))


def candidate_priority(company, url):
    host = urlsplit(url).hostname or ""
    path = folded(unquote(urlsplit(url).path))
    return (100 if domain_brand_match(company, host) else 0) + (20 if not path else 0) + (
        10 if any(word in path.split() for word in ("sobre", "contato", "institucional", "termos")) else 0)


def rank_candidates(company, urls):
    return sorted(dict.fromkeys(urls), key=lambda url: -candidate_priority(company, url))


def requires_reclassification(validation):
    return bool(validation and validation.get("website") and validation.get("rules_version") != RULES_VERSION)


def content_directory_reason(title, content=''):
    heading, body = folded(title), folded(content)
    ownership_disclaimer = ('empresa nao reivindicada', 'reivindique esta empresa', 'reivindicar esta empresa',
                            'voce e o proprietario desta empresa', 'cadastre sua empresa')
    if any(marker in heading or marker in body for marker in ownership_disclaimer):
        return 'PERFIL_DE_TERCEIRO_OU_EMPRESA_NAO_REIVINDICADA'
    if any(re.search(pattern, heading) for pattern in RULES['directory_heading_patterns']):
        return 'TITULO_DE_DIRETORIO_OU_CONSULTA'
    if sum(bool(re.search(pattern, body)) for pattern in RULES['directory_content_patterns']) >= 2:
        return 'CONTEUDO_DE_DIRETORIO_OU_CONSULTA_CADASTRAL'
    return ''
