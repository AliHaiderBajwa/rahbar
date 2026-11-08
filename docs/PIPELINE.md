# Indexing and retrieval pipeline

Indexing runs once in `Job/indexer` (and weekly in `CronJob/reindex`); querying runs on
every question in the api. Everything below was measured on our corpus of 9 documents
(279 PDF pages + 2 web pages, 70,182 words after cleaning) with `nomic-embed-text`.

## How the documents reach the Job
**Fetched at start-up, verified against the manifest.** The Job's init container
`fetch-corpus` runs `corpus/fetch.py --dest /corpus` from the indexer image: it downloads
the nine URLs in `corpus/sources.yaml` one at a time (2 s apart, a named User-Agent) into
an `emptyDir`, writes `_index.json` (file → `doc_version`, `superseded`), and compares each
SHA-256 with the committed `corpus/MANIFEST.md`, logging any file the website has changed.
The indexer container then reads `/corpus` read-only.

We chose this over baking the PDFs into the image (they are not ours to redistribute, and
a Docker Hub image is public) and over a PVC (one more stateful object to seed by hand).
The cost: every run downloads 35 MB from nu.edu.pk, once a week. CI never touches the
website: its overlay points `CORPUS_DIR` at the three fixture documents baked into the
image and removes the init container.

## Extraction (`indexer/pipeline/extract.py`)
pdfplumber page by page; Markdown minus front matter; HTML with headings kept as `#`
lines and `<table>` rows as `| a | b |` lines.

**Which documents needed OCR.** Any page with fewer than 25 characters of text is rendered
at 200 dpi (grayscale) and read with tesseract (`ocr=True` on the page):

| Document | OCR pages | Why |
| --- | ---: | --- |
| Undergraduate Academic Rules (Aug 2023) | 24 of 26 | A scan with a typed cover. Half of our version pair. |
| Faculty Entrepreneurship policy | 8 of 8 | A pure scan: `pdffonts` lists no fonts. |
| Academic Rules Handbook 2026 | 5 of 58 | Image-only cover and end pages. |
| Student Handbook 2026 | 4 of 64 | Image-only cover and end pages. |

41 pages in total; OCR is most of the 11 minutes extraction takes on our 4-core laptop
(the 24 scanned pages of the 2023 rules alone took about 6 minutes). OCR quality on the 2023 rules is good (clauses read cleanly: "5.4 Minimum CGPA
required to avoid academic warning is 2.00…"), with small errors such as "Academic
Warming", "Ifthe", "Astudent" and a lost table of contents.

**Four PDF traps we found and fixed** (each one changed what was retrieved):
1. *Off-page text.* The two 2026 handbooks carry the neighbouring page's text at negative
   x coordinates (a print imposition left in the export). Every page read as two pages
   interleaved line by line. Fix: clip each page to its bounding box. The handbook went
   from about 15,800 words to 9,212, and clause 1.20 stopped being glued to item c) of the
   class-cancellation rule.
2. *Page frames read as tables.* The prospectus draws a border round each page;
   pdfplumber returned each page as one table cell holding three columns of prose. Fix:
   ignore "tables" covering more than 60% of the page or with cells over 400 characters.
3. *Multi-column pages.* Read left to right, lines from different columns interleave.
   Fix: find a vertical gutter that almost no character crosses (30-70% of the width) and
   read column by column, recursively, so three columns work.
4. *Tables appended at the end of the page* attached them to the wrong section. Fix:
   split the page into bands at each table's top and bottom and emit the table in place.

**One before-and-after table** (the Fee Structure page and handbook clause 2.3 come out
clean; this one does not). *Annual Report 2023-24, p. 19, "Graduation Summary"*, as
printed, one row:

| Campus | BBA | BS (AF) | BS (AI) | BS (BA) | BS (CS) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Lahore | 90 | 41 | - | - | 428 |

What extraction produces: the table has no ruling lines, so pdfplumber sees no table; the
gutter detector then splits it between column groups, and the numbers end up two blocks
away from their row labels:

```
Graduation Summary
BBA BS (AF)
Karachi 12 -
Lahore 90 41
...
BS (AI) BS (BA) BS (CS)
32 - 577
- - 428
```

**What we could not recover.** "428" is in the index but no longer next to "Lahore", and
the "G. Total" row lost an empty cell. Golden question g11 ("How many BS (CS) students
graduated from Lahore in 2023-24?") is our one answerable question with recall 0: the
model, correctly, declines rather than guess. A fix would need layout-aware table
extraction (word positions → grid) for ruling-less tables; we documented it instead.
Also lost: headings inside the OCR'd Entrepreneurship policy (the "Eligibility criteria"
heading is read as body text, so its chunk is labelled "Objectives"; golden g12 has
recall 0 for that reason even though the right passage is retrieved).

## Cleaning (`indexer/pipeline/clean.py`)
Rules, in order:
1. Unicode NFKC (the `ﬁ` ligature becomes `fi`).
2. Drop pdfminer `(cid:NN)` placeholders and soft hyphens.
3. Re-join words hyphenated across a line break.
4. Remove running headers and footers: a line that appears among the first or last three
   lines of at least half the pages (compared ignoring case, digits, spaces, `|` and a
   trailing roman page number, so "U ndergraduate Academic Rules … 2026 III" and
   "Undergraduate Academic Rules … 2026 7" are the same line), and lines that are only a
   page number.
5. Re-join letter-spaced words from justified text (`t u t o r i a l s` → `tutorials`).
6. Collapse spaces and blank lines.

**Deliberately not stripped:** clause numbers at the start of a line ("5.4", "1.21"), even
when the same pattern repeats at the top of many pages (a unit test pins this), and table
rows. The chunker builds section labels and citations from the first, and the second is
what keeps "Rs. 12,000" next to "BBA / BS".

## Chunking (`indexer/pipeline/chunk.py`, ADR 0002)
Sections → paragraphs → sentences → words, under a target size, with overlap inside a
section only. Sections are Markdown headings, numbered titles ("4. Registration") and
numbered clauses ("1.21 …", labelled `1.21 Attendance Requirements` with the last title
above it). Each chunk's first line is its section label (a contextual header).
Unit: words; nomic-embed-text averages about 1.3 tokens per word here.

**Shipped: 150 words, 30 words of overlap.** Three settings and a fixed-size baseline,
each indexed in full into its own collection and scored on the 19 golden questions that
have relevant passages (`python eval/sweep_chunking.py`, raw numbers in
`eval/sweep_results.json`):

| Strategy | Size (words) | Overlap | Chunks | Storage amplification | recall@3 | recall@5 | recall@5, dense only |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| structured | **150** | **30** | 1,008 | **1.09** | **0.868** | **0.868** | 0.816 |
| structured | 300 | 60 | 812 | 1.02 | 0.842 | 0.868 | 0.816 |
| structured | 500 | 100 | 758 | 0.98 | 0.842 | 0.868 | 0.816 |
| fixed windows | 100 | 0 | 655 | 0.96 | 0.210 | 0.316 | 0.263 |

**Storage amplification** = stored words / original words = 76,418 / 70,182 = **1.09**
for the shipped setting (the lecture's example was 150/120 = 1.25). Overlap adds words
only inside sections longer than 150 words; the contextual header adds a few per chunk.
Values below 1.0 are possible because a heading line counts once in the original but is
not repeated when a section yields one chunk, and fragments under 8 words are dropped.

**Why 150/30.** Recall@5 ties across the structured settings: most sections in the rules
documents are single clauses of 20-120 words, so any size from 150 up keeps each clause
whole, and the section boundaries, not the size, decide what a chunk contains. The size
matters only for the long sections (annual-report chapters, the Generative AI policy),
and there the smaller chunks rank the right passage higher: 150/30 is the only setting
that also reaches 0.868 at recall@3. It costs 196 more vectors than 300/60 and about
three minutes more indexing (685 s vs 517 s). The fixed baseline loses 55 points because windows cut clauses
in half and label each window with the section where it happens to start.

## Embedding
`nomic-embed-text` through Ollama, **768 dimensions**, task prefixes `search_document: `
(indexer, `indexer/pipeline/embed.py`) and `search_query: ` (api,
`api/app/retrieval/embedder.py`). Batches of 16 with retries; 1,008 chunks take about
11 minutes on our laptop CPU.

**Where the pin lives and how it is enforced.** `EMBED_MODEL` is one key in
`k8s/base/configmap.yaml`, read by both the indexer and the api (`envFrom`). The store
stage writes `embed_model` and `dimension` into the collection's metadata. The api
compares them with its own `EMBED_MODEL` at start-up (`main.py`, logged) and on every
readiness probe (`routes/health.py` → `ChromaRetriever.check_pin`); on a mismatch
`/readyz` returns 503 naming both models, and the pod stays 0/1 READY. A query vector whose
length differs from the stored `dimension` is also refused (`PinMismatch`).

## Storage and metadata schema
Collection `rahbar`, cosine space, written build-aside-then-swap (ADR 0001).

| Level | Field | Example |
| --- | --- | --- |
| chunk id | `<document slug>-<position>` | `academic-rules-handbook-jun-2026-0100` (stable: same document and position, same id) |
| chunk | `source` | `academic-rules-handbook-jun-2026.pdf` |
| chunk | `section` | `4.25 Withdrawal from a Course` |
| chunk | `page` | 1-based page number where the chunk starts |
| chunk | `doc_version` | `2026-06` |
| chunk | `superseded` | `false` (`true` for the 2023 rules) |
| collection | `embed_model`, `dimension` | `nomic-embed-text`, `768` |
| collection | `index_id`, `indexed_at` | `bf61ff194391`, `2026-11-07T15:…Z` (new on every build; namespaces the cache) |
| collection | `chunk_size`, `chunk_overlap`, `chunks` | `150`, `30`, `1008` |

**Chunk count shipped: 1,008.**

## Retrieval
Dense top-k (`TOP_K=5`) with `where: {superseded: false}` unless the question asks about
an older edition (a year 2019-2025, or "old/previous/former … rules/policy/handbook"),
plus a **lexical leg** when the question contains an exact term (a clause number such as
`1.21`, a threshold such as `2.00`, a course code, a comma-grouped amount): the same
query restricted with `where_document: {$contains: term}`. The legs are merged with
reciprocal-rank fusion. Effect on recall@5 by question type, shipped index:

| Question type | Dense only | Hybrid (shipped) |
| --- | ---: | ---: |
| exact (3) | **0.667** | 1.000 |
| ordinary (12) | 0.792 | 0.792 |
| version (3) | 1.000 | 1.000 |
| injection (1) | 1.000 | 1.000 |

Dense retrieval misses "What does clause 1.21 … require?" outright: to an embedding,
"1.21" is barely a token, and the attendance clause it names never mentions the phrase in
the question. The lexical leg finds the one chunk containing "1.21". See
ENGINEERING-NOTES question 5.

**Relevance floor: 0.66.** If the best passage's cosine similarity is below
`RELEVANCE_FLOOR`, the api abstains without calling the model. On the shipped index the
lowest best-score among answerable golden questions is 0.716 (e01); off-topic probes
("What is the capital of France?" 0.641, "Who won the 2022 football world cup?" 0.629,
"Write me a poem about the sea." 0.573) fall below 0.66. The hostel and transport
questions score 0.736 and 0.669, above the floor, because the annual reports mention
hostels: those abstentions are the model's job (rule 1 of the prompt), and both are made.

## Semantic cache
Redis, keyed by `index_id` (ADR 0003). We picked the threshold with
`python eval/cache_threshold.py`: 6 paraphrase pairs that should share an answer and 8
pairs that must not (raw numbers: `eval/cache_threshold_results.json`).

| Threshold | False hits (of 8) | Paraphrase hits (of 6) |
| ---: | ---: | ---: |
| 0.90 | 4 | 5 |
| 0.92 | 2 | 3 |
| 0.94 | 1 | 1 |
| 0.95 | 1 | 0 |
| **0.96** | **0** | 0 |

**The false hit that ruled 0.95 out:** "Can I use ChatGPT in lab courses?" vs "Can I use
ChatGPT in non-lab courses?", cosine **0.9501**. The answers are opposite (prohibited in
labs; allowed, sometimes encouraged, in non-lab courses). At 0.92 two more appear:
undergraduate vs graduate minimum attendance (0.924) and repeating vs registering for
summer courses (0.913). **Shipped: 0.96**, the lowest threshold with zero false hits. The
honest consequence: with this embedding model none of our paraphrase pairs reach 0.96
either (the closest is 0.949), so in practice the cache serves repeated questions, not
rephrased ones. We would rather miss a paraphrase (one more LLM call) than serve the lab
rule to a student asking about non-lab courses.

Never cached: follow-ups, abstentions, fallbacks, and any answer whose citations were all
rejected. The cache is invalidated on every re-index because keys are namespaced by the
new `index_id` (unit test `test_reindex_invalidates_the_cache`).

## Evaluation method
`eval/run_eval.py` asks each golden question through the Ingress and scores the response's
`retrieved` list (the api returns the chunks it actually used, best first).

* **Matching rule for recall@k.** A relevant passage is `(source, section)`. It counts as
  retrieved if a chunk in the top k has the same `source` and a matching section: a golden
  clause number matches that clause or any sub-clause (`5.4` matches
  `5.4 Academic Warning/Probation` and `5.4.2 …` but never `5.41`); if the golden section
  also has a title, the retrieved title must start with it (so `6.2 Use in Lab Courses`
  does not match `6.2` of the summer-semester rules in the same handbook); an unnumbered
  golden section matches a retrieved section that starts with it (`Tuition Fee` matches
  `Tuition Fee (w.e.f. Fall 2026 semester)`). recall@k = relevant passages found in the top
  k / relevant passages, averaged over questions that have any.
* **Citation validity** = citations whose `(source, section)` equals a retrieved chunk's,
  over all citations in all responses.
* **Abstention correctness** = must-abstain questions answered with
  `answered_from_context: false`, over must-abstain questions.
* We also report the answer rate on answerable questions and whether the `expect` string
  appears in the answer. Neither is gated: they depend on the model's wording.
* **The gate** (section 5.4): fail if recall@5 is more than 5 points below
  `eval/baseline.json`, if any must-abstain question is answered, or if citation validity
  is below 95%. CI runs the same script against the three-document fixture corpus in a
  k3d cluster, with `eval/fixtures/baseline.fixture.json`.

The shipped numbers are in `eval/results.json` and `eval/baseline.json`.
