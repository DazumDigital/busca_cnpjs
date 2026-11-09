from app.providers.base import JsonAPI, SearchProvider

SEARCH_ENDPOINT = 'https://google.serper.dev/search'


def search_urls(data):
    organic = data['organic']
    if not isinstance(organic, list):
        raise ValueError('Formato inválido')
    urls = [r['link'] for r in organic if isinstance(r, dict) and isinstance(r.get('link'), str)]
    graph = data.get('knowledgeGraph', {})
    if isinstance(graph, dict) and isinstance(graph.get('website'), str):
        urls.insert(0, graph['website'])
    return list(dict.fromkeys(urls))[:10]


class Serper(SearchProvider):
    """Não persiste snippets, títulos, SERPs integrais ou cabeçalhos com credenciais."""
    def __init__(self, api: JsonAPI, key: str):
        self.api, self.key = api, key

    async def search(self, query):
        return await self.api.request("POST", SEARCH_ENDPOINT,
                                      payload={"q": query, "gl": "br", "hl": "pt-br", "num": 10},
                                      headers={"X-API-KEY": self.key}, project=search_urls)
