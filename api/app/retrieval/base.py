"""The retriever interface. The rest of the API only ever sees this."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..schemas import Passage


@dataclass
class IndexInfo:
    """What the live collection says about itself (written by the indexer's store stage)."""

    exists: bool
    count: int = 0
    embed_model: str | None = None
    dimension: int | None = None
    index_id: str = "none"
    metadata: dict = field(default_factory=dict)


class PinMismatch(RuntimeError):
    """The index was built with a different embedding model than this API uses."""


class Retriever(Protocol):
    name: str

    def embed_query(self, question: str) -> list[float] | None:
        """The question's vector, or None when this retriever has no embeddings (stub).
        The answer service embeds once and uses the vector for the cache and retrieval."""
        ...

    def retrieve(self, question: str, k: int, vector: list[float] | None = None,
                 include_superseded: bool = False) -> list[Passage]:
        """Return up to k passages, best first."""
        ...

    def index_info(self) -> IndexInfo:
        """Describe the live collection. Raises if the store is unreachable."""
        ...

    def current_index_id(self) -> str:
        """The live index's id, for namespacing the semantic cache."""
        ...

    def heartbeat(self) -> None:
        """Raise if the vector store is unreachable."""
        ...

    def check_pin(self, info: IndexInfo | None = None) -> None:
        """Raise PinMismatch if the index was built with another embedding model."""
        ...
