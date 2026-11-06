from app.models import ProviderError


class Enrichment:
    def __init__(self, provider):
        self.provider = provider

    async def get(self, cnpj):
        if self.provider is None:
            raise ProviderError("DADOS_NAO_CONFIGURADOS")
        return await self.provider.lookup(cnpj)
