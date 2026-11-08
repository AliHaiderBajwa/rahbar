"""POST /api/ask. The Ingress sends /api/* here unchanged, so the prefix stays."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..logjson import request_id_var
from ..schemas import AskRequest, AskResponse
from ..services.answer import answer_question

router = APIRouter(prefix="/api")
log = logging.getLogger("rahbar.ask")


@router.post("/ask", response_model=AskResponse)
def ask(body: AskRequest, request: Request) -> AskResponse | JSONResponse:
    state = request.app.state
    try:
        index_id = state.retriever.current_index_id() if state.cache is not None else "none"
        response = answer_question(body.question, state.retriever, state.provider,
                                   state.settings, cache=state.cache, index_id=index_id)
    except Exception as exc:
        # Retrieval itself failed (Chroma or the embedder is down). Readiness will pull
        # this pod out of the Service shortly; until then answer with a clear 503.
        log.error("retrieval unavailable", extra={"error": f"{type(exc).__name__}: {exc}"[:300]})
        return JSONResponse(status_code=503, content={
            "detail": "The document index is temporarily unavailable. Please try again.",
            "request_id": request_id_var.get()})
    if response.cached:
        return JSONResponse(content=response.model_dump(), headers={"X-Cache": "HIT"})
    return response
