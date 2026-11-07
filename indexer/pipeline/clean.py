"""Stage 2: Clean. Remove artefacts without destroying structure.

Rules (also listed in docs/PIPELINE.md):
1. Unicode NFKC, so the 'ﬁ' ligature becomes 'fi' and a search for 'fine' can match.
2. Drop pdfminer '(cid:NN)' glyph placeholders and soft hyphens.
3. Re-join words hyphenated across a line break ('regis-\\ntration' -> 'registration').
4. Remove lines that repeat at the top or bottom of most pages (running headers/footers,
   'FAST School of Computing' banners), and lines that are only a page number.
5. Re-join letter-spaced words from justified PDF text ('t u t o r i a l s' -> 'tutorials').
6. Collapse runs of spaces and blank lines.

Deliberately NOT stripped: clause numbers at the start of a line ('5.4', '1.21') and
table rows ('| BS | 10,000 |'). The chunker uses the first to label sections, and the
second is the only thing keeping a fee next to its programme.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

from .models import Document, Page

EDGE_LINES = 3            # how many lines at each end of a page count as header/footer
REPEAT_SHARE = 0.5        # a line on at least half the pages is boilerplate
MIN_PAGES_FOR_REPEATS = 3

_CID = re.compile(r"\(cid:\d+\)")
_CLAUSE = re.compile(r"^\s*\d{1,2}(\.\d{1,2})+\s+\S")
_PAGE_NO = re.compile(r"^\s*((page\s*)?\d{1,4}(\s*(of|/)\s*\d{1,4})?|[ivxl](\s?[ivxl]){0,5})\s*$", re.I)
_HYPHEN_BREAK = re.compile(r"([a-z])-\n([a-z])")
_SPACES = re.compile(r"[ \t ]+")
_BLANKS = re.compile(r"\n{3,}")
_LETTER_SPACED = re.compile(r"\b(?:[A-Za-z] ){3,}[A-Za-z]\b")


def _normalise_line(line: str) -> str:
    """Key for 'is this the same header line': case, digits, roman page numbers and all
    spacing ignored ('U ndergraduate ... 2026 III' and 'Undergraduate ... 2026 7' match)."""
    key = re.sub(r"[\s|]+", "", line.lower())
    key = re.sub(r"[ivxl]+$", "", key) if re.search(r"\d[ivxl]+$", key) else key
    return re.sub(r"\d+", "#", key).strip("#")


def _edges(lines: list[str]) -> list[str]:
    content = [ln for ln in lines if ln.strip()]
    return content[:EDGE_LINES] + content[-EDGE_LINES:]


def repeated_lines(pages: list[Page]) -> set[str]:
    """Normalised lines that sit at a page edge on at least REPEAT_SHARE of the pages."""
    if len(pages) < MIN_PAGES_FOR_REPEATS:
        return set()
    counts: Counter[str] = Counter()
    for page in pages:
        counts.update({_normalise_line(ln) for ln in _edges(page.text.splitlines())
                       if not _CLAUSE.match(ln)})   # a clause is content, never a header
    need = max(MIN_PAGES_FOR_REPEATS, int(len(pages) * REPEAT_SHARE))
    return {line for line, n in counts.items() if n >= need and line.strip("# ")}


def clean_text(text: str, boilerplate: set[str] | None = None) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _CID.sub("", text).replace("­", "")
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    lines = text.splitlines()
    edge_keys = {_normalise_line(ln) for ln in _edges(lines)} if boilerplate else set()
    kept = []
    for line in lines:
        key = _normalise_line(line)
        if boilerplate and key in boilerplate and key in edge_keys and not _CLAUSE.match(line):
            continue
        if _PAGE_NO.match(line):
            continue
        line = _LETTER_SPACED.sub(lambda m: m.group(0).replace(" ", ""), line)
        kept.append(_SPACES.sub(" ", line).strip())
    return _BLANKS.sub("\n\n", "\n".join(kept)).strip() + "\n"


def clean(doc: Document) -> Document:
    """Strip headers and footers that repeat on every page; keep headings and
    section boundaries, because chunking and citations depend on them."""
    boilerplate = repeated_lines(doc.pages)
    pages = [Page(source=p.source, page=p.page, text=clean_text(p.text, boilerplate), ocr=p.ocr)
             for p in doc.pages]
    return Document(source=doc.source, doc_version=doc.doc_version,
                    superseded=doc.superseded, pages=pages)
