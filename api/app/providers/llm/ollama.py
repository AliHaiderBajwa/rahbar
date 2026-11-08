"""Ollama provider: ollama-host or ollama-incluster (section 3.5)."""

from __future__ import annotations

import httpx

from ...config import Settings
from .base import Completion, GenerationError, JsonChatProvider
from .prompt import answer_schema


class OllamaProvider(JsonChatProvider):
    name = "ollama"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.url = settings.ollama_url.rstrip("/") + "/api/chat"
        self.client = httpx.Client(timeout=httpx.Timeout(settings.llm_timeout, connect=5.0))

    def complete(self, messages: list[dict[str, str]]) -> Completion:
        payload = {
            "model": self.settings.llm_model,
            "messages": messages,
            "stream": False,
            # Constrained decoding: Ollama only emits tokens that fit the Answer schema.
            "format": answer_schema(),
            "options": {"temperature": self.settings.temperature,
                        "num_predict": self.settings.max_tokens},
        }
        try:
            r = self.client.post(self.url, json=payload)
        except httpx.TransportError as exc:
            raise GenerationError(f"ollama unreachable: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise GenerationError(f"ollama returned HTTP {r.status_code}: {r.text[:200]}")
        body = r.json()
        return Completion(text=(body.get("message") or {}).get("content", ""),
                          tokens_in=int(body.get("prompt_eval_count", 0)),
                          tokens_out=int(body.get("eval_count", 0)))
