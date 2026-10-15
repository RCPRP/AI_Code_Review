"""Read-only access to the repo under review: two git tools plus a sandboxed filesystem."""

import re
import subprocess
from pathlib import Path

from deepagents import FilesystemPermission
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from langchain_core.tools import tool

from .config import CONFIG, log

# Branch names, tags, SHAs and HEAD~N only. Blocks option injection such as "--output=/tmp/x".
SAFE_REF = re.compile(r"^(?!-)[A-Za-z0-9._/~^-]{1,100}$")

# Agent paths are virtual: the repo is mounted at /repo/. These are never readable.
SECRET_GLOBS = ["/repo/**/.env*", "/repo/.env*", "/repo/**/*.pem", "/repo/**/*.key", "/repo/**/id_rsa*", "/repo/.git/**"]

PERMISSIONS = [
    FilesystemPermission(operations=["write"], paths=["/repo/**"], mode="deny"),
    FilesystemPermission(operations=["read"], paths=SECRET_GLOBS, mode="deny"),
]


def run_git(repo: Path, args: list[str]) -> str:
    """Run git without a shell, with a timeout and an output size cap."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=CONFIG.git_timeout_s, check=False,
    )
    if proc.returncode != 0:
        return f"git error: {proc.stderr.strip()[:500]}"
    out = proc.stdout
    if len(out) > CONFIG.max_diff_chars:
        log.warning("git output truncated from %d to %d chars", len(out), CONFIG.max_diff_chars)
        out = out[: CONFIG.max_diff_chars] + (
            f"\n\n[TRUNCATED at {CONFIG.max_diff_chars} chars. Review file by file with git_diff(path=...).]"
        )
    return out


def diff_text(repo: Path, base: str, head: str) -> str:
    if not (SAFE_REF.match(base) and SAFE_REF.match(head)):
        raise ValueError("invalid git ref")
    return run_git(repo, ["diff", "--unified=5", "--no-color", f"{base}...{head}"])


def make_git_tools(repo: Path):
    @tool
    def git_changed_files(base: str, head: str = "HEAD") -> str:
        """List files changed between two git refs, with line counts added/removed."""
        if not (SAFE_REF.match(base) and SAFE_REF.match(head)):
            return "Rejected: invalid git ref."
        return run_git(repo, ["diff", "--numstat", f"{base}...{head}"])

    @tool
    def git_diff(base: str, head: str = "HEAD", path: str | None = None) -> str:
        """Show the unified diff between two git refs. Pass `path` to limit it to one file."""
        if not (SAFE_REF.match(base) and SAFE_REF.match(head)):
            return "Rejected: invalid git ref."
        args = ["diff", "--unified=5", "--no-color", f"{base}...{head}"]
        if path:
            if path.startswith("-") or ".." in Path(path).parts:
                return "Rejected: invalid path."
            args += ["--", path.lstrip("/").removeprefix("repo/")]
        return run_git(repo, args)

    return [git_changed_files, git_diff]


def make_backend(repo: Path | None) -> CompositeBackend | StateBackend:
    """Scratch notes live in agent state. The repo, if given, is mounted read-only at /repo/."""
    if repo is None:
        return StateBackend()
    return CompositeBackend(
        default=StateBackend(),
        routes={"/repo/": FilesystemBackend(root_dir=str(repo), virtual_mode=True, max_file_size_mb=CONFIG.max_file_mb)},
    )
