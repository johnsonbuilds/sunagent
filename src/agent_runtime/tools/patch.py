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

import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agent_runtime.execution.base import Workspace
from agent_runtime.execution.local import LocalWorkspace
from agent_runtime.tools.edit_match import MatchError, apply_edit
from agent_runtime.tools.syntax_gate import syntax_error


BEGIN = "<<<<<<< SEARCH"
SEP = "======="
END = ">>>>>>> REPLACE"

# Unified-diff markers that can never start a legitimate block line: seeing
# one means the model pasted a diff instead of SEARCH/REPLACE blocks.  A
# bare `---` is deliberately NOT listed (valid markdown/YAML content); a
# `--- a/...` line therefore falls through to path handling and fails later
# with the precise cannot-read error.
_DIFF_LINE_PREFIXES = ("diff --git ", "+++ ")
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+.*@@")

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


def _error_text(error: Any) -> str:
    """Best-effort one-line rendering of a structured workspace error."""
    if isinstance(error, Mapping):
        message = error.get("message", error)
        err_type = error.get("type")
        detail = str(message) if not isinstance(message, str) else message
        return f"{err_type}: {detail}" if err_type else detail
    return str(error)


def _check_path(path: str, line_number: int) -> None:
    """Generic workspace-path legality: relative and non-escaping.

    Readability (does the file exist?) is checked later against the
    workspace, where the precise reason is known; this only rejects paths
    that can never be valid, whatever the model meant.
    """
    if path.startswith("/"):
        raise ValueError(
            f"apply_patch: line {line_number}: path {path!r} must be "
            "workspace-relative (no leading '/')")
    normalized = posixpath.normpath(path)
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError(
            f"apply_patch: line {line_number}: path {path!r} escapes the "
            "workspace ('..' not allowed)")


def parse_patch(patch: str) -> _Patch:
    """Parse patch text into ordered blocks, validating the grammar."""
    lines = patch.splitlines()
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if (stripped.startswith(_DIFF_LINE_PREFIXES)
                or _HUNK_HEADER_RE.match(stripped)):
            raise ValueError(
                f"apply_patch: line {number}: looks like a unified diff "
                f"(found {stripped[:20]!r}). This tool does not accept "
                "unified diffs. Resend as: <bare workspace-relative path>, "
                f"then {BEGIN} / {SEP} / {END} sections.")
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
            _check_path(line.strip(), number)
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
                    raise ValueError(
                        f"apply_patch: block {block.index}: cannot read file "
                        f"{block.path!r}: {_error_text(read['error'])}. "
                        "Paths must be workspace-relative and match an "
                        "existing file exactly; verify with find_files.")
                parent = posixpath.dirname(block.path)
                if parent not in ("", "."):
                    listing = await ws.list_dir(parent)
                    if "error" in listing:
                        raise ValueError(
                            f"apply_patch: block {block.index}: parent "
                            f"directory {parent!r} of new file "
                            f"{block.path!r} does not exist "
                            f"({_error_text(listing['error'])}); not "
                            "creating it. Check the path spelling, or "
                            "create the directory first.")
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
            raise ValueError(
                f"apply_patch: cannot write file {path!r}: "
                f"{_error_text(written['error'])}")
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
