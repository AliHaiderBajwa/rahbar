# Engineering notes

Seven questions from section 7, answered from our own files. Line numbers refer to the
commit this file was submitted with.

## 1. Indexing in API startup: what breaks with 3 replicas and one Chroma volume?

**The failure:** three concurrent writers on one index. `k8s/base/api-deployment.yaml:9`
asks for `replicas: 3`, so a rollout or a fresh apply starts three api pods at once. If
each indexed in its startup hook, all three would run our store stage against the same
`chroma-0` and its one ReadWriteOnce volume (`k8s/base/chroma-statefulset.yaml:71`,
`volumeClaimTemplates`). With our build-aside store that is worse than duplicates: each
pod deletes `rahbar__building`, recreates it and starts adding chunks, so pod B deletes the
staging collection pod A is half-way through, and whichever finishes last publishes an
index that may hold a mixture, or a fraction, of the 1,008 chunks. Without build-aside the
three would `add` the same ids into the live collection mid-query.

The knock-on failures: every restart (rolling update, OOMKill, node drain) re-runs about
20 minutes of download, OCR and embedding; and the startup probe
(`api-deployment.yaml:60-63`, 30 × 2 s = 60 s) would kill a pod that is still OCR-ing,
so a rollout would never finish.

**The object that prevents it:** `Job/indexer` (`k8s/base/indexer-job.yaml:5`,
`restartPolicy: OnFailure` at `:20`, `backoffLimit: 4` at `:11`) and `CronJob/reindex`
with `concurrencyPolicy: Forbid` (`k8s/base/reindex-cronjob.yaml:11`): exactly one writer,
ever. The api only reads (`api/app/retrieval/chroma.py` calls `get_collection`, `query`,
`count`; nothing in `api/` calls `add`, and the checker's "no indexing in startup" test
agrees). Its readiness probe (`api-deployment.yaml:69-70` → `/readyz`) keeps it out of the
Service until the Job has published a non-empty collection, which we watched happen on
our cluster (`docs/evidence/api-not-ready-before-index.txt`). ADR 0001 has the full
reasoning.

## 2. Readiness depends on Chroma, liveness does not. Swap them, and Chroma is slow for 90 seconds?

Today: `livenessProbe` → `/healthz` (`api-deployment.yaml:64-68`), which touches nothing
(`api/app/routes/health.py:25`), and `readinessProbe` → `/readyz` (`:69-73`), which checks
Chroma, the collection, the pin and Redis (`health.py:30-73`). A 90-second Chroma stall
makes `/readyz` fail; after 3 failures × 10 s (30 s) all three pods leave the Service and
users get a 503 from the Ingress for about a minute. Nothing restarts. When Chroma
recovers, the next readiness probe passes and traffic returns within 10 s.

Swapped: liveness now probes Chroma every 20 s with `failureThreshold: 3`. After 60 s of
the stall the kubelet **kills all three api containers**, at the same moment, because they
all see the same Chroma. They restart, the startup probe passes (it still checks only
that the process is up), liveness fails again because Chroma is still slow, and the pods
enter CrashLoopBackOff with growing back-off delays (10 s, 20 s, 40 s,
… up to 5 minutes). Chroma recovers at 90 s, but the api pods are now sitting out their
back-off, so a 90-second database blip becomes several minutes of total outage, every
cold start reconnects to Chroma at once, and there is nothing a user can do. Meanwhile
readiness now probes `/healthz` and says "ready" for pods that cannot reach Chroma, so
during the stall the Service keeps sending requests to pods that answer 503.

Readiness protects users, liveness protects the pod: a dependency check belongs only in
readiness.

## 3. While the model generates, what does the CPU-based HPA measure, and what should scale this service?

`k8s/base/hpa.yaml:16` targets 70% of `requests.cpu` (50m, `api-deployment.yaml:51`), so
it scales when a pod averages more than 35m of CPU. While a request waits for Groq
(about 1-12 s per answer in our golden run, `eval/results.json` → `seconds`), the
pod's CPU is almost idle: the work happens on Groq's GPUs, and our process is blocked on a
socket. We measured it on our cluster: during a full 21-question golden run the three api
pods stayed at **9-10m of CPU** each (20% of request; the HPA showed `cpu: 10%/70%`) (`docs/evidence/kubectl-top.txt`). So the HPA
measures our own overhead (JSON parsing, the cosine scan of the cache, Prometheus
bookkeeping), not demand. Fifty users waiting on the model look like zero load, and the
HPA will not add a pod while requests queue up behind uvicorn's single worker.

What should scale it is **concurrency**: in-flight requests per pod (or queue length),
with request latency (p95 of `rahbar_request_seconds{path="/api/ask"}`) and the
provider's rate limit as guards. To use it we would need (1) a `rahbar_inflight_requests`
gauge in the api (increment in the middleware in `api/app/main.py`, decrement in its
`finally`), (2) Prometheus scraping `/metrics`, (3) prometheus-adapter (or KEDA) exposing
that series to the custom metrics API, and (4) an HPA `type: Pods` metric with a target
such as 4 in-flight requests per pod. One more caveat: scaling out does not raise Groq's
8,000 tokens-per-minute limit; past that point more pods only produce more 429s, so the
real ceiling for this service is the provider quota, and `rahbar_fallbacks_total` is the
signal that we hit it.

We still keep the CPU HPA (`minReplicas: 3`, `maxReplicas: 8`): it is the right signal
for the cache-hit path, which does real CPU work, and it proves the plumbing works
(`docs/evidence/hpa-scale-up-under-load.txt`: 3 → 6 → 8 replicas within 32 seconds of a
CPU-bound load, back to 3 two minutes after it stopped).

## 4. The indexer's peak memory, its limit, the margin, and the exit code if it is wrong

**Peak.** We sampled `kubectl top pods` every 15 s for the whole in-cluster indexer run
(`docs/evidence/kubectl-top-indexer.txt`): peak **186Mi** and **1411m** CPU, while
OCR-ing the scanned 2023 rules. The first version of the extractor was much worse: in
Docker with a 1 GiB limit it reached **1,024 MiB of 1 GiB** (`docker stats`, before
the fix in `indexer/pipeline/extract.py` that closes each pdfplumber page and OCR image
after use; pdfplumber otherwise caches every parsed page of a 26-page scan).

**Limit and margin.** `k8s/base/indexer-job.yaml:55-57`: request 256Mi, limit
**512Mi**: 326Mi (2.7x) above the sampled peak. The margin is wide on purpose: samples
are 15 s apart, so a short spike while rendering one page at 200 dpi can be missed, and
the next corpus revision may add a longer scan. CPU is limited to 1500m; the measured
1411m means OCR runs close to it, but going over a CPU limit only throttles (OCR gets
slower), it does not kill.

**Set it wrong:** the container is **OOMKilled with exit code 137** (128 + SIGKILL).
We provoked it with a copy of the Job limited to 96Mi
(`docs/evidence/indexer-oomkilled.txt`). With `restartPolicy: OnFailure` the kubelet
restarts the container in place, which is the CrashLoopBackOff the handout warns about,
and every restart starts the download and OCR again from zero. The Job fails for good
after `backoffLimit: 4` retries (`indexer-job.yaml:11`). Meanwhile nothing breaks for
users: the build-aside store never touched the live collection, so the api keeps serving
the previous index (ADR 0001). The fix is `kubectl top`, then a limit above the measured
peak, not a guess.

## 5. recall@5 by question type: where is it worst, and why?

Shipped (hybrid) retrieval, from `eval/results.json`, with the dense-only numbers from the
sweep (`eval/sweep_results.json`, same 150/30 index, `HYBRID=false`):

| Type | Questions | Dense only | Hybrid (shipped) |
| --- | ---: | ---: | ---: |
| exact | 3 | **0.667** | 1.000 |
| ordinary | 12 | 0.792 | 0.792 |
| version | 3 | 1.000 | 1.000 |
| injection | 1 | 1.000 | 1.000 |

**With pure embeddings, exact terms are the worst type**, as the handout predicted: e01,
"What does clause 1.21 of the academic rules require?", retrieves five chunks about
attendance and registration and not clause 1.21 itself. An embedding represents meaning;
"1.21" carries almost none (it is a few sub-word tokens that look like any other number),
and the question contains none of the clause's own words ("100% attendance", "condoned").
The same happens to codes, CGPA values and fees: "2.00" and "2.67" are nearly the same
vector.

The lecture's fix is **hybrid retrieval**: combine a lexical match with the dense one.
Ours is in `api/app/retrieval/chroma.py`: `exact_terms` (`:46`) pulls clause numbers,
thresholds, codes and amounts out of the question; each becomes a second query restricted
with `where_document: {"$contains": term}` (`:131`); the legs are fused with
reciprocal-rank fusion (`rrf_merge`, `:55`). That lifts exact-term recall@5 from 0.667 to
1.000 without moving any other type.

**After that fix the worst type is ordinary (0.792)**, and the misses are not retrieval
problems at all: g11 (the graduates-by-campus table, recall 0) is an extraction loss, and
g12 (the scanned start-up policy, recall 0) is an OCR'd heading the chunker could not
recognise, so the right passage is retrieved under the label "Objectives" and the
evaluator cannot credit it (PIPELINE.md, "What we could not recover"). g04 finds one of
its two relevant clauses. In other words: stage A, not stage C.

## 6. EMBED_MODEL changed in the ConfigMap, only the api restarted: what happens?

We did exactly this (`docs/evidence/failure-analysis/C-topk1-and-embed-pin.txt`, and on
the cluster in `docs/evidence/embed-pin-mismatch.txt`). The api starts, reads the
collection's metadata (written by the indexer at `indexer/pipeline/store.py:41-42`), and
`check_pin` (`api/app/retrieval/chroma.py:112-119`) finds `embed_model='nomic-embed-text'`
where the ConfigMap now says something else. At start-up it logs, at ERROR,
`"embedding model mismatch: refusing to become ready"` with the conflict
(`api/app/main.py:53-57`); every readiness probe then returns 503 with
`"failed": ["embed_model"]` naming both models (`api/app/routes/health.py:55-58`). The pod
sits at 0/1 READY, `maxUnavailable: 0` (`api-deployment.yaml:17`) stops the rollout from
removing an old ready pod, and users keep being served by the pods that still agree with
the index. As a second line, a query vector whose length differs from the collection's
`dimension` raises `PinMismatch` (`chroma.py:152-154`).

Without the guardrail: nothing visible at all. The new model's vectors would be compared
with the old model's; with a different dimension Chroma would raise on every query (a
loud failure, at least), but with the *same* dimension (two 768-d models) it would return
nearest neighbours computed across incompatible spaces: confident, cited, wrong answers,
no error in any log, and a recall@5 that only the nightly eval would notice. Fixing it is
then "re-index", which the guardrail tells you to do before a single user sees it.

## 7. The failure that cost us more than an hour

**Symptoms.** Our first green-looking CI run had one red job, `e2e-eval`:
`error: timed out waiting for the condition on jobs/indexer`. `kubectl get pods` in the
log showed the three api pods Running but 0/1, and `chroma-0` in **CrashLoopBackOff**
with 6 restarts. The indexer pod was gone: it had failed to reach Chroma, retried, and the
Job had given up after `backoffLimit`. The same manifests and the same Chroma image had
run fine on the laptop all day.

**What we wrongly believed first.** That it was storage. k3s uses the local-path
provisioner, which ignores `fsGroup`, and our StatefulSet runs Chroma as uid 1000 with a
read-only root filesystem; an earlier laptop run had died with "unable to open database
file" for exactly that reason. We spent the first stretch re-creating the pod's exact
constraints in Docker (uid 1000, `--read-only`, a 64 Mi `/tmp`, a 768 Mi memory limit, a
root-owned mode-0777 data directory like local-path makes)... and Chroma started
perfectly. We also lost time to a red herring on the laptop: Docker Desktop had been
started and had switched the CLI's context, so `docker images` suddenly showed none of
our images and a bind mount failed with "mounts denied", which looked like the same
storage problem and was not.

**What told us the truth.** The difference between our reproduction and the pod was not
the filesystem; it was the environment. Kubernetes injects service-link variables for
every Service in the namespace, and our Service is called `chroma`, so the pod gets
`CHROMA_PORT=tcp://10.43.x.x:8000`. Running the image with that one variable:

```
docker run --rm -e CHROMA_PORT=tcp://10.43.12.34:8000 chromadb/chroma:1.0.12
thread 'main' panicked at rust/frontend/src/config.rs:182:23:
Error loading config: invalid type: found string "tcp://10.43.12.34:8000",
expected u16 for key "PORT" in `CHROMA_` environment variable(s)
```

Chroma reads every `CHROMA_*` variable as configuration. The fix is one line per pod,
`enableServiceLinks: false` (`k8s/base/chroma-statefulset.yaml`, with a comment; we set it
on every workload, since none uses service links), and the next e2e run went green. What
we changed in the process: the CI script now prints `kubectl logs statefulset/chroma
--previous` and the namespace's events when the Job fails
(`scripts/ci/k3d-e2e.sh`), because the one line that would have saved us most of that time, the panic message, was
in a log nobody printed.
