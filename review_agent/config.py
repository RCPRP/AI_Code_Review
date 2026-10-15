"""Runtime configuration, read from environment variables. No secrets live in code."""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

# The nearest .env above the working directory, else the .env two folders up (the LangChain course layout).
# .env values win over the shell, as in the course.
_env = find_dotenv(usecwd=True) or str(Path(__file__).resolve().parents[2] / ".env")
load_dotenv(_env, override=True)


def _default_model() -> str:
    """Gemini if a Google key is set, else Anthropic, else OpenAI. Override with REVIEW_MODEL."""
    if os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_CLOUD_PROJECT"):
        # flash-lite is the most reliable on the free tier. With a paid key, try REVIEW_MODEL=google_genai:gemini-3.8-flash.
        return "google_genai:gemini-3.1-flash-lite"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic:claude-sonnet-4-6"
    return "openai:gpt-4.1"


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


@dataclass(frozen=True)
class ReviewConfig:
    model: str = field(default_factory=lambda: os.getenv("REVIEW_MODEL") or _default_model())
    model_timeout_s: int = field(default_factory=lambda: _int("REVIEW_MODEL_TIMEOUT_S", 120))
    model_max_retries: int = field(default_factory=lambda: _int("REVIEW_MODEL_MAX_RETRIES", 3))
    max_diff_chars: int = field(default_factory=lambda: _int("REVIEW_MAX_DIFF_CHARS", 60_000))
    max_file_mb: int = field(default_factory=lambda: _int("REVIEW_MAX_FILE_MB", 1))
    git_timeout_s: int = field(default_factory=lambda: _int("REVIEW_GIT_TIMEOUT_S", 20))
    recursion_limit: int = field(default_factory=lambda: _int("REVIEW_RECURSION_LIMIT", 60))
    embedding_model: str = field(default_factory=lambda: os.getenv("REVIEW_EMBEDDING_MODEL", "models/gemini-embedding-001"))
    retrieval_k: int = field(default_factory=lambda: _int("REVIEW_RETRIEVAL_K", 15))
    # Gemini through Vertex AI with gcloud credentials, when there's no API key: set GOOGLE_CLOUD_PROJECT.
    gcp_project: str | None = field(default_factory=lambda: None if (os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"))
                                    else os.getenv("GOOGLE_CLOUD_PROJECT"))
    gcp_location: str = field(default_factory=lambda: os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"))
    eval_workers: int = field(default_factory=lambda: _int("REVIEW_EVAL_WORKERS", 4))


CONFIG = ReviewConfig()

logging.basicConfig(
    level=os.getenv("REVIEW_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
for _noisy in ("httpx", "httpcore", "anthropic", "openai", "google_genai"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("code_review")
