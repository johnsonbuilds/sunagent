"""Run metrics persistence: the ``metrics/runs.jsonl`` index and run closing.

Record shapes live in :mod:`agent_runtime.run_records`; live counters live in
:mod:`agent_runtime.trace_stats`.  This module owns the metrics index and the
run-closing helper.

It deliberately does not import :mod:`agent_runtime.trace`: ``finalize_run``
accepts any object satisfying :class:`TraceLike`, so the dependency points from
metrics to the trace's structural surface, never back into the trace module.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any, Mapping, Protocol

from agent_runtime import run_records
from agent_runtime.storage import metrics_path

if TYPE_CHECKING:
    from agent_runtime.run_records import RunMetrics
    from agent_runtime.trace_stats import RunStats


logger = logging.getLogger(__name__)

_APPEND_LOCK = Lock()

# Keep one JSONL line comfortably below the size where concurrent ``os.write``
# calls from two processes could interleave.
_MAX_LINE_BYTES = 1_000_000


class TraceLike(Protocol):
    """The trace surface metrics needs, without importing the trace module."""

    run_id: str
    stats: RunStats
    origin: str
    session_id: str | None
    task_id: str | None
    run_index: int | None
    model: str
    harness: Any

    @property
    def output_path(self) -> Path | None: ...

    @property
    def started_at(self) -> float: ...

    def record_metrics(self, record: RunMetrics) -> Any: ...

    async def flush(self) -> None: ...


def _trace_bytes(trace: TraceLike) -> int:
    path = getattr(trace, "output_path", None)
    try:
        return path.stat().st_size if path is not None else 0
    except OSError:
        return 0


def finalize_run(trace: TraceLike, *, status: str, answer: str = "",
                 error: Mapping[str, Any] | None = None,
                 origin: str | None = None,
                 session_id: str | None = None,
                 task_id: str | None = None,
                 run_index: int | None = None,
                 model: str = "", path: Path | None = None) -> RunMetrics:
    """Close one run: build its metrics, emit ``run.metrics``, append it.

    The single entry point every run owner (webui, CLI, Harbor) calls on its way
    out, so the trace event and the ``runs.jsonl`` line cannot disagree and an
    error/cancel path cannot silently skip the record.
    """
    stats = trace.stats
    finished = time.time()
    started = stats.started_at or getattr(trace, "started_at", finished)
    duration_ms = max(0.0, (finished - started) * 1000)
    harness = getattr(trace, "harness", None)
    record = run_records.RunMetrics(
        run_id=trace.run_id,
        origin=origin or getattr(trace, "origin", run_records.DEFAULT_ORIGIN),
        harness_id=getattr(harness, "id", "") or "",
        genes_hash=getattr(harness, "genes_hash", "") or "",
        model=model or getattr(trace, "model", "") or "",
        session_id=session_id if session_id is not None else trace.session_id,
        task_id=task_id if task_id is not None else trace.task_id,
        run_index=run_index if run_index is not None else trace.run_index,
        status=status,
        has_answer=bool((answer or "").strip()) or stats.has_answer,
        started_at=started,
        finished_at=finished,
        duration_ms=duration_ms,
        iterations=stats.iterations,
        tool_calls=stats.tool_calls,
        tool_errors=stats.tool_errors,
        error_kinds=dict(stats.error_kinds),
        edited_files=list(stats.edited_files),
        tokens=stats.token_usage(),
        latency_ms=stats.latency(duration_ms),
        ttft_ms=stats.ttft_ms,
        retries=stats.retries,
        summary_invalid=stats.summary_invalid,
        summary_attempts=stats.summary_attempts,
        compactions=stats.compactions,
        policy_blocks=stats.policy_blocks,
        error=_clean_error(error),
        trace_bytes=_trace_bytes(trace),
        dropped_events=stats.dropped_events,
    )
    trace.record_metrics(record)
    append_run_metrics(record, path)
    return record


async def afinalize_run(trace: TraceLike, **kwargs: Any) -> RunMetrics:
    """Finalize a run and persist the closing trace events.

    ``finalize_run`` emits ``run.metrics``, and the owning turn has usually
    already flushed in its own ``finally``.  Emitting then starts a fresh writer
    task, so without the second flush that task parks on the queue forever (one
    leaked task per run, holding the trace alive) and the record may never reach
    the file.  Run owners therefore call this instead of ``finalize_run``.

    A cancellation landing on either flush is remembered rather than swallowed:
    the record is still built and appended, and the ``CancelledError`` is
    re-raised once the trace tail is on disk.  ``asyncio.shield`` is deliberately
    not used — it hands the cancellation back to the caller anyway and orphans
    the shielded coroutine as a stray task.
    """
    run_id = getattr(trace, "run_id", "")
    # Drain first so the record reports the trace's final size.
    cancelled = await _flush_trace(trace, "preflush", run_id)
    record = finalize_run(trace, **kwargs)
    # Persist ``run.metrics`` and stop the writer it just restarted.
    cancelled = (await _flush_trace(trace, "postflush", record.run_id)
                 or cancelled)
    if cancelled is not None:
        raise cancelled
    return record


async def _flush_trace(trace: TraceLike, phase: str,
                       run_id: str) -> asyncio.CancelledError | None:
    """Flush the trace, reporting a cancellation instead of raising it."""
    try:
        await trace.flush()
    except asyncio.CancelledError as exc:
        logger.warning("metrics.run.%s_cancelled run=%s", phase, run_id)
        return exc
    except Exception:
        logger.warning("metrics.run.%s_failed run=%s", phase, run_id,
                       exc_info=True)
    return None


def _clean_error(error: Mapping[str, Any] | None) -> dict[str, str] | None:
    if not isinstance(error, Mapping):
        return None
    stage = run_records.parse_text(error.get("stage")) or ""
    code = run_records.parse_text(error.get("code")) or ""
    if not stage and not code:
        return None
    return {"stage": stage, "code": code}


def append_run_metrics(record: RunMetrics | Mapping[str, Any],
                       path: Path | None = None) -> Path:
    """Append one metrics line to ``metrics/runs.jsonl``.

    One newline-terminated line is appended with ``O_APPEND`` so concurrent
    writers (Harbor runs several trials at once) cannot interleave inside a
    line.  ``allow_nan=False`` keeps the file valid JSON; the dataclasses already
    clamp non-finite floats.

    A failed write is logged and swallowed: this runs in the ``finally`` path of
    a run and must never mask the run's own error.  A truncated line fails to
    parse and is skipped on read.
    """
    entry = (record if isinstance(record, run_records.RunMetrics)
             else run_records.RunMetrics.from_dict(record))
    target = Path(path) if path is not None else metrics_path()
    try:
        line = json.dumps(entry.to_dict(), ensure_ascii=False,
                          separators=(",", ":"), default=str, allow_nan=False)
    except (TypeError, ValueError) as exc:
        logger.warning("metrics.run.serialize_failed run=%s error=%s",
                       entry.run_id, exc)
        return target
    payload = memoryview((line + "\n").encode("utf-8"))
    if len(payload) > _MAX_LINE_BYTES:
        logger.warning("metrics.run.line_too_large run=%s bytes=%d",
                       entry.run_id, len(payload))
    with _APPEND_LOCK:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(target,
                                 os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        except OSError as exc:
            # A read-only home must not turn into a run failure.
            logger.warning("metrics.run.append_failed path=%s run=%s error=%s",
                           target, entry.run_id, exc)
            return target
        try:
            while payload:
                written = os.write(descriptor, payload)
                if written <= 0:
                    raise OSError("short write")
                payload = payload[written:]
        except OSError as exc:
            logger.warning("metrics.run.append_failed path=%s run=%s error=%s",
                           target, entry.run_id, exc)
        finally:
            os.close(descriptor)
    return target


def read_run_metrics(path: Path | None = None) -> list[RunMetrics]:
    """Read ``runs.jsonl``, skipping lines that are not valid records."""
    target = Path(path) if path is not None else metrics_path()
    if not target.is_file():
        return []
    records: list[RunMetrics] = []
    with target.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                parsed = None
            # Valid JSON that is not an object (``null``, ``123``, ``[]``) would
            # otherwise default into a fake ``completed`` run.
            if not isinstance(parsed, Mapping):
                logger.warning("metrics.run.line_skipped path=%s line=%d",
                               target, number)
                continue
            records.append(run_records.RunMetrics.from_dict(parsed))
    return records


__all__ = [
    "afinalize_run", "append_run_metrics", "finalize_run", "read_run_metrics",
]
