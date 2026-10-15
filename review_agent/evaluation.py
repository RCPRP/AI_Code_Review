"""Deterministic scoring: a finding matches an expected issue when it names the same file and one of its keywords
(matched at the start of a word). Each finding can satisfy at most one expected issue.

precision = findings that match some expected issue / all findings
recall    = expected issues caught / all expected issues
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from .config import CONFIG, log
from .schema import Finding, ReviewReport


def matches(finding: Finding, expected: dict) -> bool:
    if not finding.path.endswith(expected["file"]):
        return False
    text = f"{finding.title} {finding.explanation} {finding.suggestion}".lower()
    return any(re.search(r"(?<![a-z0-9_])" + re.escape(k.lower()), text) for k in expected["keywords"])


def assign(findings: list[Finding], expected_issues: list[dict]) -> dict[str, Finding]:
    """One-to-one: each expected issue gets the first unused matching finding. Rarer matches are assigned first."""
    used: set[int] = set()
    out: dict[str, Finding] = {}
    candidates = {e["id"]: [i for i, f in enumerate(findings) if matches(f, e)] for e in expected_issues}
    for exp in sorted(expected_issues, key=lambda e: len(candidates[e["id"]])):
        i = next((i for i in candidates[exp["id"]] if i not in used), None)
        if i is not None:
            used.add(i)
            out[exp["id"]] = findings[i]
    return out


def match_report(report: ReviewReport, expected_issues: list[dict]) -> list[dict]:
    """One row per expected issue: caught or not, and by which finding."""
    hits = assign(report.findings, expected_issues)
    return [{"expected": e["description"], "caught": e["id"] in hits,
             "by_finding": hits[e["id"]].title if e["id"] in hits else "",
             "severity": hits[e["id"]].severity if e["id"] in hits else ""} for e in expected_issues]


def score(pr: dict, report: ReviewReport) -> dict:
    expected = pr["expected_issues"]
    hits = assign(report.findings, expected)
    caught_ids = [e["id"] for e in expected if e["id"] in hits]
    true_pos = sum(any(matches(f, e) for e in expected) for f in report.findings)
    return {"expected": len(expected), "caught": len(caught_ids), "caught_ids": caught_ids,
            "findings": len(report.findings), "true_positive_findings": true_pos}


def metrics(rows: list[dict]) -> dict:
    ok = [r for r in rows if r.get("error") is None]
    findings = sum(r["findings"] for r in ok)
    expected = sum(r["expected"] for r in rows)
    p = sum(r["true_positive_findings"] for r in ok) / findings if findings else 0.0
    rc = sum(r["caught"] for r in ok) / expected if expected else 0.0
    f1 = 2 * p * rc / (p + rc) if p + rc else 0.0
    lat = [r["latency_s"] for r in ok]
    ctx = [r.get("context_chars", 0) for r in ok]
    return {"precision": round(p, 3), "recall": round(rc, 3), "f1": round(f1, 3),
            "errors": len(rows) - len(ok), "avg_latency_s": round(sum(lat) / len(lat), 1) if lat else None,
            "avg_context_chars": int(sum(ctx) / len(ctx)) if ctx else 0}


def run_eval(prs: list[dict], approaches: dict[str, Callable[[dict], dict]], workers: int | None = None) -> list[dict]:
    """Run every PR through every approach in parallel. An approach is pr -> {"report", "latency_s", ...}.
    A failed run is recorded as an error row and the eval carries on."""
    jobs = [(pr, name, fn) for pr in prs for name, fn in approaches.items()]
    rows = []

    def one(pr, name, fn):
        try:
            out = fn(pr)
            return {"pr": pr["id"], "approach": name, **score(pr, out["report"]), "latency_s": out["latency_s"],
                    "context_chars": out.get("context_chars", 0), "report": out["report"], "error": None}
        except Exception as e:  # noqa: BLE001 - one bad run must not sink the eval
            log.exception("eval failed pr=%s approach=%s", pr["id"], name)
            return {"pr": pr["id"], "approach": name, "expected": len(pr["expected_issues"]), "caught": 0, "caught_ids": [],
                    "findings": 0, "true_positive_findings": 0, "latency_s": None, "report": None, "error": repr(e)[:300]}

    with ThreadPoolExecutor(max_workers=workers or CONFIG.eval_workers) as pool:
        futures = [pool.submit(one, *j) for j in jobs]
        for i, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            rows.append(r)
            print(f"  {i:3d}/{len(jobs)}  {r['pr']:34} {r['approach']:14} caught {r['caught']}/{r['expected']}"
                  + (f"  ERROR {r['error'][:80]}" if r["error"] else ""))
    order = {p["id"]: i for i, p in enumerate(prs)}
    return sorted(rows, key=lambda r: (order[r["pr"]], list(approaches).index(r["approach"])))


def metrics_by_approach(rows: list[dict]) -> dict[str, dict]:
    names = list(dict.fromkeys(r["approach"] for r in rows))
    return {n: metrics([r for r in rows if r["approach"] == n]) for n in names}
