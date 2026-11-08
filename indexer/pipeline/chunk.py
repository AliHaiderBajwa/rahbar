"""Stage 3: Chunk. Structure-aware, with overlap.

Unit: CHUNK_SIZE and CHUNK_OVERLAP are counted in words (whitespace-separated tokens).
nomic-embed-text's BPE tokenizer gives roughly 1.3 model tokens per word on this corpus,
so a 300-word chunk is about 400 model tokens, well under the model's 2048-token window.

Order of splitting, as the RAG lecture lays it out:
1. sections   - a Markdown heading, or a numbered heading such as '5.4 Academic Warning'
2. paragraphs - blank lines, or a line that starts a new numbered clause or bullet
3. sentences  - only when one paragraph is longer than the target size
4. words      - only when one sentence is longer than the target size (rare: tables)

Every chunk starts with its section label on the first line (a contextual header), so the
embedding knows which clause a sentence came from, and keeps source, section, page,
doc_version and superseded as metadata. Overlap never crosses a section boundary: the
tail of 'Attendance' is no use at the top of 'Academic Warning'.

CHUNK_STRATEGY=fixed switches to plain fixed-size word windows. It exists only so we
can break stage B on purpose for docs/FAILURE-ANALYSIS.md.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .models import Chunk, Document

MIN_CHUNK_WORDS = 8           # a heading with nothing under it is not worth a vector
MAX_TITLE_WORDS = 8

_MD_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*$")
# '4. Registration', '6.1 Use in Non-Lab Courses', '1.21 Students are required to ...'
_NUMBERED = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,2})\.?\s+(\S.*)$")
_CLAUSE_START = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){1,2}|\(?[a-z]\)|\(?[ivx]{1,4}\)|[•\-–*])\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+(?=[A-Z0-9(\"'])")
_TRAILING_PAGE = re.compile(r"\s+\d{1,3}$")
_SMALL = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or", "the",
          "to", "with", "under", "&", "/", "-", "vs"}


@dataclass
class _Para:
    section: str
    page: int
    text: str


def is_title(text: str) -> bool:
    """A short Title Case line with no full stop: 'Attendance Requirements',
    'Use in Non-Lab Courses', 'ACADEMIC HONESTY'. Body lines are rarely like this."""
    text = text.rstrip(":").strip()
    words = text.split()
    if not 1 <= len(words) <= MAX_TITLE_WORDS or text.endswith((".", ",", ";")):
        return False
    if re.search(r"\b(19|20)\d\d\b", text) and len(words) > 3:
        return False       # 'Undergraduate Academic Rules and Regulations 2026': a running header
    alpha = [w for w in words if w[0].isalpha()]
    if not alpha or len(text) < 4:
        return False
    capped = sum(1 for w in alpha if w[0].isupper() or w.lower() in _SMALL)
    return capped == len(alpha) and alpha[0][0].isupper()


def classify(line: str) -> tuple[str, str, str]:
    """('md' | 'heading' | 'clause' | 'topic' | 'body', number, title) for one line.

    heading: a numbered short title, '4. Registration' or '6.1 Use in Lab Courses'
    clause:  a numbered rule, '1.21 Students are required to maintain ...'; the line is
             content, and the clause number becomes the section ('1.21 Attendance
             Requirements', using the last title seen above it)
    topic:   an unnumbered title, 'Attendance Requirements'
    """
    s = line.strip()
    if not s or s.startswith("|"):
        return "body", "", ""
    m = _MD_HEADING.match(s)
    if m:
        return "md", "", " ".join(m.group(1).split())
    m = _NUMBERED.match(s)
    if m:
        number, rest = m.group(1), _TRAILING_PAGE.sub("", m.group(2).strip()).rstrip(":")
        if is_title(rest):
            return "heading", number, rest
        if "." in number:
            return "clause", number, rest
        return "body", "", ""
    if is_title(s) and not s[0].isdigit():
        return "topic", "", s.title() if s.isupper() else s
    return "body", "", ""


def heading_of(line: str) -> str | None:
    """The section label a line starts, or None (kept for tests and readers)."""
    kind, number, title = classify(line)
    if kind == "md":
        return title
    if kind == "heading":
        return f"{number} {title}"
    return None


def paragraphs(doc: Document) -> list[_Para]:
    """Walk the pages once, tracking the current section, and cut paragraphs."""
    section = "Introduction"
    topic = ""
    out: list[_Para] = []
    buf: list[str] = []
    buf_page = 1

    def flush() -> None:
        nonlocal buf
        text = " ".join(buf).strip()
        if text:
            out.append(_Para(section, buf_page, text))
        buf = []

    for page in doc.pages:
        for raw in page.text.splitlines():
            line = raw.strip()
            if not line:
                flush()
                continue
            kind, number, title = classify(line)
            if kind == "md":
                flush()
                section = title
                topic = re.sub(r"^\d+(?:\.\d+)*\.?\s*", "", title)
                continue
            if kind == "heading":
                flush()
                section, topic = f"{number} {title}", title
                continue
            if kind == "topic":
                flush()
                section = topic = title
                continue
            if kind == "clause":
                flush()
                label = topic or " ".join(title.split()[:6])
                section = f"{number} {label}"
                # the clause line is content: fall through and keep it
            if line.startswith("|"):
                # table rows: keep each row on its own line inside one paragraph
                if buf and not buf[-1].startswith("|"):
                    flush()
                if not buf:
                    buf_page = page.page
                buf.append(line + "\n")
                continue
            if _CLAUSE_START.match(line) or (buf and buf[-1].startswith("|")):
                flush()
            if not buf:
                buf_page = page.page
            buf.append(line)
        flush()   # a paragraph does not run across a page break in our metadata
    return out


def _words(text: str) -> list[str]:
    return text.split()


def _split_long(text: str, size: int) -> list[str]:
    """Sentences, then hard word windows, for a paragraph longer than `size` words."""
    if "\n" in text and text.lstrip().startswith("|"):
        pieces = [ln for ln in text.splitlines() if ln.strip()]   # table: one row per piece
    else:
        pieces = _SENTENCE_END.split(text)
    out: list[str] = []
    for piece in pieces:
        words = _words(piece)
        if len(words) <= size:
            out.append(piece)
        else:
            out.extend(" ".join(words[i:i + size]) for i in range(0, len(words), size))
    return out


def _slug(source: str) -> str:
    stem = source.rsplit(".", 1)[0]
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:60] or "doc"


def _make_chunks(doc: Document, packed: list[tuple[str, int, str]]) -> list[Chunk]:
    slug = _slug(doc.source)
    return [
        Chunk(id=f"{slug}-{i:04d}", text=f"{section}\n{body}", source=doc.source,
              section=section, page=page, doc_version=doc.doc_version,
              superseded=doc.superseded)
        for i, (section, page, body) in enumerate(packed)
    ]


def chunk(doc: Document, size: int, overlap: int) -> list[Chunk]:
    """Split on sections, then paragraphs, then sentences, keeping each chunk under
    `size` tokens, with `overlap` tokens shared between neighbours. Every chunk keeps
    the section heading it sits under, its page, doc_version and superseded."""
    if size <= 0 or not 0 <= overlap < size:
        raise ValueError(f"need 0 <= overlap < size, got size={size} overlap={overlap}")
    if os.environ.get("CHUNK_STRATEGY", "structured") == "fixed":
        return fixed_chunks(doc, size, overlap)

    packed: list[tuple[str, int, str]] = []       # (section, page, body)
    cur: list[str] = []                            # pieces in the chunk being built
    cur_section, cur_page = "", 1

    def emit() -> None:
        nonlocal cur
        body = "\n".join(cur).strip()
        if len(_words(body)) >= MIN_CHUNK_WORDS:
            packed.append((cur_section, cur_page, body))
        cur = []

    for para in paragraphs(doc):
        if para.section != cur_section:
            if cur:
                emit()
            cur_section, cur_page = para.section, para.page
        pieces = [para.text] if len(_words(para.text)) <= size else _split_long(para.text, size)
        for piece in pieces:
            n_cur = len(_words("\n".join(cur)))
            if cur and n_cur + len(_words(piece)) > size:
                tail = _words("\n".join(cur))[-overlap:] if overlap else []
                emit()
                cur_page = para.page
                if tail:
                    cur = [" ".join(tail)]
            if not cur:
                cur_page = para.page
            cur.append(piece)
    if cur:
        emit()
    return _make_chunks(doc, packed)


def fixed_chunks(doc: Document, size: int, overlap: int) -> list[Chunk]:
    """Plain word windows across the whole document, blind to structure."""
    stream: list[tuple[str, str, int]] = []       # (word, section, page)
    for para in paragraphs(doc):
        stream.extend((w, para.section, para.page) for w in _words(para.text))
    step = size - overlap
    packed = []
    for start in range(0, len(stream), step):
        window = stream[start:start + size]
        if len(window) < MIN_CHUNK_WORDS:
            break
        packed.append((window[0][1], window[0][2], " ".join(w for w, _, _ in window)))
    return _make_chunks(doc, packed)
