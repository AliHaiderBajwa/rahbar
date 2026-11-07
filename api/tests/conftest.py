"""Shared fakes. Nothing here touches a network, a model or a cluster (section 6)."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.providers.llm.base import Completion, JsonChatProvider
from app.schemas import Passage


def passage(cid: str, section: str, score: float, text: str = "Some rule text.",
            source: str = "handbook.pdf", page: int = 1) -> Passage:
    return Passage(chunk_id=cid, text=text, source=source, section=section, page=page,
                   score=score)


class ScriptedProvider(JsonChatProvider):
    """A JsonChatProvider whose 'model' replies with the scripted strings in order, and
    remembers the messages it was sent."""

    name = "scripted"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> Completion:
        self.calls.append(messages)
        return Completion(text=self.replies.pop(0), tokens_in=100, tokens_out=20)


class ExplodingProvider:
    name = "exploding"

    def generate(self, question, passages):  # type: ignore[no-untyped-def]
        raise TimeoutError("provider timed out")


class FixedRetriever:
    """Returns the given passages; records whether superseded docs were requested."""

    name = "fixed"

    def __init__(self, passages: list[Passage], vector: list[float] | None = None) -> None:
        self.passages = passages
        self.vector = vector
        self.include_superseded: bool | None = None

    def embed_query(self, question: str) -> list[float] | None:
        return self.vector

    def retrieve(self, question, k, vector=None, include_superseded=False):  # type: ignore[no-untyped-def]
        self.include_superseded = include_superseded
        return [p.model_copy() for p in self.passages[:k]]

    def current_index_id(self) -> str:
        return "idx-1"


@pytest.fixture()
def settings() -> Settings:
    return Settings(retriever="stub", llm_provider="stub", relevance_floor=0.5, top_k=5,
                    cache_threshold=0.95)
