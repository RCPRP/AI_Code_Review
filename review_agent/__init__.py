"""Production-grade code review agent built on deepagents."""

from .config import CONFIG
from .context_engine import ContextRetriever, build_retriever, chunk_repository, embed_chunks
from .evaluation import match_report, metrics, metrics_by_approach, run_eval
from .prompts import DEFAULT_REVIEW_PROMPT, PATTERN_AGENT_PROMPT, SECURITY_AGENT_PROMPT
from .reviewer import ensemble_review, merge_findings, review
from .sample_prs import load_all_prs, load_pr
from .schema import SEVERITY_ORDER, Finding, ReviewReport
from .session import ReviewSession
from .toy_repository import get_toy_repository

__all__ = [
    "CONFIG", "ContextRetriever", "DEFAULT_REVIEW_PROMPT", "Finding", "PATTERN_AGENT_PROMPT", "ReviewReport",
    "ReviewSession", "SECURITY_AGENT_PROMPT", "SEVERITY_ORDER", "build_retriever", "chunk_repository", "embed_chunks",
    "ensemble_review", "get_toy_repository", "load_all_prs", "load_pr", "match_report", "merge_findings", "metrics",
    "metrics_by_approach", "review", "run_eval",
]
