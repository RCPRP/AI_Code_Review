"""Fast tests, no model calls. From the repo root:
    uv run pytest -q
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from review_agent.context_engine import chunk_repository  # noqa: E402
from review_agent.evaluation import matches, metrics  # noqa: E402
from review_agent.reviewer import merge_findings  # noqa: E402
from review_agent.sample_prs import SAMPLE_PRS, load_pr  # noqa: E402
from review_agent.schema import Finding  # noqa: E402
from review_agent.toy_repository import get_toy_repository  # noqa: E402
from review_agent.tools import make_git_tools  # noqa: E402


def _f(**kw):
    base = dict(severity="high", category="security", file="app/x.py", line=10, title="SQL injection in search",
                explanation="x", suggestion="y", confidence=0.8)
    return Finding(**{**base, **kw})


@pytest.fixture(scope="module")
def git_tools():
    return make_git_tools(load_pr("pr_002_sql_injection")["repo"])


def test_git_diff_rejects_option_injection(git_tools):
    assert "Rejected" in git_tools[1].invoke({"base": "--output=/tmp/pwn", "head": "HEAD"})


def test_git_diff_rejects_path_traversal(git_tools):
    assert "Rejected" in git_tools[1].invoke({"base": "main", "head": "feature", "path": "../../etc/passwd"})


def test_git_tools_work_on_valid_refs(git_tools):
    changed, diff = git_tools
    assert "search_users" in diff.invoke({"base": "main", "head": "feature"})
    assert "app/routes/users.py" in changed.invoke({"base": "main", "head": "feature"})


def test_every_sample_pr_builds_and_has_a_diff():
    assert len(SAMPLE_PRS) == 15
    for p in SAMPLE_PRS:
        pr = load_pr(p["id"])
        assert pr["diff"].strip(), p["id"]
        assert len(pr["expected_issues"]) == 2
        for e in pr["expected_issues"]:
            assert e["file"] in pr["diff"], (p["id"], e["file"])


def test_chunking_splits_functions_and_skips_secrets():
    chunks = chunk_repository(get_toy_repository())
    names = {(c.type, c.name) for c in chunks}
    assert ("function", "get_current_user") in names
    assert ("function", "safe_join") in names
    assert not any(".env" in c.file_path for c in chunks)
    assert any(c.type == "doc" for c in chunks)


def test_matching_needs_file_and_keyword():
    exp = {"file": "app/x.py", "keywords": ["injection"]}
    assert matches(_f(file="/repo/app/x.py"), exp)
    assert not matches(_f(file="app/y.py"), exp)
    assert not matches(_f(title="naming"), exp)


def test_merge_keeps_agreed_and_serious_unique_findings():
    sec = [_f(), _f(title="Weak hash", line=40, severity="low")]
    pat = [_f(title="Injection: query not parameterised", line=11, category="security", severity="medium"),
           _f(title="Missing rate limit decorator", line=80, severity="medium")]
    merged = merge_findings(sec, pat)
    titles = [f.title for f in merged]
    assert "SQL injection in search" in titles               # agreed, keeps the higher severity
    assert "Missing rate limit decorator" in titles          # unique, medium
    assert "Weak hash" not in titles                         # unique, low -> dropped
    assert len(merged) == 2


def test_metrics_precision_recall():
    rows = [{"expected": 2, "caught": 1, "findings": 4, "true_positive_findings": 1, "latency_s": 1, "error": None}]
    m = metrics(rows)
    assert m["precision"] == 0.25 and m["recall"] == 0.5


def test_service_rejects_repos_outside_allowed_roots(monkeypatch, tmp_path):
    monkeypatch.setenv("REVIEW_ALLOWED_ROOTS", str(tmp_path))
    import importlib
    import review_agent.service as service
    importlib.reload(service)
    from fastapi.testclient import TestClient
    client = TestClient(service.app)
    assert client.get("/healthz").json()["ok"]
    r = client.post("/sessions", json={"repo_path": "/etc"})
    assert r.status_code == 403
    assert client.post("/sessions/nope/messages", json={"message": "hi"}).status_code == 404


def test_ui_replays_recordings_when_model_unavailable(monkeypatch, tmp_path):
    import importlib
    import json

    import review_agent.recordings as recordings
    monkeypatch.setenv("REVIEW_MODE", "replay")
    monkeypatch.setattr(recordings, "RECORDINGS_DIR", tmp_path)
    report = {"verdict": "request_changes", "summary": "s", "findings": [
        {"severity": "high", "category": "security", "file": "app/x.py", "line": 3, "title": "t", "explanation": "e",
         "suggestion": "f", "confidence": 0.9}]}
    (tmp_path / "pr_001_auth_bypass.json").write_text(json.dumps({
        "pr_id": "pr_001_auth_bypass", "model": "test-model", "recorded_at": "now",
        "start": {"reply": "hello", "report": report, "tool_calls": ["git_diff", "task"], "latency_s": 1},
        "followups": [{"question": "Which issue should I fix first?", "reply": "fix t", "report": None}]}))
    import review_agent.service as service
    importlib.reload(service)
    import review_agent.ui as ui
    importlib.reload(ui)
    from fastapi.testclient import TestClient
    c = TestClient(ui.app)
    assert "Code Review Agent" in c.get("/").text
    assert len(c.get("/api/prs").json()) == 15
    r = c.post("/api/prs/pr_001_auth_bypass/review").json()
    assert r["recorded"] and r["reply"] == "hello" and r["recorded_model"] == "test-model"
    a = c.post(f"/api/sessions/{r['session_id']}/messages", json={"message": "which issue should I fix first"}).json()
    assert a["reply"] == "fix t" and a["recorded"]
    b = c.post(f"/api/sessions/{r['session_id']}/messages", json={"message": "something else"}).json()
    assert "recorded run" in b["reply"]
    assert c.post("/api/prs/pr_002_sql_injection/review").status_code == 502  # no recording, no live in replay mode
