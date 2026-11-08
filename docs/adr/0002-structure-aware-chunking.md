# ADR 0002: Structure-aware chunking at 150 words with 30 words of overlap

## Status
Accepted · 7 November 2026 · Taimoor, reviewed by Ali

## Context
Our corpus is mostly numbered rules: "1.21 Students are required to maintain 100%
attendance…", "5.4 Minimum CGPA required to avoid academic warning is 2.00". A student's
question is usually answered by one clause, and the citation must name that clause. Two
of the documents also contain long passages (the Generative AI policy, the annual
reports' narrative chapters) and tables (fees, grade points, graduates by campus).

A fixed-size window (for example 100 tokens, no overlap) cuts clauses in half, glues the
end of one rule to the start of the next, and makes "section" metadata meaningless,
because a chunk can span three clauses.

## Decision
`indexer/pipeline/chunk.py` splits in the order the RAG lecture gives:

1. **Sections.** A Markdown heading, a numbered title ("4. Registration", "6.1 Use in Lab
   Courses") or a numbered clause ("1.21 …"). A clause is labelled with its number and the
   last title above it, for example `1.21 Attendance Requirements`. That label is the
   chunk's `section` metadata and the first line of its text (a contextual header).
2. **Paragraphs** within a section: blank lines, a new sub-clause `a)`, or a bullet.
3. **Sentences**, only when a paragraph is longer than the target size.
4. **Words**, only when one sentence is (in practice: very long table rows).

Overlap is applied between consecutive chunks **of the same section** and never crosses a
section boundary. Sizes are in words; nomic-embed-text's tokenizer gives about 1.3 model
tokens per word on our text, so 150 words is about 200 model tokens.

We measured three (size, overlap) settings plus the fixed-size baseline on the golden
set (`eval/sweep_chunking.py`, table in `docs/PIPELINE.md`). All three structured
settings reach the same recall@5 (0.868), because most sections are single clauses that
fit in any of them; 150/30 is the only one that also reaches 0.868 at recall@3, where the
larger chunks drop to 0.842. We ship **150/30**: the best recall@3, for 9% storage
amplification and about 200 more vectors. The fixed-size baseline scores 0.316.

## Options considered
* **Fixed windows** (`CHUNK_STRATEGY=fixed`, kept for the stage-B experiment): lower recall
  and section labels that cite the wrong clause.
* **One chunk per clause, no size limit**: what we mostly get for the rules handbooks
  anyway, but the Generative AI policy and annual reports have sections of 1,000+ words
  that would blur into a single vector.
* **300/60 or 500/100**: same recall@5, slightly worse recall@3, fewer chunks. The
  difference is small; if indexing time mattered more than ranking, 300/60 would be the
  choice.
* **Semantic chunking by embedding similarity**: an extra model pass at indexing time,
  and boundaries we cannot explain in a citation.

## Consequences
* Storage amplification is small (the overlap only applies inside long sections) and is
  reported in PIPELINE.md.
* Section labels depend on heading detection, which depends on extraction. The PDF traps
  we found (off-page text, page frames read as tables, three-column pages) are fixed in
  `extract.py`. A badly OCR'd heading still produces a poor label; PIPELINE.md lists them.
* The golden set's `section` values must match the labels the chunker produces. The eval
  matches by clause number, so `5.4` matches `5.4 Academic Warning/Probation`.
