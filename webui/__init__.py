"""SunAgent web UI: a browser front end for the agent runtime.

The web UI is a channel: it consumes the same user-facing agent events
as the CLI and renders them in a browser. This package holds the HTTP
server, the session store, and the static assets; the event-to-frame
translation lives in :mod:`agent_runtime.channels.webui`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


if importlib.util.find_spec("agent_runtime") is None:  # pragma: no cover
    # Running from a source tree where the package is not installed.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from .sessions import SessionStore


__all__ = ["SessionStore", "create_app"]


def __getattr__(name: str):
    """Expose ``create_app`` lazily: building the app needs FastAPI."""
    if name == "create_app":
        from .server import create_app
        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
