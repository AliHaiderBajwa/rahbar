# ADR 0005: Three optional fields added to the starter contract

## Status
Accepted · 7 November 2026 · Taimoor, reviewed by Ali

## Context
The starter kit says: change a contract (request, response, metadata fields) only with an
ADR. We needed three small additions.

## Decision
All three are optional with defaults, so every existing consumer (the UI, `run_eval.py`,
the starter tests) keeps working unchanged.

| Model | Field | Why |
| --- | --- | --- |
| `Passage` | `doc_version: str \| None`, `superseded: bool = False` | The prompt shows each passage's edition, so when a user explicitly asks about the 2023 rules the model can say which edition it is quoting. |
| `RetrievedRef` | `doc_version: str \| None` | The same, visible to the evaluator and in the UI's retrieved table. |
| `AskResponse` | `cached: bool = False` | The starter UI already shows a cache chip when this is true; we also send `X-Cache: HIT`. |

The chunk metadata keeps exactly the five required fields (`source`, `section`, `page`,
`doc_version`, `superseded`); collection-level metadata adds `embed_model`, `dimension`,
`index_id`, `indexed_at`, `chunk_size`, `chunk_overlap` and `chunks`.

## Consequences
None for existing clients. The Retriever protocol also gained `embed_query`,
`index_info`, `current_index_id`, `heartbeat` and `check_pin` (internal interfaces, not
the HTTP contract), so the cache and the readiness probe can use them.
