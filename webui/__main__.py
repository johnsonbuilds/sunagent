"""Run the SunAgent web UI: ``python -m webui``."""

from __future__ import annotations

import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m webui",
        description="Serve the SunAgent web UI.")
    parser.add_argument("--host", default=os.getenv("AGENT_RUNTIME_WEBUI_HOST", "127.0.0.1"),
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
        import uvicorn
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise SystemExit("The web UI needs uvicorn: pip install fastapi uvicorn") from exc

    from .server import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port,
                log_level=args.log_level, reload=args.reload)


if __name__ == "__main__":
    main()
