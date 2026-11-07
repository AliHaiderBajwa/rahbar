"""Relevance floor, the citation guard and follow-up detection (section 3.4)."""

from __future__ import annotations

from app.schemas import Citation
from app.services.grounding import below_floor, is_followup, valid_citations

from .conftest import passage


def test_relevance_floor_abstains_on_weak_or_empty_retrieval() -> None:
    assert below_floor([], 0.5)
    assert below_floor([passage("a", "1.1 X", 0.31), passage("b", "1.2 X", 0.42)], 0.5)
    assert not below_floor([passage("a", "1.1 X", 0.31), passage("b", "1.2 X", 0.71)], 0.5)


def test_citation_guard_keeps_retrieved_and_drops_fabricated() -> None:
    retrieved = [passage("a", "5.4 Academic Warning/Probation", 0.8, page=12)]
    cites = [
        Citation(source="handbook.pdf", section="5.4 Academic Warning/Probation", page=12),
        Citation(source="handbook.pdf", section="9.9 A clause we never retrieved", page=3),
        Citation(source="other.pdf", section="5.4 Academic Warning/Probation", page=12),
    ]
    kept, dropped = valid_citations(cites, retrieved)
    assert kept == [cites[0]]
    assert dropped == 2


def test_citation_guard_repairs_a_clause_number_to_the_retrieved_label() -> None:
    retrieved = [passage("a", "1.21 Attendance Requirements", 0.8, page=10)]
    kept, dropped = valid_citations([Citation(source="handbook.pdf", section="1.21", page=99)],
                                    retrieved)
    assert dropped == 0
    assert kept == [Citation(source="handbook.pdf", section="1.21 Attendance Requirements",
                             page=10)]


def test_citation_guard_does_not_confuse_1_2_with_1_21() -> None:
    retrieved = [passage("a", "1.21 Attendance Requirements", 0.8)]
    kept, dropped = valid_citations([Citation(source="handbook.pdf", section="1.2")], retrieved)
    assert kept == [] and dropped == 1


def test_followups_are_detected() -> None:
    assert is_followup("and for MS students?")
    assert is_followup("What about summer?")
    assert is_followup("Is that refundable?")
    assert not is_followup("What CGPA puts a student on academic warning?")
