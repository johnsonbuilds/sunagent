"""One place that decides where the runtime keeps what it writes.

Everything the runtime persists lives under one root (``<home>/``):

    sessions/   one JSON document per browser conversation
    traces/     one JSONL file per agent turn
    metrics/    one JSONL index of run-level measurements
    diagnosis/  one evidence bundle per diagnosis (bundle.json + summary.md)
    memory/     persistent cross-session memory (AGENT_RUNTIME_MEMORY)
    board.json  the single web UI task board
    board_events.jsonl  board lifecycle events (append-only history)

``<home>`` is ``AGENT_RUNTIME_HOME`` when set, and ``workspace/`` next to the
project root otherwise.  Persistent memory sits here rather than in the
sandbox because it must survive a change of sandbox; the model is still told
a sandbox-relative path, because the file tools refuse anything outside it.

Not owned here: the agent's sandbox (``AGENT_RUNTIME_WORKSPACE`` with its
``.outputs/`` spills and ``.scripts/``) and the versioned research artefacts
(``harnesses/``, ``evaluation/records/``) — the sandbox is part of the
model's contract, the artefacts are meant to be committed and compared.
"""

from __future__ import annotations

import os
from pathlib import Path


# The project root, i.e. the directory that holds ``src/`` and ``webui/``.
# Anchored on this module so the default root ignores the process cwd.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_HOME_DIRNAME = "workspace"


def runtime_home() -> Path:
    """The root directory of everything the runtime persists.

    ``AGENT_RUNTIME_HOME`` overrides it; the default is ``workspace/`` inside
    the project.  Read per call so the environment can change at runtime.
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


def trace_path(run_id: str) -> Path:
    """The trace file for one agent turn."""
    return traces_dir() / f"{run_id}.jsonl"


def board_path() -> Path:
    """Where the web UI task board is stored."""
    return runtime_home() / "board.json"


def board_events_path() -> Path:
    """Append-only stream of board lifecycle events (never diagnosis)."""
    return runtime_home() / "board_events.jsonl"


def metrics_dir() -> Path:
    """Where run-level measurement records are stored."""
    return runtime_home() / "metrics"


def diagnosis_dir() -> Path:
    """Evidence bundles written for model-driven diagnosis.

    They live in the runtime home, not the agent sandbox: the diagnosis tools
    read them directly, and a narrowed sandbox must not hide or rewrite them.
    """
    return runtime_home() / "diagnosis"


def metrics_path() -> Path:
    """The append-only index of one metrics record per run."""
    return metrics_dir() / "runs.jsonl"


__all__ = [
    "DEFAULT_HOME_DIRNAME", "runtime_home",
    "board_path", "board_events_path", "diagnosis_dir", "metrics_dir",
    "metrics_path",
    "sessions_dir", "trace_path", "traces_dir",
]
