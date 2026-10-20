"""Run the SunAgent web UI: ``uv run webui``."""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any

from dotenv import load_dotenv

from .auth import AuthNotConfigured, require_credentials
from .server import create_app


# Uvicorn waits for open connections before it gives up on a graceful
# shutdown. The event streams never end on their own, so the
# application closes them when the process is asked to stop; this
# timeout is only the backstop for the paths that do not run the app.
_GRACEFUL_TIMEOUT = 10


async def _serve(app: Any, host: str, port: int, log_level: str,
                 reload: bool) -> None:
    import uvicorn

    if reload:
        # Reload needs an import string: the child process builds the
        # app itself and installs its own exit handlers.
        config = uvicorn.Config("webui.server:create_app", factory=True,
                                host=host, port=port, log_level=log_level,
                                timeout_graceful_shutdown=_GRACEFUL_TIMEOUT)
    else:
        config = uvicorn.Config(app, host=host, port=port,
                                log_level=log_level,
                                timeout_graceful_shutdown=_GRACEFUL_TIMEOUT)
    await uvicorn.Server(config).serve()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="uv run webui",
        description="Serve the SunAgent web UI.")
    parser.add_argument("--host",
                        default=os.getenv("AGENT_RUNTIME_WEBUI_HOST", "127.0.0.1"),
                        help="interface to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int,
                        default=int(os.getenv("AGENT_RUNTIME_WEBUI_PORT", "8000")),
                        help="port to bind (default: 8000)")
    parser.add_argument("--reload", action="store_true",
                        help="restart the server when the source changes")
    parser.add_argument("--log-level", default="info",
                        help="uvicorn log level (default: info)")
    args = parser.parse_args()

    try:
        import uvicorn  # noqa: F401
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise SystemExit("The web UI needs uvicorn: pip install fastapi uvicorn") from exc

    # The web UI runs shell commands, so it never starts open. The
    # password is checked here too, so that --reload fails with the
    # same message instead of a traceback in the child process.
    load_dotenv()
    try:
        credentials = require_credentials()
    except AuthNotConfigured as exc:
        raise SystemExit(str(exc)) from exc
    print(f"webui: authentication enabled (user={credentials.username})")

    # With --reload the child process builds the app from the import
    # string, so the parent does not need one.
    app = None if args.reload else create_app()

    try:
        asyncio.run(_serve(app, args.host, args.port, args.log_level,
                           args.reload))
    except KeyboardInterrupt:
        # Uvicorn re-raises the interrupt once it has finished shutting
        # down. The server is already stopped, so exit quietly instead
        # of printing a traceback.
        raise SystemExit(130)


if __name__ == "__main__":
    main()
