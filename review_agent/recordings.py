"""Record real review sessions and replay them, so a live demo still works with no key, no credit or no network.

Record (makes real model calls):
    uv run python -m review_agent.recordings [pr_id ...]

Recordings are saved to code_review_agent/recordings/<pr_id>.json and replayed by the UI.

Benchmark for the UI overview (diff-only vs context-aware, real model calls):
    uv run python -m review_agent.recordings --benchmark
"""

import json
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .config import CONFIG, log
from .schema import ReviewReport

RECORDINGS_DIR = Path(__file__).resolve().parents[1] / "recordings"

# The UI offers these as one-click questions, so every recording answers them.
SUGGESTED_QUESTIONS = [
    "Which issue should I fix first?",
    "Show me the fixed code",
    "Is any of this a false alarm?",
]


def _norm(q: str) -> str:
    return " ".join(q.lower().split()).rstrip("?.! ")


def load_recording(pr_id: str) -> dict | None:
    path = RECORDINGS_DIR / f"{pr_id}.json"
    return json.loads(path.read_text()) if path.exists() else None


class ReplaySession:
    """Same interface as ReviewSession. Plays back a recorded real session."""

    recorded = True

    def __init__(self, recording: dict, live_answer=None):
        """live_answer(question, history, report) -> str answers questions the recording doesn't cover."""
        self.rec, self.session_id, self.turns = recording, uuid.uuid4().hex, []
        self._live = live_answer
        self.report: ReviewReport | None = None
        self._answers = {_norm(f["question"]): f for f in recording.get("followups", [])}

    def start(self) -> dict:
        s = self.rec["start"]
        self.report = ReviewReport.model_validate(s["report"])
        self.turns.append({"turn": "start", "reply": s["reply"], "recorded": True})
        return {"reply": s["reply"], "report_updated": True, "tool_calls": s.get("tool_calls", []),
                "latency_s": s.get("latency_s", 0), "recorded": True}

    def ask(self, question: str) -> dict:
        hit = self._answers.get(_norm(question))
        recorded, t0 = True, time.perf_counter()
        if hit:
            if hit.get("report"):
                self.report = ReviewReport.model_validate(hit["report"])
            reply = hit["reply"]
        elif self._live is not None:
            history = [{"q": t.get("question"), "a": t["reply"]} for t in self.turns]
            try:
                reply, recorded = self._live(question, history, self.report), False
            except Exception as e:  # noqa: BLE001
                log.warning("live follow-up failed: %r", e)
                reply = ("I couldn't reach the model just now, so I can only replay the suggested questions for "
                         "this recorded review. Try again in a moment.")
        else:
            reply = ("This review is a recorded run, so I can only replay answers to the suggested questions. "
                     "With a working model key, I answer any question live.")
        self.turns.append({"turn": f"followup_{len(self.turns)}", "question": question, "reply": reply, "recorded": recorded})
        return {"reply": reply, "report_updated": bool(hit and hit.get("report")), "tool_calls": [],
                "latency_s": round(time.perf_counter() - t0, 1), "recorded": recorded}


def record_pr(pr_id: str, retriever=None, model: str | None = None) -> Path:
    from .sample_prs import load_pr
    from .session import ReviewSession

    pr = load_pr(pr_id)
    session = ReviewSession(pr["repo"], title=pr["title"], task_context=pr["task_context"], retriever=retriever, model=model)
    start = session.start()
    rec = {
        "pr_id": pr_id, "model": session.model, "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "start": {"reply": start["reply"], "report": session.report.model_dump(), "tool_calls": start["tool_calls"],
                  "latency_s": start["latency_s"]},
        "followups": [],
    }
    for q in SUGGESTED_QUESTIONS:
        out = session.ask(q)
        rec["followups"].append({"question": q, "reply": out["reply"],
                                 "report": session.report.model_dump() if out["report_updated"] else None})
    RECORDINGS_DIR.mkdir(exist_ok=True)
    path = RECORDINGS_DIR / f"{pr_id}.json"
    path.write_text(json.dumps(rec, indent=2))
    log.info("recorded %s -> %s", pr_id, path.name)
    return path


def main(pr_ids: list[str]) -> None:
    from .context_engine import build_retriever
    from .sample_prs import SAMPLE_PRS
    from .toy_repository import get_toy_repository

    ids = pr_ids or [p["id"] for p in SAMPLE_PRS if not (RECORDINGS_DIR / f"{p['id']}.json").exists()]
    retriever = build_retriever(get_toy_repository())
    # Free-tier quotas and "high demand" errors are common, so retry with backoff, then try the fallback model.
    models = [CONFIG.model] + [m for m in os.getenv("REVIEW_FALLBACK_MODELS", "").split(",") if m]
    t0 = time.perf_counter()

    def with_retries(pr_id: str) -> str:
        last = None
        for model in models:
            for attempt in range(3):
                try:
                    record_pr(pr_id, retriever, model)
                    return model
                except Exception as e:  # noqa: BLE001
                    last = e
                    log.warning("record %s with %s failed (attempt %d): %s", pr_id, model, attempt + 1, str(e)[:120])
                    time.sleep(30 * (attempt + 1))
        raise last

    failed = []
    with ThreadPoolExecutor(max_workers=CONFIG.eval_workers) as pool:
        futures = {pool.submit(with_retries, i): i for i in ids}
        for fut, pr_id in futures.items():
            try:
                print(f"✓ {pr_id} ({fut.result()})", flush=True)
            except Exception as e:  # noqa: BLE001
                failed.append(pr_id)
                print(f"✗ {pr_id}: {e!r}"[:300], flush=True)
    print(f"\nRecorded {len(ids) - len(failed)}/{len(ids)} in {time.perf_counter() - t0:.0f}s")
    sys.exit(1 if failed else 0)


def benchmark() -> Path:
    """Score diff-only vs context-aware review on all sample PRs and save the summary for the UI overview."""
    from .context_engine import build_retriever
    from .evaluation import metrics_by_approach, run_eval
    from .reviewer import review
    from .sample_prs import load_all_prs
    from .toy_repository import get_toy_repository

    retriever = build_retriever(get_toy_repository())
    prs = load_all_prs()
    rows = run_eval(prs, {
        "diff_only": lambda p: review(p),
        "context_aware": lambda p: review(p, context=retriever.retrieve_for_pr(p), task_context=p["task_context"], mode="selective"),
    })
    summary = {
        "model": CONFIG.model, "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "metrics": metrics_by_approach(rows),
        "per_pr": [{k: r[k] for k in ("pr", "approach", "expected", "caught", "findings", "error")} for r in rows],
        "issues": {p["id"]: [{"id": e["id"], "description": e["description"]} for e in p["expected_issues"]] for p in prs},
        "caught": {f"{r['pr']}|{r['approach']}": r["caught_ids"] for r in rows},
    }
    RECORDINGS_DIR.mkdir(exist_ok=True)
    path = RECORDINGS_DIR / "_benchmark.json"
    path.write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary["metrics"], indent=2))
    return path


if __name__ == "__main__":
    if sys.argv[1:] == ["--benchmark"]:
        benchmark()
    else:
        main(sys.argv[1:])
