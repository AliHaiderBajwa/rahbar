# Failure analysis

We broke five of the six stages on purpose, one at a time, on the shipped index (1,008
chunks, 150/30) with Groq `openai/gpt-oss-20b`, measured what changed, and restored each
before the next. Raw outputs are in `docs/evidence/failure-analysis/` and
`eval/sweep_results.json`. The switches used exist only for these experiments and default
to the shipped behaviour: `OCR=off` (indexer), `CHUNK_STRATEGY=fixed` (indexer),
`TOP_K`, `PROMPT_RULES=off` and `SUPERSEDED_FILTER=off` (api).

The lecture's rule held: only stage F is the model's fault, so we would check A to E
first. Every symptom below shows up in a number we already log or compute.

| Stage | What we broke | recall@5 (19 answerable golden questions) | Visible in |
| --- | --- | ---: | --- |
| none | shipped system | 0.868 | `eval/results.json` |
| A · Extraction | no OCR for the two scanned PDFs | 0.816 (v03 → 0) | indexer log `empty_pages`, chunk count per document |
| B · Chunking | fixed 100-word windows, no overlap | **0.316** | recall@5 by type, section labels in `retrieved` |
| C · Retrieval | `top_k: 1` | **0.526** | recall@k, `top_k` and `chunk_ids` in the per-request log |
| C · Retrieval | `EMBED_MODEL` changed on the api only | (pod not ready) | `/readyz` 503, ERROR log line, `kubectl get pods` 0/1 |
| D · Prompt | removed the "use the supplied context" rule | 0.868 (no change) | abstention correctness, `abstain_reason` |
| E · Source | removed the `superseded: false` filter | 0.868 (no change) | rank and `doc_version` of the top passage in the log |

---

## Stage A · Extraction: the scanned PDFs indexed without OCR
- **What we broke:** `OCR=off` for the indexer, then rebuilt the two scanned documents (the
  2023 Academic Rules, 24 of 26 pages scanned, and the Faculty Entrepreneurship policy, all
  8 scanned) into a copy of the index (`fa_no_ocr`, 860 chunks instead of 1,008).
- **Symptom:** the 2023 rules shrank to **1 chunk** (its typed cover page) and the policy to
  **0 chunks**. Golden g12 ("How long must a faculty member have worked full-time … to set
  up a technical start-up?") retrieved a student-handbook preamble and an annual-report
  table instead (best score 0.697) and the model declined. v03 ("Under the older 2023
  academic rules, what was the deadline to withdraw?") retrieved the **2026** clause 4.25
  as its best match; the model declined rather than present the 2026 rule as the 2023
  one, but the question became unanswerable. recall@5 0.868 → 0.816 (v03 drops to 0; g12 is
  already 0 in the shipped system because OCR loses its heading, PIPELINE.md; the other
  17 questions retrieve from documents this break does not touch, so we did not re-ask
  them).
- **Diagnosis, as if we had not caused it:** the first sign is a whole category of
  questions failing together (everything about one document), which points at extraction,
  not retrieval. The indexer's own log says it directly:
  `{"msg": "extracted", "source": "undergraduate-academic-rules-aug-2023.pdf", "pages": 26,
  "ocr_pages": 24, "empty_pages": 24}` followed by `"chunked" … "chunks": 1`. A 26-page
  document producing one chunk is the number to alarm on. `pdffonts` on the file then
  shows no fonts on the body pages.
- **Remedy:** OCR every page with fewer than 25 characters of text (`extract.py`,
  `ocr_page`), which needs `tesseract-ocr` and `poppler-utils` in the indexer image, and a
  memory limit sized for it (ENGINEERING-NOTES question 4).

## Stage B · Chunking: fixed 100-word windows, no overlap
- **What we broke:** `CHUNK_STRATEGY=fixed`, size 100, overlap 0: plain word windows over
  each document, blind to clauses and headings (the sweep's baseline row).
- **Symptom:** recall@5 **0.868 → 0.316**, recall@3 0.868 → 0.210. By type: version 1.0 →
  **0.0**, ordinary 0.79 → 0.25, exact 1.0 → 0.67. Windows start mid-clause, so "4.25 The
  deadline to withdraw…" sits in a chunk labelled with whatever clause the window started
  in (`4.23 …`), and the end of one rule is glued to the next.
- **Diagnosis, as if we had not caused it:** the retrieved passages *contain* the right
  words but carry the wrong section labels, so citations point at the wrong clause: in the
  per-request log, `chunk_ids` look reasonable while the `retrieved` sections do not match
  the clause the answer quotes. recall@5 by type collapses unevenly (version questions to
  0) because those depend on one exact clause. A sharp, uniform drop across many question
  types right after a re-index (new `index_id`, new `chunk_size` in the collection
  metadata) points at chunking.
- **Remedy:** structure-aware chunking (ADR 0002): sections, then paragraphs, then
  sentences, overlap only inside a section, the section label as the chunk's first line.

## Stage C · Retrieval: `top_k: 1`, and a changed `EMBED_MODEL` on the api only
- **What we broke (1):** retrieval with k = 1.
- **Symptom:** recall **0.868 → 0.526**. Twelve of 19 questions lose a relevant passage.
  Questions with two relevant passages (the fee question needs both "Admission Fee" and
  "Fee Refund Policy") can never score above 0.5, and close calls flip: e01 ("clause 1.21")
  loses its only relevant passage.
- **Diagnosis:** the per-request log line carries `top_k` and the list of `chunk_ids` and
  `scores`. One chunk id per request, and answers that address only half of a two-part
  question, say it is the retrieval breadth, not the model. In the metrics, abstentions
  with `reason="model"` rise while the relevance-floor abstentions do not.
- **What we broke (2):** `EMBED_MODEL=all-minilm` on the api only; the index was built
  with `nomic-embed-text`.
- **Symptom:** the api never becomes ready. `/readyz` → **503**
  `{"failed": ["embed_model"], "checks": {"embed_model": "embedding model mismatch:
  collection 'rahbar' was indexed with embed_model='nomic-embed-text' (dimension=768) but
  this API has EMBED_MODEL='all-minilm'"}}`, and at start-up the api logs
  `"level": "ERROR", "msg": "embedding model mismatch: refusing to become ready"`.
- **Diagnosis:** `kubectl get pods` shows the api Running but 0/1 READY; `kubectl describe
  pod` shows the readiness probe failing with 503; `kubectl logs` names both models.
  Without the guardrail there would be no symptom at all except bad answers: vectors from
  two models are not comparable, and Chroma would happily return nearest neighbours
  computed across them (see ENGINEERING-NOTES question 6).
- **Remedy:** `TOP_K` 5 from the ConfigMap; the embedding pin enforced in code.

## Stage D · Prompt: the "use the supplied context" instruction removed
- **What we broke:** `PROMPT_RULES=off` removes rule 1 of the system prompt ("Use the
  supplied context only. If the evidence does not contain the answer, set
  answered_from_context: false…"). We asked the questions that need restraint: the two
  must-abstain questions (hostel, transport) and g11 (the table we cannot extract).
- **Symptom: none we could provoke.** Abstention correctness stayed at **100%** (both
  must-abstain questions declined), and g11 still produced no answer (that run hit the
  schema-validation fallback). With rule 1 kept, the same three questions also declined
  or answered from evidence. The result is in
  `docs/evidence/failure-analysis/D-prompt-rule-*.json`.
- **Why the break did not bite:** the instruction is not our only one. The user message
  still ends "Answer the question from the evidence above", rule 2 still says the evidence
  is data, and the output schema forces the model to commit to `answered_from_context`
  for every answer. One sentence was redundant. That is defence in depth working, and it
  is also a warning about how we test: a single golden run would have told us "the prompt
  rule does nothing", when what it really shows is that this model, at temperature 0.2,
  follows the remaining instructions. A weaker model (the handout's `llama3.2:3b`) is
  where we would expect this break to show.
- **Diagnosis, had it broken:** abstention correctness below 100% in `results.json` (the
  CI gate fails on it), and `rahbar_abstentions_total{reason="model"}` dropping while
  traffic stays the same.
- **Remedy:** keep the rule; the gate's abstention check is what would catch its loss.

## Stage E · Source: the superseded filter removed
- **What we broke:** `SUPERSEDED_FILTER=off`: the retriever no longer adds
  `where: {superseded: false}`, so the repealed 2023 rules compete with the 2026 handbook.
- **Symptom in retrieval:** for "When is the last day to withdraw from a course?" the
  **2023 clause 4.25 ranks first** (cosine 0.865, `superseded: True`, "the last day of
  classes"), just ahead of the 2026 clause 4.25 (0.861, "the last day of the makeup week").
  For "How long is the summer semester?" the 2023 "8 to 9 weeks" passage ranks first. The
  two editions say nearly the same thing in nearly the same words, so the embedding cannot
  tell them apart; the score gap is 0.004.
- **Symptom in the answer:** none, this time. The model answered "the last day of the
  makeup week" and "8 weeks" from the 2026 passages, because our prompt shows each
  passage's `doc_version` and `superseded` and tells the model to prefer the current
  edition (rule 4). Recall@5 is unchanged (0.868) because the 2026 clause is still in the
  top five.
- **Why we still treat it as a failure:** the handout's point stands: metadata filtering
  fixes failure mode E, a prompt cannot be relied on to. With the filter off, a repealed
  clause occupies the top slot and one of five evidence slots, and the right answer
  depends on the model noticing an attribute. A model that ignores it quotes a repealed
  rule with a valid citation, which no citation check can catch.
- **Diagnosis, as if we had not caused it:** the per-request log line lists the retrieved
  `chunk_ids`; ids starting `undergraduate-academic-rules-aug-2023-` on a question that did
  not ask about 2023 (`include_superseded: false` in the same log line) mean the filter is
  not being applied. A golden version question whose `expect` string ("makeup week")
  disappears from the answer is the eval-side signal.
- **Remedy:** the filter in the retriever (`api/app/retrieval/chroma.py`), on by default;
  superseded documents only when the question names an older edition (golden v03 checks
  that path still works).

## Stage F · Generation (not run)
Raising the temperature to 1.2 is the one break that is the model's fault. We did not run
it: Groq's free tier gives us 8,000 tokens a minute, and we spent the budget on A-E, which
the lecture says to check first. What we would expect and watch: `rahbar_fallbacks_total`
rising (more replies that fail schema validation even after the retry), answers that
paraphrase numbers ("about 2" instead of "2.00", lowering the `expect` hit rate), and
`rahbar_citations_dropped_total` rising as the model invents section names.
