"""Small, provider-independent event tracing for agent runs."""

from __future__ import annotations

import json
import asyncio
import logging
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from agent_runtime.harness import HarnessSpec

logger = logging.getLogger(__name__)

# Sentinel queued by ``flush`` to tell the background writer to exit after
# every real event ahead of it has been persisted.
_STOP = object()


@dataclass(frozen=True)
class RunEvent:
    """One immutable event emitted while an agent run is executing."""

    run_id: str
    event_id: str
    event_type: str
    timestamp: float
    iteration: int
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RunTrace:
    """Collect events in memory and append them to JSONL asynchronously.

    Events are still kept in ``events`` for post-run inspection, but with an
    ``output_path`` they are also handed to a single background writer task
    through a queue. ``emit`` never performs I/O and never awaits, so tracing
    does not add latency to the agent loop; ``flush`` acts as the drain and
    stop barrier at the end of a turn (and is idempotent, so callers may flush
    more than once).
    """

    def __init__(self, run_id: str | None = None,
                 output_path: str | Path | None = None,
                 sink: Callable[[RunEvent], None] | None = None,
                 harness: "HarnessSpec | None" = None) -> None:
        self.run_id = run_id or f"run-{uuid4().hex}"
        self.events: list[RunEvent] = []
        self.harness = harness
        self._output_path = Path(output_path) if output_path else None
        self._sink = sink
        # Events emitted before a running loop (or its writer) is available.
        self._pending: list[RunEvent] = []
        self._queue: asyncio.Queue[Any] | None = None
        self._writer: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._write_error: BaseException | None = None
        # ``_write_events`` may run from the writer task or from ``flush``
        # after a cross-loop fallback; the lock keeps a single harness header
        # and keeps whole lines from interleaving.
        self._write_lock = threading.Lock()
        self._wrote_harness_header = False

    def emit(self, event_type: str, iteration: int = 0,
             **data: Any) -> RunEvent:
        event = RunEvent(self.run_id, f"evt-{uuid4().hex}", event_type,
                         time.time(), iteration, data)
        self.events.append(event)
        if self._sink:
            self._sink(event)
        if self._output_path is None:
            return event
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop yet (e.g. sync tests): keep the event and let
            # ``flush`` write it directly.
            self._pending.append(event)
            return event
        if self._ensure_writer(loop):
            self._queue.put_nowait(event)
        else:
            # A writer already exists on a different loop; writing from a
            # second writer would break ordering, so buffer for ``flush``.
            self._pending.append(event)
        return event

    def _ensure_writer(self, loop: asyncio.AbstractEventLoop) -> bool:
        """Start the background writer on ``loop`` if it is not running.

        Returns ``True`` when the caller may enqueue onto ``self._queue``.
        """
        if self._writer is not None:
            return self._loop is loop
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._queue = queue
        self._loop = loop
        if self._pending:
            # Preserve order: events buffered before the writer existed go
            # first, ahead of anything emitted from now on.
            pending, self._pending = self._pending, []
            for event in pending:
                queue.put_nowait(event)
        self._writer = loop.create_task(self._writer_loop())
        return True

    async def _writer_loop(self) -> None:
        """Consume events, batching bursts into one off-loop write."""
        queue = self._queue
        assert queue is not None
        while True:
            item = await queue.get()
            if item is _STOP:
                queue.task_done()
                return
            batch = [item]
            stop_after = False
            while not queue.empty():
                nxt = queue.get_nowait()
                if nxt is _STOP:
                    queue.task_done()
                    stop_after = True
                    break
                batch.append(nxt)
            try:
                # Disk work happens in a worker thread: the agent loop keeps
                # running while the trace is persisted.
                await asyncio.to_thread(self._write_events, batch)
            except Exception as exc:  # keep draining; surface it at flush
                if self._write_error is None:
                    logger.warning("run trace write failed: %s", exc)
                    self._write_error = exc
            finally:
                for _ in batch:
                    queue.task_done()
            if stop_after:
                return

    async def flush(self) -> None:
        """Persist queued events and stop the writer without blocking the loop.

        Safe to call more than once: a second call only writes events that
        were buffered outside the writer and finds the queue already drained.
        """
        writer = self._writer
        if writer is not None and self._loop is asyncio.get_running_loop():
            queue = self._queue
            assert queue is not None
            try:
                await queue.join()
                queue.put_nowait(_STOP)
                await writer
            except asyncio.CancelledError:
                # The cleanup itself was cancelled again (double cancel).
                # Awaiting is no longer reliable, so persist the tail
                # synchronously before the task dies.
                self._drain_cancelled(queue)
                raise
            finally:
                self._writer = None
                self._queue = None
                self._loop = None
        if self._pending:
            pending, self._pending = self._pending, []
            await asyncio.to_thread(self._write_events, pending)
        if self._write_error is not None:
            error, self._write_error = self._write_error, None
            raise error

    def _drain_cancelled(self, queue: asyncio.Queue[Any]) -> None:
        """Best-effort synchronous drain after a cancelled ``flush``.

        Blocking I/O is acceptable here: the task is already dying and the
        alternative is losing the trace tail (or leaking a writer parked on
        ``queue.get``). The write lock still serialises against an in-flight
        batch, so records are never interleaved or written twice; ordering
        between that batch and the tail is best effort only.
        """
        leftovers: list[RunEvent] = []
        while not queue.empty():
            item = queue.get_nowait()
            queue.task_done()
            if item is not _STOP:
                leftovers.append(item)
        # Let a still-running writer exit after its in-flight batch instead
        # of parking on ``queue.get()`` forever in a long-lived event loop.
        queue.put_nowait(_STOP)
        if self._pending:
            pending, self._pending = self._pending, []
            leftovers.extend(pending)
        if not leftovers:
            return
        try:
            self._write_events(leftovers)
        except Exception as exc:  # nothing left to surface the error to
            logger.warning("run trace cancel-path write failed: %s", exc)

    def _write_events(self, events: list[RunEvent]) -> None:
        if not events or self._output_path is None:
            return
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            with self._output_path.open("a", encoding="utf-8") as stream:
                if self.harness and not self._wrote_harness_header:
                    # The first JSONL record tags the file with the harness
                    # genome that produced every event below it.
                    header = {
                        "record_type": "harness",
                        "run_id": self.run_id,
                        "harness_id": self.harness.id,
                        "genes_hash": self.harness.genes_hash,
                        "harness": self.harness.to_dict(),
                    }
                    stream.write(json.dumps(header, ensure_ascii=True,
                                            default=str) + "\n")
                    self._wrote_harness_header = True
                for event in events:
                    stream.write(json.dumps(event.to_dict(), ensure_ascii=True,
                                            default=str) + "\n")

    @contextmanager
    def span(self, name: str, iteration: int | None = None,
             **meta: Any):
        """Trace an operation with explicit start, error, and end events."""
        event_iteration = iteration if iteration is not None else 0
        span_meta = dict(meta)
        started = time.monotonic()
        self.emit(f"{name}.start", event_iteration, **span_meta)
        failed = False
        cancelled = False
        try:
            yield span_meta
        except asyncio.CancelledError:
            # CancelledError is a BaseException, not an Exception;
            # without this branch the span would report success.
            cancelled = True
            raise
        except Exception as exc:
            failed = True
            span_meta["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
            error_meta = dict(span_meta)
            error_meta["error"] = str(exc)
            self.emit(f"{name}.error", event_iteration, **error_meta)
            raise
        finally:
            span_meta["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
            if cancelled:
                span_meta["status"] = "cancelled"
            else:
                span_meta["status"] = "error" if failed else "success"
            self.emit(f"{name}.end", event_iteration, **span_meta)


__all__ = ["RunEvent", "RunTrace"]
