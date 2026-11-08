"""The API process. It reads the index; it never builds it (section 4.2).

Run locally with stubs:   make dev     (then open http://localhost:8000)
Run in the container:     uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .logjson import configure_logging, request_id_var
from .providers.llm import get_provider
from .retrieval import get_retriever
from .retrieval.base import PinMismatch
from .routes import ask, health, metrics
from .services.cache import make_cache

log = logging.getLogger("rahbar.api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    app.state.settings = settings
    app.state.retriever = get_retriever(settings)
    app.state.provider = get_provider(settings)
    app.state.cache = make_cache(settings.redis_host, settings.redis_port,
                                 settings.cache_threshold, settings.cache_ttl,
                                 settings.cache_max_entries)
    # This process only reads the index; the indexer Job writes it (ADR 0001).
    # The embedding pin (section 4.4): compare the collection's embed_model with ours.
    # A mismatch is logged here, and /readyz keeps answering 503 for as long as it lasts,
    # so the pod sits at 0/1 READY instead of serving vectors from the wrong model.
    log.info(
        "api started",
        extra={"retriever": settings.retriever, "llm_provider": settings.llm_provider,
               "embed_model": settings.embed_model, "top_k": settings.top_k,
               "relevance_floor": settings.relevance_floor, "hybrid": settings.hybrid,
               "cache": settings.redis_host or "disabled"},
    )
    if settings.retriever != "stub":
        try:
            app.state.retriever.check_pin()
            log.info("embedding pin ok", extra={"embed_model": settings.embed_model})
        except PinMismatch as exc:
            log.error("embedding model mismatch: refusing to become ready",
                      extra={"conflict": str(exc), "api_embed_model": settings.embed_model})
        except Exception as exc:   # Chroma not up yet: readiness will keep checking
            log.warning("could not check the embedding pin yet",
                        extra={"error": f"{type(exc).__name__}"})
    yield


app = FastAPI(title="RAHBAR API", lifespan=lifespan)


@app.middleware("http")
async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    token = request_id_var.set(rid)
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = rid
        return response
    finally:
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")   # route template, not the raw URL
        elapsed = time.perf_counter() - start
        metrics.REQUESTS.labels(path=path, status=str(status)).inc()
        metrics.LATENCY.labels(path=path).observe(elapsed)
        if path not in ("/healthz", "/readyz", "/metrics"):
            log.info("request", extra={"path": path, "status": status,
                                       "ms": round(elapsed * 1000, 1)})
        request_id_var.reset(token)


app.include_router(health.router)
app.include_router(metrics.router)
app.include_router(ask.router)

# Development only: `make dev` serves the chat UI from this process, so you can try it
# without a cluster. On Kubernetes the ui Deployment serves it and this stays unset.
_ui_dir = os.environ.get("SERVE_UI_DIR")
if _ui_dir:
    app.mount("/", StaticFiles(directory=_ui_dir, html=True), name="ui")
