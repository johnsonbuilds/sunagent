"""The apply_patch meta-tool: batched edits to a single file, all-or-nothing.

The call takes a file path plus an ordered list of ``{old_text, new_text}``
edits.  Every ``old_text`` must match the file exactly once (same rule as
``edit_file``), and edits apply in order against the in-memory content, so
a later edit may depend on an earlier one.

The whole call is validated before anything is written: any failure
raises before the first write, so a bad call never leaves a half-edited
file behind.  File creation is out of scope — use ``write_file`` for new
files (``old_text`` must be non-empty).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_runtime.execution.base import Workspace
from agent_runtime.execution.local import LocalWorkspace
from agent_runtime.tools.edit_match import MatchError, apply_edit
from agent_runtime.tools.syntax_gate import syntax_error


def _error_text(error: Any) -> str:
    """Best-effort one-line rendering of a structured workspace error."""
    if isinstance(error, Mapping):
        message = error.get("message", error)
        err_type = error.get("type")
        detail = str(message) if not isinstance(message, str) else message
        return f"{err_type}: {detail}" if err_type else detail
    return str(error)


def _validate_edits(edits: Any) -> list[dict[str, str]]:
    if not isinstance(edits, list) or not edits:
        raise ValueError("edits must be a non-empty array of "
                         "{old_text, new_text} objects")
    normalized: list[dict[str, str]] = []
    for index, edit in enumerate(edits, 1):
        if not isinstance(edit, Mapping):
            raise ValueError(
                f"apply_patch: edit {index}: must be an object with "
                "old_text and new_text")
        if set(edit.keys()) != {"old_text", "new_text"}:
            raise ValueError(
                f"apply_patch: edit {index}: unexpected keys "
                f"{sorted(edit.keys())!r}; allowed keys: "
                "['new_text', 'old_text']")
        old_text, new_text = edit["old_text"], edit["new_text"]
        if not isinstance(old_text, str) or not old_text:
            raise ValueError(
                f"apply_patch: edit {index}: old_text is empty; "
                "old_text must be a non-empty string")
        if not isinstance(new_text, str):
            raise ValueError(
                f"apply_patch: edit {index}: new_text must be a string "
                "(empty string deletes the match)")
        normalized.append({"old_text": old_text, "new_text": new_text})
    return normalized


async def apply_patch(path: str, edits: list[dict[str, str]], *,
                      workspace: Workspace | None = None) -> dict[str, Any]:
    """Validate and apply every edit, writing only after all checks pass."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string")
    normalized = _validate_edits(edits)
    ws = workspace or LocalWorkspace()

    read = await ws.read_file(path)
    if "error" in read:
        raise ValueError(
            f"apply_patch: cannot read file {path!r}: "
            f"{_error_text(read['error'])}")

    content = read["content"]
    match_modes: list[str] = []  # per-edit, in order; audit trail
    for index, edit in enumerate(normalized, 1):
        try:
            content, mode = apply_edit(content, edit["old_text"],
                                       edit["new_text"])
        except MatchError as exc:
            raise ValueError(f"apply_patch: edit {index}: {exc}") from exc
        match_modes.append(mode)

    problem = syntax_error(path, content)
    if problem:
        raise ValueError(
            f"apply_patch: {path}: edited content fails syntax check "
            f"({problem}). Nothing was written.")

    written = await ws.write_file(path, content)
    if "error" in written:
        raise ValueError(
            f"apply_patch: cannot write file {path!r}: "
            f"{_error_text(written['error'])}")

    return {
        "path": path,
        "edits_applied": len(normalized),
        "match_modes": match_modes,
        "bytes_written": written.get("bytes_written", 0),
    }


__all__ = ["apply_patch"]
