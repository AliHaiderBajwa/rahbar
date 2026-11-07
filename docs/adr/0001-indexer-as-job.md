# ADR 0001: The indexer is a Job; the API only reads the index

## Status
Accepted · 5 November 2026 · Ali (Kubernetes side), Taimoor (pipeline side)

## Context
RAHBAR has two workloads with opposite shapes. Indexing (extract, OCR, clean, chunk,
embed, store) is heavy, slow and rare: it takes about nine minutes on our laptop, most of
it OCR-ing the scanned 2023 rules, and its memory peaks near 1 GiB. Answering is light and
constant: one embedding, one vector query and one LLM call per question, served by three
replicas of the api.

The obvious design, "build the index when the api starts", ties the two together, and on a
cluster that is wrong four times over:

1. **Three writers, one volume.** With `replicas: 3` (`k8s/base/api-deployment.yaml:9`)
   three pods start at once and all three write to the one Chroma volume. Each is half-way
   through a different PDF; the result is an index that no single run produced, with
   duplicate or missing chunks and nobody to blame.
2. **Every restart pays for indexing again.** A rolling update, an OOMKill, a node drain or
   a failed liveness probe each re-run nine minutes of OCR and embedding.
3. **Readiness becomes a lie.** Either the pod reports ready before its index exists and
   serves empty retrievals, or it reports ready only after nine minutes, and the startup
   probe (`failureThreshold: 30 × periodSeconds: 2` = 60 s) kills it half-way, forever.
   A rolling update would swap three working pods for three that are still chunking PDFs.
4. **No re-index without a redeploy.** New documents would mean restarting the api: a
   configuration change that needs a rebuild, the "Friday failure" from Lecture 05.

## Decision
Indexing runs **only** in `Job/indexer` (`k8s/base/indexer-job.yaml`) and in the weekly
`CronJob/reindex` built from the same pod spec (`k8s/base/reindex-cronjob.yaml`). The api
never writes to Chroma: `api/app/retrieval/chroma.py` only calls `get_collection`, `query`
and `count`, and the API's startup (`api/app/main.py`, `lifespan`) only reads the
collection's metadata to check the embedding pin.

* `restartPolicy: OnFailure`, `backoffLimit: 4` and `activeDeadlineSeconds: 3600` bound a
  failing run; `concurrencyPolicy: Forbid` (`reindex-cronjob.yaml:11`) means two indexers
  can never run at once.
* Re-indexing is one command, with no redeploy and no downtime:
  `kubectl create job --from=cronjob/reindex manual-reindex-1 -n rahbar` (`make index`).
* The api's readiness probe (`/readyz`, `api/app/routes/health.py:76`) reports 503 until
  the collection exists, is non-empty, and was built with the same `EMBED_MODEL`. A pod
  with nothing to retrieve is held out of the Service instead of serving empty answers.

**What happens to the old index while a new one is built** (the question the starter kit
asked in `store.py`). The indexer builds the new index *aside*, in `rahbar__building`, and
only when every chunk is written and counted does it delete the live collection and rename
the staging one into its place (`indexer/pipeline/store.py:81-84`). The api keeps answering
from the old index for the whole run; the gap is one delete and one rename (31-34 ms in our
logs, `swap_ms`). A Job that dies half-way leaves only a stale `__building` collection,
which the next run deletes first. The live index is never half-written.

The swap also invalidates the semantic cache without the indexer ever touching Redis: each
build stamps a new `index_id` into the collection metadata, and the api namespaces cache
keys by it (ADR 0003).

## Options considered
* **Index in the api's startup.** Rejected for the four reasons above.
* **An init container on the api Deployment.** Still three writers, and still a re-index
  on every pod restart; it only moves the problem.
* **A sidecar that watches the corpus.** Always-on cost for a rare event, and still one
  writer per replica unless we add leader election, which is complexity we do not need.
* **Delete-then-write in place.** Simpler, but the api would see an empty or partial
  collection for minutes; readiness would drop all three pods at once. Build-aside costs
  double storage for a few minutes, which is nothing at our size (about 1,000 chunks).

## Consequences
* A fresh cluster has a window where the api is Running but 0/1 READY: until the first Job
  completes. That is correct behaviour, and the README says so.
* The Job's resources are sized for the worst document (OCR), not for answering; the api's
  are sized for answering. Each can be tuned without touching the other.
* A changed Job spec cannot be re-applied over a finished Job (`spec.template` is
  immutable); our CD creates a fresh cluster each run, and on a long-lived cluster we
  re-index with `kubectl create job --from=cronjob/reindex` instead of re-applying.
* Watch for: the CronJob's pod spec drifting from the Job's. Both are generated from the
  same block, and a reviewer should diff them on every change.
