"""FastAPI application that serves the SunAgent web UI.

The web UI is a channel, not a second runtime: the server owns the
session bookkeeping and the HTTP plumbing, while
:class:`~agent_runtime.channels.webui.WebUIChannel` owns the
translation of agent events into browser frames. The two meet at a
single injected sink that forwards frames to the session's frame bus.

Endpoints:

    GET    /                          the Next.js static export (index.html)
    GET    /api/health                liveness probe
    GET    /api/config                model catalog, harnesses, tool defaults
    GET    /api/sessions              list sessions, newest first
    POST   /api/sessions              create a session (model/harness optional)
    GET    /api/sessions/{id}         one session with its transcript
    PATCH  /api/sessions/{id}         rename, switch model, pick harness
    DELETE /api/sessions/{id}         delete a session
    POST   /api/sessions/{id}/messages  send a message, start a turn
    POST   /api/sessions/{id}/cancel    stop the running turn
    GET    /api/sessions/{id}/events    SSE stream of frames
    POST   /api/system/restart          restart the server via ./run.sh restart

Every route sits behind HTTP basic authentication when a user name and
a password are configured (see :mod:`webui.auth`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from dotenv import load_dotenv

from agent_runtime import storage
from agent_runtime.agent import RunTrace, run_turn
from agent_runtime.runtime_config import RuntimeConfig
from agent_runtime.channels.webui import WebUIChannel
from agent_runtime.events import EventEmitter
from agent_runtime.harness import (
    HarnessError,
    HarnessSpec,
    list_harnesses,
    resolve_harness,
)
from agent_runtime.model_catalog import (
    ModelCatalog,
    ModelCatalogError,
    load_model_catalog,
)

from .auth import BasicAuthMiddleware, credentials_from_env
from .sessions import STREAM_STOP, Session, SessionStore
from .turn_context import TurnSnapshot, resolve_turn


logger = logging.getLogger(__name__)

# ``npm run build`` in webui/frontend emits a static Next.js export here.
# FastAPI serves it, which keeps the page, the API, and the event stream
# on one origin (and therefore behind the same basic-auth challenge).
FRONTEND_ROOT = Path(__file__).resolve().parent / "frontend" / "out"

_BUILD_HINT = """<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SunAgent — frontend not built</title>
<style>
  body { margin: 0; min-height: 100vh; display: grid; place-items: center;
         background: #0a0908; color: #eae0d5;
         font: 15px/1.7 -apple-system, BlinkMacSystemFont, "Segoe UI",
               system-ui, sans-serif; }
  main { max-width: 560px; padding: 32px; }
  h1 { margin: 0 0 12px; font-size: 20px; }
  code { display: block; margin-top: 16px; padding: 12px 14px;
         border: 1px solid #2c2a27; border-radius: 8px;
         background: #15130f; color: #c6ac8f;
         font-family: ui-monospace, Menlo, Consolas, monospace; }
</style>
</head>
<body>
<main>
  <h1>SunAgent frontend is not built</h1>
  <p>The Next.js UI has not been generated yet. Build it, then reload:</p>
  <code>cd webui/frontend &amp;&amp; npm install &amp;&amp; npm run build</code>
</main>
</body>
</html>
"""

_KEEPALIVE_SECONDS = 15.0

# Delay before the detached restart command runs, giving the HTTP
# response time to reach the browser.
_RESTART_DELAY_SECONDS = 0.5

# Every application built in this process, so the shutdown path can
# reach the session store no matter how the app was started.
_APPS: "weakref.WeakSet[FastAPI]" = weakref.WeakSet()


def shutdown_active_sessions() -> None:
    """Close the event streams and stop the running turns of every app.

    Server-sent events never end on their own, and uvicorn waits for
    open connections before it finishes shutting down. Closing them
    here is what makes Ctrl+C return immediately.
    """
    for app in list(_APPS):
        store = getattr(app.state, "sessions", None)
        if store is not None:
            store.shutdown()


def _install_exit_handlers() -> None:
    """Close the event streams when the process is asked to stop.

    The handler runs before the server's own: it closes the streams and
    then hands the signal on, so the normal graceful shutdown still
    happens. Installing it from the application (rather than from the
    entry point) covers every way the server can be started.
    """
    previous: dict[int, Any] = {}

    def handle(signum: int, frame: Any) -> None:
        shutdown_active_sessions()
        handler = previous.get(signum)
        if handler not in (None, signal.SIG_DFL, signal.SIG_IGN):
            handler(signum, frame)
        elif signum == signal.SIGINT:
            raise KeyboardInterrupt

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, handle)
        except (OSError, ValueError):  # pragma: no cover - not the main thread
            previous.pop(signum, None)



class MessageRequest(BaseModel):
    message: str = Field(default="", max_length=100_000)


class PatchSessionRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    model: str | None = None
    harness: str | None = None


class CreateSessionRequest(BaseModel):
    title: str = Field(default="", max_length=200)
    model: str | None = None
    harness: str | None = None


class AgentRunner:
    """Defaults shared by every web UI session.

    The model catalog and the harness list are read once at startup.
    Per-turn objects are built by :func:`webui.turn_context.resolve_turn`
    when a turn starts, so a session can switch models without touching
    a turn that is already running.
    """

    def __init__(self, runtime: RuntimeConfig | None = None) -> None:
        self.runtime = runtime if runtime is not None else RuntimeConfig.from_env()
        self.catalog: ModelCatalog = load_model_catalog()
        self.harness: HarnessSpec = self._resolve_harness()
        listed = list_harnesses()
        if not any(spec.id == self.harness.id for spec in listed):
            # The default may come from a path outside ``harnesses/``; it
            # still belongs in the picker so the current value is visible.
            listed = [self.harness, *listed]
        self.harnesses: list[HarnessSpec] = listed
        self.stream = self.runtime.streaming

    @staticmethod
    def _resolve_harness() -> HarnessSpec:
        try:
            return resolve_harness(os.getenv("AGENT_RUNTIME_HARNESS"))
        except HarnessError as exc:
            logger.warning("webui.harness.invalid error=%s", exc)
            return resolve_harness(None)

    def status(self) -> dict[str, Any]:
        """The browser-safe startup view: ids and labels, never secrets."""
        default_entry = self.catalog.default_entry
        return {
            "model": default_entry.model_id,
            "harness": self.harness.id,
            "models": self.catalog.public(),
            "default_model": self.catalog.default,
            "harnesses": [{"id": spec.id} for spec in self.harnesses],
            "default_harness": self.harness.id,
            "stream": self.stream,
            "tools": list(self.harness.tools.enabled),
            "error": None,
        }


def _session_sink(session: Session) -> Callable[[dict[str, Any]], None]:
    """Build the sink that feeds one session's frame bus.

    Segment frames are the persistable form of a block, so they are
    appended to the transcript here, before the frame reaches the
    browser.
    """

    def sink(frame: dict[str, Any]) -> None:
        if frame.get("type") == "segment":
            segment = frame.get("segment")
            if isinstance(segment, dict):
                session.append_segment(segment)
        session.bus.publish(frame)

    return sink


def _sse(frame: dict[str, Any]) -> str:
    """Render one frame as a server-sent event."""
    payload = json.dumps(frame, ensure_ascii=False, default=str)
    return f"id: {frame.get('seq', 0)}\ndata: {payload}\n\n"


def create_app(store: SessionStore | None = None) -> FastAPI:
    """Build the web UI application."""
    # The CLI loads .env in its entry point. The server loads it here so
    # every launch path -- the console script, `python -m webui`, and
    # `uvicorn webui.server:create_app --factory` -- sees the same
    # provider configuration.
    load_dotenv(override=True)
    # The web UI can run shell commands, so it is worth a password.
    # Authentication is on when the user name and the password are
    # configured, and off -- with a warning -- when they are not.
    credentials = credentials_from_env()
    sessions = store if store is not None else SessionStore()
    sessions.load()
    runner = AgentRunner()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        _install_exit_handlers()
        yield
        # Backstop for the paths that do not run the entry point: the
        # streams are closed even if the server was stopped abruptly.
        shutdown_active_sessions()

    app = FastAPI(title="SunAgent Web UI", docs_url="/api/docs",
                  redoc_url=None, lifespan=lifespan)
    app.state.sessions = sessions
    app.state.runner = runner
    _APPS.add(app)

    if credentials is not None:
        # The password covers the pages, the API, and the event stream
        # alike.
        app.add_middleware(BasicAuthMiddleware, credentials=credentials)
        logger.info("webui.auth.enabled user=%s", credentials.username)
    else:
        logger.info("webui.auth.disabled")

    if (FRONTEND_ROOT / "_next").is_dir():
        app.mount("/_next", StaticFiles(directory=str(FRONTEND_ROOT / "_next")),
                  name="next-assets")

    @app.get("/", include_in_schema=False)
    async def index() -> Any:
        page = FRONTEND_ROOT / "index.html"
        if page.is_file():
            return FileResponse(page)
        return HTMLResponse(_BUILD_HINT, status_code=503)

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "sessions": len(sessions.list())}

    @app.get("/api/config")
    async def config() -> dict[str, Any]:
        return runner.status()

    @app.post("/api/system/restart")
    async def restart_system() -> dict[str, Any]:
        """Run ``./run.sh restart`` in the current directory.

        The task is detached from the server's process group and starts
        after a short delay, so this response reaches the browser before
        ``run.sh`` stops the server it is talking to.
        """
        script = Path.cwd() / "run.sh"
        if not script.is_file():
            raise HTTPException(status_code=500,
                                detail="run.sh not found in the current directory")

        try:
            # The detached shell outlives the server and waits briefly
            # so this response is flushed before run.sh stops it.
            await asyncio.create_subprocess_exec(
                "bash", "-c",
                f"sleep {_RESTART_DELAY_SECONDS}; exec ./run.sh restart",
                cwd=str(Path.cwd()),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:
            logger.exception("webui.restart.failed")
            raise HTTPException(status_code=500,
                                detail=f"could not run ./run.sh restart: {exc}")
        return {"ok": True, "command": "./run.sh restart"}

    @app.get("/api/sessions")
    async def list_sessions() -> list[dict[str, Any]]:
        return [session.summary() for session in sessions.list()]

    @app.post("/api/sessions", status_code=201)
    async def create_session(
            payload: CreateSessionRequest | None = None) -> dict[str, Any]:
        title = payload.title if payload is not None else None
        model = payload.model if payload is not None else None
        harness = payload.harness if payload is not None else None
        if model:
            _require_model(runner.catalog, model)
        if harness:
            _require_harness(harness)
        # New sessions always carry explicit ids, so the browser does not
        # have to guess the defaults and the first turn needs no backfill.
        session = sessions.create(
            title or None,
            model=model or runner.catalog.default,
            harness=harness or runner.harness.id,
        )
        return session.summary()

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        return _session_detail(_require_session(sessions, session_id))

    @app.patch("/api/sessions/{session_id}")
    async def patch_session(session_id: str,
                            payload: PatchSessionRequest) -> dict[str, Any]:
        session = _require_session(sessions, session_id)
        harness_changed = bool(
            payload.harness and payload.harness != session.harness)
        # Validate everything before mutating, so a rejected request
        # leaves the session untouched.
        if payload.model:
            _require_model(runner.catalog, payload.model)
        if harness_changed:
            # The harness owns the system prompt and tool set, so it is
            # only pickable before the first turn. The model can change
            # at any time; it takes effect on the next turn.
            if session.harness_locked:
                raise HTTPException(
                    status_code=409,
                    detail="harness is locked for this session")
            _require_harness(payload.harness or "")

        if payload.title is not None:
            session.title = payload.title.strip() or session.title
        if payload.model:
            session.model = payload.model
        if harness_changed:
            session.harness = payload.harness
        session.updated_at = time.time()
        sessions.save(session)
        return _session_detail(session)

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, Any]:
        if not sessions.delete(session_id):
            raise HTTPException(status_code=404, detail="session not found")
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/messages")
    async def send_message(session_id: str,
                           payload: MessageRequest) -> dict[str, Any]:
        session = _require_session(sessions, session_id)
        message = payload.message.strip()
        if not message:
            raise HTTPException(status_code=400, detail="message is empty")
        if session.busy:
            raise HTTPException(status_code=409, detail="session is busy")

        # Resolve the session's ids into an immutable snapshot before the
        # user message is recorded: a bad catalog entry is a 4xx, not a
        # dangling empty turn. The snapshot is all the turn will see.
        session.model = session.model or runner.catalog.default
        session.harness = session.harness or runner.harness.id
        snapshot = _resolve_snapshot(session, runner)

        session.append_user_message(message)
        sessions.save(session)

        run_id = f"run-{uuid4().hex[:12]}"
        trace = RunTrace(run_id=run_id,
                         output_path=_trace_path(run_id),
                         harness=snapshot.harness)
        events = EventEmitter(run_id=trace.run_id)
        channel = WebUIChannel(sink=_session_sink(session))
        events.subscribe(channel)
        session.busy = True

        task = asyncio.create_task(_execute(session, message, snapshot, trace,
                                             events, channel, sessions))
        session.task = task
        return {"run_id": trace.run_id}

    @app.post("/api/sessions/{session_id}/cancel")
    async def cancel_turn(session_id: str) -> dict[str, Any]:
        session = _require_session(sessions, session_id)
        task = session.task
        if task is None or task.done():
            return {"ok": False}
        task.cancel()
        return {"ok": True}

    @app.get("/api/sessions/{session_id}/events")
    async def session_events(session_id: str,
                             request: Request) -> StreamingResponse:
        session = _require_session(sessions, session_id)
        after = _last_event_id(request)
        queue = session.bus.subscribe()

        async def stream():
            try:
                for frame in session.bus.replay(after):
                    yield _sse(frame)
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        frame = await asyncio.wait_for(queue.get(),
                                                        timeout=_KEEPALIVE_SECONDS)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if frame is STREAM_STOP:
                        break
                    yield _sse(frame)
            finally:
                session.bus.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no",
                                          "Connection": "keep-alive"})

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend_file(path: str) -> FileResponse:
        """Serve remaining files from the static Next.js export.

        The export is a directory of plain files (``index.txt``,
        ``404.html``, icons and so on). Only paths that stay inside the
        export root are served.
        """
        root = FRONTEND_ROOT.resolve()
        candidate = (root / path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            raise HTTPException(status_code=404, detail="not found") from None
        if candidate.is_file():
            return FileResponse(candidate)
        raise HTTPException(status_code=404, detail="not found")

    return app


async def _execute(session: Session, message: str, snapshot: TurnSnapshot,
                   trace: RunTrace, events: EventEmitter,
                   channel: WebUIChannel, store: SessionStore) -> None:
    """Run one agent turn for a session and report the outcome.

    ``snapshot`` is captured when the turn starts; nothing here reads the
    session's current model or harness, so a switch made while the turn
    is running cannot affect it.
    """
    try:
        await run_turn(message, snapshot.llm, snapshot.tools,
                       conversation=session.conversation,
                       harness=snapshot.harness, stream=snapshot.stream,
                       trace=trace, events=events, runtime=snapshot.runtime,
                       summarizer_factory=snapshot.summarizer_factory)
    except asyncio.CancelledError:
        session.bus.publish({"type": "run.error", "stage": "cancelled",
                             "error": "run cancelled"})
        raise
    except Exception as exc:
        logger.exception("webui.run.failed session=%s", session.id)
        session.bus.publish({"type": "run.error", "stage": "runtime",
                             "error": str(exc)})
    finally:
        channel.close()
        events.close()
        session.busy = False
        session.updated_at = time.time()
        session.task = None
        store.save(session)


def _session_detail(session: Session) -> dict[str, Any]:
    return {**session.summary(), "segments": session.segments}


def _require_model(catalog: ModelCatalog, model_id: str) -> None:
    try:
        catalog.require(model_id)
    except ModelCatalogError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _require_harness(harness_id: str) -> HarnessSpec:
    try:
        return resolve_harness(harness_id)
    except HarnessError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _resolve_snapshot(session: Session, runner: AgentRunner) -> TurnSnapshot:
    try:
        return resolve_turn(catalog=runner.catalog, model_id=session.model,
                            harness_id=session.harness,
                            base_runtime=runner.runtime)
    except (ModelCatalogError, HarnessError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # The exception text may carry endpoint details; log only the
        # type and keep the browser message generic.
        logger.warning("webui.snapshot.failed session=%s model=%s error=%s",
                       session.id, session.model, type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="could not create the model client; check model.json",
        ) from None


def _require_session(store: SessionStore, session_id: str) -> Session:
    session = store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


def _last_event_id(request: Request) -> int:
    raw = request.headers.get("last-event-id") or request.query_params.get("after")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _trace_path(run_id: str) -> Path:
    """Where the trace of one turn is written.

    Traces are a sibling of the sessions, not a child of them: both
    live under the runtime home (see :mod:`agent_runtime.storage`).
    """
    return storage.trace_path(run_id)


__all__ = ["AgentRunner", "create_app"]
