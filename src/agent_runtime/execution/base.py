"""Interfaces for executing commands and managing files in an environment."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


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

    Like :class:`ShellExecutor`, a workspace never raises for
    environmental failures (missing file, unreadable directory): those
    come back as a structured ``{"error": {"type", "message"}}`` result.
    Invalid input — a path that escapes the workspace root, or a bad
    offset/limit — raises ``ValueError`` so the agent loop can turn it
    into an observation.

    ``read_file`` pages by lines: ``offset`` is 1-based and ``limit``
    caps the number of lines.  With the defaults (``offset=1`` and
    ``limit=None``) the file is returned verbatim, which is what
    composing code (such as edit_file) relies on; paged reads join the
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

        Returns the same shape as tools.grep_search (matches/match_count/
        truncated/files_scanned/files_skipped), or None to let the caller
        fall back to the generic walk+read loop.
        """
        return None

    async def find_paths(self, pattern: str, path: str = ".",
                         max_results: int = 100) -> dict[str, Any] | None:
        """Fast server-side filename search; None means no fast path."""
        return None


def paginate_lines(text: str, offset: int, limit: int | None) -> dict[str, Any]:
    """Shared line-pagination semantics for ``read_file`` results.

    Returns ``content`` plus ``start_line``/``end_line``/``total_lines``
    and a ``truncated`` flag telling the caller whether more lines
    remain (page again with a larger ``offset``).
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
    # A page that covers the whole file returns it verbatim (trailing
    # newline included); partial pages are a line slice.
    content = text if offset == 1 and len(selected) == total else "\n".join(selected)
    return {"content": content, "start_line": offset,
            "end_line": end, "total_lines": total, "truncated": end < total}
