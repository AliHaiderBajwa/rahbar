"""Grounding rules that do not need a model: the relevance floor, the citation guard,
and the follow-up detector that keeps context-dependent questions out of the cache."""

from __future__ import annotations

import re

from ..schemas import Answer, Citation, Passage

ABSTAIN_TEXT = ("The documents I have do not cover this question, so I cannot answer it "
                "reliably. Please check with the Academic Office.")
FALLBACK_TEXT = ("I could not produce a reliable answer from the documents just now. "
                 "Please try again, or check with the Academic Office.")
UNGROUNDED_TEXT = ("I found related passages, but could not tie an answer to them with a "
                   "valid citation, so I will not guess.")


def below_floor(passages: list[Passage], floor: float) -> bool:
    """True when nothing retrieved is relevant enough to answer from (section 3.4)."""
    return not passages or max(p.score for p in passages) < floor


def abstention(reason: str, text: str = ABSTAIN_TEXT) -> Answer:
    return Answer(reasoning=f"abstained: {reason}", answer=text, citations=[],
                  answered_from_context=False, confidence="low")


def _section_key(section: str) -> str:
    """'5.4 Academic Warning' -> '5.4'; '2. Academic warning' -> '2'; else the lower text."""
    m = re.match(r"\s*(\d+(?:\.\d+)*)", section)
    return m.group(1) if m else " ".join(section.lower().split())


def valid_citations(citations: list[Citation], passages: list[Passage]) -> tuple[list[Citation], int]:
    """Keep only citations that match a passage retrieved for THIS request, rewritten to
    that passage's exact source/section/page. Returns (kept, dropped_count).

    Match order: exact (source, section); then same source and same clause number, so
    'section: 5.4' still matches '5.4 Academic Warning'. Anything else is fabricated.
    """
    kept: list[Citation] = []
    dropped = 0
    for c in citations:
        match = next((p for p in passages
                      if p.source == c.source and p.section == c.section), None)
        if match is None:
            key = _section_key(c.section)
            match = next((p for p in passages
                          if p.source == c.source and _section_key(p.section) == key), None)
        if match is None:
            dropped += 1
            continue
        fixed = Citation(source=match.source, section=match.section, page=match.page)
        if fixed not in kept:
            kept.append(fixed)
    return kept, dropped


_FOLLOWUP_START = re.compile(
    r"^\s*(and|also|but|so|then|what about|how about|why|same for|what if)\b", re.I)
_PRONOUN = re.compile(r"\b(it|that|this|they|them|those|these|he|she|its)\b", re.I)


def is_followup(question: str) -> bool:
    """A question that only makes sense after an earlier turn. Never cached: the cached
    answer belongs to someone else's conversation (section 3.3)."""
    words = question.split()
    if _FOLLOWUP_START.match(question):
        return True
    return len(words) <= 6 and bool(_PRONOUN.search(question))
