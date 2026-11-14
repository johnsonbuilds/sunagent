"""Serial task execution for the web UI board.

The board's ``todo`` column is the queue.  A single asyncio worker claims
the highest-priority queued task, runs one turn for its conversation,
and then moves the card to ``review``.  There is deliberately no worker
pool: the board is a single-user tool, and serial execution keeps file
and command side effects predictable.

Only tasks that were explicitly queued (``queued_at``) are executed.
That lets a board created before this feature keep its old ``todo``
cards without suddenly launching them on startup.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from .board import BoardStore, BoardTask
from .sessions import Session, SessionStore


logger = logging.getLogger(__name__)

TurnStatus = Literal["completed", "error", "cancelled", "busy"]


@dataclass(frozen=True)
class TurnOutcome:
    """How one board-driven turn ended."""

    status: TurnStatus
    run_id: str = ""
    answer: str = ""
    error: str | None = None


EnsureSession = Callable[[str], str]
RunTurn = Callable[[Session, str], Awaitable[TurnOutcome]]


@dataclass
class _CurrentRun:
    task_id: str
    run_id: str
    session_id: str | None = None


class BoardExecutor:
    """One worker that drains the Todo column in priority order."""

    def __init__(self, board: BoardStore, sessions: SessionStore, *,
                 ensure_session: EnsureSession,
                 run_turn: RunTurn,
                 poll_interval: float = 1.0) -> None:
        self.board = board
        self.sessions = sessions
        self._ensure_session = ensure_session
        self._run_turn = run_turn
        self._poll_interval = max(0.1, poll_interval)
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._turn_task: asyncio.Task[TurnOutcome] | None = None
        self._current: _CurrentRun | None = None
        self._stopping = False
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def running_task_id(self) -> str | None:
        return self._current.task_id if self._current is not None else None

    def status(self) -> dict[str, object]:
        """The executor slice embedded in the board snapshot."""
        return {"paused": self._paused,
                "running_task_id": self.running_task_id}

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._stopping = False
        self._loop_task = asyncio.create_task(self._loop(),
                                              name="board-executor")

    def wake(self) -> None:
        """Ask the loop to re-check the queue."""
        self._wake.set()

    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)
        if not self._paused:
            self._wake.set()

    def cancel_task(self, task_id: str) -> None:
        current = self._current
        if (current is not None and current.task_id == task_id
                and self._turn_task is not None
                and not self._turn_task.done()):
            self._turn_task.cancel()

    def cancel_session(self, session_id: str) -> None:
        current = self._current
        if (current is not None and current.session_id == session_id
                and self._turn_task is not None
                and not self._turn_task.done()):
            self._turn_task.cancel()

    def request_stop(self) -> None:
        """Synchronous stop used by the process signal handler."""
        self._stopping = True
        self._wake.set()
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()

    async def stop(self) -> None:
        self.request_stop()
        loop = self._loop_task
        self._loop_task = None
        if loop is not None:
            try:
                await loop
            except asyncio.CancelledError:  # pragma: no cover - shutdown path
                pass
            except Exception:  # pragma: no cover - defensive shutdown
                logger.exception("webui.board.executor.stop_failed")

    async def _loop(self) -> None:
        while not self._stopping:
            if self._paused:
                await self._wait()
                continue

            candidate = self.board.next_todo()
            if candidate is None:
                await self._wait()
                continue

            # Do not claim a card whose conversation is already busy with
            # a manual turn.  A completion wakes the loop; the timeout is
            # only a safety net for missed notifications.
            session = (self.sessions.get(candidate.session_id)
                       if candidate.session_id else None)
            if session is not None and session.busy:
                await self._wait()
                continue

            claimed = None
            try:
                claimed = self.board.claim_next_todo()
            except Exception:
                logger.exception("webui.board.executor.claim_failed")
                await self._wait()
                continue
            if claimed is None:
                continue
            await self._run(claimed)

    async def _wait(self) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(),
                                   timeout=self._poll_interval)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    async def _run(self, task: BoardTask) -> None:
        assert task.run_id is not None
        self._current = _CurrentRun(task.id, task.run_id)
        try:
            session_id = self._ensure_session(task.id)
            self._current.session_id = session_id or None
            if not session_id:
                self._finish(task, "review", "Session could not be created")
                return
            session = self.sessions.get(session_id)
            if session is None:
                self._finish(task, "review", "Session not found")
                return
            if session.busy:
                self.board.requeue_run(task.id, task.run_id)
                return
            if self._stopping:
                self.board.requeue_run(
                    task.id, task.run_id,
                    error="Interrupted by server shutdown")
                return

            self._turn_task = asyncio.create_task(
                self._run_turn(session, task.description),
                name=f"board-task-{task.id}",
            )
            session.task = self._turn_task
            turn_task = self._turn_task
            try:
                outcome = await self._turn_task
            except asyncio.CancelledError:
                if self._stopping:
                    self.board.requeue_run(
                        task.id, task.run_id,
                        error="Interrupted by server shutdown")
                else:
                    # A user moving the card out of Doing wins the race
                    # because finish_run only touches a still-Doing card.
                    self.board.finish_run(
                        task.id, task.run_id, status="review",
                        error="Run cancelled")
                return
            except Exception as exc:
                logger.exception("webui.board.executor.run_failed task=%s",
                                 task.id)
                self._finish(task, "review",
                             str(exc) or "Run failed")
                return
            finally:
                if session.task is turn_task:
                    session.task = None

            if outcome.status == "completed":
                self.board.finish_run(task.id, task.run_id,
                                      status="review")
            elif outcome.status == "busy":
                self.board.requeue_run(task.id, task.run_id)
            elif outcome.status == "error":
                self.board.finish_run(task.id, task.run_id, status="review",
                                      error=outcome.error or "Run failed")
            else:
                self.board.finish_run(task.id, task.run_id, status="review",
                                      error=outcome.error or "Run cancelled")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("webui.board.executor.failed task=%s", task.id)
            self._finish(task, "review", str(exc) or "Run failed")
        finally:
            self._turn_task = None
            self._current = None

    def _finish(self, task: BoardTask, status: str,
                error: str | None) -> None:
        assert task.run_id is not None
        try:
            self.board.finish_run(task.id, task.run_id,
                                  status=status, error=error)
        except Exception:  # pragma: no cover - board write failure
            logger.exception("webui.board.executor.finish_failed task=%s",
                             task.id)


__all__ = ["BoardExecutor", "TurnOutcome", "TurnStatus"]
