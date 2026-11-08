# ADR 0004: Groq as the LLM provider, Ollama for embeddings, and what leaves the cluster

## Status
Accepted · 6 November 2026 · Ali and Taimoor

## Context
Section 3.5 asks for a pluggable provider selected by `LLM_PROVIDER`, with the embedding
model pinned separately. Our development laptop has 3.8 GB of RAM: it cannot run
`llama3.2:3b` next to a Kubernetes cluster (the model alone needs about 3 GB).

## Decision
* **Generation: `LLM_PROVIDER=hosted`, Groq's free tier** (OpenAI-compatible
  `/chat/completions`), model `openai/gpt-oss-20b`, JSON mode
  (`response_format: json_object`) at temperature 0.2, validated with Pydantic afterwards.
  The key is in `Secret/rahbar-secret`, created on the command line (`make secret`); the
  committed `k8s/base/secret.placeholder.yaml` holds `REPLACE_ME` and is not in any
  kustomization.
* **Embeddings: `nomic-embed-text` through Ollama** (768 dimensions), pinned by
  `EMBED_MODEL` in the one ConfigMap both the api and the indexer read, and checked in code
  against the collection's metadata (section 4.4). On minikube Ollama runs on the host
  (`host.minikube.internal:11434`, the default `ollama-host` path); on our k3d laptop
  cluster it runs as a container on the cluster's Docker network.
* **The interface is one method.** A provider implements `complete(messages)`;
  `JsonChatProvider.generate` (`api/app/providers/llm/base.py`) does schema enforcement,
  validation, the single retry and the error contract for all of them. `ollama.py` and
  `hosted.py` are each about 50 lines, and adding a provider is one file plus one line in
  `providers/llm/__init__.py`. CI and the unit tests use `stub`: no key, no network.

## Data governance
What leaves the cluster on each question, and to whom:

| Data | Goes to | Why |
| --- | --- | --- |
| The question text | Ollama (our machine) and Groq | embedding; generation |
| Up to 5 retrieved passages from public FAST-NUCES documents | Groq | the evidence |
| Nothing else | | no names, roll numbers, sessions or IP addresses are sent |

* The corpus is public university documents only (no SLATE, Flex or NU Mail; CORPUS.md).
* Users could type personal data into a question. The UI does not ask for any, the api
  does not attach any, and we log the question only in the per-request JSON line, which
  stays on stdout in the cluster. A production deployment for real students should either
  self-host generation (`ollama-incluster`, one ConfigMap change) or sign Groq's data
  processing terms; we note this rather than pretend the free tier is a compliance story.
* Groq's free tier has rate limits. A 429 is retried with the server's `retry-after` (at
  most 20 s in total); anything else becomes the safe fallback, never a 500.

## Options considered
* **`ollama-host` with `llama3.2:3b`**, the handout's default: the best privacy story, but
  not runnable on our hardware alongside the cluster. The code path exists and is tested
  with a fake transport; switching is `LLM_PROVIDER=ollama-host` in the ConfigMap.
* **Google AI Studio or OpenRouter**: equally free. Groq's OpenAI-compatible API kept
  `hosted.py` generic, and its JSON mode is supported on every model it serves.

## Consequences
* Generation quality is better than a 3B local model would give us, at the cost of sending
  public passages and student questions to a third party.
* A Groq outage or rate limit shows up as fallbacks (`rahbar_fallbacks_total`), not errors.
* Rotating the key is `kubectl create secret … --dry-run=client -o yaml | kubectl apply -f -`
  and `kubectl rollout restart deploy/api`; no image or manifest changes.
