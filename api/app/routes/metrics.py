"""Prometheus metrics at /metrics (section 4.8)."""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

router = APIRouter()

REQUESTS = Counter("rahbar_requests_total", "HTTP requests", ["path", "status"])
LATENCY = Histogram(
    "rahbar_request_seconds",
    "HTTP request latency",
    ["path"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)
RETRIEVAL_SECONDS = Histogram(
    "rahbar_retrieval_seconds", "Query embedding plus vector search",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5))
GENERATION_SECONDS = Histogram(
    "rahbar_generation_seconds", "LLM call(s), including the one validation retry",
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60))
CACHE_LOOKUPS = Counter("rahbar_cache_lookups_total", "Semantic cache lookups", ["result"])
CACHE_HIT_RATIO = Gauge("rahbar_cache_hit_ratio", "hits / (hits + misses) since this pod started")
ABSTENTIONS = Counter("rahbar_abstentions_total", "Answers with answered_from_context=false",
                      ["reason"])
FALLBACKS = Counter("rahbar_fallbacks_total", "Safe canned responses served", ["reason"])
CITATIONS_DROPPED = Counter("rahbar_citations_dropped_total",
                            "Model citations that matched no retrieved chunk")
TOKENS_IN = Histogram("rahbar_tokens_in", "Prompt tokens per request",
                      buckets=(250, 500, 1000, 1500, 2000, 3000, 4000, 6000, 8000))
TOKENS_OUT = Histogram("rahbar_tokens_out", "Completion tokens per request",
                       buckets=(25, 50, 100, 200, 300, 500, 800, 1200))
INDEX_CHUNKS = Gauge("rahbar_index_chunks", "Chunks in the live collection, seen by /readyz")

_hits = 0
_misses = 0


def record_cache(result: str) -> None:
    """result is hit, miss or skip (skip = not eligible: follow-up, no cache, error)."""
    global _hits, _misses
    CACHE_LOOKUPS.labels(result=result).inc()
    if result == "hit":
        _hits += 1
    elif result == "miss":
        _misses += 1
    if _hits + _misses:
        CACHE_HIT_RATIO.set(_hits / (_hits + _misses))


@router.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
