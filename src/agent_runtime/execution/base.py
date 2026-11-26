"""Interfaces for executing commands and managing files in an environment."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol


# Source-level output cap (chars per stream): executors truncate here so a
# runaway print cannot OOM the process before the transcript boundary spills.
MAX_OUTPUT_CHARS = 32_000


def _truncate_stream(text: str) -> tuple[str, bool]:
    """Cap one output stream; returns (text, truncated)."""
    if len(text) > MAX_OUTPUT_CHARS:
        return text[:MAX_OUTPUT_CHARS] + "\n... (output truncated)", True
    return text, False


def truncate_result(result: dict[str, Any]) -> dict[str, Any]:
    """Apply the source-level cap to a shell result in place (by copy)."""
    stdout, t1 = _truncate_stream(result.get("stdout") or "")
    stderr, t2 = _truncate_stream(result.get("stderr") or "")
    if not (t1 or t2):
        return result
    return {**result, "stdout": stdout, "stderr": stderr, "truncated": True}


def error_text(error: Any) -> str:
    """Render a structured workspace error as one factual line.

    Keeps the wording identical across tools that must raise because the
    failure is model-fixable.
    """
    if isinstance(error, Mapping):
        message = error.get("message", error)
        error_type = error.get("type")
        detail = str(message) if not isinstance(message, str) else message
        return f"{error_type}: {detail}" if error_type else detail
    return str(error)


# Shared search/index constants: owned here so both the tools layer and the
# execution layer import from one place (harbor.py used to import from
# tools.search, an execution -> tools dependency).
OUTPUTS_DIR = ".outputs"
SCRIPTS_DIR = ".scripts"
SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
    "target", ".next",
    # Agent working dirs: generated scripts and spilled observations are
    # addressed directly, never discovered via a repo-wide search.
    SCRIPTS_DIR, OUTPUTS_DIR,
    # Runtime state (sessions, traces, summaries) is noise in a search.
    "workspace",
})

MAX_FILE_BYTES = 1_000_000
PREVIEW_CHARS = 240

DEFAULT_GREP_RESULTS = 200
MAX_GREP_RESULTS = 1000
MATCHES_PER_FILE = 50

DEFAULT_FIND_RESULTS = 100
MAX_FIND_RESULTS = 1000


def matches_path(path: str, pattern: str) -> bool:
    """Match a workspace-relative posix path against a wildcard pattern.

    ``*`` crosses directory separators (VS Code Ctrl+P behavior), and a
    pattern without a ``/`` also matches the basename.
    """
    import fnmatch
    import posixpath

    if fnmatch.fnmatchcase(path, pattern):
        return True
    if pattern.startswith("**/"):
        if fnmatch.fnmatchcase(path, pattern[3:]):
            return True
    if "/" not in pattern and fnmatch.fnmatchcase(posixpath.basename(path),
                                                   pattern):
        return True
    return False


class ShellExecutor(Protocol):
    async def execute(
        self,
        command: str,
        cwd: str | None = None,
        timeout: float | None = 30.0,
    ) -> dict[str, Any]:
        """Execute a shell command and return its structured result."""


class Workspace(Protocol):
    """File storage rooted at a directory inside one execution environment.

    Environmental failures (missing file, unreadable directory) come back as
    a structured ``{"error": {"type", "message"}}`` result, never an
    exception.  Invalid input — a path escaping the root, a bad
    offset/limit — raises ``ValueError`` so the agent loop can turn it into
    an observation.

    ``read_file`` pages by lines: ``offset`` is 1-based and ``limit`` caps
    the number of lines.  With the defaults the file is returned verbatim,
    which composing code (such as edit_file) relies on; paged reads join the
    selected lines and therefore drop a trailing newline.
    """

    root: str | Path | None

    async def read_file(self, path: str, offset: int = 1,
                        limit: int | None = None) -> dict[str, Any]:
        """Read a workspace file, optionally one page of lines at a time."""

    async def write_file(self, path: str, content: str) -> dict[str, Any]:
        """Create or overwrite a workspace file (parent dirs created)."""

    async def list_dir(self, path: str = ".") -> dict[str, Any]:
        """List one directory's entries, directories first."""

    async def search_contents(self, pattern: str, path: str = ".",
                              include: str | None = None,
                              ignore_case: bool = False,
                              max_results: int = 200) -> dict[str, Any] | None:
        """Fast server-side content search; None means no fast path.

        Returns the same shape as tools.grep_search, or None to let the
        caller fall back to the generic walk+read loop.
        """
        return None

    async def find_paths(self, pattern: str, path: str = ".",
                         max_results: int = 100) -> dict[str, Any] | None:
        """Fast server-side filename search; None means no fast path."""
        return None


def paginate_lines(text: str, offset: int, limit: int | None) -> dict[str, Any]:
    """Shared line-pagination semantics for ``read_file`` results.

    Returns ``content`` plus ``start_line``/``end_line``/``total_lines`` and
    a ``truncated`` flag telling the caller whether more lines remain.
    """
    if offset < 1:
        raise ValueError("offset must be >= 1")
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1")

    total = len(text.splitlines())
    if offset == 1 and limit is None:
        return {"content": text, "start_line": 1, "end_line": total,
                "total_lines": total, "truncated": False}

    lines = text.splitlines()
    selected = (lines[offset - 1:] if limit is None
                else lines[offset - 1: offset - 1 + limit])
    end = min(offset - 1 + len(selected), total)
    # A page covering the whole file returns it verbatim (trailing newline
    # included); partial pages are a line slice.
    content = text if offset == 1 and len(selected) == total else "\n".join(selected)
    return {"content": content, "start_line": offset,
            "end_line": end, "total_lines": total, "truncated": end < total}
