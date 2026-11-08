"""Hosted free-tier provider over an OpenAI-compatible API (we use Groq).

The key arrives as LLM_API_KEY from the rahbar-secret Secret, created on the command
line (`make secret`). It never appears in a file we commit, and never in a log line:
the error messages below carry the status code and the provider's message only.
"""

from __future__ import annotations

import time

import httpx

from ...config import Settings
from .base import Completion, GenerationError, JsonChatProvider

MAX_RATE_LIMIT_WAIT = 20.0    # seconds we will wait on a 429 before giving up


class HostedProvider(JsonChatProvider):
    name = "hosted"

    def __init__(self, settings: Settings) -> None:
        if not settings.hosted_base_url:
            raise ValueError("LLM_PROVIDER=hosted needs HOSTED_BASE_URL in the ConfigMap")
        self.settings = settings
        self.url = settings.hosted_base_url.rstrip("/") + "/chat/completions"
        self.client = httpx.Client(timeout=httpx.Timeout(settings.llm_timeout, connect=5.0))

    def complete(self, messages: list[dict[str, str]]) -> Completion:
        if not self.settings.llm_api_key:
            raise GenerationError("LLM_API_KEY is empty: create rahbar-secret (make secret)")
        payload = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
            # JSON mode: the strongest enforcement every Groq model supports. The schema
            # itself is in the system prompt, and Pydantic validates the result anyway.
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}"}
        waited = 0.0
        while True:
            try:
                r = self.client.post(self.url, json=payload, headers=headers)
            except httpx.TransportError as exc:
                raise GenerationError(f"hosted LLM unreachable: {type(exc).__name__}") from exc
            if r.status_code == 429 and waited < MAX_RATE_LIMIT_WAIT:
                pause = min(float(r.headers.get("retry-after", "2") or 2), 10.0)
                time.sleep(pause)
                waited += pause
                continue
            if r.status_code != 200:
                raise GenerationError(f"hosted LLM returned HTTP {r.status_code}: "
                                      f"{r.text[:200]}")
            body = r.json()
            usage = body.get("usage") or {}
            return Completion(text=body["choices"][0]["message"]["content"] or "",
                              tokens_in=int(usage.get("prompt_tokens", 0)),
                              tokens_out=int(usage.get("completion_tokens", 0)))
