"""FastAPI application that serves the SunAgent web UI.

The server owns session bookkeeping and HTTP plumbing; the agent
channel translates runtime events into browser frames. Every route sits
behind HTTP basic authentication when credentials are configured.
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

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool

from dotenv import load_dotenv

from agent_runtime import storage
from agent_runtime.agent import RunTrace, run_turn
from agent_runtime.diagnosis import delete_bundle
from agent_runtime.runtime_config import RuntimeConfig
from agent_runtime.channels.webui import WebUIChannel
from agent_runtime.events import EventEmitter
from agent_runtime.metrics import afinalize_run
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
from .evaluation import (
    DEFAULT_RANGE,
    DIAGNOSIS_ORIGINS,
    DiagnosisPlan,
    EvaluationError,
    EvaluationNotFound,
    EvaluationStore,
)
from .executor import BoardExecutor, RunRequest, TurnOutcome
from .sessions import STREAM_STOP, Session, SessionStore
from .turn_context import TurnSnapshot, resolve_turn
from .workspace import WorkspaceBrowser, WorkspaceError


logger = logging.getLogger(__name__)

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

_RESTART_DELAY_SECONDS = 0.5

_APPS: "weakref.WeakSet[FastAPI]" = weakref.WeakSet()


def shutdown_active_sessions() -> None:
    """Close event streams and stop running turns of every app."""
    for app in list(_APPS):
        executor = getattr(app.state, "executor", None)
        if executor is not None:
            executor.request_stop()
        store = getattr(app.state, "sessions", None)
        if store is not None:
            store.shutdown()


def _install_exit_handlers() -> None:
    """Close event streams when the process is asked to stop."""
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


class CommentRequest(BaseModel):
    body: str = Field(default="", max_length=10_000)


class PatchSessionRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    model: str | None = None
    harness: str | None = None


class CreateSessionRequest(BaseModel):
    title: str = Field(default="", max_length=200)
    model: str | None = None
    harness: str | None = None


class BranchSessionRequest(BaseModel):
    """Where to fork a conversation: one assistant block."""

    segment_id: str = Field(min_length=1, max_length=200)
    title: str | None = Field(default=None, max_length=200)


class CreateTaskRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=20_000)
    status: str = "backlog"
    priority: str = "normal"
    tags: list[str] = Field(default_factory=list)
    model: str | None = Field(default=None, max_length=200)
    harness: str | None = Field(default=None, max_length=200)
    # ``null`` lets the harness decide; an explicit value wins.  Strict:
    # a string boolean would silently flip whether a run is judged on
    # producing files at all.
    expects_artifact: StrictBool | None = None


class PatchTaskRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=20_000)
    status: str | None = None
    position: int | None = Field(default=None, ge=0)
    priority: str | None = None
    tags: list[str] | None = None
    model: str | None = Field(default=None, max_length=200)
    harness: str | None = Field(default=None, max_length=200)
    expects_artifact: StrictBool | None = None


class ExecutorStateRequest(BaseModel):
    paused: bool


class DiagnoseRequest(BaseModel):
    """A diagnosis of one task, run, or cohort.

    ``target`` is validated by the evaluation store, not here: the shapes
    differ (``id`` vs ``filters``) and the store owns the rule.
    """

    target: dict[str, Any]
    force: bool = False


class AgentRunner:
    """Defaults shared by every web UI session."""

    def __init__(self, runtime: RuntimeConfig | None = None) -> None:
        self.runtime = runtime if runtime is not None else RuntimeConfig.from_env()
        self.catalog: ModelCatalog = load_model_catalog()
        self.harness: HarnessSpec = self._resolve_harness()
        # ``internal: true`` harnesses (the diagnosis one) still resolve by
        # id, but never show up in the picker or in ``/api/config``.
        listed = list_harnesses(include_internal=False)
        if not self.harness.internal and not any(
                spec.id == self.harness.id for spec in listed):
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
        """Browser-safe startup view: ids and labels, never secrets."""
        default_entry = self.catalog.default_entry
        return {
            "model": default_entry.model_id,
            "harness": self.harness.id,
            "models": self.catalog.public(),
            "default_model": self.catalog.default,
            # ``mutation`` is what the picker's info icon shows: the one
            # gene change that produced this harness from its parent.
            "harnesses": [{"id": spec.id, "mutation": spec.mutation,
                           "reason": spec.reason}
                          for spec in self.harnesses],
            "default_harness": self.harness.id,
            "stream": self.stream,
            "tools": list(self.harness.tools.enabled),
            "error": None,
        }


def _session_sink(session: Session) -> Callable[[dict[str, Any]], None]:
    """Build the sink that feeds one session's frame bus."""

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
               workspace: WorkspaceBrowser | None = None,
               enable_executor: bool = True) -> FastAPI:
    """Build the web UI FastAPI app."""
    load_dotenv(override=True)
    credentials = credentials_from_env()
    sessions = store if store is not None else SessionStore()
    sessions.load()
    tasks = board if board is not None else BoardStore()
    tasks.load()
    browser = workspace if workspace is not None else WorkspaceBrowser()
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

    async def run_board_task(session: Session, message: str,
                             run: RunRequest) -> TurnOutcome:
        if session.busy:
            return TurnOutcome("busy", error="Session is busy")
        session.model = session.model or runner.catalog.default
        session.harness = session.harness or runner.harness.id
        try:
            snapshot = _resolve_snapshot(session, runner)
            trace, events, channel, _ = _prepare_turn(
                session, message, snapshot, sessions,
                run_id=run.run_id, task_id=run.task_id,
                run_index=run.run_index)
        except HTTPException as exc:
            return TurnOutcome("error", error=str(exc.detail))
        except Exception as exc:
            logger.exception("webui.board.turn_prepare_failed session=%s",
                             session.id)
            return TurnOutcome("error",
                               error=str(exc) or "could not start the run")
        return await _execute_turn(session, message, snapshot, trace, events,
                                   channel, sessions, run=run)

    executor = BoardExecutor(
        tasks, sessions,
        ensure_session=ensure_board_session,
        run_turn=run_board_task,
    )

    # The evaluation index reads the run metrics, the board event stream,
    # the session store, and the benchmark records; it is refreshed
    # lazily on the first query after a write.
    evaluation = EvaluationStore(sessions, tasks)

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
    app.state.workspace = browser
    app.state.evaluation = evaluation
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

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
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
        """Run ``./run.sh restart`` in the current directory."""
        script = Path.cwd() / "run.sh"
        if not script.is_file():
            raise HTTPException(status_code=500,
                                detail="run.sh not found in the current directory")

        try:
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
        if payload.model:
            _require_model(runner.catalog, payload.model)
        if harness_changed:
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
            logger.warning("webui.board.unlink_failed session=%s", session_id)
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/branch", status_code=201)
    async def branch_session(session_id: str,
                             payload: BranchSessionRequest) -> dict[str, Any]:
        """Copy a conversation up to one of its answers into a new session."""
        session = _require_session(sessions, session_id)
        if session.busy:
            raise HTTPException(status_code=409, detail="session is busy")
        child = sessions.branch(session, payload.segment_id,
                                title=payload.title)
        if child is None:
            raise HTTPException(
                status_code=400,
                detail="this message cannot start a branch")
        return _session_detail(child)

    @app.post("/api/sessions/{session_id}/messages")
    async def send_message(session_id: str,
                           payload: MessageRequest) -> dict[str, Any]:
        session = _require_session(sessions, session_id)
        message = payload.message.strip()
        if not message:
            raise HTTPException(status_code=400, detail="message is empty")
        if session.busy:
            raise HTTPException(status_code=409, detail="session is busy")

        session.model = session.model or runner.catalog.default
        session.harness = session.harness or runner.harness.id
        snapshot = _resolve_snapshot(session, runner)

        trace, events, channel, run_id = _prepare_turn(
            session, message, snapshot, sessions)
        task = asyncio.create_task(
            _execute_turn(session, message, snapshot, trace, events,
                          channel, sessions))
        session.task = task
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
                         model=payload.model, harness=payload.harness,
                         expects_artifact=payload.expects_artifact)
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
        patch = {key: value for key, value in
                 payload.model_dump(exclude_unset=True).items()
                 # ``expects_artifact: null`` is a real edit ("let the
                 # harness decide"), not "field omitted".
                 if value is not None or key == "expects_artifact"}
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

    @app.get("/api/board/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        try:
            return tasks.detail(task_id, sessions)
        except (BoardNotFoundError, BoardStorageError) as exc:
            raise _board_http_error(exc) from exc

    @app.post("/api/board/tasks/{task_id}/comments", status_code=201)
    async def add_task_comment(task_id: str,
                               payload: CommentRequest) -> dict[str, Any]:
        try:
            _, deferred = tasks.add_comment(task_id, payload.body)
            detail = tasks.detail(task_id, sessions)
        except (BoardNotFoundError, BoardValidationError,
                BoardStorageError) as exc:
            raise _board_http_error(exc) from exc
        executor.wake()
        return {"board": board_snapshot(), "detail": detail,
                "deferred": deferred}

    @app.get("/api/evaluation/overview")
    async def evaluation_overview(
            range_: str = Query(DEFAULT_RANGE, alias="range"),
            origin: str | None = None,
            harness: str | None = None,
            model: str | None = None) -> dict[str, Any]:
        """Run-level KPIs, sliced by ``origin`` (never by ``task_id``)."""
        return await asyncio.to_thread(
            _evaluation_call, evaluation.overview,
            origins=_parse_origins(origin), range_=range_,
            harness=harness, model=model)

    @app.get("/api/evaluation/board-overview")
    async def evaluation_board_overview(
            range_: str = Query(DEFAULT_RANGE, alias="range")) -> dict[str, Any]:
        """Board outcome: TSR / first pass / rework / funnel / artifacts."""
        return await asyncio.to_thread(
            _evaluation_call, evaluation.board_overview, range_=range_)

    @app.get("/api/evaluation/benchmarks")
    async def evaluation_benchmarks() -> dict[str, Any]:
        """Harbor benchmark records, kept in their own view."""
        return await asyncio.to_thread(
            _evaluation_call, evaluation.benchmarks)

    @app.get("/api/evaluation/tasks")
    async def evaluation_tasks(
            range_: str = Query(DEFAULT_RANGE, alias="range"),
            status: str | None = None,
            harness: str | None = None,
            model: str | None = None,
            origin: str | None = None,
            q: str | None = None,
            sort: str = "updated_at",
            order: str = "desc",
            page: int = 1,
            page_size: int = 50) -> dict[str, Any]:
        return await asyncio.to_thread(
            _evaluation_call, evaluation.tasks,
            range_=range_, status=status, harness=harness, model=model,
            origins=_parse_origins(origin), query=q, sort=sort, order=order,
            page=page, page_size=page_size)

    @app.get("/api/evaluation/tasks/{task_id}")
    async def evaluation_task_detail(task_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(
            _evaluation_call, evaluation.task_detail, task_id)

    @app.get("/api/evaluation/runs/{run_id}")
    async def evaluation_run_detail(run_id: str) -> dict[str, Any]:
        """``run.metrics`` plus a bounded trace digest (never the payloads)."""
        return await asyncio.to_thread(
            _evaluation_call, evaluation.run_detail, run_id)

    @app.post("/api/evaluation/diagnose")
    async def evaluation_diagnose(payload: DiagnoseRequest) -> dict[str, Any]:
        """Open a diagnosis conversation for one target (manual, one turn).

        The session is an ordinary conversation (``origin=diagnosis``) so it
        shows up in the sidebar, can be questioned further, and can be
        re-run with another model.  Nothing here is scheduled: only this
        call starts a diagnosis.
        """
        model = _diagnosis_model(runner)
        plan = await asyncio.to_thread(_evaluation_call, evaluation.diagnose,
                                       payload.target, model=model,
                                       force=payload.force)
        if plan.duplicate:
            return {"duplicate": True,
                    "existing_session_id": plan.existing_session_id,
                    "bundle_hash": plan.bundle_hash,
                    "diagnosis_id": plan.diagnosis_id}
        session = plan.session
        assert session is not None
        try:
            snapshot = _resolve_snapshot(session, runner)
            trace, events, channel, run_id = _prepare_turn(
                session, plan.message, snapshot, sessions)
        except Exception:
            # Nothing ran: a roster/model problem must not leave a session
            # without a report (it would block ``duplicate`` checks forever)
            # or an orphan bundle behind.
            sessions.delete(session.id)
            if plan.diagnosis_id:
                delete_bundle(plan.diagnosis_id, evaluation.diagnosis_dir)
            raise
        task = asyncio.create_task(
            _execute_turn(session, plan.message, snapshot, trace, events,
                          channel, sessions))
        session.task = task
        return {"duplicate": False, "session_id": session.id,
                "bundle_hash": plan.bundle_hash,
                "diagnosis_id": plan.diagnosis_id, "run_id": run_id,
                "bundle_path": plan.bundle_path}

    @app.get("/api/evaluation/diagnoses")
    async def evaluation_diagnoses(
            task_id: str | None = None) -> dict[str, Any]:
        """Diagnosis sessions linked to a task through ``Session.target``."""
        return await asyncio.to_thread(
            _evaluation_call, evaluation.diagnoses, task_id=task_id)

    @app.post("/api/evaluation/refresh")
    async def evaluation_refresh() -> dict[str, Any]:
        """Rebuild the in-memory index from disk."""
        return await asyncio.to_thread(
            evaluation.refresh, force=True)

    @app.get("/api/workspace/tree")
    async def workspace_tree(path: str = "",
                             ignored: bool = False) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(browser.list_dir, path, ignored)
        except WorkspaceError as exc:
            raise _workspace_http_error(exc) from exc

    @app.get("/api/workspace/file")
    async def workspace_file(path: str) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(browser.read_file, path)
        except WorkspaceError as exc:
            raise _workspace_http_error(exc) from exc

    @app.get("/api/workspace/changes")
    async def workspace_changes() -> dict[str, Any]:
        try:
            return await asyncio.to_thread(browser.changes)
        except WorkspaceError as exc:
            raise _workspace_http_error(exc) from exc

    @app.get("/api/workspace/diff")
    async def workspace_diff(path: str,
                             old_path: str | None = None,
                             context: int = 3,
                             full: bool = False) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                browser.diff, path, old_path, context, full)
        except WorkspaceError as exc:
            raise _workspace_http_error(exc) from exc

    @app.api_route("/{path:path}", methods=["GET", "HEAD"],
                   include_in_schema=False)
    async def frontend_file(path: str) -> FileResponse:
        """Serve remaining files from the static Next.js export."""
        root = FRONTEND_ROOT.resolve()

        def resolve_inside(relative: str) -> Path | None:
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                return None
            return candidate

        for relative in (
            path,
            f"{path.rstrip('/')}.html",
            f"{path.rstrip('/')}/index.html",
        ):
            candidate = resolve_inside(relative)
            if candidate is not None and candidate.is_file():
                return FileResponse(candidate)

        not_found = root / "404.html"
        if not_found.is_file():
            return FileResponse(not_found, status_code=404)
        raise HTTPException(status_code=404, detail="not found")

    return app


def _prepare_turn(session: Session, message: str, snapshot: TurnSnapshot,
                  store: SessionStore, *, run_id: str | None = None,
                  task_id: str | None = None,
                  run_index: int | None = None
                  ) -> tuple[RunTrace, EventEmitter, WebUIChannel, str]:
    """Record the user message and build one turn's I/O objects.

    A board run arrives with its own ``run_id``; a chat turn gets a fresh
    one.  The trace carries the same join keys as ``runs.jsonl`` so the
    two can always be matched, and ``origin`` comes from the session (a
    diagnosis follow-up must not be counted as a plain chat run).
    """
    run_id = run_id or f"run-{uuid4().hex[:12]}"
    trace = RunTrace(run_id=run_id,
                     output_path=_trace_path(run_id),
                     harness=snapshot.harness,
                     retain_events="control",
                     origin=session.origin,
                     session_id=session.id,
                     task_id=task_id,
                     run_index=run_index,
                     model=snapshot.model)
    trace.record_config(
        runtime=snapshot.runtime,
        model_params={"model": snapshot.model,
                      "temperature": snapshot.runtime.temperature,
                      "stream": snapshot.stream,
                      "max_iterations": snapshot.harness.control.max_iterations},
        tools=snapshot.tools.schemas,
    )
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
                        store: SessionStore,
                        run: RunRequest | None = None) -> TurnOutcome:
    """Run one agent turn and report whether it completed cleanly."""
    errors: list[tuple[str, str]] = []
    outcome: TurnOutcome | None = None
    failure: dict[str, str] | None = None

    def collect_error(event: Any) -> None:
        if event.event_type == "runtime.error":
            # Keep the stage (``llm``, ``agent``, …): Phase 2 splits
            # failure causes by it, and a blanket ``runtime`` erases that.
            stage = str(event.data.get("stage") or "runtime")
            errors.append((stage, str(event.data.get("error") or "run failed")))

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
        outcome = TurnOutcome("error", trace.run_id,
                              error=str(exc) or "run failed")
        failure = {"stage": "runtime", "code": str(exc) or "run failed"}
        return outcome
    else:
        if errors:
            stage, message = errors[-1]
            outcome = TurnOutcome("error", trace.run_id, answer=answer,
                                  error=message)
            failure = {"stage": stage, "code": message}
            return outcome
        outcome = TurnOutcome("completed", trace.run_id, answer=answer)
        return outcome
    finally:
        # Every exit path (success, error, cancel) writes exactly one
        # metrics record; the trace event and the JSONL line come from
        # the same call so they cannot disagree.
        #
        # The nested ``try`` matters: recording awaits, so a second
        # ``cancel()`` (double-click on Cancel, cancel-then-delete,
        # shutdown) would otherwise skip the synchronous cleanup below
        # and leave the session stuck ``busy`` forever.
        try:
            await _record_run_metrics(trace, outcome, session, run, failure)
        finally:
            unsubscribe()
            channel.close()
            events.close()
            session.busy = False
            session.updated_at = time.time()
            session.task = None
            # A diagnosis report is its first *successful* answer: later
            # follow-up turns append more assistant segments, and a run that
            # failed midway must not freeze a half-written one.
            _mark_diagnosis_report(session, outcome)
            store.save(session)


async def _record_run_metrics(trace: RunTrace, outcome: TurnOutcome | None,
                              session: Session, run: RunRequest | None,
                              failure: dict[str, str] | None = None) -> None:
    """Close the run's metrics record, never masking the run's error."""
    try:
        await afinalize_run(
            trace,
            status=_metrics_status(outcome),
            answer=outcome.answer if outcome is not None else "",
            error=failure,
            origin=session.origin,
            session_id=session.id,
            task_id=run.task_id if run is not None else None,
            run_index=run.run_index if run is not None else None,
            # The trace is the run's real model; the session's is only a
            # default and can differ after a per-request override.
            model=trace.model or session.model or "",
        )
    except Exception:
        logger.exception("webui.run.metrics_failed session=%s run=%s",
                         session.id, trace.run_id)


def _mark_diagnosis_report(session: Session,
                           outcome: TurnOutcome | None) -> None:
    """Freeze the first completed answer of a diagnosis session.

    Only a completed turn with non-empty text can be the report: a run that
    errored, was cancelled, or streamed a preamble before failing leaves an
    unfrozen session that can be diagnosed again.  ``mark_report_segment``
    ignores later calls, so a follow-up cannot replace the report.
    """
    if outcome is None or outcome.status != "completed":
        return
    if session.origin not in DIAGNOSIS_ORIGINS or session.report_segment_id:
        return
    for segment in reversed(session.segments):
        if segment.get("type") != "assistant":
            continue
        if not str(segment.get("content") or "").strip():
            continue
        session.mark_report_segment(str(segment.get("id") or ""))
        return


def _diagnosis_model(runner: AgentRunner) -> str:
    """Catalog id a diagnosis runs on (``AGENT_RUNTIME_DIAGNOSIS_MODEL``).

    The variable holds a catalog ``id``, not a wire model name: the entry
    carries its own endpoint and key, so a diagnosis can be pointed at a
    stronger model than the default without duplicating secrets.
    """
    raw = (os.getenv("AGENT_RUNTIME_DIAGNOSIS_MODEL") or "").strip()
    if not raw:
        return runner.catalog.default
    try:
        return runner.catalog.require(raw).id
    except ModelCatalogError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _metrics_status(outcome: TurnOutcome | None) -> str:
    if outcome is None:
        return "cancelled"
    if outcome.status == "completed":
        return "completed"
    if outcome.status == "cancelled":
        return "cancelled"
    # ``busy`` never starts a run; anything else is a failed run.
    return "error"


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


def _workspace_http_error(exc: WorkspaceError) -> HTTPException:
    """Map a workspace-browser failure to its response."""
    return HTTPException(status_code=exc.status, detail=exc.message)


def _parse_origins(raw: str | None) -> tuple[str, ...] | None:
    """Read a comma-separated ``origin`` filter."""
    if not raw:
        return None
    parts = tuple(part.strip() for part in raw.split(",") if part.strip())
    return parts or None


def _evaluation_call(call: Callable[..., Any], *args: Any,
                     **kwargs: Any) -> Any:
    """Run one evaluation query, mapping its failures to HTTP."""
    try:
        return call(*args, **kwargs)
    except EvaluationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except EvaluationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
        spec = resolve_harness(harness_id)
    except HarnessError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if spec.internal:
        # The diagnosis harness is not a choice a user or a board task
        # makes; a diagnosis opens it through /api/evaluation/diagnose.
        raise HTTPException(status_code=400,
                            detail=f"harness {spec.id!r} is not selectable")
    return spec


def _resolve_snapshot(session: Session, runner: AgentRunner) -> TurnSnapshot:
    try:
        return resolve_turn(catalog=runner.catalog, model_id=session.model,
                            harness_id=session.harness,
                            base_runtime=runner.runtime)
    except (ModelCatalogError, HarnessError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
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
    """Where the trace of one turn is written."""
    return storage.trace_path(run_id)


__all__ = ["AgentRunner", "create_app"]
