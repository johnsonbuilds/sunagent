"""File meta-tools: thin LLM-facing shells over a Workspace.

All real work (path containment, encoding, paging, error shaping)
lives in the execution layer; these handlers only bind the protocol
into tool-callable functions, mirroring how ``run_command`` relates
to ``ShellExecutor``.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from agent_runtime.execution.base import Workspace
from agent_runtime.execution.local import LocalWorkspace
from agent_runtime.tools.code import OUTPUTS_DIR
from agent_runtime.tools.edit_match import apply_edit
from agent_runtime.tools.syntax_gate import syntax_error


DEFAULT_READ_LIMIT = 2000


async def write_file(path: str, content: str, *,
                     workspace: Workspace | None = None) -> dict[str, Any]:
    """Create or overwrite a file with the given full content."""
    ws = workspace or LocalWorkspace()
    # Backends auto-create parent directories, so a typo'd path would
    # otherwise land a garbage file with a success receipt. Creation under
    # a missing directory is rejected here (backend-agnostic: both local
    # and Harbor workspaces expose list_dir); overwrites always pass
    # through untouched since their parent trivially exists. Root-level
    # paths skip the extra round trip.
    parent = str(PurePosixPath(path).parent)
    if parent not in ("", "."):
        listing = await ws.list_dir(parent)
        if "error" in listing:
            raise ValueError(
                f"write_file: parent directory {parent!r} of new file "
                f"{path!r} does not exist; check the path spelling, or "
                "create the directory first (e.g. mkdir -p via run_command).")
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
        return read
    content = read["content"]
    updated, mode = apply_edit(content, old_str, new_str)
    problem = syntax_error(path, updated)
    if problem:
        raise ValueError(
            f"edit_file: {path}: edited content fails syntax check ({problem}). "
            "Nothing was written; fix the edit, or use write_file if the "
            "intermediate state is intentional.")
    written = await ws.write_file(path, updated)
    if "error" in written:
        return written
    return {"path": path, "occurrences_replaced": 1, "match_mode": mode,
            "bytes_written": written["bytes_written"]}


__all__ = [
    "DEFAULT_READ_LIMIT", "edit_file", "list_dir", "read_file", "read_output",
    "write_file",
]
