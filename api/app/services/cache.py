"""Semantic cache in Redis (section 3.3, ADR 0003).

A hit is a stored question whose embedding has cosine similarity >= CACHE_THRESHOLD
with the new question's embedding. Entries live in one Redis list per index:

    rahbar:cache:<index_id>   newest first, capped at CACHE_MAX_ENTRIES, TTL CACHE_TTL

index_id is written by the indexer into the Chroma collection's metadata on every
re-index. A new index means a new key, so answers built from the old index can never be
served again; `invalidate_except` also deletes the old keys the first time a pod sees
the new index_id. The indexer never needs network access to Redis for this, which is
what lets the NetworkPolicy allow redis from the api only.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import struct
from typing import Any

log = logging.getLogger("rahbar.cache")

PREFIX = "rahbar:cache:"
TURNS = "rahbar:turns"
MAX_TURNS = 1000


def _pack(vector: list[float]) -> str:
    return base64.b64encode(struct.pack(f"{len(vector)}f", *vector)).decode()


def _unpack(blob: str) -> list[float]:
    raw = base64.b64decode(blob)
    return list(struct.unpack(f"{len(raw) // 4}f", raw))


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class SemanticCache:
    def __init__(self, client: Any, threshold: float, ttl: int, max_entries: int) -> None:
        self.r = client
        self.threshold = threshold
        self.ttl = ttl
        self.max_entries = max_entries
        self.current_index: str | None = None

    @staticmethod
    def key(index_id: str) -> str:
        return PREFIX + index_id

    def ping(self) -> bool:
        return bool(self.r.ping())

    def invalidate_except(self, index_id: str) -> int:
        """Delete cache entries from every other index. Runs when this pod first sees a
        new index_id, i.e. right after a re-index."""
        if self.current_index == index_id:
            return 0
        removed = 0
        for k in self.r.scan_iter(match=PREFIX + "*"):
            name = k.decode() if isinstance(k, bytes) else k
            if name != self.key(index_id):
                removed += int(self.r.delete(name))
        if self.current_index is not None or removed:
            log.info("cache invalidated for new index",
                     extra={"index_id": index_id, "keys_removed": removed})
        self.current_index = index_id
        return removed

    def lookup(self, index_id: str, vector: list[float]) -> tuple[dict, float] | None:
        best: tuple[dict, float] | None = None
        for raw in self.r.lrange(self.key(index_id), 0, self.max_entries - 1):
            entry = json.loads(raw)
            sim = cosine(vector, _unpack(entry["v"]))
            if sim >= self.threshold and (best is None or sim > best[1]):
                best = (entry["response"], sim)
        return best

    def store(self, index_id: str, vector: list[float], question: str, response: dict) -> None:
        k = self.key(index_id)
        entry = json.dumps({"q": question, "v": _pack(vector), "response": response})
        pipe = self.r.pipeline()
        pipe.lpush(k, entry)
        pipe.ltrim(k, 0, self.max_entries - 1)
        pipe.expire(k, self.ttl)
        pipe.execute()

    def record_turn(self, turn: dict) -> None:
        """Section 3.2 step 8: store the turn (bounded, for debugging and analytics)."""
        pipe = self.r.pipeline()
        pipe.lpush(TURNS, json.dumps(turn))
        pipe.ltrim(TURNS, 0, MAX_TURNS - 1)
        pipe.execute()


def make_cache(host: str, port: int, threshold: float, ttl: int, max_entries: int
               ) -> SemanticCache | None:
    if not host:
        return None
    import redis

    client = redis.Redis(host=host, port=port, socket_timeout=1.0, socket_connect_timeout=1.0)
    return SemanticCache(client, threshold, ttl, max_entries)
