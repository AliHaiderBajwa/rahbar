"""Stage 1: Extract. PDF, HTML or Markdown to text, page by page.

What this stage does, and what it loses, is written up in docs/PIPELINE.md:
- PDF pages with a text layer go through pdfplumber. Tables found by pdfplumber are cut
  out of the running text and re-emitted as pipe-delimited rows, so a column of fees
  does not get interleaved with the column next to it.
- PDF pages with no text layer (a scan) are rendered and OCR'd with tesseract, and the
  page is marked ocr=True. If tesseract is not installed the page stays empty and we
  log the loss instead of pretending.
- HTML pages keep their headings (as Markdown '#' lines) and their tables (as rows).
- Markdown files lose only their front matter.
"""

from __future__ import annotations

import json
import os
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .models import Document, Page

# A page with fewer visible characters than this is treated as having no text layer.
MIN_TEXT_CHARS = 25
OCR_DPI = 200


def _log(msg: str, **fields: object) -> None:
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "logger": "rahbar.indexer.extract", "msg": msg, **fields}), flush=True)


def extract(path: str, doc_version: str, superseded: bool) -> Document:
    """Return the document's text page by page.

    Detect pages with no text layer and OCR them; mark those pages ocr=True.
    """
    p = Path(path)
    doc = Document(source=p.name, doc_version=doc_version, superseded=superseded)
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        doc.pages = _extract_pdf(p)
    elif suffix in {".html", ".htm"}:
        doc.pages = [Page(source=p.name, page=1, text=html_to_text(p.read_text("utf-8", "replace")))]
    elif suffix in {".md", ".txt"}:
        doc.pages = [Page(source=p.name, page=1, text=strip_front_matter(p.read_text("utf-8")))]
    else:
        raise ValueError(f"cannot extract {p.name}: unsupported type {suffix!r}")
    ocr_pages = [pg.page for pg in doc.pages if pg.ocr]
    empty = [pg.page for pg in doc.pages if len(pg.text.strip()) < MIN_TEXT_CHARS]
    _log("extracted", source=p.name, pages=len(doc.pages), ocr_pages=len(ocr_pages),
         empty_pages=len(empty))
    return doc


# --------------------------------------------------------------------------- Markdown

def strip_front_matter(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                return "\n".join(lines[i + 1:]).strip() + "\n"
    return text


# --------------------------------------------------------------------------- PDF

def table_to_rows(table: list[list[Any]]) -> str:
    """Render a pdfplumber table as one pipe-delimited line per row. Cells keep their
    order, so 'Late registration | 3,000' stays a pair instead of two loose numbers."""
    lines = []
    for row in table:
        cells = [" ".join(str(c).split()) if c is not None else "" for c in row]
        if any(cells):
            lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _extract_pdf(path: Path) -> list[Page]:
    import pdfplumber

    pages: list[Page] = []
    with pdfplumber.open(str(path)) as pdf:
        for number, raw_page in enumerate(pdf.pages, start=1):
            # Keep only what is inside the visible page. The 2026 handbooks were exported
            # from a print imposition and carry the NEIGHBOURING page's text at negative
            # x coordinates; without this, every clause appears twice, interleaved line
            # by line with another page (docs/PIPELINE.md, "Extraction").
            page = raw_page.within_bbox(raw_page.bbox)
            text, tables = _page_text_and_tables(page)
            # pdfplumber caches every parsed page; release it, or a long scanned PDF
            # holds all of its pages in memory at once (the indexer's OOM risk, 4.5).
            raw_page.close()
            if len(text.strip()) < MIN_TEXT_CHARS and not tables:
                ocr_text = ocr_page(path, number)
                pages.append(Page(source=path.name, page=number, text=ocr_text, ocr=True))
                continue
            pages.append(Page(source=path.name, page=number, text=text))
    return pages


def find_gutter(chars: list[dict], width: float, bins: int = 60) -> float | None:
    """x position of an empty vertical gutter between two text columns, or None.

    Two-column pages are the worst extraction trap in this corpus: read left to right,
    line 3 of clause 1.20 is followed by line 3 of item c) from the next column. We
    look for a vertical band in the middle 30-70% of the page that almost no character
    crosses, with plenty of text on both sides.
    """
    if len(chars) < 200:
        return None
    covered = [0] * bins
    for c in chars:
        start = max(0, int(c["x0"] / width * bins))
        end = min(bins - 1, int(c["x1"] / width * bins))
        for b in range(start, end + 1):
            covered[b] += 1
    lo, hi = int(bins * 0.30), int(bins * 0.70)
    best = min(range(lo, hi), key=lambda b: covered[b])
    if covered[best] > len(chars) * 0.004:
        return None
    x = (best + 0.5) / bins * width
    left = sum(1 for c in chars if c["x1"] <= x)
    right = sum(1 for c in chars if c["x0"] >= x)
    if min(left, right) < len(chars) * 0.2:
        return None
    return x


def _columns(region: Any, depth: int = 0) -> list[Any]:
    """The region as one block, or split at a gutter into columns, left to right.
    Recursive, so the prospectus's three-column pages come out as three columns."""
    if depth >= 2:
        return [region]
    x0, top, x1, bottom = region.bbox
    try:
        chars = [{**c, "x0": c["x0"] - x0, "x1": c["x1"] - x0} for c in region.chars]
        gx = find_gutter(chars, float(x1 - x0))
    except Exception:
        gx = None
    if gx is None:
        return [region]
    left = region.crop((x0, top, x0 + gx, bottom))
    right = region.crop((x0 + gx, top, x1, bottom))
    return _columns(left, depth + 1) + _columns(right, depth + 1)


def _page_text_and_tables(page: Any) -> tuple[str, list[str]]:
    """The page's text in reading order, with each real table rendered as rows at the
    point where it sits on the page, so a table stays under the heading above it.
    Returns (text, rendered_tables)."""
    try:
        found = page.find_tables()
    except Exception:  # pdfplumber can trip over malformed drawings; keep the text
        found = []
    tables: list[tuple[float, float, str]] = []      # (top, bottom, rows)
    for table in found:
        rows = table.extract()
        # A 'table' with one column or one row is usually a ruled box around a paragraph,
        # and one that covers most of the page is a page border: the prospectus draws a
        # frame round every page, and reading it as a table glues three columns of prose
        # into one cell. Leave both to the text extractor.
        tx0, ttop, tx1, tbottom = table.bbox
        area = (tx1 - tx0) * (tbottom - ttop) / (float(page.width) * float(page.height))
        longest = max((len(str(c or "")) for r in rows for c in r), default=0)
        if not rows or len(rows) < 2 or max(len(r) for r in rows) < 2 or area > 0.6 \
                or longest > 400:
            continue
        tables.append((ttop, tbottom, table_to_rows(rows)))
    tables.sort()
    x0, top, x1, bottom = page.bbox
    parts: list[str] = []
    cursor = top
    for ttop, tbottom, rows in tables + [(bottom, bottom, "")]:
        if ttop > cursor + 1:
            band = page.crop((x0, cursor, x1, min(ttop, bottom)))
            parts.extend(region.extract_text(x_tolerance=1.5, y_tolerance=3) or ""
                         for region in _columns(band))
        if rows:
            parts.append(rows)
        cursor = max(cursor, tbottom)
    return "\n\n".join(p for p in parts if p.strip()), [t[2] for t in tables]


def ocr_page(path: Path, number: int) -> str:
    """OCR one page. Needs tesseract-ocr and poppler-utils (both in the indexer image).
    OCR=off skips it: only for breaking stage A on purpose (docs/FAILURE-ANALYSIS.md)."""
    if os.environ.get("OCR", "on") == "off":
        return ""
    try:
        import pytesseract
        from pdf2image import convert_from_path

        images = convert_from_path(str(path), dpi=OCR_DPI, first_page=number, last_page=number,
                                   grayscale=True)
        text = "\n".join(pytesseract.image_to_string(img) for img in images)
        for img in images:
            img.close()
        return text
    except Exception as exc:  # no tesseract/poppler: record the loss honestly
        _log("ocr unavailable, page left empty", source=path.name, page=number,
             error=type(exc).__name__)
        return ""


# --------------------------------------------------------------------------- HTML

_SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form",
              "button", "select", "iframe"}
_BLOCK_TAGS = {"p", "div", "li", "br", "section", "article", "ul", "ol", "dd", "dt"}
_HEADINGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}


class _HtmlText(HTMLParser):
    """Visible text with headings as '#' lines and tables as pipe rows."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.heading: str | None = None
        self.table_depth = 0
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self.skip += 1
        elif self.skip:
            return
        elif tag in _HEADINGS:
            self.heading = _HEADINGS[tag]
            self.out.append("\n\n" + self.heading + " ")
        elif tag == "table":
            self.table_depth += 1
            self.out.append("\n\n")
        elif tag == "tr":
            self.row = []
        elif tag in {"td", "th"}:
            self.cell = []
        elif tag in _BLOCK_TAGS and not self.table_depth:
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif self.skip:
            return
        elif tag in _HEADINGS:
            self.heading = None
            self.out.append("\n\n")
        elif tag in {"td", "th"} and self.cell is not None and self.row is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if any(self.row):
                self.out.append("| " + " | ".join(self.row) + " |\n")
            self.row = None
        elif tag == "table":
            self.table_depth = max(0, self.table_depth - 1)
            self.out.append("\n")
        elif tag in _BLOCK_TAGS and not self.table_depth:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if self.cell is not None:
            self.cell.append(data)
        elif self.heading is not None:
            self.out.append(" ".join(data.split()))
        elif not self.table_depth:
            self.out.append(data)


def html_to_text(html: str) -> str:
    parser = _HtmlText()
    parser.feed(html)
    text = "".join(parser.out)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"
