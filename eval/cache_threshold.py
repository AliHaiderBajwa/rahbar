#!/usr/bin/env python3
"""Choose CACHE_THRESHOLD with data (section 3.3).

The lecture's rule: the lowest threshold with zero false hits on our test questions.
A false hit is a pair of questions that need DIFFERENT answers but whose embeddings are
similar enough that the cache would serve one's answer to the other. A true hit is a
paraphrase pair that should share an answer.

    python eval/cache_threshold.py --ollama http://localhost:11434

Writes eval/cache_threshold_results.json and prints the table used in PIPELINE.md.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]

# (a, b): paraphrases. The cached answer to a is a correct answer to b.
SAME = [
    ("What CGPA do I need to avoid academic warning?",
     "What is the minimum CGPA to stay off academic warning?"),
    ("How many courses can I repeat in summer?",
     "What is the maximum number of courses I can repeat in the summer semester?"),
    ("Is the rechecking fee refunded?",
     "Do I get my rechecking fee back?"),
    ("What is the admission fee?",
     "How much is the admission fee?"),
    ("Can I use ChatGPT in lab courses?",
     "Is ChatGPT allowed in labs?"),
    ("What is the attendance requirement?",
     "What attendance is required in lectures?"),
]

# (a, b): different questions. Serving a's answer to b would be wrong.
DIFFERENT = [
    ("What CGPA do I need to avoid academic warning?",
     "What CGPA do I need to graduate?"),                         # 2.00 vs 2.00 + rules
    ("What is the deadline to withdraw from a course?",
     "What is the deadline to drop a course?"),                   # makeup week vs first weeks
    ("What is the deadline to withdraw from a course?",
     "What was the deadline to withdraw from a course under the 2023 rules?"),
    ("How many courses can I repeat in the summer semester?",
     "How many courses can I register for in the summer semester?"),
    ("What is the tuition fee per credit hour for BS?",
     "What is the tuition fee per credit hour for MS?"),
    ("Can I use ChatGPT in lab courses?",
     "Can I use ChatGPT in non-lab courses?"),
    ("What is the minimum attendance for undergraduate programs?",
     "What is the minimum attendance for graduate programs?"),
    ("What happens if I fail a course?",
     "What happens if I miss a final exam?"),
]

THRESHOLDS = [0.85, 0.88, 0.90, 0.92, 0.94, 0.95, 0.96, 0.97]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ollama", default="http://localhost:11434")
    ap.add_argument("--model", default="nomic-embed-text")
    ap.add_argument("--out", default=str(ROOT / "eval" / "cache_threshold_results.json"))
    args = ap.parse_args()

    texts = sorted({t for pair in SAME + DIFFERENT for t in pair})
    r = httpx.post(f"{args.ollama}/api/embed", timeout=120,
                   json={"model": args.model, "input": [f"search_query: {t}" for t in texts]})
    r.raise_for_status()
    vec = dict(zip(texts, r.json()["embeddings"]))
    same = [(a, b, round(cosine(vec[a], vec[b]), 4)) for a, b in SAME]
    diff = [(a, b, round(cosine(vec[a], vec[b]), 4)) for a, b in DIFFERENT]

    rows = []
    for t in THRESHOLDS:
        false_hits = [(a, b, s) for a, b, s in diff if s >= t]
        true_hits = sum(1 for _, _, s in same if s >= t)
        rows.append({"threshold": t, "false_hits": len(false_hits),
                     "true_hits": true_hits, "paraphrases": len(same),
                     "false_hit_examples": false_hits})
    safe = [row["threshold"] for row in rows if row["false_hits"] == 0]
    chosen = min(safe) if safe else None
    Path(args.out).write_text(json.dumps({"model": args.model, "same": same, "different": diff,
                                          "rows": rows, "chosen": chosen}, indent=2))

    print("| Threshold | False hits (of %d) | Paraphrase hits (of %d) |" % (len(diff), len(same)))
    print("| ---: | ---: | ---: |")
    for row in rows:
        print(f"| {row['threshold']:.2f} | {row['false_hits']} | {row['true_hits']} |")
    print("\nhighest-scoring different-answer pairs:")
    for a, b, s in sorted(diff, key=lambda x: -x[2])[:4]:
        print(f"  {s:.4f}  {a!r} vs {b!r}")
    print("lowest-scoring paraphrase pairs:")
    for a, b, s in sorted(same, key=lambda x: x[2])[:3]:
        print(f"  {s:.4f}  {a!r} vs {b!r}")
    print(f"\nlowest threshold with zero false hits: {chosen}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
