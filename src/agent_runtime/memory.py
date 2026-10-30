"""Persistent cross-session memory: workspace files the agent owns.

Enabled by ``AGENT_RUNTIME_MEMORY=1`` (any of ``1``/``true``/``on``,
case-insensitive; default off).  When enabled, the agent loop renders a
memory block into the system prompt at session start — injection point
A, the same channel as the skills block — consisting of:

* a short convention paragraph (what memory is, how to update it), and
* a flat listing of ``workspace/memory/`` (one line per file, path plus
  human size; no file contents are read).

The agent reads and updates memory with the ordinary file tools
(read_file / write_file / edit_file, run_command for renames and
pruning).  The harness never parses or interprets memory content: the
filesystem layout — folders, names, file formats — is entirely the
agent's choice, refreshed only when the next session renders a new
tree.  Benchmark harnesses (tb-v4, code-v16) never set the env var, so
the memory feature leaves no trace on those paths.

The convention text below is a working draft; it lives in code (not in
harness YAML) because memory is a deployment property, not a task
gene — same family as ``AGENT_RUNTIME_WORKSPACE``.  Change history is
tracked by git alone.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Environment switch; off unless explicitly enabled.
ENV_MEMORY = "AGENT_RUNTIME_MEMORY"

#: Directory inside the workspace root where memory lives.
MEMORY_DIR = "memory"

#: Cap on listed entries, mirroring the skills-block budgets.
MAX_MEMORY_ENTRIES = 200

#: Cap on the rendered tree *listing* (the one-line-per-file section).
#: The convention paragraph and section header are small and fixed, so
#: this effectively bounds the whole block; the truncation marker may
#: push the listing slightly over the cap, as with skills.
MAX_MEMORY_CHARS = 4000

#: Marker appended when the listing exceeds either cap.
TRUNCATION_MARKER = "… {remaining} more entries not shown"

MEMORY_CONVENTION = """\
## Persistent memory

You have persistent, cross-session memory in `workspace/memory/`.
Its organization is entirely up to you: create files and folders,
name them, and format their contents however serves you best.

- The tree below is the state at session start; it does not refresh
  during this session. Read files with read_file when they look
  relevant to the current task.
- When this session produces knowledge worth keeping across sessions
  (user preferences, project facts, decisions and their reasons),
  write it with write_file or edit_file as soon as it is established.
  Task-specific details that only matter now do not belong there.
- Keep it small and truthful: prune or rewrite entries when they
  become wrong or stale."""


@dataclass
class MemoryView:
    """One render of the memory block plus its trace metadata."""

    block: str = ""
    files: int = 0
    total_bytes: int = 0
    truncated: bool = False
    tree_hash: str = ""

    def trace_meta(self) -> dict[str, Any]:
        """Compact trace metadata describing the recalled memory tree."""
        return {
            "files": self.files,
            "bytes": self.total_bytes,
            "truncated": self.truncated,
            "tree_hash": self.tree_hash,
        }


def memory_enabled() -> bool:
    """Whether the persistent-memory feature is switched on."""
    return os.getenv(ENV_MEMORY, "").strip().lower() in {"1", "true", "on"}


_KB = 1024
_MB = 1024 ** 2
_GB = 1024 ** 3
_TB = 1024 ** 4


def _human_size(size: int) -> str:
    if size < _KB:
        return f"{size} B"
    if size < _MB:
        return f"{size / _KB:.1f} KB"
    if size < _GB:
        return f"{size / _MB:.1f} MB"
    if size < _TB:
        return f"{size / _GB:.1f} GB"
    return f"{size / _TB:.1f} TB"


def _scan_tree(memory_root: Path) -> list[tuple[str, int]]:
    """Flat, deterministically ordered (path, size) pairs under memory/.

    Hidden entries (dot-prefixed files and directories) and symbolic
    links (file or directory) are skipped, so a link pointing outside
    the workspace can never leak an external file's size into the tree
    or its hash.  Directory symlinks are additionally never followed.
    Sizes come from stat, so no file contents are read while rendering.
    """
    entries: list[tuple[str, int]] = []
    for dirpath, dirnames, filenames in os.walk(memory_root, followlinks=False):
        dirnames[:] = [
            name
            for name in dirnames
            if not name.startswith(".")
            and not (Path(dirpath) / name).is_symlink()
        ]
        for name in filenames:
            if name.startswith("."):
                continue
            full = Path(dirpath) / name
            if full.is_symlink():
                continue
            try:
                size = full.stat().st_size
            except OSError:  # vanished mid-scan
                continue
            rel = full.relative_to(memory_root).as_posix()
            entries.append((f"{MEMORY_DIR}/{rel}", size))
    entries.sort(key=lambda item: item[0])
    return entries


def _render_tree(entries: list[tuple[str, int]]) -> tuple[str, bool]:
    """Render entry lines under the entry/char caps.

    The char budget counts each line plus its ``"\\n"`` separator, so the
    listing (excluding the truncation marker) never exceeds
    ``MAX_MEMORY_CHARS``.  Returns the rendered text and whether the
    caps truncated anything.
    """
    lines: list[str] = []
    chars = 0
    shown = 0
    for path, size in entries:
        line = f"  {path} ({_human_size(size)})"
        # +1 for the "\n" separator join() will insert before this line
        # (every line except the first pays one separator char).
        cost = len(line) + (1 if lines else 0)
        if (shown >= MAX_MEMORY_ENTRIES
                or chars + cost > MAX_MEMORY_CHARS):
            break
        lines.append(line)
        chars += cost
        shown += 1
    truncated = shown < len(entries)
    if truncated:
        lines.append("  " + TRUNCATION_MARKER.format(
            remaining=len(entries) - shown))
    return "\n".join(lines), truncated


def _tree_hash(entries: list[tuple[str, int]]) -> str:
    """Short hash over the full (pre-truncation) tree for attribution."""
    if not entries:
        return ""
    canonical = "\n".join(f"{path}:{size}" for path, size in entries)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def render_memory_block(workspace: Any | None) -> MemoryView:
    """Render the memory block for the given workspace.

    Disabled (env off), missing workspace, or a workspace without a
    usable ``root`` all yield an empty :class:`MemoryView` with no
    side effects.  When enabled, the memory directory is created if
    absent and only the tree is rendered — file contents are never read.

    Memory is auxiliary: filesystem failures (permission denied, a
    colliding non-directory, a vanished root) degrade to an empty view
    instead of failing the turn.
    """
    if not memory_enabled() or workspace is None:
        return MemoryView()
    root = getattr(workspace, "root", None)
    if root is None or not str(root).strip():
        return MemoryView()
    try:
        memory_root = Path(root) / MEMORY_DIR
        memory_root.mkdir(parents=True, exist_ok=True)
        entries = _scan_tree(memory_root)
    except OSError:
        return MemoryView()
    tree_text, truncated = _render_tree(entries)
    if entries:
        tree_section = f"Current contents of memory/:\n\n{tree_text}"
    else:
        tree_section = ("Current contents of memory/ (nothing recorded "
                        "yet — the directory is yours to organize).")
    block = f"{MEMORY_CONVENTION}\n\n{tree_section}"
    return MemoryView(block=block, files=len(entries),
                      total_bytes=sum(size for _, size in entries),
                      truncated=truncated, tree_hash=_tree_hash(entries))


__all__ = [
    "ENV_MEMORY", "MEMORY_DIR", "MAX_MEMORY_ENTRIES", "MAX_MEMORY_CHARS",
    "MEMORY_CONVENTION", "TRUNCATION_MARKER",
    "MemoryView", "memory_enabled", "render_memory_block",
]
