"""Run the SunAgent web UI: ``uv run webui``."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

from dotenv import load_dotenv

from .auth import DEFAULT_USER, PASSWORD_ENV, USER_ENV, auth_warning
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
    parser.add_argument("--harness", default=os.getenv("AGENT_RUNTIME_HARNESS"),
                        help="harness manifest to run (file path, id under "
                             "harnesses/, or omit for the default harness; "
                             "also set via AGENT_RUNTIME_HARNESS)")
    args = parser.parse_args()

    if args.harness:
        # AgentRunner reads AGENT_RUNTIME_HARNESS from the environment, so
        # publish the CLI flag there. This also covers --reload, where the
        # reloader child builds the app itself and inherits the environment.
        os.environ["AGENT_RUNTIME_HARNESS"] = args.harness
        # Fail fast with a clear error instead of booting with a fallback.
        from agent_runtime.harness import HarnessError, resolve_harness
        try:
            resolve_harness(args.harness)
        except HarnessError as exc:
            parser.error(str(exc))

    try:
        import uvicorn  # noqa: F401
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise SystemExit("The web UI needs uvicorn: pip install fastapi uvicorn") from exc

    # An open web UI is an open shell. Say so, without stopping the
    # server: the web UI is a local tool first, and the password is
    # there for the day it is reachable from somewhere else.
    load_dotenv(override=True)
    warning = auth_warning()
    if warning is None:
        print("webui: authentication enabled")
    else:
        print(f"webui: no authentication ({warning}). Anyone who can reach "
              f"this port can run the agent. Set {USER_ENV}={DEFAULT_USER} "
              f"and {PASSWORD_ENV} in .env to lock it down.",
              file=sys.stderr)

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
