"""Web UI for the conversational, context-aware reviewer, over the 15 mock PRs.

Run from the repo root:
    uv run uvicorn review_agent.ui:app --port 8000
then open http://localhost:8000

REVIEW_MODE:
  auto    (default) open the recorded review instantly when one exists, otherwise review live. Follow-up
          questions the recording covers are replayed; any other question is answered live by the model,
          using the PR's diff, retrieved code and the review. Replayed answers are labelled "recorded".
  live    always run a fresh live review.
  replay  never call the model; only replay recordings (see recordings.py to make them).
"""

import os
import threading
import time
from pathlib import Path

from .sample_prs import EVAL_ROOT, SAMPLE_PRS, load_pr

os.environ.setdefault("REVIEW_ALLOWED_ROOTS", str(EVAL_ROOT))

from fastapi import HTTPException  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from . import service  # noqa: E402
from .config import CONFIG, log  # noqa: E402
from .recordings import RECORDINGS_DIR, SUGGESTED_QUESTIONS, ReplaySession, load_recording  # noqa: E402
from .schema import ReviewReport  # noqa: E402

app = service.app
MODE = os.getenv("REVIEW_MODE", "auto").lower()
_HTML = Path(__file__).with_name("ui.html")
_retriever = None
_retriever_lock = threading.Lock()


def _get_retriever():
    global _retriever
    with _retriever_lock:
        if _retriever is None:
            from .context_engine import build_retriever
            from .toy_repository import get_toy_repository
            _retriever = build_retriever(get_toy_repository())
        return _retriever


class UITurn(BaseModel):
    session_id: str
    reply: str
    report: ReviewReport | None
    report_updated: bool
    recorded: bool = False
    recorded_model: str | None = None
    tool_calls: list[str] = []
    latency_s: float = 0


def _pr_or_404(pr_id: str) -> None:
    if not any(p["id"] == pr_id for p in SAMPLE_PRS):
        raise HTTPException(status_code=404, detail="unknown PR")


@app.get("/", response_class=HTMLResponse)
def index():
    return _HTML.read_text()


@app.get("/api/config")
def ui_config():
    recorded = [p["id"] for p in SAMPLE_PRS if load_recording(p["id"])]
    return {"mode": MODE, "model": CONFIG.model, "recorded_prs": recorded, "suggested_questions": SUGGESTED_QUESTIONS}


@app.get("/api/overview")
def overview():
    """Totals across the recorded context-aware reviews, plus the diff-only vs context-aware benchmark if present."""
    import json
    sev: dict[str, int] = {}
    prs = []
    for p in SAMPLE_PRS:
        rec = load_recording(p["id"])
        if not rec:
            continue
        rep = ReviewReport.model_validate(rec["start"]["report"])
        for f in rep.findings:
            sev[f.severity] = sev.get(f.severity, 0) + 1
        top = min(rep.findings, key=lambda f: ["critical", "high", "medium", "low", "nit"].index(f.severity), default=None)
        prs.append({"id": p["id"], "title": p["title"], "verdict": rep.verdict, "findings": len(rep.findings),
                    "top": {"severity": top.severity, "title": top.title} if top else None})
    bench_path = RECORDINGS_DIR / "_benchmark.json"
    bench = json.loads(bench_path.read_text()) if bench_path.exists() else None
    return {"recorded": prs, "severity": sev, "benchmark": bench}


@app.get("/api/prs")
def list_prs():
    return [{"id": p["id"], "title": p["title"], "task_context": p["task_context"], "files": sorted(p["changes"])}
            for p in SAMPLE_PRS]


@app.get("/api/prs/{pr_id}")
def get_pr(pr_id: str):
    _pr_or_404(pr_id)
    pr = load_pr(pr_id)
    return {"id": pr["id"], "title": pr["title"], "task_context": pr["task_context"], "diff": pr["diff"]}


def _start_live(pr_id: str):
    from .session import ReviewSession
    pr = load_pr(pr_id)
    session = ReviewSession(pr["repo"], title=pr["title"], task_context=pr["task_context"], retriever=_get_retriever())
    return session, session.start()


FOLLOWUP_PROMPT = """You are the senior code reviewer who just reviewed this pull request, now chatting with its author.
Answer like a helpful colleague: concise, specific, quoting code with file:line where useful and giving fixes as
code snippets. Base every answer on the diff, the retrieved codebase context and your review below. If something
isn't covered by them, say so instead of guessing."""


def _live_answer(pr_id: str):
    def answer(question: str, history: list[dict], report: ReviewReport) -> str:
        from .reviewer import make_model
        pr = load_pr(pr_id)
        ctx = _get_retriever().retrieve_for_pr(pr, n_results=CONFIG.retrieval_k)
        convo = "\n\n".join(f"Author: {h['q']}\nYou: {h['a']}" for h in history if h.get("q"))
        msg = (f"## PR: {pr['title']}\nTask: {pr['task_context']}\n\n## Diff\n```diff\n{pr['diff']}\n```\n\n"
               f"## Retrieved codebase context\n{ctx}\n\n## Your review\n{report.model_dump_json(indent=1)}\n\n"
               f"## Conversation so far\n{convo or '(none)'}\n\n## Author's new question\n{question}")
        res = make_model().invoke([("system", FOLLOWUP_PROMPT), ("user", msg)],
                                  config={"run_name": f"ui_followup:{pr_id}", "tags": ["code-review", "ui"]})
        return res.text
    return answer


def _start_replay(pr_id: str):
    rec = load_recording(pr_id)
    if rec is None:
        return None, None
    session = ReplaySession(rec, live_answer=None if MODE == "replay" else _live_answer(pr_id))
    return session, session.start()


@app.post("/api/prs/{pr_id}/review", response_model=UITurn)
def review_pr(pr_id: str):
    _pr_or_404(pr_id)
    session = out = None
    if MODE != "live":
        session, out = _start_replay(pr_id)
    if session is None and MODE != "replay":
        try:
            session, out = _start_live(pr_id)
        except Exception as e:  # noqa: BLE001
            log.warning("live review failed for %s: %r", pr_id, e)
    if session is None:
        raise HTTPException(status_code=502, detail="The model is busy or unavailable and this PR has no recorded run. Try again in a minute.")
    with service._guard:
        service._sessions[session.session_id] = (time.time(), session, threading.Lock())
        service._evict()
    recorded = isinstance(session, ReplaySession)
    return UITurn(session_id=session.session_id, reply=out["reply"], report=session.report, report_updated=True,
                  recorded=recorded, recorded_model=session.rec["model"] if recorded else None,
                  tool_calls=out.get("tool_calls", []), latency_s=out.get("latency_s", 0))


class UIMessage(BaseModel):
    message: str


@app.post("/api/sessions/{session_id}/messages", response_model=UITurn)
def ui_message(session_id: str, req: UIMessage):
    resp = service.send_message(session_id, service.MessageRequest(message=req.message))
    session, _ = service._get(session_id)
    last = session.turns[-1] if session.turns else {}
    recorded = isinstance(session, ReplaySession) and last.get("recorded", True)
    return UITurn(**resp.model_dump(), recorded=recorded, recorded_model=session.rec["model"] if recorded else None,
                  tool_calls=last.get("tool_calls", []), latency_s=last.get("latency_s", 0))
