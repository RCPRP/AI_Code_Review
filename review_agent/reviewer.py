"""Single-pass reviewers (Lessons 4 to 6): diff-only, with context, and the specialist ensemble.

These are one structured model call each, which keeps the evals fast. The production agent, which reads the
repo itself and holds a conversation, is ReviewSession in session.py.
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor

from langchain.chat_models import init_chat_model

from .config import CONFIG, log
from .prompts import DEFAULT_REVIEW_PROMPT, PATTERN_AGENT_PROMPT, SECURITY_AGENT_PROMPT
from .schema import SEVERITY_ORDER, Finding, ReviewReport


def make_model(model: str | None = None):
    model = model or CONFIG.model
    kwargs = {}
    if model.startswith("google_genai:") and CONFIG.gcp_project:
        kwargs = {"vertexai": True, "project": CONFIG.gcp_project, "location": CONFIG.gcp_location}
    return init_chat_model(model, timeout=CONFIG.model_timeout_s, max_retries=CONFIG.model_max_retries, **kwargs)


def _user_message(pr: dict, context: str | None, task_context: str | None) -> str:
    diff = pr["diff"]
    if len(diff) > CONFIG.max_diff_chars:
        diff = diff[: CONFIG.max_diff_chars] + "\n[TRUNCATED]"
    parts = [f"PR Title: {pr['title']}"]
    if task_context:
        parts.append(f"## Task this PR is meant to accomplish\n{task_context}")
    parts.append(f"## Code changes\n```diff\n{diff}\n```")
    if context:
        parts.append(f"## Codebase context\n{context}")
    return "\n\n".join(parts)


def _structured_review(system_prompt: str, message: str, *, run_name: str, tags: list[str], metadata: dict) -> ReviewReport:
    llm = make_model().with_structured_output(ReviewReport)
    return llm.invoke(
        [("system", system_prompt), ("user", message)],
        config={"run_name": run_name, "tags": ["code-review", *tags], "metadata": {"model": CONFIG.model, **metadata}},
    )


def review(pr: dict, context: str | None = None, task_context: str | None = None, *, mode: str | None = None) -> dict:
    """Lesson 4/5 entry point. Diff-only when no context is passed. Returns {"report", "latency_s", "context_chars"}."""
    mode = mode or ("with_context" if (context or task_context) else "diff_only")
    t0 = time.perf_counter()
    report = _structured_review(
        DEFAULT_REVIEW_PROMPT, _user_message(pr, context, task_context),
        run_name=f"review:{pr['id']}:{mode}", tags=[mode], metadata={"pr": pr["id"], "mode": mode},
    )
    latency = round(time.perf_counter() - t0, 1)
    log.info("review pr=%s mode=%s %.1fs findings=%d", pr["id"], mode, latency, len(report.findings))
    return {"report": report, "latency_s": latency, "context_chars": len(context or "")}


# ---------- Lesson 6: specialist ensemble ----------

def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z_]{4,}", text.lower())}


def _same_issue(a: Finding, b: Finding) -> bool:
    if a.path != b.path:
        return False
    if a.line and b.line and abs(a.line - b.line) <= 3 and a.category == b.category:
        return True
    ta, tb = _tokens(a.title), _tokens(b.title)
    return bool(ta and tb) and len(ta & tb) / min(len(ta), len(tb)) >= 0.5


def merge_findings(security: list[Finding], pattern: list[Finding]) -> list[Finding]:
    """Keep findings both specialists agree on, add unique critical/high/medium ones from either, deduplicate."""
    merged: list[Finding] = []
    used_p: set[int] = set()
    for s in security:
        j = next((j for j, p in enumerate(pattern) if j not in used_p and _same_issue(s, p)), None)
        if j is not None:
            used_p.add(j)
            p = pattern[j]
            best = s if SEVERITY_ORDER[s.severity] <= SEVERITY_ORDER[p.severity] else p
            merged.append(best.model_copy(update={"confidence": max(s.confidence, p.confidence)}))
        elif s.severity in ("critical", "high", "medium"):
            merged.append(s)
    merged += [p for j, p in enumerate(pattern) if j not in used_p and p.severity in ("critical", "high", "medium")]
    out: list[Finding] = []
    for f in sorted(merged, key=lambda f: (SEVERITY_ORDER[f.severity], -f.confidence)):
        if not any(_same_issue(f, g) for g in out):
            out.append(f)
    return out


def ensemble_review(pr: dict, context: str, task_context: str | None = None) -> dict:
    """Security and pattern specialists run in parallel on the same context, then their findings are merged."""
    msg = _user_message(pr, context, task_context)
    meta = {"pr": pr["id"], "mode": "ensemble"}
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=2) as pool:
        sec = pool.submit(_structured_review, SECURITY_AGENT_PROMPT, msg, run_name=f"review:{pr['id']}:security", tags=["ensemble", "security"], metadata=meta)
        pat = pool.submit(_structured_review, PATTERN_AGENT_PROMPT, msg, run_name=f"review:{pr['id']}:pattern", tags=["ensemble", "pattern"], metadata=meta)
        sec_r, pat_r = sec.result(), pat.result()
    findings = merge_findings(sec_r.findings, pat_r.findings)
    verdict = "request_changes" if any(f.severity in ("critical", "high") for f in findings) else ("comment" if findings else "approve")
    report = ReviewReport(verdict=verdict, summary=f"Security: {sec_r.summary}\nPatterns: {pat_r.summary}", findings=findings)
    latency = round(time.perf_counter() - t0, 1)
    log.info("ensemble pr=%s %.1fs security=%d pattern=%d merged=%d", pr["id"], latency,
             len(sec_r.findings), len(pat_r.findings), len(findings))
    return {"report": report, "latency_s": latency, "context_chars": len(context),
            "security": sec_r, "pattern": pat_r}
