"""Week-1 retriever: the same three passages for every question.

It lets you deploy and test the whole cluster before the RAG lecture.
Switch to RETRIEVER=chroma in the ConfigMap once your index exists.
"""

from __future__ import annotations

from ..schemas import Passage
from .base import IndexInfo

_PASSAGES = [
    Passage(
        chunk_id="stub-001",
        text="This is a stub passage. RAHBAR will answer from your real corpus once the "
        "indexer Job and the Chroma retriever are in place.",
        source="stub-handbook.pdf",
        section="1.1 About the stub",
        page=1,
        score=0.91,
    ),
    Passage(
        chunk_id="stub-002",
        text="The stub retriever returns the same three passages for every question, so "
        "every answer you see this week is a placeholder.",
        source="stub-handbook.pdf",
        section="1.2 What the stub returns",
        page=1,
        score=0.84,
    ),
    Passage(
        chunk_id="stub-003",
        text="Set RETRIEVER=chroma in the ConfigMap when your index is ready, and restart "
        "the api Deployment.",
        source="stub-handbook.pdf",
        section="1.3 Switching to Chroma",
        page=2,
        score=0.77,
    ),
]


class StubRetriever:
    name = "stub"

    def __init__(self, passages: list[Passage] | None = None) -> None:
        self.passages = passages if passages is not None else _PASSAGES

    def embed_query(self, question: str) -> list[float] | None:
        return None

    def retrieve(self, question: str, k: int, vector: list[float] | None = None,
                 include_superseded: bool = False) -> list[Passage]:
        return [p.model_copy() for p in self.passages[:k]]

    def index_info(self) -> IndexInfo:
        return IndexInfo(exists=True, count=len(self.passages), embed_model=None,
                         index_id="stub")

    def current_index_id(self) -> str:
        return "stub"

    def heartbeat(self) -> None:
        return None

    def check_pin(self, info: IndexInfo | None = None) -> None:
        return None          # the stub has no embeddings, so nothing can disagree
