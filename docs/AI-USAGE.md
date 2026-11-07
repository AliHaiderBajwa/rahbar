# AI usage

We used one AI tool heavily on this assignment, and this page says how. The course rule is
honest attribution, and a specific disclosure carries no penalty, so this is the
specific version.

## Tool

**Claude Code** (Anthropic's coding agent, model Claude Opus 5.5), run on Ali's laptop
on 7-8 November 2026 with access to the repository folder, a shell, Docker and the GitHub
CLI.

## What it wrote or shaped

| Area | What the AI did | What we did |
| --- | --- | --- |
| Planning | Read the handout, the starter kit and our team plan, and proposed the order of work. | Chose the seat split (Ali: Systems/DevOps, Taimoor: RAG core), the LLM provider, the corpus and the deliberately absent topic; approved the plan. |
| Indexer (`indexer/pipeline/*`) | Wrote extract, clean, chunk, embed and store, and found and fixed the PDF traps documented in CORPUS.md and PIPELINE.md (off-page text in the 2026 handbooks, page frames read as tables, three-column pages, letter-spaced text). | Reviewed the extracted text against the PDFs. |
| API (`api/app/*`) | Wrote the Chroma retriever (dense + lexical leg), the Groq and Ollama providers, validation/retry/fallback, the citation guard, the semantic cache, `/readyz`, the embedding-pin check and the metrics. | Chose Groq; created the API key. |
| Tests | Wrote the 60 unit tests (40 api, 20 indexer), including the prompt-injection tests. | |
| Kubernetes, Docker, CI/CD | Wrote every Dockerfile, manifest, overlay and workflow, and `scripts/ci/k3d-e2e.sh`. | Created the GitHub repository settings, branch protection, the Docker Hub token and the GitHub Secrets; reviewed and approved the pull requests. |
| Evaluation | Drafted the golden set from the documents (before any tuning), implemented recall@k, citation validity, the summary and the gate, and wrote and ran the chunking sweep and the cache-threshold experiment. | |
| Docs | Drafted every document in `docs/`, the ADRs and the README, from the measurements it ran. | |

## How we used its output

* Every number in PIPELINE.md, FAILURE-ANALYSIS.md and ENGINEERING-NOTES.md comes from a
  command that was actually run (the scripts are in `eval/`; the raw outputs are in
  `eval/*.json` and `docs/evidence/`). Where the AI's first guess and the measurement
  disagreed, the measurement won. Three examples:
  * the cache threshold went from a guessed 0.92 to a measured 0.96, because at 0.95
    "ChatGPT in lab courses" and "ChatGPT in non-lab courses" score 0.9501;
  * the indexer's memory limit went up after the first real run peaked at 1,024 MiB
    against a 1 GiB limit, and the extractor was changed to release each PDF page;
  * the LLM model changed when Groq turned out no longer to serve the model in our first
    ConfigMap.
* The prompt-injection defence was checked against a real model, not only the unit tests:
  golden question i01 tells the system to ignore its instructions, and the answer still
  quotes the 100% attendance rule with valid citations.

## Changes we made after review

<!-- Ali and Taimoor: list here anything you changed, rewrote or removed after reading
     the AI's work, and why. Leave it empty rather than inventing entries. -->

## What we can explain
The handout says the submission must reflect work we understand and can explain. Before
the viva we will each walk through the files in our seat (ADR 0001 and the file
ownership table in our team plan list them) and re-run `make test`, `make check` and the
clean-clone steps in the README ourselves.
