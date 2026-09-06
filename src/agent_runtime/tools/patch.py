"""The apply_patch meta-tool: batched SEARCH/REPLACE edits, all-or-nothing.

Format (Aider-style blocks; no line numbers, no unified diff):

    path/to/file.py
    <<<<<<< SEARCH
    exact original text
    =======
    replacement text
    >>>>>>> REPLACE

Multiple blocks per file and multiple files per patch are allowed; a
patch may interleave files freely.  An empty SEARCH section creates a
new file with the REPLACE section as its content.

The whole patch is validated before anything is written: every SEARCH
section must match its file exactly once (same rule as ``edit_file``),
and blocks on one file apply in order against the in-memory content, so
a later block may depend on an earlier one.  Any failure raises before
the first write, so a bad patch never leaves half-edited files behind.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_runtime.execution.base import Workspace
from agent_runtime.execution.local import LocalWorkspace
from agent_runtime.tools.edit_match import MatchError, apply_edit
from agent_runtime.tools.syntax_gate import syntax_error


BEGIN = "<<<<<<< SEARCH"
SEP = "======="
END = ">>>>>>> REPLACE"

# Lines a merge conflict leaves inside file content.  When the SEARCH or
# REPLACE text itself contains one, the block grammar cannot express the
# edit (the marker is indistinguishable from the section delimiters) —
# parse_patch points the model at edit_file instead of reporting a
# misleading syntax error.
_CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>")


@dataclass
class _Block:
    path: str
    old: str
    new: str
    index: int  # 1-based position in the patch, for error messages


@dataclass
class _Patch:
    blocks: list[_Block] = field(default_factory=list)


def parse_patch(patch: str) -> _Patch:
    """Parse patch text into ordered blocks, validating the grammar."""
    lines = patch.splitlines()
    parsed = _Patch()
    path: str | None = None
    section: str | None = None  # None | "old" | "new"
    body: list[str] = []
    old_lines: list[str] = []

    def fail(line_number: int, reason: str) -> ValueError:
        return ValueError(f"apply_patch: line {line_number}: {reason}")

    for number, line in enumerate(lines, 1):
        if line.strip() == BEGIN:
            if section is not None:
                raise fail(number, f"unexpected {BEGIN} inside a block")
            if path is None:
                raise fail(number, f"{BEGIN} must be preceded by a file path")
            section, body, old_lines = "old", [], []
        elif line.strip() == SEP:
            if section != "old":
                if any(ln.lstrip().startswith(_CONFLICT_MARKERS)
                       for ln in body + old_lines):
                    raise fail(number,
                               f"{SEP} inside block content: the SEARCH/REPLACE "
                               "text itself contains a separator line (merge-"
                               "conflict markers?); apply_patch cannot express "
                               "this — use edit_file for this edit instead")
                raise fail(number, f"{SEP} is only valid inside a SEARCH section")
            section, old_lines, body = "new", list(body), []
        elif line.strip() == END:
            if section != "new":
                raise fail(number, f"{END} without a SEARCH/REPLACE pair")
            parsed.blocks.append(_Block(path, "\n".join(old_lines),
                                        "\n".join(body), len(parsed.blocks) + 1))
            section, path, body = None, None, []
        elif section is not None:
            body.append(line)
        elif line.strip():
            if path is not None:
                raise fail(number,
                           f"expected {BEGIN} after file path {path!r}, "
                           f"got stray text")
            path = line.strip()
    if section is not None:
        raise fail(len(lines) + 1,
                   f"unterminated block (missing {END}). "
                   f"Resend the complete patch with every block "
                   f"closed by {END} on its own final line. "
                   f"Do not send only the missing marker.")
    if path is not None:
        raise fail(len(lines) + 1, f"file path {path!r} has no edit block")
    if not parsed.blocks:
        raise ValueError("apply_patch: no edit blocks found; expected "
                         f"{BEGIN}/{SEP}/{END} sections with file paths")
    return parsed


async def apply_patch(patch: str, *,
                      workspace: Workspace | None = None) -> dict[str, Any]:
    """Validate and apply every block, writing only after all checks pass."""
    if not isinstance(patch, str) or not patch.strip():
        raise ValueError("patch must be a non-empty string")
    parsed = parse_patch(patch)
    ws = workspace or LocalWorkspace()

    files: dict[str, dict[str, Any]] = {}
    match_modes: list[str] = []  # per-block, in patch order; audit trail
    for block in parsed.blocks:
        state = files.get(block.path)
        if state is None:
            read = await ws.read_file(block.path)
            if "error" in read:
                if block.old:  # editing a file that cannot be read
                    return {"path": block.path, "error": read["error"]}
                state = {"content": None}  # to be created
            else:
                state = {"content": read["content"]}
            files[block.path] = state

        content: str | None = state["content"]
        if not block.old:
            if content is not None:
                raise ValueError(
                    f"apply_patch: block {block.index}: file {block.path} "
                    "already exists; use a non-empty SEARCH section to edit it")
            state["content"] = block.new
            state["created"] = True
            continue
        if content is None:
            raise ValueError(
                f"apply_patch: block {block.index}: file {block.path} does "
                "not exist; use an empty SEARCH section to create it")
        try:
            state["content"], mode = apply_edit(content, block.old, block.new)
        except MatchError as exc:
            raise ValueError(
                f"apply_patch: block {block.index}: {exc}") from exc
        match_modes.append(mode)

    for path, state in files.items():
        problem = syntax_error(path, state["content"] or "")
        if problem:
            raise ValueError(
                f"apply_patch: {path}: edited content fails syntax check "
                f"({problem}). Nothing was written; fix the edit, or use "
                "write_file if the intermediate state is intentional.")

    created: list[str] = []
    updated: list[str] = []
    bytes_written = 0
    for path, state in files.items():
        written = await ws.write_file(path, state["content"] or "")
        if "error" in written:
            return {"path": path, "error": written["error"]}
        bytes_written += written.get("bytes_written", 0)
        (created if state.get("created") else updated).append(path)

    return {
        "blocks_applied": len(parsed.blocks),
        "files_created": created,
        "files_updated": updated,
        "match_modes": match_modes,
        "bytes_written": bytes_written,
    }


__all__ = ["BEGIN", "END", "SEP", "apply_patch", "parse_patch"]
