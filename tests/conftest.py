"""Shared test isolation.

The runtime home is process-wide state resolved from the environment, so
any test that reaches storage through the real code path (a board store
without an explicit root, ``finalize_run``, the web UI turn executor)
would otherwise append to the developer's ``workspace/`` tree.  Pointing
``AGENT_RUNTIME_HOME`` at a per-test temporary directory keeps runs,
sessions, traces and ``metrics/runs.jsonl`` inside the sandbox.

Tests that assert on the home itself still patch or clear the variable
locally, which overrides this fixture for their own scope.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

ENV_HOME = "AGENT_RUNTIME_HOME"


@pytest.fixture(autouse=True)
def isolated_runtime_home(tmp_path, monkeypatch):
    """Every test gets its own empty runtime home."""
    home = tmp_path / "runtime-home"
    monkeypatch.setenv(ENV_HOME, str(home))
    # A model endpoint or harness id leaking in from the developer's shell
    # would make unit tests depend on the machine they run on.
    for name in ("AGENT_RUNTIME_HARNESS", "AGENT_RUNTIME_WORKSPACE"):
        if name in os.environ:
            monkeypatch.delenv(name, raising=False)
    # ``run_turn``/``create_agent_turn`` now default to ``runtime.streaming``
    # (True).  Most tests use chat-only fakes, so keep the suite on the
    # non-streaming path unless a test explicitly asks for streaming.
    monkeypatch.setenv("AGENT_RUNTIME_STREAM", "0")
    return home
