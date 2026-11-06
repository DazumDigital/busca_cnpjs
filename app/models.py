from dataclasses import asdict, dataclass, field


@dataclass
class Company:
    cnpj: str
    legal_name: str = ""
    trade_name: str = ""
    city: str = ""
    state: str = ""
    registration_status: str = ""
    main_cnae: str = ""


@dataclass
class Validation:
    website: str = ""
    domain: str = ""
    confidence: str = "NAO_ENCONTRADO"
    score: int = 0
    evidence: list[str] = field(default_factory=list)
    accessible: bool = False
    confirmed: bool = False
    rules_version: str = ""
    site_type: str = "UNKNOWN"
    signals: dict = field(default_factory=dict)
    inspected_urls: list[str] = field(default_factory=list)
    page_evidence: list[dict] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


class ProviderError(Exception):
    def __init__(self, code: str, message: str = "", *, details=None):
        self.code = code
        self.details = details or {}
        super().__init__(message or code)


class QuotaExceeded(ProviderError):
    def __init__(self):
        super().__init__("COTA_ESGOTADA")


class UnsafeURL(ValueError):
    pass
