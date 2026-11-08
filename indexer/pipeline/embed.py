"""Stage 4: Embed. One vector per chunk, from the pinned model only."""

from __future__ import annotations

import json
import time

import httpx

from .config import IndexerSettings

BATCH = 16
ATTEMPTS = 4
TIMEOUT = httpx.Timeout(120.0, connect=10.0)

# nomic-embed-text was trained with task prefixes: documents and queries go through
# different prefixes, and leaving them out costs recall. The API applies the query one
# (api/app/retrieval/embedder.py); both sides key it on the same EMBED_MODEL value.
DOC_PREFIX = {"nomic-embed-text": "search_document: "}


def document_prefix(model: str) -> str:
    return DOC_PREFIX.get(model.split(":")[0], "")


def _post(client: httpx.Client, url: str, payload: dict) -> list[list[float]]:
    last = ""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            r = client.post(url, json=payload)
            if r.status_code == 200:
                vectors = r.json().get("embeddings") or []
                if len(vectors) != len(payload["input"]):
                    raise ValueError(f"asked for {len(payload['input'])} vectors, got {len(vectors)}")
                return [list(map(float, v)) for v in vectors]
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code == 404 and "not found" in r.text:
                break   # the model is not pulled: retrying will not help
        except (httpx.TransportError, ValueError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(2 ** attempt)
    raise RuntimeError(f"embedding failed after {ATTEMPTS} attempts: {last}")


def embed(texts: list[str], settings: IndexerSettings) -> list[list[float]]:
    """Embed with settings.embed_model and nothing else.

    With Ollama: POST {ollama_url}/api/embed with {"model": ..., "input": [texts]}
    returns {"embeddings": [[...], ...]}. Batch the calls; one request per chunk is slow.
    """
    prefix = document_prefix(settings.embed_model)
    url = settings.ollama_url.rstrip("/") + "/api/embed"
    vectors: list[list[float]] = []
    started = time.perf_counter()
    with httpx.Client(timeout=TIMEOUT) as client:
        for i in range(0, len(texts), BATCH):
            batch = [prefix + t for t in texts[i:i + BATCH]]
            vectors.extend(_post(client, url, {"model": settings.embed_model, "input": batch,
                                               "truncate": True}))
    dims = {len(v) for v in vectors}
    if len(dims) > 1:
        raise RuntimeError(f"mixed vector dimensions from one model: {sorted(dims)}")
    print(json.dumps({"logger": "rahbar.indexer.embed", "msg": "embedded",
                      "model": settings.embed_model, "chunks": len(vectors),
                      "dimension": dims.pop() if dims else 0,
                      "seconds": round(time.perf_counter() - started, 1)}), flush=True)
    return vectors
