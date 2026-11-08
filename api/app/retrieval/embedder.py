"""Query embedding through Ollama, with the same pinned model the indexer used."""

from __future__ import annotations

import httpx

# Must mirror indexer/pipeline/embed.py: nomic-embed-text uses task prefixes, and the
# query side takes 'search_query: ' where documents took 'search_document: '.
QUERY_PREFIX = {"nomic-embed-text": "search_query: "}


def query_prefix(model: str) -> str:
    return QUERY_PREFIX.get(model.split(":")[0], "")


class OllamaEmbedder:
    def __init__(self, base_url: str, model: str, timeout: float = 15.0) -> None:
        self.url = base_url.rstrip("/") + "/api/embed"
        self.model = model
        self.client = httpx.Client(timeout=httpx.Timeout(timeout, connect=5.0))

    def embed(self, text: str) -> list[float]:
        payload = {"model": self.model, "input": [query_prefix(self.model) + text],
                   "truncate": True}
        last: Exception | None = None
        for _ in range(2):                       # one retry on a transport blip
            try:
                r = self.client.post(self.url, json=payload)
                r.raise_for_status()
                return [float(x) for x in r.json()["embeddings"][0]]
            except httpx.TransportError as exc:
                last = exc
        raise RuntimeError(f"query embedding failed: {last!r}")
