"""Persistent cross-session memory: workspace files the agent owns.

Enabled by ``AGENT_RUNTIME_MEMORY=1`` (any of ``1``/``true``/``on``,
case-insensitive; default off).  This environment switch is the only
control over whether memory is rendered: callers always supply a
workspace, and the workspace's presence is never used as a hidden
on/off switch.  When enabled, the agent loop renders a memory block into
the system prompt at session start — injection point A, the same channel
as the skills block — consisting of:

* a short convention paragraph (what memory is, how to update it), and
* a flat listing of the memory directory (one line per file, path plus
  human size; no file contents are read).

The memory directory is *not* part of the sandbox.  It lives under the
runtime home -- ``storage.runtime_home()/memory``, i.e. ``workspace/memory/``
by default, next to ``sessions/`` and ``traces/`` -- so memory survives a
change of sandbox and stays put even when the sandbox is narrowed to a
subdirectory.  ``AGENT_RUNTIME_MEMORY_DIR`` overrides the location
outright.  The listing is still *spelled* relative to the sandbox root
(``workspace/memory/notes.md`` with the default layout) because the file
tools resolve relative paths there and refuse anything that escapes it; a
memory directory outside the sandbox therefore cannot be reached by the
agent, and is reported at warning level and skipped rather than silently
rendered as unusable paths.

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
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_runtime import storage
from agent_runtime.execution.base import Workspace

logger = logging.getLogger(__name__)

#: Environment switch; off unless explicitly enabled.
ENV_MEMORY = "AGENT_RUNTIME_MEMORY"

#: Overrides the memory directory itself (absolute, or relative to cwd).
ENV_MEMORY_DIR = "AGENT_RUNTIME_MEMORY_DIR"

#: Directory under the runtime home where memory lives.
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

You have persistent, cross-session memory in `{location}` (relative to
the sandbox root; write there with paths like `{location}notes.md`).
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


def memory_root() -> Path:
    """The absolute directory that holds persistent memory.

    Anchored on :func:`agent_runtime.storage.runtime_home` -- not on the
    agent's sandbox -- so memory counts as durable run state, like
    ``sessions/`` and ``traces/``.  ``AGENT_RUNTIME_MEMORY_DIR`` overrides
    it; a relative override resolves against the process working
    directory, exactly like ``AGENT_RUNTIME_HOME``.  Read per call so the
    environment can be changed without rebuilding anything.
    """
    raw = os.getenv(ENV_MEMORY_DIR, "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return storage.runtime_home() / MEMORY_DIR


def _convention(location: str) -> str:
    """Fill the convention paragraph with the path the agent must use."""
    return MEMORY_CONVENTION.format(location=location)


def _display_prefix(memory_dir: Path, workspace_root: Path) -> str | None:
    """How the model must spell a memory path, or ``None`` if unreachable.

    File tools resolve relative paths against the sandbox root and reject
    anything that escapes it, so the listing needs a sandbox-relative
    prefix: ``"workspace/memory/"`` for the default layout (sandbox = the
    project root, memory = the runtime home's ``memory/``), ``""`` when
    memory *is* the sandbox root.  ``None`` means the memory directory is
    outside the sandbox, where the agent could not open those files at
    all.
    """
    try:
        rel = memory_dir.resolve().relative_to(workspace_root.resolve())
    except (ValueError, OSError):
        return None
    inner = rel.as_posix()
    return "" if inner == "." else inner + "/"


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


def _scan_tree(memory_dir: Path, prefix: str) -> list[tuple[str, int]]:
    """Flat, deterministically ordered (path, size) pairs under memory/.

    Hidden entries (dot-prefixed files and directories) and symbolic
    links (file or directory) are skipped, so a link pointing outside
    the workspace can never leak an external file's size into the tree
    or its hash.  Directory symlinks are additionally never followed.
    Sizes come from stat, so no file contents are read while rendering.
    """
    entries: list[tuple[str, int]] = []
    for dirpath, dirnames, filenames in os.walk(memory_dir, followlinks=False):
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
            rel = full.relative_to(memory_dir).as_posix()
            entries.append((f"{prefix}{rel}", size))
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


def render_memory_block(workspace: Workspace) -> MemoryView:
    """Render the memory block for the given workspace.

    Whether memory is rendered is controlled solely by the
    ``AGENT_RUNTIME_MEMORY`` environment switch.  ``workspace`` is a
    required collaborator: missing storage is a wiring error, not a
    second way to turn the feature off.

    A workspace without a ``root`` (Harbor's free-roaming mode) cannot
    host persistent memory because the renderer scans the local
    filesystem; that case is reported once at warning level and yields
    an empty view instead of failing the whole task.

    When enabled, the memory directory is created if absent and only the
    tree is rendered — file contents are never read.  Filesystem
    failures (permission denied, a colliding non-directory, a vanished
    root) degrade to an empty view instead of failing the turn.
    """
    if workspace is None:
        raise ValueError(
            "render_memory_block requires a workspace; memory enablement "
            "is controlled by AGENT_RUNTIME_MEMORY, not by workspace presence")
    if not memory_enabled():
        return MemoryView()
    root = workspace.root
    if root is None or not str(root).strip():
        logger.warning(
            "memory.unsupported_workspace: AGENT_RUNTIME_MEMORY is on but "
            "the workspace has no root; persistent memory requires a rooted "
            "workspace and will be skipped")
        return MemoryView()
    memory_dir = memory_root()
    prefix = _display_prefix(memory_dir, Path(str(root)).expanduser())
    if prefix is None:
        logger.warning(
            "memory.outside_sandbox: memory directory %s is outside the "
            "sandbox root %s, so file tools cannot reach it; persistent "
            "memory is skipped (point AGENT_RUNTIME_MEMORY_DIR inside the "
            "sandbox, or widen AGENT_RUNTIME_WORKSPACE)", memory_dir, root)
        return MemoryView()
    try:
        memory_dir.mkdir(parents=True, exist_ok=True)
        entries = _scan_tree(memory_dir, prefix)
    except OSError:
        return MemoryView()
    tree_text, truncated = _render_tree(entries)
    location = prefix or "./"
    if entries:
        tree_section = f"Current contents of {location}:\n\n{tree_text}"
    else:
        tree_section = (f"Current contents of {location} (nothing recorded "
                        "yet — the directory is yours to organize).")
    block = f"{_convention(location)}\n\n{tree_section}"
    return MemoryView(block=block, files=len(entries),
                      total_bytes=sum(size for _, size in entries),
                      truncated=truncated, tree_hash=_tree_hash(entries))


__all__ = [
    "ENV_MEMORY", "ENV_MEMORY_DIR", "MEMORY_DIR",
    "MAX_MEMORY_ENTRIES", "MAX_MEMORY_CHARS",
    "MEMORY_CONVENTION", "TRUNCATION_MARKER",
    "MemoryView", "memory_enabled", "memory_root", "render_memory_block",
]
