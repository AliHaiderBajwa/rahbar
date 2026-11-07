"""Liveness and readiness (section 4.3). They answer different questions.

/healthz: is this process alive? It touches nothing: if it checked Chroma, one slow
          Chroma would make the kubelet kill every api pod at once.
/readyz:  can this pod take traffic right now? Chroma reachable, the collection exists
          and is non-empty, its embedding model matches ours (section 4.4), and Redis
          answers. Any failure -> 503 naming the dependency, and the pod is taken out of
          the Service (not restarted).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response

from ..retrieval.base import PinMismatch
from . import metrics

router = APIRouter()
log = logging.getLogger("rahbar.health")


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Is the process alive? Must never touch Chroma, Redis or the LLM."""
    return {"status": "alive"}


def readiness_checks(state: object) -> dict[str, str]:
    """Run every dependency check; each value is 'ok', 'skipped: ...' or the failure."""
    checks: dict[str, str] = {}
    settings = state.settings  # type: ignore[attr-defined]
    retriever = state.retriever  # type: ignore[attr-defined]
    cache = state.cache  # type: ignore[attr-defined]

    if settings.retriever == "stub":
        checks["chroma"] = checks["collection"] = checks["embed_model"] = "skipped: RETRIEVER=stub"
    else:
        try:
            retriever.heartbeat()
            checks["chroma"] = "ok"
        except Exception as exc:
            checks["chroma"] = f"unreachable: {type(exc).__name__}"
        if checks["chroma"] == "ok":
            try:
                info = retriever.index_info()
                metrics.INDEX_CHUNKS.set(info.count)
                if not info.exists:
                    checks["collection"] = f"collection '{settings.collection}' does not exist"
                elif info.is_empty:
                    checks["collection"] = f"collection '{settings.collection}' is empty"
                else:
                    checks["collection"] = "ok"
                try:
                    retriever.check_pin(info)
                    checks["embed_model"] = "ok" if info.exists else "unknown: no collection"
                except PinMismatch as exc:
                    checks["embed_model"] = str(exc)
            except Exception as exc:
                checks["collection"] = f"error: {type(exc).__name__}"
        else:
            checks["collection"] = checks["embed_model"] = "not checked: chroma unreachable"

    if cache is None:
        checks["redis"] = "skipped: REDIS_HOST not set"
    else:
        try:
            checks["redis"] = "ok" if cache.ping() else "no PONG"
        except Exception as exc:
            checks["redis"] = f"unreachable: {type(exc).__name__}"
    return checks


@router.get("/readyz")
def readyz(request: Request, response: Response) -> dict[str, object]:
    checks = readiness_checks(request.app.state)
    failed = [name for name, v in checks.items() if v != "ok" and not v.startswith("skipped")]
    if failed:
        response.status_code = 503
        if checks.get("embed_model", "ok") not in ("ok",) and "mismatch" in checks["embed_model"]:
            log.error("not ready: embedding pin", extra={"conflict": checks["embed_model"]})
        return {"status": "not ready", "failed": failed, "checks": checks}
    return {"status": "ready", "checks": checks}
