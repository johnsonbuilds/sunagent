"""FastAPI application that serves the SunAgent web UI.

The web UI is a channel, not a second runtime: the server owns the
session bookkeeping and the HTTP plumbing, while
:class:`~agent_runtime.channels.webui.WebUIChannel` owns the
translation of agent events into browser frames. The two meet at a
single injected sink that forwards frames to the session's frame bus.

Endpoints:

    GET    /                          the single page app
    GET    /api/health                liveness probe
    GET    /api/config                model, harness, and tool configuration
    GET    /api/sessions              list sessions, newest first
    POST   /api/sessions              create a session
    GET    /api/sessions/{id}         one session with its transcript
    PATCH  /api/sessions/{id}         rename a session
    DELETE /api/sessions/{id}         delete a session
    POST   /api/sessions/{id}/messages  send a message, start a turn
    POST   /api/sessions/{id}/cancel    stop the running turn
    GET    /api/sessions/{id}/events    SSE stream of frames

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
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from dotenv import load_dotenv

from agent_runtime import storage
from agent_runtime.agent import RunTrace, run_turn, use_streaming
from agent_runtime.channels.webui import WebUIChannel
from agent_runtime.events import EventEmitter
from agent_runtime.harness import HarnessError, resolve_harness
from agent_runtime.providers import OpenAICompatibleLLM
from agent_runtime.tools import create_default_registry

from .auth import BasicAuthMiddleware, credentials_from_env
from .sessions import STREAM_STOP, Session, SessionStore


logger = logging.getLogger(__name__)

STATIC_ROOT = Path(__file__).resolve().parent / "static"

_KEEPALIVE_SECONDS = 15.0

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


class RenameRequest(BaseModel):
    title: str = Field(default="", max_length=200)


class CreateSessionRequest(BaseModel):
    title: str = Field(default="", max_length=200)


class AgentRunner:
    """The LLM, tools, and harness shared by every web UI session.

    The provider is created lazily so the page still loads (and can
    report a helpful error) when no API key is configured.
    """

    def __init__(self) -> None:
        self.harness = self._resolve_harness()
        self.tools = create_default_registry(enabled=list(self.harness.tools.enabled))
        self.stream = use_streaming()
        self._llm: OpenAICompatibleLLM | None = None
        self._llm_error: str | None = None

    @staticmethod
    def _resolve_harness():
        try:
            return resolve_harness(os.getenv("AGENT_RUNTIME_HARNESS"))
        except HarnessError as exc:
            logger.warning("webui.harness.invalid error=%s", exc)
            return resolve_harness(None)

    @property
    def llm(self) -> OpenAICompatibleLLM:
        if self._llm is None:
            try:
                self._llm = OpenAICompatibleLLM()
                self._llm_error = None
            except Exception as exc:
                self._llm_error = str(exc)
                raise
        return self._llm

    def status(self) -> dict[str, Any]:
        model = os.getenv("MODEL_ID", "")
        if not self._llm_error and self._llm is not None:
            model = self._llm.model
        return {
            "model": model,
            "harness": self.harness.id,
            "stream": self.stream,
            "tools": list(self.harness.tools.enabled),
            "error": self._llm_error,
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
    load_dotenv()
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

    if STATIC_ROOT.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_ROOT)),
                  name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_ROOT / "index.html")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "sessions": len(sessions.list())}

    @app.get("/api/config")
    async def config() -> dict[str, Any]:
        return runner.status()

    @app.get("/api/sessions")
    async def list_sessions() -> list[dict[str, Any]]:
        return [session.summary() for session in sessions.list()]

    @app.post("/api/sessions", status_code=201)
    async def create_session(
            payload: CreateSessionRequest | None = None) -> dict[str, Any]:
        title = payload.title if payload is not None else None
        session = sessions.create(title or None)
        return session.summary()

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        session = _require_session(sessions, session_id)
        return {"id": session.id, "title": session.title,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "segments": session.segments,
                "busy": session.busy}

    @app.patch("/api/sessions/{session_id}")
    async def rename_session(session_id: str,
                             payload: RenameRequest) -> dict[str, Any]:
        session = sessions.rename(session_id, payload.title)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        return session.summary()

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

        session.append_user_message(message)
        sessions.save(session)

        run_id = f"run-{uuid4().hex[:12]}"
        trace = RunTrace(run_id=run_id,
                         output_path=_trace_path(run_id),
                         harness=runner.harness)
        events = EventEmitter(run_id=trace.run_id)
        channel = WebUIChannel(sink=_session_sink(session))
        events.subscribe(channel)
        session.busy = True

        task = asyncio.create_task(_execute(session, message, runner, trace,
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

    return app


async def _execute(session: Session, message: str, runner: AgentRunner,
                   trace: RunTrace, events: EventEmitter,
                   channel: WebUIChannel, store: SessionStore) -> None:
    """Run one agent turn for a session and report the outcome."""
    try:
        await run_turn(message, runner.llm, runner.tools,
                       conversation=session.conversation,
                       harness=runner.harness, stream=runner.stream,
                       trace=trace, events=events)
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
