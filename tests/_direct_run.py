"""Keep a direct ``python tests/xxx.py`` run out of the developer's home.

``pytest`` loads ``conftest.py``, which points ``AGENT_RUNTIME_HOME`` at a
per-test temporary directory.  Running one test file directly skips that,
so a board store without an explicit root, a turn executor, or
``finalize_run`` would append to the real ``workspace/`` tree.  The
``__main__`` block of every test file calls :func:`isolate_runtime_home`
so both ways of running the suite write to a throwaway directory.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile


ENV_HOME = "AGENT_RUNTIME_HOME"

# Shell variables that would make a unit test depend on the machine.
ENV_CLEARED = ("AGENT_RUNTIME_HARNESS", "AGENT_RUNTIME_WORKSPACE")


def isolate_runtime_home() -> str:
    """Point the runtime home at a fresh temporary directory."""
    home = tempfile.mkdtemp(prefix="sunagent-tests-")
    atexit.register(shutil.rmtree, home, True)
    os.environ[ENV_HOME] = home
    for name in ENV_CLEARED:
        os.environ.pop(name, None)
    return home


__all__ = ["ENV_CLEARED", "ENV_HOME", "isolate_runtime_home"]
