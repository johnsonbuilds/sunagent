"""Small, provider-independent event tracing for agent runs."""

from __future__ import annotations

import json
import asyncio
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence
from uuid import uuid4

from agent_runtime.metrics import (
    DEFAULT_ORIGIN,
    ENV_WHITELIST,
    PAYLOAD_EVENT_TYPES,
    RunConfig,
    RunMetrics,
    RunStats,
)

if TYPE_CHECKING:
    from agent_runtime.harness import HarnessSpec
    from agent_runtime.runtime_config import RuntimeConfig

logger = logging.getLogger(__name__)

# Sentinel queued by ``flush`` to tell the background writer to exit after
# every real event ahead of it has been persisted.
_STOP = object()

# Only these may be dropped when the writer queue is full: the stream is
# also visible to live subscribers, and losing one chunk costs nothing.
_DROPPABLE = frozenset({"llm.chunk"})

# How many events may sit in the writer queue.  Payload events are not
# retained in ``events`` any more, so the queue is the only buffer, and a
# full queue must never lose a control event.
_DEFAULT_QUEUE_SIZE = 10_000

_GIT_CACHE_SECONDS = 60.0
_git_cache: tuple[float, dict[str, Any]] = (0.0, {})
_git_lock = threading.Lock()


def git_state() -> dict[str, Any]:
    """Best-effort ``git commit``/``status`` for ``run.config``.

    Cached for a minute and bounded by a short timeout: the caller is
    about to start an LLM call, so a slow repository must not delay the
    run or fail it.  The cache is keyed on the attempt, not on its
    contents, so a non-repository (Harbor containers, tmp dirs) is not
    re-probed for every run.
    """
    global _git_cache
    with _git_lock:
        cached_at, cached = _git_cache
        if cached_at > 0.0 and time.time() - cached_at < _GIT_CACHE_SECONDS:
            return dict(cached)
    state: dict[str, Any] = {}
    for key, command in (("commit", ["git", "rev-parse", "HEAD"]),
                         ("branch", ["git", "rev-parse",
                                     "--abbrev-ref", "HEAD"]),
                         ("dirty", ["git", "status", "--porcelain"])):
        try:
            completed = subprocess.run(command, capture_output=True,
                                       text=True, timeout=2, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode != 0:
            continue
        if key == "dirty":
            state["dirty"] = bool(completed.stdout.strip())
        else:
            state[key] = completed.stdout.strip()[:200]
    with _git_lock:
        _git_cache = (time.time(), dict(state))
    return state


def env_whitelist() -> dict[str, str]:
    """The runtime environment keys that change how a run executes."""
    return {name: os.environ[name] for name in ENV_WHITELIST
            if name in os.environ}


def _runtime_snapshot(runtime: "RuntimeConfig | None") -> dict[str, Any]:
    """Runtime wiring minus credentials: enough to reproduce the run."""
    if runtime is None:
        return {}
    return {
        "model": runtime.model,
        "base_url": runtime.base_url,
        "temperature": runtime.temperature,
        "streaming": runtime.streaming,
        "stream_usage": runtime.stream_usage,
        "stream_idle_timeout": runtime.stream_idle_timeout,
        "max_reasoning_chars": runtime.max_reasoning_chars,
        "retry_4xx": runtime.retry_4xx,
        "reasoning_effort": runtime.reasoning_effort,
        "reasoning_echo_mode": runtime.reasoning_echo_mode,
        "summary_model": runtime.summary_model,
        "workspace_root": runtime.workspace_root,
        "command_guard_mode": runtime.command_guard_mode,
        "memory_enabled": runtime.memory_enabled,
        "memory_dir": runtime.memory_dir,
        "tool_error_max_retries": runtime.tool_error_max_retries,
        "harness": runtime.harness,
    }


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

    ``retain_events`` decides what stays in ``events`` for post-run
    inspection: ``all`` keeps everything (tests), ``control`` keeps only
    control events and lets payloads live in the trace file and the
    counters, ``none`` keeps nothing.  ``RunStats`` is fed regardless, so
    ``run.metrics`` never depends on what was retained.

    With an ``output_path`` events are also handed to a single
    background writer task through a queue. ``emit`` never performs I/O
    and never awaits, so tracing does not add latency to the agent loop;
    ``flush`` acts as the drain and stop barrier at the end of a turn
    (and is idempotent, so callers may flush more than once).
    """

    def __init__(self, run_id: str | None = None,
                 output_path: str | Path | None = None,
                 sink: Callable[[RunEvent], None] | None = None,
                 harness: "HarnessSpec | None" = None, *,
                 retain_events: str = "all",
                 origin: str = DEFAULT_ORIGIN,
                 session_id: str | None = None,
                 task_id: str | None = None,
                 run_index: int | None = None,
                 model: str = "",
                 trace_chunks: bool = False,
                 queue_size: int = _DEFAULT_QUEUE_SIZE) -> None:
        if retain_events not in {"all", "control", "none"}:
            raise ValueError(f"unknown retain_events: {retain_events!r}")
        self.run_id = run_id or f"run-{uuid4().hex}"
        self.events: list[RunEvent] = []
        self.harness = harness
        self.retain_events = retain_events
        self.origin = origin
        self.session_id = session_id
        self.task_id = task_id
        self.run_index = run_index
        self.model = model
        self.trace_chunks = trace_chunks
        self.stats = RunStats(started_at=time.time())
        self.dropped = 0
        self._output_path = Path(output_path) if output_path else None
        self._sink = sink
        # Events emitted before a running loop (or its writer) is available.
        self._pending: list[RunEvent] = []
        self._queue: asyncio.Queue[Any] | None = None
        self._queue_size = max(1, queue_size)
        self._writer: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._write_error: BaseException | None = None
        # ``_write_events`` may run from the writer task or from ``flush``
        # after a cross-loop fallback; the lock keeps a single harness header
        # and keeps whole lines from interleaving.
        self._write_lock = threading.Lock()
        self._wrote_harness_header = False
        self._emitted = 0
        self._last_agent_error: RunEvent | None = None

    @property
    def output_path(self) -> Path | None:
        """Where this run's trace is written, if anywhere."""
        return self._output_path

    @property
    def started_at(self) -> float:
        return self.stats.started_at

    @property
    def event_count(self) -> int:
        """How many events were emitted (retention does not change it)."""
        return self._emitted

    def agent_error(self) -> RunEvent | None:
        """The last ``agent.error`` event, if the run failed.

        Cached at emit time: ``retain_events="none"`` keeps none of the
        events, and scanning ``self.events`` would then report success.
        """
        return self._last_agent_error

    def emit(self, event_type: str, iteration: int = 0,
             **data: Any) -> RunEvent:
        event = RunEvent(self.run_id, f"evt-{uuid4().hex}", event_type,
                         time.time(), iteration, data)
        self._emitted += 1
        if event_type == "agent.error":
            self._last_agent_error = event
        self.stats.observe(event_type, iteration, data)
        if self._retains(event_type):
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
            self._enqueue(event)
        else:
            # A writer already exists on a different loop; writing from a
            # second writer would break ordering, so buffer for ``flush``.
            self._pending.append(event)
        return event

    def _retains(self, event_type: str) -> bool:
        if self.retain_events == "all":
            return True
        if self.retain_events == "none":
            return False
        return event_type not in PAYLOAD_EVENT_TYPES

    def _enqueue(self, event: RunEvent) -> None:
        """Queue one event, dropping only cheap payload when full."""
        queue = self._queue
        assert queue is not None
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            if event.event_type in _DROPPABLE:
                self.dropped += 1
                self.stats.dropped_events = self.dropped
                return
            # A control event must not be lost; ``flush`` writes it even
            # if that means writing the tail after the queue drained.
            logger.warning("run trace queue full; deferring %s to flush",
                           event.event_type)
            self._pending.append(event)

    def record_config(self, *, runtime: "RuntimeConfig | None" = None,
                      model_params: Mapping[str, Any] | None = None,
                      tools: Sequence[Mapping[str, Any]] | None = None,
                      extra: Mapping[str, Any] | None = None
                      ) -> RunConfig:
        """Emit ``run.config``: the conditions this run executed under."""
        config = RunConfig(
            run_id=self.run_id,
            origin=self.origin,
            harness_id=getattr(self.harness, "id", "") or "",
            genes_hash=getattr(self.harness, "genes_hash", "") or "",
            model=self.model,
            session_id=self.session_id,
            task_id=self.task_id,
            run_index=self.run_index,
            model_params=dict(model_params or {}),
            tools=[dict(tool) for tool in tools or ()],
            runtime=_runtime_snapshot(runtime),
            env=env_whitelist(),
            git=git_state(),
        )
        payload = config.to_dict()
        if extra:
            payload.update(dict(extra))
        self.emit("run.config", 0, **payload)
        return config

    def record_metrics(self, record: RunMetrics) -> RunEvent:
        """Emit ``run.metrics``: the measurements of this run."""
        return self.emit("run.metrics", 0, **record.to_dict())

    def _ensure_writer(self, loop: asyncio.AbstractEventLoop) -> bool:
        """Start the background writer on ``loop`` if it is not running.

        Returns ``True`` when the caller may enqueue onto ``self._queue``.
        """
        if self._writer is not None:
            return self._loop is loop
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=self._queue_size)
        self._queue = queue
        self._loop = loop
        if self._pending:
            # Preserve order: events buffered before the writer existed go
            # first, ahead of anything emitted from now on.
            pending, self._pending = self._pending, []
            for event in pending:
                # ``_enqueue``: a bare ``put_nowait`` would raise
                # ``QueueFull`` and lose the whole migration.
                self._enqueue(event)
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
                # ``await put`` (not ``put_nowait``): a full queue is the
                # one case where the stop sentinel must still land.
                await queue.put(_STOP)
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
                    # genome that produced every event below it, and carries
                    # the join keys so a stray trace can still be attributed.
                    header = {
                        "record_type": "harness",
                        "run_id": self.run_id,
                        "origin": self.origin,
                        "session_id": self.session_id,
                        "task_id": self.task_id,
                        "run_index": self.run_index,
                        "model": self.model,
                        "harness_id": self.harness.id,
                        "genes_hash": self.harness.genes_hash,
                        "harness": self.harness.to_dict(),
                    }
                    stream.write(json.dumps(header, ensure_ascii=True,
                                            default=str) + "\n")
                    self._wrote_harness_header = True
                for event in events:
                    stream.write(json.dumps(event.to_dict(), ensure_ascii=False,
                                            separators=(",", ":"),
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


__all__ = ["RunEvent", "RunTrace", "env_whitelist", "git_state"]
