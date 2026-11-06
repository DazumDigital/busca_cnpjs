from dataclasses import asdict

from app.models import Company
from app.providers.base import CompanyProvider, JsonAPI
from app.services.importer import normalize_cnpj


class BrasilAPI(CompanyProvider):
    def __init__(self, api: JsonAPI):
        self.api = api

    async def lookup(self, cnpj):
        def project(data):
            if normalize_cnpj(data.get("cnpj")) != cnpj or not isinstance(data.get("razao_social"), str) or not data["razao_social"].strip():
                raise ValueError("Identidade ausente ou divergente")
            def text(key):
                value = data.get(key)
                if isinstance(value, (dict, list, bool)):
                    raise ValueError("Campo empresarial incompatível")
                return str(value) if value is not None else ""
            return asdict(Company(cnpj=cnpj, legal_name=text("razao_social"),
                                  trade_name=text("nome_fantasia"), city=text("municipio"),
                                  state=text("uf"), registration_status=text("descricao_situacao_cadastral"),
                                  main_cnae=text("cnae_fiscal")))
        value = await self.api.request("GET", f"https://brasilapi.com.br/api/cnpj/v1/{cnpj}", project=project)
        return Company(**value)
