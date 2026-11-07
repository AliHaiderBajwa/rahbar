"""The LLM provider interface (section 3.5). Swapping providers is one file.

A provider only has to say how to send chat messages and get text back (`complete`).
`JsonChatProvider.generate` does the rest the same way for every provider:
enforce the schema in the request, validate the reply with Pydantic, retry once with
the validation error, and raise GenerationError when that fails too. The answer
service turns GenerationError into the safe fallback, so a user never sees a 500.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Protocol

from pydantic import ValidationError

from ...schemas import Answer, Passage
from .prompt import build_messages


class GenerationError(RuntimeError):
    """The model did not produce a valid Answer after one retry."""


@dataclass
class Completion:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0
    attempts: int = 0


class LLMProvider(Protocol):
    name: str

    def generate(self, question: str, passages: list[Passage]) -> Answer:
        """Answer the question from the passages, as an `Answer`."""
        ...


def parse_answer(text: str) -> Answer:
    """Pydantic is the validator. Tolerate a ```json fence, nothing else."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned[cleaned.find("{"):]
    return Answer.model_validate_json(cleaned)


class JsonChatProvider:
    name = "json-chat"
    last_usage: Usage

    def complete(self, messages: list[dict[str, str]]) -> Completion:  # pragma: no cover
        raise RuntimeError("a provider must implement complete()")

    def generate(self, question: str, passages: list[Passage]) -> Answer:
        with_rules = os.environ.get("PROMPT_RULES", "on") != "off"   # stage-D experiment only
        messages = build_messages(question, passages, with_rules=with_rules)
        self.last_usage = Usage()
        error = ""
        for attempt in (1, 2):                       # the first call and exactly one retry
            reply = self.complete(messages)
            self.last_usage.tokens_in += reply.tokens_in
            self.last_usage.tokens_out += reply.tokens_out
            self.last_usage.attempts = attempt
            try:
                return parse_answer(reply.text)
            except (ValidationError, json.JSONDecodeError, ValueError) as exc:
                error = str(exc)[:300]
                messages = messages + [
                    {"role": "assistant", "content": reply.text[:2000]},
                    {"role": "user", "content": "That reply did not validate against the "
                     f"schema: {error}. Reply again with ONLY the JSON object."},
                ]
        raise GenerationError(f"invalid model output after retry: {error}")
