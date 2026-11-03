"""File meta-tools: thin LLM-facing shells over a Workspace.

All real work (path containment, encoding, paging, error shaping)
lives in the execution layer; these handlers only bind the protocol
into tool-callable functions, mirroring how ``run_command`` relates
to ``ShellExecutor``.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from agent_runtime.execution.base import Workspace, error_text
from agent_runtime.execution.local import LocalWorkspace
from agent_runtime.execution.base import OUTPUTS_DIR
from agent_runtime.tools.edit_match import MatchError, apply_edit
from agent_runtime.tools.syntax_gate import syntax_error
from agent_runtime.tools.spec import ToolSpec
from functools import partial


DEFAULT_READ_LIMIT = 2000


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
    ``.outputs/`` raise ``ValueError`` (model-fixable input, so the loop
    error budget sees it).
    """
    candidate = PurePosixPath(path)
    if (candidate.is_absolute() or ".." in candidate.parts
            or not candidate.parts or candidate.parts[0] != OUTPUTS_DIR):
        raise ValueError(
            f"read_output only reads files under {OUTPUTS_DIR}/")
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
            f"{error_text(read['error'])}")
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
            f"{error_text(written['error'])}")
    return {"path": path, "occurrences_replaced": 1, "match_mode": mode,
            "bytes_written": written["bytes_written"]}


def write_file_spec(workspace: Workspace) -> ToolSpec:
    return ToolSpec("write_file",
                    "Create a new file or replace the entire content of an "
                    "existing file. Provide the complete intended file content "
                    "in one call. Parent directories are created automatically; "
                    "this tool is for scenarios where the whole file content "
                    "is being defined at once: new-file creation and full-file "
                    "rewrites.",
                    {"type": "object", "properties": {
                        "path": {"type": "string",
                                 "description": "File path inside the workspace"},
                        "content": {"type": "string",
                                    "description": "Complete file content"}},
                     "required": ["path", "content"]},
                    partial(write_file, workspace=workspace))


def read_file_spec(workspace: Workspace) -> ToolSpec:
    return ToolSpec("read_file",
                    "Read a file as text. Long files are returned one page of "
                    "lines at a time; continue with a larger offset when the "
                    "result reports truncated: true.",
                    {"type": "object", "properties": {
                        "path": {"type": "string",
                                 "description": "File path inside the workspace"},
                        "offset": {"type": "integer",
                                   "description": "First line to return (1-based)",
                                   "default": 1},
                        "limit": {"type": "integer",
                                  "description": "Maximum number of lines to return",
                                  "default": DEFAULT_READ_LIMIT}},
                     "required": ["path"]},
                    partial(read_file, workspace=workspace))


def read_output_spec(workspace: Workspace) -> ToolSpec:
    return ToolSpec("read_output",
                    "Page through a tool output that was saved under .outputs/ "
                    "when it exceeded the transcript budget (the observation "
                    "names the file). Returns one page of lines at a time; "
                    "continue with a larger offset when the result reports "
                    "truncated: true. Never re-run a tool just to see its "
                    "output again — page the saved file instead.",
                    {"type": "object", "properties": {
                        "path": {"type": "string",
                                 "description": "Path under .outputs/, e.g. "
                                                "'.outputs/<name>.txt' as named "
                                                "by the observation"},
                        "offset": {"type": "integer",
                                   "description": "First line to return (1-based)",
                                   "default": 1},
                        "limit": {"type": "integer",
                                  "description": "Maximum number of lines to return",
                                  "default": DEFAULT_READ_LIMIT}},
                     "required": ["path"]},
                    partial(read_output, workspace=workspace))


def list_dir_spec(workspace: Workspace) -> ToolSpec:
    return ToolSpec("list_dir",
                    "List the entries of a directory (directories first, "
                    "then files).",
                    {"type": "object", "properties": {
                        "path": {"type": "string",
                                 "description": "Directory path inside the workspace",
                                 "default": "."}},
                     "required": []},
                    partial(list_dir, workspace=workspace))


def edit_file_spec(workspace: Workspace) -> ToolSpec:
    return ToolSpec("edit_file",
                    "Replace exactly one occurrence of old_str with new_str in a file."
                    "old_str must match uniquely (exact first, tolerant fallbacks "
                    "for line-number prefixes and indentation; "
                    "the applied mode is reported back as match_mode). "
                    "Zero or ambiguous matches raise with a rendered diagnosis. "
                    "Edited content that no longer parses (Python, JSON, YAML, "
                    "shell) is rejected and nothing is written. "
                    "Best suited for single-point edits and iterative small "
                    "changes, including text containing conflict markers.",
                    {"type": "object", "properties": {
                        "path": {"type": "string",
                                 "description": "File path inside the workspace"},
                        "old_str": {"type": "string",
                                    "description": "Text to replace, must match "
                                                   "uniquely (exact preferred; "
                                                   "line-number-prefix-stripped "
                                                   "and indent-insensitive "
                                                   "fallbacks allowed)"},
                        "new_str": {"type": "string",
                                    "description": "Replacement text (empty to delete)"}},
                     "required": ["path", "old_str", "new_str"]},
                    partial(edit_file, workspace=workspace))


__all__ = [
    "DEFAULT_READ_LIMIT", "edit_file", "list_dir", "read_file", "read_output",
    "write_file", "write_file_spec", "read_file_spec", "read_output_spec",
    "list_dir_spec", "edit_file_spec",
]
