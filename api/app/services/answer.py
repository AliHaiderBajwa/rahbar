"""Turns a question into an AskResponse: the eight steps of section 3.2.

1 validate (Pydantic, in the route)  2 cache check  3 embed  4 retrieve (+ superseded
filter)  5 augment  6 generate  7 validate, retry once, fall back  8 store turn + cache.

Each rule lives in its own module so it can be unit-tested: grounding.py (relevance
floor, citation guard, follow-ups), cache.py (semantic cache), providers/llm/base.py
(schema validation and the retry).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..config import Settings
from ..logjson import request_id_var
from ..providers.llm.base import GenerationError, LLMProvider
from ..retrieval.base import Retriever
from ..retrieval.chroma import wants_older_version
from ..routes import metrics as m
from ..schemas import Answer, AskResponse, Passage, RetrievedRef
from .cache import SemanticCache
from .grounding import (
    FALLBACK_TEXT,
    UNGROUNDED_TEXT,
    abstention,
    below_floor,
    is_followup,
    valid_citations,
)

log = logging.getLogger("rahbar.answer")


def _refs(passages: list[Passage]) -> list[RetrievedRef]:
    return [RetrievedRef(chunk_id=p.chunk_id, source=p.source, section=p.section, page=p.page,
                         score=p.score, doc_version=p.doc_version) for p in passages]


def _generate(question: str, passages: list[Passage], provider: LLMProvider
              ) -> tuple[Answer, str | None, dict[str, int]]:
    """Call the model. Returns (answer, fallback_reason or None, usage)."""
    started = time.perf_counter()
    try:
        answer = provider.generate(question, passages)
        reason = None
    except GenerationError as exc:
        log.warning("generation failed, serving fallback", extra={"error": str(exc)[:300]})
        answer, reason = abstention("fallback", FALLBACK_TEXT), "invalid_output"
    except Exception as exc:  # timeout, provider down: still never a 500
        log.error("provider error, serving fallback",
                  extra={"error": f"{type(exc).__name__}: {str(exc)[:200]}"})
        answer, reason = abstention("fallback", FALLBACK_TEXT), "provider_error"
    m.GENERATION_SECONDS.observe(time.perf_counter() - started)
    usage = getattr(provider, "last_usage", None)
    tokens = {"in": getattr(usage, "tokens_in", 0), "out": getattr(usage, "tokens_out", 0),
              "attempts": getattr(usage, "attempts", 1)}
    return answer, reason, tokens


def answer_question(question: str, retriever: Retriever, provider: LLMProvider,
                    settings: Settings, cache: SemanticCache | None = None,
                    index_id: str = "none") -> AskResponse:
    rid = request_id_var.get()
    followup = is_followup(question)

    # 3. Embed once; the vector serves the cache and the retriever.
    t0 = time.perf_counter()
    vector = retriever.embed_query(question)

    # 2. Cache check (skipped for follow-ups and when there is no cache or no vector).
    cache_result = "skip"
    if cache is not None and vector is not None and not followup:
        try:
            cache.invalidate_except(index_id)
            hit = cache.lookup(index_id, vector)
            cache_result = "hit" if hit else "miss"
            if hit:
                payload, sim = hit
                m.record_cache("hit")
                m.RETRIEVAL_SECONDS.observe(time.perf_counter() - t0)
                log.info("answered", extra={"cache": "hit", "similarity": round(sim, 4),
                                            "answered_from_context": True,
                                            "chunk_ids": [r["chunk_id"] for r in
                                                          payload.get("retrieved", [])]})
                return AskResponse(**{**payload, "request_id": rid, "cached": True})
        except Exception as exc:  # a broken cache degrades to no cache, never to an error
            log.warning("cache lookup failed", extra={"error": str(exc)[:200]})
            cache_result = "skip"
    m.record_cache(cache_result)

    # 4. Retrieve, excluding superseded documents unless the user asked for an old version.
    include_old = wants_older_version(question)
    passages = retriever.retrieve(question, k=settings.top_k, vector=vector,
                                  include_superseded=include_old)
    m.RETRIEVAL_SECONDS.observe(time.perf_counter() - t0)

    fallback_reason: str | None = None
    tokens = {"in": 0, "out": 0, "attempts": 0}
    dropped = 0
    if below_floor(passages, settings.relevance_floor):
        # Honest abstention before spending a model call (section 3.4).
        answer = abstention(f"best score below RELEVANCE_FLOOR={settings.relevance_floor}")
        abstain_reason: str | None = "relevance_floor"
    else:
        # 5-7. Augment, generate, validate (with one retry inside the provider), fall back.
        answer, fallback_reason, tokens = _generate(question, passages, provider)
        abstain_reason = "fallback" if fallback_reason else None
        # The citation guard: every citation must match a chunk retrieved for this request.
        kept, dropped = valid_citations(answer.citations, passages)
        if dropped:
            m.CITATIONS_DROPPED.inc(dropped)
        answer = answer.model_copy(update={"citations": kept})
        if answer.answered_from_context and not kept:
            answer = abstention("no valid citation", UNGROUNDED_TEXT)
            abstain_reason = "no_valid_citation"
        elif not answer.answered_from_context and not abstain_reason:
            abstain_reason = "model"
            answer = answer.model_copy(update={"citations": []})

    if fallback_reason:
        m.FALLBACKS.labels(reason=fallback_reason).inc()
    if abstain_reason:
        m.ABSTENTIONS.labels(reason=abstain_reason).inc()
    if tokens["in"] or tokens["out"]:
        m.TOKENS_IN.observe(tokens["in"])
        m.TOKENS_OUT.observe(tokens["out"])

    response = AskResponse(
        request_id=rid, answer=answer.answer, citations=answer.citations,
        answered_from_context=answer.answered_from_context, confidence=answer.confidence,
        retrieved=_refs(passages), provider=provider.name,
    )

    # One line per request with everything needed to debug stages A-E (section 4.8).
    log.info("answered", extra={
        "retriever": retriever.name, "provider": provider.name, "top_k": settings.top_k,
        "cache": cache_result, "include_superseded": include_old,
        "chunk_ids": [p.chunk_id for p in passages],
        "scores": [round(p.score, 4) for p in passages],
        "answered_from_context": answer.answered_from_context,
        "abstain_reason": abstain_reason, "fallback": fallback_reason,
        "citations": len(answer.citations), "citations_dropped": dropped,
        "tokens_in": tokens["in"], "tokens_out": tokens["out"], "attempts": tokens["attempts"],
    })

    # 8. Store the turn, and a cache entry only when it is safe to share with everyone.
    if cache is not None:
        _store(cache, index_id, vector, question, response, followup, abstain_reason, rid)
    return response


def _store(cache: SemanticCache, index_id: str, vector: list[float] | None, question: str,
           response: AskResponse, followup: bool, abstain_reason: str | None, rid: str) -> None:
    never_cache = (followup or vector is None or abstain_reason is not None
                   or not response.answered_from_context or not response.citations)
    try:
        cache.record_turn({"request_id": rid, "ts": int(time.time()), "question": question,
                           "answered_from_context": response.answered_from_context,
                           "chunk_ids": [r.chunk_id for r in response.retrieved]})
        if not never_cache and vector is not None:
            payload: dict[str, Any] = response.model_dump()
            payload.pop("request_id", None)
            cache.store(index_id, vector, question, payload)
    except Exception as exc:
        log.warning("cache store failed", extra={"error": str(exc)[:200]})
