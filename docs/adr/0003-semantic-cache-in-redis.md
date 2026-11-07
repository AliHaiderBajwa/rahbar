# ADR 0003: The semantic cache lives in Redis, keyed by index_id

## Status
Accepted · 7 November 2026 · Taimoor (cache logic), Ali (Redis Deployment and PVC)

## Context
Section 3.3 asks for a semantic cache in Redis or in a second Chroma collection, with a
hit defined as cosine similarity ≥ `CACHE_THRESHOLD`, a set of questions that must never be
cached, and invalidation on every re-index.

## Decision
**Redis**, as `Deployment/redis` with a PVC and append-only persistence
(`k8s/base/redis.yaml`), reachable only from the api (`NetworkPolicy/redis-from-api`).

* Entries are stored per index, in the list `rahbar:cache:<index_id>`, newest first,
  capped at `CACHE_MAX_ENTRIES` (256) with a TTL (`CACHE_TTL`, 24 h) and
  `maxmemory-policy allkeys-lru` as a backstop. Each entry holds the question's vector
  (packed float32) and the full response (`api/app/services/cache.py`).
* A lookup reads the list for the current `index_id` and takes the best cosine at or above
  the threshold. At our scale (hundreds of entries) a linear scan is under a millisecond.
* **Invalidation on re-index** comes from `index_id`, which the indexer writes into the
  collection metadata on every build (ADR 0001). A new index means a new key, so answers
  from the old index can never be served; the first pod to see the new id also deletes
  the old keys (`invalidate_except`). The indexer does not need to reach Redis at all.
* **Never cached** (`services/answer.py`, `_store`): follow-up questions, abstentions,
  answers whose citations were all rejected, fallbacks after a validation failure, and
  anything with `answered_from_context: false`.
* A Redis outage degrades to "no cache", never to an error for the user; `/readyz` reports
  it so the problem is visible.

## Options considered
* **A second Chroma collection.** Chroma already does nearest-neighbour search, so a hit
  would be one query. But it would put a second writer (the api) on the Chroma volume that
  ADR 0001 keeps single-writer, and Chroma has no TTL, so expiry would be our code.
* **An exact-match cache** (hash of the normalised question). Safe and simple, but it
  misses every paraphrase, which is most of the benefit.
* **Redis with a vector index (RediSearch).** Faster at scale; needs Redis Stack, a bigger
  image and more memory than our laptop cluster has. Not worth it below ~10k entries.

## Consequences
* The threshold matters more than the store. We chose 0.96 from data
  (`eval/cache_threshold.py`): at 0.95 "Can I use ChatGPT in lab courses?" and "… in
  non-lab courses?" score 0.9501 and would share an answer, which is exactly wrong. At
  0.96 there are no false hits, but none of our six paraphrase pairs hit either: with
  nomic-embed-text the cache is, in practice, a repeat-question cache. We accept that;
  a wrong cached answer is served to everyone, a miss only costs one LLM call.
* The cache's correctness depends on `index_id` changing on every re-index; a unit test
  (`api/tests/test_cache.py::test_reindex_invalidates_the_cache`) pins that down.
* Watch for: the linear scan if the cache ever grows past a few thousand entries.
