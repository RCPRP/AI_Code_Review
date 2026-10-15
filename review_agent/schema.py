"""Typed review output. The agent is forced to return a ReviewReport via response_format."""

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["critical", "high", "medium", "low", "nit"]
Category = Literal["correctness", "security", "tests", "performance", "maintainability", "style"]
SEVERITY_ORDER = {s: i for i, s in enumerate(["critical", "high", "medium", "low", "nit"])}


class Finding(BaseModel):
    severity: Severity
    category: Category
    file: str = Field(description="Repo-relative path, e.g. app/users.py")
    line: int | None = Field(None, description="Line number in the NEW version of the file, if known")
    title: str = Field(description="One-line summary of the problem")
    explanation: str = Field(description="Why this is a problem and what can go wrong")
    suggestion: str = Field(description="Concrete fix, ideally a code snippet")
    confidence: float = Field(ge=0, le=1, description="How sure you are this is a real problem")

    @property
    def path(self) -> str:
        """File path normalised to repo-relative form."""
        return self.file.lstrip("/").removeprefix("repo/")


class ReviewReport(BaseModel):
    verdict: Literal["approve", "comment", "request_changes"]
    summary: str = Field(description="2-4 sentences on what the change does and its overall quality")
    findings: list[Finding] = Field(default_factory=list)
