from app.providers.base import JsonAPI, SearchProvider


class Serper(SearchProvider):
    """Não persiste snippets, títulos, SERPs integrais ou cabeçalhos com credenciais."""
    def __init__(self, api: JsonAPI, key: str):
        self.api, self.key = api, key

    async def search(self, query):
        def project(data):
            organic = data["organic"]
            if not isinstance(organic, list):
                raise ValueError("Formato inválido")
            urls = [r["link"] for r in organic if isinstance(r, dict) and isinstance(r.get("link"), str)]
            graph = data.get("knowledgeGraph", {})
            if isinstance(graph, dict) and isinstance(graph.get("website"), str):
                urls.insert(0, graph["website"])
            return list(dict.fromkeys(urls))[:10]
        return await self.api.request("POST", "https://google.serper.dev/search",
                                      payload={"q": query, "gl": "br", "hl": "pt-br", "num": 10},
                                      headers={"X-API-KEY": self.key}, project=project)
