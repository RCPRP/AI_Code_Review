"""HTTP API around ReviewSession.

Run from the repo root:
    REVIEW_ALLOWED_ROOTS=/path/to/repos uv run uvicorn review_agent.service:app --port 8000

POST /sessions                    start a review   -> session_id, reply, report
POST /sessions/{id}/messages      ask a follow-up  -> reply, report (if it changed)
GET  /sessions/{id}               current report and turn history
GET  /healthz
"""

import os
import threading
import time
from collections import OrderedDict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .config import CONFIG, log
from .schema import ReviewReport
from .session import ReviewSession

# Only repos under these roots can be reviewed. The API must not read arbitrary paths on the host.
ALLOWED_ROOTS = [Path(p).resolve() for p in os.getenv("REVIEW_ALLOWED_ROOTS", str(Path.cwd())).split(os.pathsep) if p]
MAX_SESSIONS = int(os.getenv("REVIEW_MAX_SESSIONS", "100"))
SESSION_TTL_S = int(os.getenv("REVIEW_SESSION_TTL_S", "3600"))

app = FastAPI(title="Code review agent", version="1.0")
_sessions: "OrderedDict[str, tuple[float, ReviewSession, threading.Lock]]" = OrderedDict()
_guard = threading.Lock()


class StartRequest(BaseModel):
    repo_path: str
    base: str = Field("main", max_length=100)
    head: str = Field("HEAD", max_length=100)
    title: str = Field("", max_length=300)
    task_context: str | None = Field(None, max_length=5000)


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class TurnResponse(BaseModel):
    session_id: str
    reply: str
    report: ReviewReport | None
    report_updated: bool


def _check_repo(path: str) -> Path:
    repo = Path(path).expanduser().resolve()
    if not any(repo == root or root in repo.parents for root in ALLOWED_ROOTS):
        raise HTTPException(status_code=403, detail="repo_path is outside REVIEW_ALLOWED_ROOTS")
    if not (repo / ".git").exists():
        raise HTTPException(status_code=400, detail="repo_path is not a git repository")
    return repo


def _evict() -> None:
    now = time.time()
    for sid in [s for s, (t, _, _) in _sessions.items() if now - t > SESSION_TTL_S]:
        _sessions.pop(sid, None)
    while len(_sessions) > MAX_SESSIONS:
        _sessions.popitem(last=False)


def _get(session_id: str) -> tuple[ReviewSession, threading.Lock]:
    with _guard:
        _evict()
        if session_id not in _sessions:
            raise HTTPException(status_code=404, detail="session not found or expired")
        _, s, lock = _sessions.pop(session_id)
        _sessions[session_id] = (time.time(), s, lock)
        return s, lock


@app.get("/healthz")
def healthz():
    return {"ok": True, "model": CONFIG.model, "sessions": len(_sessions)}


@app.post("/sessions", response_model=TurnResponse)
def start_session(req: StartRequest):
    repo = _check_repo(req.repo_path)
    try:
        session = ReviewSession(repo, req.base, req.head, title=req.title, task_context=req.task_context)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        out = session.start()
    except Exception:
        log.exception("review failed")
        raise HTTPException(status_code=502, detail="review failed") from None
    with _guard:
        _sessions[session.session_id] = (time.time(), session, threading.Lock())
        _evict()
    return TurnResponse(session_id=session.session_id, reply=out["reply"], report=session.report, report_updated=True)


@app.post("/sessions/{session_id}/messages", response_model=TurnResponse)
def send_message(session_id: str, req: MessageRequest):
    session, lock = _get(session_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="this session is already answering a message")
    try:
        out = session.ask(req.message)
    except Exception:
        log.exception("follow-up failed")
        raise HTTPException(status_code=502, detail="follow-up failed") from None
    finally:
        lock.release()
    return TurnResponse(session_id=session_id, reply=out["reply"], report=session.report, report_updated=out["report_updated"])


@app.get("/sessions/{session_id}")
def get_session(session_id: str):
    session, _ = _get(session_id)
    return {"session_id": session_id, "report": session.report, "turns": session.turns}
