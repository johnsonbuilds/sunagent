"""Persistent cross-session memory: workspace files the agent owns.

Enabled by ``RuntimeConfig.memory_enabled`` (``AGENT_RUNTIME_MEMORY``, default
off).  When on, the agent loop renders a memory block into the system prompt at
session start (injection point A, same channel as the skills block): a short
convention paragraph plus a flat listing of the memory directory.  File
contents are never read.

The directory lives under the runtime home (``workspace/memory/`` by default),
not in the sandbox, so memory survives a change of sandbox;
``RuntimeConfig.memory_dir`` overrides it.  The listing is spelled
sandbox-relative because the file tools resolve paths there and refuse
anything that escapes, so a memory directory outside the sandbox is reported
at warning level and skipped rather than rendered as unusable paths.

The agent reads and updates memory with the ordinary file tools; the harness
never parses its content.  The convention text lives in code (not harness
YAML) because memory is a deployment property, not a task gene.
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

#: Cap on the rendered tree *listing*; the convention paragraph and header
#: are small and fixed, so this effectively bounds the whole block.
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


def memory_root(memory_dir: str | Path | None = None) -> Path:
    """The absolute directory that holds persistent memory.

    Anchored on :func:`agent_runtime.storage.runtime_home`, not on the agent's
    sandbox, so memory counts as durable run state.  A relative override
    resolves against the process working directory, like ``AGENT_RUNTIME_HOME``.
    """
    raw = (str(memory_dir).strip() if memory_dir is not None else "")
    if not raw:
        from agent_runtime.runtime_config import RuntimeConfig

        raw = RuntimeConfig.from_env().memory_dir or ""
    if raw:
        return Path(raw).expanduser().resolve()
    return storage.runtime_home() / MEMORY_DIR


def _convention(location: str) -> str:
    """Fill the convention paragraph with the path the agent must use."""
    return MEMORY_CONVENTION.format(location=location)


def _display_prefix(memory_dir: Path, workspace_root: Path) -> str | None:
    """How the model must spell a memory path, or ``None`` if unreachable.

    File tools resolve relative paths against the sandbox root and reject
    anything that escapes it, so the listing needs a sandbox-relative prefix.
    ``None`` means the agent could not open those files at all.
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

    Hidden entries and symlinks are skipped so a link pointing outside the
    workspace can never leak an external file's size into the tree or its hash.
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

    The char budget counts each line plus its separator, so the listing never
    exceeds ``MAX_MEMORY_CHARS``.  Returns the text and whether anything was
    truncated.
    """
    lines: list[str] = []
    chars = 0
    shown = 0
    for path, size in entries:
        line = f"  {path} ({_human_size(size)})"
        # +1 for the "\n" join() inserts before every line but the first.
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


def render_memory_block(workspace: Workspace, *,
                        enabled: bool | None = None,
                        memory_dir: str | Path | None = None) -> MemoryView:
    """Render the memory block for the given workspace.

    ``enabled=None`` resolves the environment here once, for callers with no
    composition root.  ``workspace`` is required: missing storage is a wiring
    error, not a second way to turn the feature off.  A workspace without a
    ``root`` (Harbor's free-roaming mode) cannot host memory, since the renderer
    scans the local filesystem; that and any filesystem failure degrade to an
    empty view instead of failing the turn.
    """
    if workspace is None:
        raise ValueError(
            "render_memory_block requires a workspace; memory enablement "
            "is controlled by AGENT_RUNTIME_MEMORY, not by workspace presence")
    if enabled is None:
        from agent_runtime.runtime_config import RuntimeConfig

        enabled = RuntimeConfig.from_env().memory_enabled
    if not enabled:
        return MemoryView()
    root = workspace.root
    if root is None or not str(root).strip():
        logger.warning(
            "memory.unsupported_workspace: AGENT_RUNTIME_MEMORY is on but "
            "the workspace has no root; persistent memory requires a rooted "
            "workspace and will be skipped")
        return MemoryView()
    memory_dir_path = memory_root(memory_dir)
    prefix = _display_prefix(memory_dir_path, Path(str(root)).expanduser())
    if prefix is None:
        logger.warning(
            "memory.outside_sandbox: memory directory %s is outside the "
            "sandbox root %s, so file tools cannot reach it; persistent "
            "memory is skipped (point AGENT_RUNTIME_MEMORY_DIR inside the "
            "sandbox, or widen AGENT_RUNTIME_WORKSPACE)", memory_dir_path, root)
        return MemoryView()
    try:
        memory_dir_path.mkdir(parents=True, exist_ok=True)
        entries = _scan_tree(memory_dir_path, prefix)
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
    "MemoryView", "memory_root", "render_memory_block",
]
