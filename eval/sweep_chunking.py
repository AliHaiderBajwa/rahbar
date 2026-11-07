#!/usr/bin/env python3
"""Chunking sweep (section 3.1): measure recall@k for several (size, overlap) settings.

    # 1. extract + clean once, in the indexer image (it has tesseract for the OCR pages)
    docker run --rm -v $PWD/corpus/raw:/corpus:ro -v /tmp/out:/out rahbar-indexer:dev \
        --dump-docs /out/docs.json
    # 2. chunk, embed, store and score every configuration
    python eval/sweep_chunking.py --docs /tmp/out/docs.json \
        --chroma localhost:8001 --ollama http://localhost:11434 \
        --configs 150:30 300:60 500:100 --fixed 100:0 --publish 300:60

Each configuration goes into its own collection (sweep_<size>_<overlap>), built by the
indexer's own chunk/embed/store code, and is scored through the api's own
ChromaRetriever, so the numbers are the ones the service would get. --publish also writes
the chosen configuration to the live collection, reusing its vectors.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "indexer"), str(ROOT / "api"), str(ROOT / "eval")]
os.chdir(ROOT / "eval")     # chromadb's settings read a .env in the working directory

from app.config import Settings  # noqa: E402
from app.retrieval.chroma import ChromaRetriever, wants_older_version  # noqa: E402
from pipeline.config import get_settings as indexer_settings  # noqa: E402
from pipeline.embed import embed  # noqa: E402
from pipeline.run import chunk_all, load_documents  # noqa: E402
from pipeline.store import store  # noqa: E402
from run_eval import load_golden, recall_at_k  # noqa: E402


def words(text: str) -> int:
    return len(text.split())


def score(collection: str, host: str, port: int, ollama: str, golden: list[dict],
          hybrid: bool, k: int = 5) -> dict:
    retriever = ChromaRetriever(Settings(retriever="chroma", chroma_host=host, chroma_port=port,
                                         collection=collection, ollama_url=ollama, hybrid=hybrid))
    rows = []
    for q in golden:
        if not q.get("relevant"):
            continue
        hits = retriever.retrieve(q["question"], k=k,
                                  include_superseded=wants_older_version(q["question"]))
        refs = [{"source": p.source, "section": p.section} for p in hits]
        rows.append({"id": q["id"], "type": q.get("type", "ordinary"),
                     "r3": recall_at_k(q["relevant"], refs, 3),
                     "r5": recall_at_k(q["relevant"], refs, 5)})
    by_type: dict[str, list[float]] = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r["r5"])
    return {
        "recall@3": round(sum(r["r3"] for r in rows) / len(rows), 4),
        "recall@5": round(sum(r["r5"] for r in rows) / len(rows), 4),
        "recall@5_by_type": {t: round(sum(v) / len(v), 4) for t, v in sorted(by_type.items())},
        "per_question": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", required=True)
    ap.add_argument("--chroma", default="localhost:8000")
    ap.add_argument("--ollama", default="http://localhost:11434")
    ap.add_argument("--golden", default=str(ROOT / "eval" / "golden.yaml"))
    ap.add_argument("--configs", nargs="+", default=["150:30", "300:60", "500:100"])
    ap.add_argument("--fixed", nargs="*", default=["100:0"], help="fixed-size baselines")
    ap.add_argument("--publish", help="size:overlap to also write to the live collection")
    ap.add_argument("--out", default=str(ROOT / "eval" / "sweep_results.json"))
    args = ap.parse_args()

    host, port = args.chroma.split(":")
    docs = load_documents(args.docs)
    golden = load_golden(Path(args.golden))
    original = sum(words(p.text) for d in docs for p in d.pages)
    base = replace(indexer_settings(), chroma_host=host, chroma_port=int(port),
                   ollama_url=args.ollama)
    runs = [(c, "structured") for c in args.configs] + [(c, "fixed") for c in args.fixed]
    results = []
    for spec, strategy in runs:
        size, overlap = (int(x) for x in spec.split(":"))
        os.environ["CHUNK_STRATEGY"] = strategy
        name = f"sweep_{strategy}_{size}_{overlap}"
        settings = replace(base, chunk_size=size, chunk_overlap=overlap, collection=name)
        started = time.perf_counter()
        chunks = chunk_all(docs, settings)
        vectors = embed([c.text for c in chunks], settings)
        store(chunks, vectors, settings)
        stored = sum(words(c.text) for c in chunks)
        row = {"strategy": strategy, "size": size, "overlap": overlap, "chunks": len(chunks),
               "stored_words": stored, "original_words": original,
               "amplification": round(stored / original, 3),
               "index_seconds": round(time.perf_counter() - started, 1),
               "hybrid": score(name, host, int(port), args.ollama, golden, hybrid=True),
               "dense_only": score(name, host, int(port), args.ollama, golden, hybrid=False)}
        results.append(row)
        print(json.dumps({k: v for k, v in row.items() if k not in ("hybrid", "dense_only")}
                         | {"r5_hybrid": row["hybrid"]["recall@5"],
                            "r5_dense": row["dense_only"]["recall@5"]}), flush=True)
        if args.publish == spec and strategy == "structured":
            os.environ["CHUNK_STRATEGY"] = "structured"
            store(chunks, vectors, replace(settings, collection=base.collection))
            print(f"published {spec} to collection '{base.collection}'", flush=True)

    Path(args.out).write_text(json.dumps({"original_words": original, "runs": results},
                                         indent=2), encoding="utf-8")
    print("\n| Strategy | Size (words) | Overlap | Chunks | Amplification | recall@3 | recall@5 "
          "| recall@5 dense only |")
    print("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in results:
        print(f"| {r['strategy']} | {r['size']} | {r['overlap']} | {r['chunks']} | "
              f"{r['amplification']:.2f} | {r['hybrid']['recall@3']:.3f} | "
              f"{r['hybrid']['recall@5']:.3f} | {r['dense_only']['recall@5']:.3f} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
