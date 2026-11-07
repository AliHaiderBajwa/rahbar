"""Unit tests for the five stages. No Chroma, no Ollama: fakes stand in for both."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import embed as embed_mod
from pipeline.chunk import chunk, classify, is_title, paragraphs
from pipeline.clean import clean, clean_text, repeated_lines
from pipeline.config import get_settings
from pipeline.extract import extract, find_gutter, html_to_text, strip_front_matter, table_to_rows
from pipeline.models import Document, Page
from pipeline.store import store

FIXTURES = Path(__file__).resolve().parents[2] / "eval" / "fixtures"


def doc(*pages: str, source: str = "rules.pdf") -> Document:
    return Document(source=source, doc_version="2026", superseded=False,
                    pages=[Page(source=source, page=i, text=t) for i, t in enumerate(pages, 1)])


# --- extract ----------------------------------------------------------------------------

def test_markdown_front_matter_is_stripped() -> None:
    assert strip_front_matter("---\na: 1\n---\n# Title\nbody\n") == "# Title\nbody\n"


def test_fixture_markdown_extracts_one_page() -> None:
    d = extract(str(FIXTURES / "fees-2026.md"), "2026", False)
    assert len(d.pages) == 1 and "10,000" in d.pages[0].text and "doc_version" not in d.pages[0].text


def test_html_keeps_headings_and_table_rows() -> None:
    html = ("<nav>menu</nav><h2>Tuition Fee</h2><p>Per credit hour.</p><table>"
            "<tr><th>Program</th><th>Fee</th></tr><tr><td>BS</td><td>10,000</td></tr></table>"
            "<script>x()</script>")
    text = html_to_text(html)
    assert "## Tuition Fee" in text and "| BS | 10,000 |" in text
    assert "menu" not in text and "x()" not in text


def test_table_rows_keep_cells_paired() -> None:
    assert table_to_rows([["Charge", "PKR"], ["Late registration", "3,000"], [None, None]]) == \
        "| Charge | PKR |\n| Late registration | 3,000 |"


def test_gutter_found_between_two_columns_only() -> None:
    def char(x0: float) -> dict:
        return {"x0": x0, "x1": x0 + 5}
    two_columns = [char(x) for x in range(20, 180, 2)] * 3 + [char(x) for x in range(220, 380, 2)] * 3
    assert 180 <= (find_gutter(two_columns, 400.0) or 0) <= 220
    one_column = [char(x) for x in range(20, 380, 2)] * 3
    assert find_gutter(one_column, 400.0) is None


# --- clean ------------------------------------------------------------------------------

def test_repeating_header_and_page_numbers_are_removed_but_clauses_kept() -> None:
    pages = [f"Undergraduate Academic Rules 2026 {n}\n5.{n} Rule number {n} text.\n{n}"
             for n in range(1, 6)]
    cleaned = clean(doc(*pages))
    assert repeated_lines(doc(*pages).pages)
    for i, p in enumerate(cleaned.pages, 1):
        assert "Undergraduate" not in p.text
        assert f"5.{i} Rule number {i} text." in p.text


def test_hyphenation_ligatures_and_letter_spacing() -> None:
    assert clean_text("regis-\ntration of the ﬁnal t u t o r i a l s") == \
        "registration of the final tutorials\n"


# --- chunk ------------------------------------------------------------------------------

def test_line_classification() -> None:
    assert classify("## 1. Attendance") == ("md", "", "1. Attendance")
    assert classify("4. Registration 13") == ("heading", "4", "Registration")
    assert classify("6.1 Use in Non-Lab Courses:") == ("heading", "6.1", "Use in Non-Lab Courses")
    assert classify("1.21 Students are required to maintain 100% attendance in")[0] == "clause"
    assert classify("Attendance Requirements") == ("topic", "", "Attendance Requirements")
    assert not is_title("Undergraduate Academic Rules and Regulations 2026")


def test_clauses_become_sections_under_their_topic() -> None:
    d = doc("Attendance Requirements\n1.21 Students are required to maintain 100%\n"
            "attendance in lectures.\n1.22 Failure to meet attendance makes a student "
            "ineligible.")
    sections = [p.section for p in paragraphs(d)]
    assert sections == ["1.21 Attendance Requirements", "1.22 Attendance Requirements"]


def test_chunks_carry_required_metadata_and_stable_ids() -> None:
    d = extract(str(FIXTURES / "academic-rules-2026.md"), "2026", False)
    chunks = chunk(clean(d), 80, 20)
    assert chunks and chunks == chunk(clean(d), 80, 20)          # deterministic ids
    for c in chunks:
        assert set(c.metadata()) == {"source", "section", "page", "doc_version", "superseded"}
        assert c.text.startswith(c.section)                       # contextual header
    assert any(c.section == "1.1 Attendance" for c in chunks)


def test_long_section_splits_with_overlap_inside_the_section() -> None:
    body = " ".join(f"Sentence number {i} states a rule." for i in range(60))
    chunks = chunk(doc("## 7. Long Definitions\n" + body), 50, 10)
    assert len(chunks) > 3
    assert all(len(c.text.split()) <= 50 + 10 + 4 for c in chunks)
    first, second = chunks[0].text.split(), chunks[1].text.split()
    assert first[-10:] == second[3:13]                            # 10 words shared
    assert {c.section for c in chunks} == {"7. Long Definitions"}


def test_fixed_strategy_ignores_structure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHUNK_STRATEGY", "fixed")
    text = "## A\n" + "word " * 120 + "\n## B\n" + "term " * 120
    chunks = chunk(doc(text), 100, 0)
    assert any("word" in c.text and "term" in c.text for c in chunks)


def test_bad_sizes_are_rejected() -> None:
    with pytest.raises(ValueError):
        chunk(doc("x"), 50, 50)


# --- embed and store --------------------------------------------------------------------

def test_embed_batches_and_prefixes(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[list[str]] = []

    def fake_post(client, url, payload):  # type: ignore[no-untyped-def]
        sent.append(payload["input"])
        return [[0.1, 0.2, 0.3] for _ in payload["input"]]

    monkeypatch.setattr(embed_mod, "_post", fake_post)
    vectors = embed_mod.embed([f"t{i}" for i in range(20)], get_settings())
    assert len(vectors) == 20 and len(sent) == 2
    assert sent[0][0] == "search_document: t0"


class FakeCollection:
    def __init__(self, name: str, metadata: dict) -> None:
        self.name, self.metadata, self.rows = name, metadata, {}

    def add(self, ids, documents, embeddings, metadatas) -> None:  # type: ignore[no-untyped-def]
        for i, d, e, m in zip(ids, documents, embeddings, metadatas):
            self.rows[i] = (d, e, m)

    def count(self) -> int:
        return len(self.rows)

    def modify(self, name: str) -> None:
        self.client.collections[name] = self.client.collections.pop(self.name)
        self.name = name


class FakeClient:
    def __init__(self) -> None:
        self.collections: dict[str, FakeCollection] = {}

    def delete_collection(self, name: str) -> None:
        del self.collections[name]

    def create_collection(self, name: str, metadata: dict) -> FakeCollection:
        col = FakeCollection(name, metadata)
        col.client = self   # type: ignore[attr-defined]
        self.collections[name] = col
        return col


def test_store_builds_aside_then_swaps_and_records_the_pin() -> None:
    settings = get_settings()
    client = FakeClient()
    client.create_collection("rahbar", {"embed_model": "old"})        # the live index
    chunks = chunk(clean(extract(str(FIXTURES / "fees-2026.md"), "2026", False)), 80, 20)
    n = store(chunks, [[0.5, 0.5]] * len(chunks), settings, client=client)
    assert n == len(chunks)
    assert set(client.collections) == {"rahbar"}                       # staging renamed away
    meta = client.collections["rahbar"].metadata
    assert meta["embed_model"] == settings.embed_model and meta["dimension"] == 2
    assert meta["hnsw:space"] == "cosine" and len(meta["index_id"]) == 12


def test_store_refuses_an_empty_or_mismatched_index() -> None:
    with pytest.raises(ValueError):
        store([], [], get_settings(), client=FakeClient())
