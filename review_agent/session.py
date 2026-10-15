"""The production reviewer: a conversational deep agent with full context.

Context comes from three places: retrieved code chunks (context engine), the task description, and live
read-only access to the repo through tools. Specialist subagents do focused security and pattern passes.
Every turn shares a checkpointed thread, so follow-up questions remember the review.
Structured findings come from the `submit_review` tool call, so the agent can talk naturally and still
emit a typed ReviewReport.
"""

import time
import uuid
from pathlib import Path

from deepagents import create_deep_agent
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from .config import CONFIG, log
from .prompts import CONVERSATIONAL_PROMPT, PATTERN_AGENT_PROMPT, SECURITY_AGENT_PROMPT
from .reviewer import make_model
from .schema import ReviewReport
from .tools import PERMISSIONS, SAFE_REF, diff_text, make_backend, make_git_tools

_REPO_NOTE = ("\n\nThe repository is mounted read-only at /repo/. Use git_diff and read_file to read the change, "
              "related code and /repo/docs/CONVENTIONS.md before reporting. Return your findings as plain text.")


@tool(args_schema=ReviewReport)
def submit_review(verdict: str, summary: str, findings: list) -> str:
    """Record the structured review: verdict, summary and every finding. Call once per complete review."""
    return "Review recorded. Now reply to the author conversationally."


class ReviewSession:
    """A multi-turn review of base...head in a local git repo.

    >>> s = ReviewSession(repo, task_context="...", retriever=retriever)
    >>> reply = s.start()          # runs the review, returns a conversational summary
    >>> s.report                   # the typed ReviewReport
    >>> s.ask("why is the first finding a problem?")
    """

    def __init__(self, repo: Path, base: str = "main", head: str = "feature", *, title: str = "",
                 task_context: str | None = None, retriever=None, checkpointer=None, session_id: str | None = None,
                 model: str | None = None):
        if not (SAFE_REF.match(base) and SAFE_REF.match(head)):
            raise ValueError("invalid git ref")
        self.repo = Path(repo).resolve()
        if not (self.repo / ".git").exists():
            raise ValueError(f"{self.repo} is not a git repository")
        self.base, self.head, self.title, self.task_context = base, head, title, task_context
        self.retriever = retriever
        self.model = model or CONFIG.model
        self.session_id = session_id or uuid.uuid4().hex
        self.report: ReviewReport | None = None
        self.turns: list[dict] = []

        git_tools = make_git_tools(self.repo)
        subagents = [
            {"name": "security-reviewer", "description": "Focused security pass (OWASP checklist). Give it the refs and files.",
             "system_prompt": SECURITY_AGENT_PROMPT + _REPO_NOTE, "tools": git_tools, "permissions": PERMISSIONS},
            {"name": "pattern-reviewer", "description": "Checks the diff against team conventions and existing code patterns.",
             "system_prompt": PATTERN_AGENT_PROMPT + _REPO_NOTE, "tools": git_tools, "permissions": PERMISSIONS},
        ]
        # InMemorySaver keeps one process's sessions. In production pass a durable saver (SQLite/Postgres).
        self._agent = create_deep_agent(
            model=make_model(self.model), name="code_reviewer", tools=[*git_tools, submit_review],
            system_prompt=CONVERSATIONAL_PROMPT, subagents=subagents, backend=make_backend(self.repo),
            permissions=PERMISSIONS, checkpointer=checkpointer or InMemorySaver(),
        )

    def _config(self, turn: str) -> dict:
        return {
            "configurable": {"thread_id": self.session_id},
            "recursion_limit": CONFIG.recursion_limit,
            "run_name": f"review_session:{turn}",
            "tags": ["code-review", "session"],
            "metadata": {"session_id": self.session_id, "repo": self.repo.name, "base": self.base, "head": self.head,
                         "model": self.model},
        }

    def _run(self, content: str, turn: str) -> dict:
        t0 = time.perf_counter()
        before = len(self._agent.get_state({"configurable": {"thread_id": self.session_id}}).values.get("messages", []))
        result = self._agent.invoke({"messages": [{"role": "user", "content": content}]}, config=self._config(turn))
        new = result["messages"][before:]
        updated = None
        for m in new:
            for tc in getattr(m, "tool_calls", None) or []:
                if tc["name"] == "submit_review":
                    updated = ReviewReport.model_validate(tc["args"])
        if updated:
            self.report = updated
        reply = next((m.text for m in reversed(new) if isinstance(m, AIMessage) and m.text.strip()), "")
        tools = [tc["name"] for m in new for tc in (getattr(m, "tool_calls", None) or [])]
        out = {"reply": reply, "report_updated": updated is not None, "tool_calls": tools,
               "latency_s": round(time.perf_counter() - t0, 1)}
        self.turns.append({"turn": turn, **out})
        log.info("session=%s turn=%s %.1fs tools=%s report_updated=%s", self.session_id[:8], turn,
                 out["latency_s"], tools, out["report_updated"])
        return out

    def start(self) -> dict:
        diff = diff_text(self.repo, self.base, self.head)
        parts = [f"Please review this pull request: `{self.base}`...`{self.head}`" + (f" - {self.title}" if self.title else "")]
        if self.task_context:
            parts.append(f"## Task this PR is meant to accomplish\n{self.task_context}")
        if self.retriever is not None:
            ctx = self.retriever.retrieve_for_pr({"title": self.title, "task_context": self.task_context or "", "diff": diff},
                                                 n_results=CONFIG.retrieval_k)
            parts.append(f"## Related code retrieved from the codebase (paths are relative to /repo/)\n{ctx}")
        out = self._run("\n\n".join(parts), "start")
        if self.report is None:  # one nudge if the model forgot the structured output
            out = self._run("Please call submit_review with your complete findings now, then give me a one-line summary.", "submit_retry")
        if self.report is None:
            raise RuntimeError("Agent did not submit a structured review")
        return out

    def ask(self, question: str) -> dict:
        if self.report is None:
            raise RuntimeError("call start() first")
        return self._run(question, f"followup_{len(self.turns)}")
