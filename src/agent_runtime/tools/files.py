"""File meta-tools: thin LLM-facing shells over a Workspace.

All real work (path containment, encoding, paging, error shaping)
lives in the execution layer; these handlers only bind the protocol
into tool-callable functions, mirroring how ``run_command`` relates
to ``ShellExecutor``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from agent_runtime.execution.base import Workspace
from agent_runtime.execution.local import LocalWorkspace
from agent_runtime.tools.code import OUTPUTS_DIR
from agent_runtime.tools.edit_match import MatchError, apply_edit
from agent_runtime.tools.syntax_gate import syntax_error


DEFAULT_READ_LIMIT = 2000


def _error_text(error: Any) -> str:
    """Best-effort one-line rendering of a structured workspace error.

    Same helper as tools.patch: keeps the factual wording identical
    (``cannot read file 'x': FileNotFoundError: ...``) while the
    transport is unified to ``raise ValueError`` so the loop's error
    budget sees every model-fixable failure.
    """
    if isinstance(error, Mapping):
        message = error.get("message", error)
        err_type = error.get("type")
        detail = str(message) if not isinstance(message, str) else message
        return f"{err_type}: {detail}" if err_type else detail
    return str(error)


async def write_file(path: str, content: str, *,
                     workspace: Workspace | None = None) -> dict[str, Any]:
    """Create or overwrite a file with the given full content."""
    # Backends create parent directories automatically (local mkdir -p /
    # Harbor mkdir -p), so no tool-side precheck: one write, no extra RTT,
    # no TOCTOU between list_dir and write.
    ws = workspace or LocalWorkspace()
    return await ws.write_file(path, content)


async def read_file(path: str, offset: int = 1, limit: int | None = DEFAULT_READ_LIMIT, *,
                    workspace: Workspace | None = None) -> dict[str, Any]:
    """Read a file as text, one page of lines at a time."""
    return await (workspace or LocalWorkspace()).read_file(path, offset, limit)


async def read_output(path: str, offset: int = 1,
                      limit: int | None = DEFAULT_READ_LIMIT, *,
                      workspace: Workspace | None = None) -> dict[str, Any]:
    """Page through a spilled tool output saved under ``.outputs/``.

    Companion to the observation spiller: when a tool result exceeds the
    transcript budget, its full text lands in ``.outputs/`` and the model
    pages through it here instead of re-running the tool.  Paths outside
    ``.outputs/`` are rejected.
    """
    candidate = PurePosixPath(path)
    if (candidate.is_absolute() or ".." in candidate.parts
            or not candidate.parts or candidate.parts[0] != OUTPUTS_DIR):
        return {"path": path,
                "error": {"type": "InvalidPath",
                          "message": f"read_output only reads files under {OUTPUTS_DIR}/"}}
    return await (workspace or LocalWorkspace()).read_file(path, offset, limit)


async def list_dir(path: str = ".", *,
                   workspace: Workspace | None = None) -> dict[str, Any]:
    """List a directory's entries (directories first, then files)."""
    return await (workspace or LocalWorkspace()).list_dir(path)


async def edit_file(path: str, old_str: str, new_str: str, *,
                    workspace: Workspace | None = None) -> dict[str, Any]:
    """Replace exactly one occurrence of ``old_str`` with ``new_str``.

    Matching runs the edit-match ladder (see ``edit_match``): exact bytes
    first, then de-numbered / indent-insensitive fallbacks.  Every level
    must match exactly once — zero or ambiguous matches raise with a
    rendered diagnosis, and non-exact levels are reported back via
    ``match_mode`` so nothing is silently normalized.  Before writing,
    the edited content passes a syntax gate (see ``syntax_gate``);
    extensions without a checker are exempt.
    """
    if not old_str:
        raise ValueError("old_str must not be empty")
    ws = workspace or LocalWorkspace()
    read = await ws.read_file(path)
    if "error" in read:
        raise ValueError(
            f"edit_file: cannot read file {path!r}: "
            f"{_error_text(read['error'])}")
    content = read["content"]
    try:
        updated, mode = apply_edit(content, old_str, new_str)
    except MatchError as exc:
        raise ValueError(f"edit_file: {exc}") from exc
    problem = syntax_error(path, updated)
    if problem:
        raise ValueError(
            f"edit_file: {path}: edited content fails syntax check ({problem}). "
            "Nothing was written.")
    written = await ws.write_file(path, updated)
    if "error" in written:
        raise ValueError(
            f"edit_file: cannot write file {path!r}: "
            f"{_error_text(written['error'])}")
    return {"path": path, "occurrences_replaced": 1, "match_mode": mode,
            "bytes_written": written["bytes_written"]}


__all__ = [
    "DEFAULT_READ_LIMIT", "edit_file", "list_dir", "read_file", "read_output",
    "write_file",
]
