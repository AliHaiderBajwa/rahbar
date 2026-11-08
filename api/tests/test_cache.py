"""The semantic cache (section 3.3): hits, the never-cache rules, invalidation."""

from __future__ import annotations

import fakeredis
import pytest

from app.config import Settings
from app.services.answer import answer_question
from app.services.cache import SemanticCache, cosine

from .test_answer_service import GOOD, WARNING
from .conftest import FixedRetriever, ScriptedProvider, passage


@pytest.fixture()
def cache() -> SemanticCache:
    return SemanticCache(fakeredis.FakeRedis(), threshold=0.95, ttl=60, max_entries=10)


def test_cosine() -> None:
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([1.0], [1.0, 2.0]) == -1.0


def test_second_similar_question_is_served_from_cache(settings: Settings,
                                                      cache: SemanticCache) -> None:
    retriever = FixedRetriever(WARNING, vector=[1.0, 0.0, 0.0])
    first = answer_question("What CGPA avoids warning?", retriever, ScriptedProvider(GOOD),
                            settings, cache=cache, index_id="idx-1")
    assert first.cached is False
    retriever.vector = [0.99, 0.05, 0.0]     # a paraphrase: cosine ~0.999
    provider = ScriptedProvider()            # would raise if called
    second = answer_question("Which CGPA keeps me off warning?", retriever, provider, settings,
                             cache=cache, index_id="idx-1")
    assert second.cached is True and provider.calls == []
    assert second.citations == first.citations


def test_dissimilar_question_misses(settings: Settings, cache: SemanticCache) -> None:
    retriever = FixedRetriever(WARNING, vector=[1.0, 0.0, 0.0])
    answer_question("What CGPA avoids warning?", retriever, ScriptedProvider(GOOD), settings,
                    cache=cache, index_id="idx-1")
    retriever.vector = [0.8, 0.6, 0.0]       # cosine 0.8: below the 0.95 threshold
    r = answer_question("What is the fee?", retriever, ScriptedProvider(GOOD), settings,
                        cache=cache, index_id="idx-1")
    assert r.cached is False


def test_abstentions_and_followups_are_never_cached(settings: Settings,
                                                    cache: SemanticCache) -> None:
    weak = FixedRetriever([passage("x", "2.1 Other", 0.1)], vector=[0.0, 1.0, 0.0])
    answer_question("How do I get a hostel room?", weak, ScriptedProvider(), settings,
                    cache=cache, index_id="idx-1")
    follow = FixedRetriever(WARNING, vector=[0.0, 0.0, 1.0])
    answer_question("and what about that?", follow, ScriptedProvider(GOOD), settings,
                    cache=cache, index_id="idx-1")
    assert cache.r.llen(SemanticCache.key("idx-1")) == 0


def test_reindex_invalidates_the_cache(settings: Settings, cache: SemanticCache) -> None:
    retriever = FixedRetriever(WARNING, vector=[1.0, 0.0, 0.0])
    answer_question("What CGPA avoids warning?", retriever, ScriptedProvider(GOOD), settings,
                    cache=cache, index_id="old-index")
    assert cache.r.exists(SemanticCache.key("old-index"))
    # the indexer Job published a new index: same question, new index_id
    r = answer_question("What CGPA avoids warning?", retriever, ScriptedProvider(GOOD),
                        settings, cache=cache, index_id="new-index")
    assert r.cached is False
    assert not cache.r.exists(SemanticCache.key("old-index"))
