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
    GET    /api/board                   the task board snapshot
    POST   /api/board/tasks             create a task
    PATCH  /api/board/tasks/{id}        edit or move a task
    DELETE /api/board/tasks/{id}        delete a task
    POST   /api/board/tasks/{id}/session  create or reuse its conversation
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
from .board import (
    BoardNotFoundError,
    BoardStorageError,
    BoardStore,
    BoardValidationError,
)
from .executor import BoardExecutor, TurnOutcome
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
        executor = getattr(app.state, "executor", None)
        if executor is not None:
            executor.request_stop()
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


class CreateTaskRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=20_000)
    status: str = "backlog"
    priority: str = "normal"
    tags: list[str] = Field(default_factory=list)
    model: str | None = Field(default=None, max_length=200)
    harness: str | None = Field(default=None, max_length=200)


class PatchTaskRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=20_000)
    status: str | None = None
    position: int | None = Field(default=None, ge=0)
    priority: str | None = None
    tags: list[str] | None = None
    model: str | None = Field(default=None, max_length=200)
    harness: str | None = Field(default=None, max_length=200)


class ExecutorStateRequest(BaseModel):
    paused: bool


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


def create_app(store: SessionStore | None = None,
               board: BoardStore | None = None, *,
               enable_executor: bool = True) -> FastAPI:
    """Build the web UI application.

    ``enable_executor`` is for tests: the API and board routes are fully
    wired, but the serial worker is not started, so a test can inspect
    Todo transitions without launching a real model turn.
    """
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
    tasks = board if board is not None else BoardStore()
    tasks.load()
    try:
        tasks.reconcile(sessions)
    except BoardStorageError:
        # A read-only board must not stop the chat UI from starting.
        logger.warning("webui.board.reconcile_failed")
    try:
        tasks.recover_interrupted()
    except BoardStorageError:
        logger.warning("webui.board.recover_failed")
    runner = AgentRunner()

    def ensure_board_session(task_id: str) -> str:
        session_id, _ = tasks.ensure_session(
            task_id, sessions,
            model=runner.catalog.default,
            harness=runner.harness.id,
        )
        return session_id

    async def run_board_task(session: Session, message: str) -> TurnOutcome:
        if session.busy:
            return TurnOutcome("busy", error="Session is busy")
        session.model = session.model or runner.catalog.default
        session.harness = session.harness or runner.harness.id
        try:
            snapshot = _resolve_snapshot(session, runner)
            trace, events, channel, _ = _prepare_turn(
                session, message, snapshot, sessions)
        except HTTPException as exc:
            return TurnOutcome("error", error=str(exc.detail))
        except Exception as exc:
            logger.exception("webui.board.turn_prepare_failed session=%s",
                             session.id)
            return TurnOutcome("error",
                               error=str(exc) or "could not start the run")
        return await _execute_turn(session, message, snapshot, trace, events,
                                   channel, sessions)

    executor = BoardExecutor(
        tasks, sessions,
        ensure_session=ensure_board_session,
        run_turn=run_board_task,
    )

    def board_snapshot() -> dict[str, Any]:
        return {**tasks.snapshot(sessions), "executor": executor.status()}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        _install_exit_handlers()
        if enable_executor:
            executor.start()
        yield
        # Stop the worker before the session stores close: this cancels
        # the current turn and puts an interrupted task back in Todo.
        await executor.stop()
        # Backstop for the paths that do not run the entry point: the
        # streams are closed even if the server was stopped abruptly.
        shutdown_active_sessions()

    app = FastAPI(title="SunAgent Web UI", docs_url="/api/docs",
                  redoc_url=None, lifespan=lifespan)
    app.state.sessions = sessions
    app.state.board = tasks
    app.state.executor = executor
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
        return {"ok": True, "sessions": len(sessions.list()),
                "tasks": len(tasks.list_all()),
                "executor": executor.status()}

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
        session = sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        executor.cancel_session(session_id)
        await _cancel_session_turn(session)
        if not sessions.delete(session_id):
            raise HTTPException(status_code=404, detail="session not found")
        try:
            tasks.unlink_session(session_id)
        except BoardStorageError:
            # The conversation is already gone; a stale link is better
            # than failing the delete the user asked for.
            logger.warning("webui.board.unlink_failed session=%s", session_id)
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

        trace, events, channel, run_id = _prepare_turn(
            session, message, snapshot, sessions)
        task = asyncio.create_task(
            _execute_turn(session, message, snapshot, trace, events,
                          channel, sessions))
        session.task = task
        # A queued board task may be waiting for this conversation to
        # become idle; let the executor re-check once the turn ends.
        task.add_done_callback(lambda _: executor.wake())
        return {"run_id": run_id}

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

    @app.get("/api/board")
    async def get_board() -> dict[str, Any]:
        return board_snapshot()

    @app.patch("/api/board/executor")
    async def set_executor(payload: ExecutorStateRequest) -> dict[str, Any]:
        executor.set_paused(payload.paused)
        return board_snapshot()

    @app.post("/api/board/tasks", status_code=201)
    async def create_task(payload: CreateTaskRequest) -> dict[str, Any]:
        if payload.model:
            _require_model(runner.catalog, payload.model)
        if payload.harness:
            _require_harness(payload.harness)
        try:
            tasks.create(payload.title, description=payload.description,
                         status=payload.status, priority=payload.priority,
                         tags=payload.tags,
                         model=payload.model, harness=payload.harness)
        except (BoardValidationError, BoardStorageError) as exc:
            raise _board_http_error(exc) from exc
        if payload.status == "todo":
            executor.wake()
        return board_snapshot()

    @app.patch("/api/board/tasks/{task_id}")
    async def patch_task(task_id: str,
                         payload: PatchTaskRequest) -> dict[str, Any]:
        before = tasks.get(task_id)
        if before is None:
            raise HTTPException(status_code=404, detail="task not found")
        # ``exclude_unset`` keeps "field omitted" distinct from "field set
        # to null"; null is dropped so it can never erase a real value.
        patch = {key: value for key, value in
                 payload.model_dump(exclude_unset=True).items()
                 if value is not None}
        if patch.get("model"):
            _require_model(runner.catalog, patch["model"])
        if patch.get("harness"):
            _require_harness(patch["harness"])

        linked = sessions.get(before.session_id) if before.session_id else None
        harness_edit = ("harness" in patch
                        and patch["harness"] != before.harness)
        if linked is not None and harness_edit and linked.harness_locked:
            raise HTTPException(
                status_code=409,
                detail="harness is locked for this task's conversation")

        try:
            tasks.update(task_id, patch)
        except (BoardNotFoundError, BoardValidationError,
                BoardStorageError) as exc:
            raise _board_http_error(exc) from exc

        after = tasks.get(task_id)
        assert after is not None
        if linked is not None:
            # Keep the card and its conversation in agreement. The model
            # may change at any time, including back to the default; the
            # harness only while the conversation is still empty.
            changed = False
            if "model" in patch and linked.model != after.model:
                linked.model = after.model
                changed = True
            if ("harness" in patch and not linked.harness_locked
                    and linked.harness != after.harness):
                linked.harness = after.harness
                changed = True
            if changed:
                linked.updated_at = time.time()
                sessions.save(linked)
        if before.status == "doing" and after.status != "doing":
            # The human changed the card while the executor owned it:
            # cancel the run; finish_run will not overwrite their status.
            executor.cancel_task(task_id)
        if after.status == "todo":
            executor.wake()
        return board_snapshot()

    @app.delete("/api/board/tasks/{task_id}")
    async def delete_task(task_id: str,
                          delete_session: bool = False) -> dict[str, Any]:
        task = tasks.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        if task.status == "doing":
            executor.cancel_task(task_id)
        deleted_session = False
        if delete_session and task.session_id:
            executor.cancel_session(task.session_id)
            session = sessions.get(task.session_id)
            if session is not None:
                await _cancel_session_turn(session)
            deleted_session = sessions.delete(task.session_id)
        try:
            tasks.delete(task_id)
        except BoardStorageError as exc:
            raise _board_http_error(exc) from exc
        return {"board": board_snapshot(),
                "deleted_session": deleted_session}

    @app.post("/api/board/tasks/{task_id}/session")
    async def create_task_session(task_id: str) -> dict[str, Any]:
        try:
            session_id, created = tasks.ensure_session(
                task_id, sessions,
                model=runner.catalog.default,
                harness=runner.harness.id,
            )
        except (BoardNotFoundError, BoardValidationError,
                BoardStorageError) as exc:
            raise _board_http_error(exc) from exc
        return {"board": board_snapshot(),
                "session_id": session_id, "created": created}

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


def _prepare_turn(session: Session, message: str, snapshot: TurnSnapshot,
                  store: SessionStore
                  ) -> tuple[RunTrace, EventEmitter, WebUIChannel, str]:
    """Record the user message and build one turn's I/O objects."""
    run_id = f"run-{uuid4().hex[:12]}"
    trace = RunTrace(run_id=run_id,
                     output_path=_trace_path(run_id),
                     harness=snapshot.harness)
    events = EventEmitter(run_id=trace.run_id)
    channel = WebUIChannel(sink=_session_sink(session))
    events.subscribe(channel)

    session.busy = True
    session.append_user_message(message)
    store.save(session)
    return trace, events, channel, trace.run_id


async def _execute_turn(session: Session, message: str,
                        snapshot: TurnSnapshot, trace: RunTrace,
                        events: EventEmitter, channel: WebUIChannel,
                        store: SessionStore) -> TurnOutcome:
    """Run one agent turn and report whether it completed cleanly.

    ``snapshot`` is captured when the turn starts; nothing here reads the
    session's current model or harness, so a switch made while the turn
    is running cannot affect it.

    ``AgentTurn`` serialises some runtime failures into the returned
    text, so the event stream is watched for ``runtime.error`` as well as
    the usual exception path.  Without that, a failed task would look
    completed and move to Review.
    """
    errors: list[str] = []

    def collect_error(event: Any) -> None:
        if event.event_type == "runtime.error":
            errors.append(str(event.data.get("error") or "run failed"))

    unsubscribe = events.subscribe(collect_error)
    try:
        answer = await run_turn(message, snapshot.llm, snapshot.tools,
                                conversation=session.conversation,
                                harness=snapshot.harness,
                                stream=snapshot.stream,
                                trace=trace, events=events,
                                runtime=snapshot.runtime,
                                summarizer_factory=snapshot.summarizer_factory)
    except asyncio.CancelledError:
        session.bus.publish({"type": "run.error", "stage": "cancelled",
                             "error": "run cancelled"})
        raise
    except Exception as exc:
        logger.exception("webui.run.failed session=%s", session.id)
        session.bus.publish({"type": "run.error", "stage": "runtime",
                             "error": str(exc)})
        return TurnOutcome("error", trace.run_id,
                           error=str(exc) or "run failed")
    else:
        if errors:
            return TurnOutcome("error", trace.run_id, answer=answer,
                               error=errors[-1])
        return TurnOutcome("completed", trace.run_id, answer=answer)
    finally:
        unsubscribe()
        channel.close()
        events.close()
        session.busy = False
        session.updated_at = time.time()
        session.task = None
        store.save(session)


async def _cancel_session_turn(session: Session) -> None:
    """Cancel and join a running turn before deleting its session."""
    task = session.task
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # pragma: no cover - the turn already logs failures
        logger.exception("webui.session.cancel_failed session=%s", session.id)


def _session_detail(session: Session) -> dict[str, Any]:
    return {**session.summary(), "segments": session.segments}


def _board_http_error(exc: Exception) -> HTTPException:
    """Map a board failure to the response the browser should see."""
    if isinstance(exc, BoardNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, BoardValidationError):
        return HTTPException(status_code=400, detail=str(exc))
    logger.exception("webui.board.failed")
    return HTTPException(status_code=500, detail="could not save the board")


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
