"""One place that decides where the runtime keeps what it writes.

The runtime persists three kinds of thing, and they used to live in
three unrelated directories: browser sessions under ``runs/webui/``, run
traces under ``runs/webui/traces/`` and the compaction summary under
``memory/``.  Every writer resolved its own path, so there was no way
to answer "what does this project write to disk?" by reading one file.

This module is that answer.  Everything the runtime persists lives
under one root:

    <home>/
      sessions/   one JSON document per browser conversation
      traces/     one JSONL file per agent turn
      memory/     the latest compaction summary

``<home>`` is ``AGENT_RUNTIME_HOME`` when set, and ``workspace/`` next
to the project root otherwise.

What this module does *not* own is the agent's sandbox (``AGENT_RUNTIME_WORKSPACE``
with its ``.outputs/`` spills and ``.scripts/``) and the versioned
research artefacts (``harnesses/``, ``evaluation/records/``).  Those are
deliberately separate: the sandbox is the agent's own working area and
its paths are part of the model's contract, while the research
artefacts are meant to be committed and compared across runs.
"""

from __future__ import annotations

import os
from pathlib import Path


# The project root, i.e. the directory that holds ``src/`` and
# ``webui/``.  Anchored on this module so the default root does not
# depend on the process working directory.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_HOME_DIRNAME = "workspace"


def runtime_home() -> Path:
    """The root directory of everything the runtime persists.

    ``AGENT_RUNTIME_HOME`` overrides it; the default is ``workspace/``
    inside the project.  Read per call so the environment can be
    changed without rebuilding anything.
    """
    raw = os.getenv("AGENT_RUNTIME_HOME")
    if raw:
        return Path(raw).expanduser()
    return _PROJECT_ROOT / DEFAULT_HOME_DIRNAME


def sessions_dir() -> Path:
    """Where browser conversations are stored (one JSON document each)."""
    return runtime_home() / "sessions"


def traces_dir() -> Path:
    """Where run traces are stored (one JSONL file per agent turn)."""
    return runtime_home() / "traces"


def memory_dir() -> Path:
    """Where the compaction summaries are stored."""
    return runtime_home() / "memory"


def session_memory_path() -> Path:
    """The file holding the most recent compaction summary.

    One file per run, overwritten by each new summary: the summary
    describes the current task, so an older one has no value.
    """
    return memory_dir() / "session_memory.md"


def trace_path(run_id: str) -> Path:
    """The trace file for one agent turn."""
    return traces_dir() / f"{run_id}.jsonl"


__all__ = [
    "DEFAULT_HOME_DIRNAME", "memory_dir", "runtime_home", "session_memory_path",
    "sessions_dir", "trace_path", "traces_dir",
]
