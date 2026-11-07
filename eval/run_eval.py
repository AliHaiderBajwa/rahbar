#!/usr/bin/env python3
"""Score a running RAHBAR against a golden set, and gate CI on the result (section 5).

    python eval/run_eval.py --api http://<ingress>                 # your corpus
    python eval/run_eval.py --api ... --golden eval/fixtures/golden.fixture.yaml
    python eval/run_eval.py --api ... --update-baseline            # after a deliberate improvement

Exit code 0 when every gate passes, 1 when any fails. CI relies on that exit code.

Given: talking to the API, reading the golden set, writing results.json, the exit
code. Yours: the four functions marked STARTER-TODO. They are the evaluation.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx
import yaml

HERE = Path(__file__).resolve().parent

# The gate from section 5.4. Change these only with a reason written in docs/PIPELINE.md.
MAX_RECALL5_DROP = 0.05        # recall@5 may fall at most 5 points below the baseline
MIN_CITATION_VALIDITY = 0.95   # at least 95% of citations must match retrieved passages
REQUIRED_ABSTENTION = 1.0      # every must-abstain question must be declined


def load_golden(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    questions = data.get("questions") or []
    if not questions:
        sys.exit(f"no questions in {path}")
    return questions


def ask(client: httpx.Client, api: str, question: str) -> dict:
    """POST one question and return the AskResponse as a dict (see api/app/schemas.py)."""
    for attempt in range(3):   # a pod being replaced mid-run answers 502/503 once
        response = client.post(f"{api.rstrip('/')}/api/ask", json={"question": question})
        if response.status_code < 500:
            break
        time.sleep(2 * (attempt + 1))
    response.raise_for_status()
    return response.json()


def _split_section(section: str) -> tuple[str, str]:
    """'5.4 Academic Warning' -> ('5.4', 'academic warning'); '2. Academic warning' ->
    ('2', 'academic warning'); 'Tuition Fee' -> ('', 'tuition fee')."""
    m = re.match(r"\s*(\d+(?:\.\d+)*)\.?\s*(.*)$", section)
    if m:
        return m.group(1), " ".join(m.group(2).lower().split())
    return "", " ".join(section.lower().split())


def section_matches(golden: str, retrieved: str) -> bool:
    """The matching rule (documented in docs/PIPELINE.md, 'Evaluation method'):
    - a golden clause number matches that clause or any sub-clause: '5.4' matches
      '5.4 Academic Warning/Probation' and '5.4.2 ...', but never '5.41';
    - if the golden section also has a title, the retrieved title must start with it
      ('6.1 Use in Lab Courses' does not match '6.1 Rules for Summer Semester');
    - an unnumbered golden section matches a retrieved section that starts with it."""
    g_num, g_title = _split_section(golden)
    r_num, r_title = _split_section(retrieved)
    if g_num:
        if not (r_num == g_num or r_num.startswith(g_num + ".")):
            return False
        return not g_title or r_title.startswith(g_title)
    return f"{r_num} {r_title}".strip().startswith(g_title) or r_title.startswith(g_title)


def _hit(rel: dict, ref: dict) -> bool:
    return ref.get("source") == rel.get("source") and \
        section_matches(str(rel.get("section", "")), str(ref.get("section", "")))


def recall_at_k(relevant: list[dict], retrieved: list[dict], k: int) -> float | None:
    """Fraction of the relevant (source, section) pairs found in the top k retrieved.
    Return None when the question has no relevant passages (must-abstain questions)."""
    if not relevant:
        return None
    top = retrieved[:k]
    found = sum(1 for rel in relevant if any(_hit(rel, ref) for ref in top))
    return found / len(relevant)


def citation_validity(response: dict) -> float | None:
    """Fraction of the response's citations that match a passage in response['retrieved'].
    Return None when there are no citations."""
    citations = response.get("citations") or []
    if not citations:
        return None
    retrieved = {(r.get("source"), r.get("section")) for r in response.get("retrieved") or []}
    valid = sum(1 for c in citations if (c.get("source"), c.get("section")) in retrieved)
    return valid / len(citations)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def summarise(rows: list[dict]) -> dict:
    """Overall recall@3, recall@5, citation validity and abstention correctness, plus
    recall@5 broken down by question type. This dict becomes results.json."""
    per_q, r3, r5 = [], [], []
    by_type: dict[str, dict[str, list[float]]] = {}
    cites_total = cites_valid = 0
    abstain_ok, abstain_n, answered_n, answerable_n, expect_hits, expect_n = 0, 0, 0, 0, 0, 0
    for row in rows:
        resp = row["response"]
        retrieved = resp.get("retrieved") or []
        q3 = recall_at_k(row["relevant"], retrieved, 3)
        q5 = recall_at_k(row["relevant"], retrieved, 5)
        if q5 is not None and q3 is not None:
            r3.append(q3)
            r5.append(q5)
            t = by_type.setdefault(row["type"], {"r3": [], "r5": []})
            t["r3"].append(q3)
            t["r5"].append(q5)
        cites = resp.get("citations") or []
        valid_frac = citation_validity(resp)
        if valid_frac is not None:
            cites_total += len(cites)
            cites_valid += round(valid_frac * len(cites))
        answered = bool(resp.get("answered_from_context"))
        if row["must_abstain"]:
            abstain_n += 1
            abstain_ok += int(not answered)
        else:
            answerable_n += 1
            answered_n += int(answered)
        expect_ok = None
        if row.get("expect") and not row["must_abstain"]:
            expect_n += 1
            expect_ok = str(row["expect"]).lower() in str(resp.get("answer", "")).lower()
            expect_hits += int(expect_ok)
        per_q.append({
            "id": row["id"], "type": row["type"], "recall@3": q3, "recall@5": q5,
            "answered_from_context": answered, "must_abstain": row["must_abstain"],
            "citations": len(cites), "citation_validity": valid_frac,
            "expect_in_answer": expect_ok, "cached": bool(resp.get("cached", False)),
            "top_score": max((r.get("score", 0) for r in retrieved), default=None),
            "retrieved": [f"{r.get('source')} | {r.get('section')}" for r in retrieved[:5]],
            "seconds": row.get("seconds"),
        })
    return {
        "recall@3": _mean(r3),
        "recall@5": _mean(r5),
        "recall@5_by_type": {t: _mean(v["r5"]) for t, v in sorted(by_type.items())},
        "recall@3_by_type": {t: _mean(v["r3"]) for t, v in sorted(by_type.items())},
        "citation_validity": round(cites_valid / cites_total, 4) if cites_total else 1.0,
        "citations_checked": cites_total,
        "abstention_correctness": round(abstain_ok / abstain_n, 4) if abstain_n else None,
        "must_abstain_questions": abstain_n,
        "answer_rate_on_answerable": round(answered_n / answerable_n, 4) if answerable_n else None,
        "expect_in_answer_rate": round(expect_hits / expect_n, 4) if expect_n else None,
        "per_question": per_q,
    }


def gate(results: dict, baseline: dict | None) -> list[str]:
    """Return one message per failed gate (section 5.4); an empty list means pass."""
    failures = []
    r5 = results.get("recall@5")
    if baseline and baseline.get("recall@5") is not None and r5 is not None:
        floor = baseline["recall@5"] - MAX_RECALL5_DROP
        if r5 < floor - 1e-9:
            failures.append(f"recall@5 {r5:.3f} is more than {MAX_RECALL5_DROP:.0%} below the "
                            f"baseline {baseline['recall@5']:.3f} (floor {floor:.3f})")
    elif baseline is None:
        print("  (no baseline yet: the recall gate is skipped; run with --update-baseline)")
    abst = results.get("abstention_correctness")
    if abst is not None and abst < REQUIRED_ABSTENTION:
        wrong = [q["id"] for q in results.get("per_question", [])
                 if q["must_abstain"] and q["answered_from_context"]]
        failures.append(f"abstention correctness {abst:.0%}: must-abstain questions answered "
                        f"from context: {', '.join(wrong)}")
    cv = results.get("citation_validity")
    if cv is not None and cv < MIN_CITATION_VALIDITY:
        failures.append(f"citation validity {cv:.1%} is below {MIN_CITATION_VALIDITY:.0%}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate RAHBAR against a golden set.")
    parser.add_argument("--api", required=True, help="base URL, e.g. http://192.168.49.2")
    parser.add_argument("--host", help="Host header, when your Ingress rule names a host")
    parser.add_argument("--golden", type=Path, default=HERE / "golden.yaml")
    parser.add_argument("--baseline", type=Path, default=HERE / "baseline.json")
    parser.add_argument("--out", type=Path, default=HERE / "results.json")
    parser.add_argument("--update-baseline", action="store_true",
                        help="write these results as the new baseline")
    args = parser.parse_args()

    questions = load_golden(args.golden)
    rows = []
    headers = {"Host": args.host} if args.host else {}
    with httpx.Client(timeout=120.0, headers=headers) as client:
        for q in questions:
            started = time.perf_counter()
            response = ask(client, args.api, q["question"])
            rows.append({
                "id": q["id"], "type": q.get("type", "ordinary"),
                "must_abstain": bool(q.get("must_abstain", False)),
                "relevant": q.get("relevant") or [],
                "expect": q.get("expect"),
                "response": response,
                "seconds": round(time.perf_counter() - started, 2),
            })
            print(f"  {q['id']:<5} {rows[-1]['seconds']:>6.2f}s  {q['question'][:60]}")

    try:
        results = summarise(rows)
        baseline = json.loads(args.baseline.read_text()) if args.baseline.exists() else None
        failures = gate(results, baseline)
    except NotImplementedError as exc:
        print(f"\nnot implemented yet: {exc}")
        return 2

    results["golden"] = str(args.golden)
    results["questions"] = len(rows)
    args.out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nresults written to {args.out}")
    if args.update_baseline:
        args.baseline.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"baseline updated: {args.baseline}")

    if failures:
        print("\nGATE FAILED")
        for message in failures:
            print(f"  - {message}")
        return 1
    print("\ngate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
