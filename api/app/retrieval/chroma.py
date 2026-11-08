"""The real retriever (sections 3.2 and 4.4). Read-only: it never writes to Chroma.

Two legs, fused with reciprocal-rank fusion when HYBRID=true:
- dense:   the question's vector against every chunk (cosine)
- lexical: the same vector, but only over chunks that literally contain an exact term
           from the question ('5.4', '2.00', 'CS1002'). Embeddings blur exact terms;
           a substring filter does not. This is our hybrid-retrieval step (ENGINEERING
           NOTES question 5 has the before/after numbers).
Both legs exclude superseded documents unless the question explicitly asks about an
older version.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from ..config import Settings
from ..schemas import Passage
from .base import IndexInfo, PinMismatch
from .embedder import OllamaEmbedder

log = logging.getLogger("rahbar.retrieval")

RRF_K = 60
MAX_EXACT_TERMS = 3

# '5.4', '1.21', '2.00', 'CS1002', 'MT-1003', '10,000'
_EXACT = re.compile(r"\b(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?|[A-Z]{2,4}[- ]?\d{3,4}|\d{1,3}(?:,\d{3})+)\b")
# An explicit request for an older edition: 'the 2023 rules', 'the previous policy',
# 'what did the old handbook say'. A bare 'earlier' is not enough: 'ignore earlier
# instructions' is an injection attempt, not a question about the 2023 rules.
_OLDER = re.compile(
    r"\b(19\d\d|20(?:0\d|1\d|2[0-5]))\b"
    r"|\b(old|older|previous|earlier|former|prior|superseded|repealed|outdated)\s+(?:\w+\s+){0,2}"
    r"(version|edition|rules?|polic(?:y|ies)|handbook|regulations?|catalog(?:ue)?|years?)\b"
    r"|\bused to\b", re.I)


def wants_older_version(question: str) -> bool:
    return bool(_OLDER.search(question))


def exact_terms(question: str) -> list[str]:
    seen: list[str] = []
    for m in _EXACT.finditer(question):
        term = m.group(1)
        if term not in seen:
            seen.append(term)
    return seen[:MAX_EXACT_TERMS]


def rrf_merge(legs: list[list[Passage]], k: int) -> list[Passage]:
    """Reciprocal-rank fusion: a chunk ranked high by either leg rises."""
    score: dict[str, float] = {}
    best: dict[str, Passage] = {}
    for leg in legs:
        for rank, p in enumerate(leg):
            score[p.chunk_id] = score.get(p.chunk_id, 0.0) + 1.0 / (RRF_K + rank + 1)
            best.setdefault(p.chunk_id, p)
    order = sorted(score, key=lambda cid: (-score[cid], -best[cid].score))
    return [best[cid] for cid in order[:k]]


class ChromaRetriever:
    name = "chroma"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.embedder = OllamaEmbedder(settings.ollama_url, settings.embed_model)
        self._client: Any = None

    # The client is created lazily: chromadb.HttpClient talks to the server in its
    # constructor, and an API that crashes at import because Chroma is still starting
    # would CrashLoop instead of simply reporting not-ready.
    @property
    def client(self) -> Any:
        if self._client is None:
            import chromadb  # the thin client, from chromadb-client in requirements.txt
            from chromadb.config import Settings as ChromaSettings

            self._client = chromadb.HttpClient(
                host=self.settings.chroma_host, port=self.settings.chroma_port,
                settings=ChromaSettings(anonymized_telemetry=False))
        return self._client

    def heartbeat(self) -> None:
        self.client.heartbeat()

    def _collection(self) -> Any:
        return self.client.get_collection(self.settings.collection)

    def index_info(self) -> IndexInfo:
        try:
            col = self._collection()
        except Exception as exc:
            if "does not exist" in str(exc) or "NotFound" in type(exc).__name__:
                return IndexInfo(exists=False)
            raise
        meta = dict(col.metadata or {})
        dim = meta.get("dimension")
        return IndexInfo(exists=True, count=int(col.count()), embed_model=meta.get("embed_model"),
                         dimension=int(dim) if dim is not None else None,
                         index_id=str(meta.get("index_id", "none")), metadata=meta)

    def current_index_id(self) -> str:
        """The live index's id (one cheap metadata read). It namespaces the cache."""
        return str((self._collection().metadata or {}).get("index_id", "none"))

    def check_pin(self, info: IndexInfo | None = None) -> None:
        """Raise PinMismatch when the index and this API disagree on the embedding model."""
        info = info or self.index_info()
        if info.exists and info.embed_model != self.settings.embed_model:
            raise PinMismatch(
                f"embedding model mismatch: collection '{self.settings.collection}' was "
                f"indexed with embed_model={info.embed_model!r} (dimension={info.dimension}) "
                f"but this API has EMBED_MODEL={self.settings.embed_model!r}")

    def embed_query(self, question: str) -> list[float] | None:
        return self.embedder.embed(question)

    def _query(self, col: Any, vector: list[float], n: int, where: dict | None,
               contains: str | None = None) -> list[Passage]:
        kwargs: dict[str, Any] = {"query_embeddings": [vector], "n_results": n,
                                  "include": ["documents", "metadatas", "distances"]}
        if where:
            kwargs["where"] = where
        if contains:
            kwargs["where_document"] = {"$contains": contains}
        res = col.query(**kwargs)
        out = []
        for cid, doc, meta, dist in zip(res["ids"][0], res["documents"][0],
                                        res["metadatas"][0], res["distances"][0]):
            meta = meta or {}
            out.append(Passage(
                chunk_id=cid, text=doc or "", source=str(meta.get("source", "?")),
                section=str(meta.get("section", "?")),
                page=int(meta["page"]) if meta.get("page") is not None else None,
                score=round(1.0 - float(dist), 4),          # cosine distance -> similarity
                doc_version=str(meta.get("doc_version")) if meta.get("doc_version") else None,
                superseded=bool(meta.get("superseded", False)),
            ))
        return out

    def retrieve(self, question: str, k: int, vector: list[float] | None = None,
                 include_superseded: bool = False) -> list[Passage]:
        vector = vector if vector is not None else self.embedder.embed(question)
        col = self._collection()
        meta = col.metadata or {}
        if meta.get("dimension") and int(meta["dimension"]) != len(vector):
            raise PinMismatch(f"query vector has {len(vector)} dimensions, index has "
                              f"{meta['dimension']}: embedding model mismatch")
        # SUPERSEDED_FILTER=off exists only to break stage E on purpose (FAILURE-ANALYSIS.md).
        filter_on = os.environ.get("SUPERSEDED_FILTER", "on") != "off"
        where = {"superseded": False} if filter_on and not include_superseded else None
        legs = [self._query(col, vector, k, where)]
        if self.settings.hybrid:
            for term in exact_terms(question):
                try:
                    legs.append(self._query(col, vector, k, where, contains=term))
                except Exception as exc:   # a lexical leg must never fail the request
                    log.warning("lexical leg failed", extra={"term": term, "error": str(exc)})
        return rrf_merge(legs, k) if len(legs) > 1 else legs[0]
