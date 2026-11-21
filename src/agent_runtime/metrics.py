"""Run-level measurement records: ``run.config`` and ``run.metrics``.

The evaluation pipeline reads exactly two machine-written sources: the
per-run trace (``traces/run-*.jsonl``, the authoritative replay log) and
the compact metrics index (``metrics/runs.jsonl``, one line per run).
This module owns the shape of the second one so every writer agrees on
it and every reader can be written against it.

Two record types, both keyed by the same join keys:

``RunConfig``
    The conditions of a run: model parameters, tool schemas, runtime
    settings, environment whitelist, git state.  Without it a trace is
    not reproducible even though it is complete.  Written once per run
    as the ``run.config`` trace event.

``RunMetrics``
    The measurements of a run: outcome, iterations, tool counts, token
    usage, latency, retries, edited files.  Written once per run as the
    ``run.metrics`` trace event and appended to ``metrics/runs.jsonl``.

Both records carry the join keys (``origin``/``run_id``/``session_id``/
``task_id``/``run_index``/``harness_id``/``genes_hash``/``model``), which
is what lets the evaluation index slice by origin, harness, model, or
day without re-reading traces.

Who writes ``runs.jsonl``: the caller that owns the run (``webui/server.py``,
``run_agent.py``, ``integrations/harbor.py``), by calling
:func:`append_run_metrics` once the run has finished.  ``RunTrace`` stays
a trace writer and never decides where the metrics index lives.  A line
can be rebuilt from its trace, so deleting ``metrics/`` loses no
information (see ``scripts/backfill_metrics.py``).

Add fields by giving them a default; readers ignore unknown keys and
missing ones fall back to the default, so old lines stay readable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from agent_runtime import storage

if TYPE_CHECKING:
    from agent_runtime.runtime_config import RuntimeConfig
    from agent_runtime.trace import RunTrace


logger = logging.getLogger(__name__)

# Which surface started the run.  This is a slicing dimension, not a
# filter: diagnosis/improvement runs are kept in ``runs.jsonl`` but are
# excluded from harness and board metrics.
ORIGINS = ("cli", "webui", "harbor", "diagnosis", "improvement")
DEFAULT_ORIGIN = "cli"

RUN_STATUSES = ("completed", "error", "cancelled")

# Events whose payload is large enough that keeping them in ``trace.events``
# for a long run costs more memory than the diagnosis value they add.  They
# are still written to the trace file and still counted in ``RunStats``.
PAYLOAD_EVENT_TYPES = frozenset({
    "llm.start", "llm.end", "llm.chunk", "tool.start", "tool.end",
    "tool.error", "context.llm_summary.text",
})

# Environment keys that change how a run behaves.  Read explicitly rather
# than "every AGENT_RUNTIME_*" so credentials can never leak into a trace.
ENV_WHITELIST = (
    "AGENT_RUNTIME_HARNESS", "AGENT_RUNTIME_WORKSPACE", "AGENT_RUNTIME_HOME",
    "AGENT_RUNTIME_MEMORY", "AGENT_RUNTIME_MEMORY_DIR",
    "AGENT_RUNTIME_STREAM_IDLE_TIMEOUT", "AGENT_RUNTIME_MAX_REASONING_CHARS",
    "AGENT_RUNTIME_RETRY_4XX", "AGENT_RUNTIME_REASONING_EFFORT",
    "AGENT_RUNTIME_REASONING_ECHO", "AGENT_RUNTIME_STREAM_USAGE",
    "AGENT_RUNTIME_COMMAND_GUARD",
)

_APPEND_LOCK = Lock()

# Keep one JSONL line comfortably below the size where a concurrent
# ``os.write`` from two processes could interleave.
_MAX_LINE_BYTES = 1_000_000


def _text(value: Any) -> str | None:
    """Read a non-blank string, trimmed the way every writer trims it."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _int(value: Any, default: int = 0) -> int:
    # ``bool`` is an ``int`` subclass, but ``{"iterations": true}``
    # must not turn into a count of one.
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _float(value: Any, default: float = 0.0) -> float:
    """Read a finite float; JSON has no ``NaN`` or ``Infinity``."""
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _flag(value: Any) -> bool:
    """Read a boolean flag; a string ``"false"`` is not ``False``."""
    return value if isinstance(value, bool) else False


def _finite(value: Any, default: float = 0.0) -> float:
    """Clamp a computed float to something JSON can carry."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    return number if math.isfinite(number) else default


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text_list(value: Any) -> list[str]:
    """Read a list of paths, dropping blanks so evidence stays truthful."""
    if not isinstance(value, list):
        return []
    paths: list[str] = []
    for item in value:
        text = _text(item)
        if text is not None and text not in paths:
            paths.append(text)
    return paths


@dataclass
class RunStats:
    """Live counters a :class:`~agent_runtime.trace.RunTrace` feeds per event.

    Everything a ``run.metrics`` record needs is accumulated here while the
    run executes, so the record survives the ``retain_events`` policy: with
    ``control`` the payload events never stay in ``trace.events``, and a
    reader that scanned the leftover list would report zeros.
    """

    started_at: float = 0.0
    iterations: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    error_kinds: dict[str, int] = field(default_factory=dict)
    edited_files: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    summarizer_prompt_tokens: int = 0
    summarizer_completion_tokens: int = 0
    llm_ms: float = 0.0
    tool_ms: float = 0.0
    summarizer_ms: float = 0.0
    ttft_ms: float | None = None
    retries: int = 0
    summary_attempts: int = 0
    summary_invalid: int = 0
    compactions: int = 0
    policy_blocks: int = 0
    has_answer: bool = False
    dropped_events: int = 0

    def observe(self, event_type: str, iteration: int,
                data: Mapping[str, Any]) -> None:
        """Fold one emitted trace event into the counters."""
        if isinstance(iteration, int) and not isinstance(iteration, bool):
            self.iterations = max(self.iterations, iteration)
        if event_type == "llm.end":
            self.llm_ms += _float(data.get("duration_ms"))
            if self.ttft_ms is None:
                self.ttft_ms = _optional_float(data.get("ttft_ms"))
        elif event_type == "llm.retry":
            self.retries += 1
        elif event_type == "tool.end":
            self._observe_tool_end(data)
        elif event_type == "agent.completed":
            answer = data.get("answer")
            self.has_answer = bool(str(answer).strip()) if answer else False
        elif event_type == "agent.end":
            self._observe_agent_end(data)
        elif event_type.startswith("context.llm_summary."):
            if not event_type.endswith(".text"):
                self.summary_attempts += 1
            if event_type == "context.llm_summary.invalid":
                self.summary_invalid += 1
        elif event_type.startswith("context.") and not event_type.endswith(".text"):
            if data.get("request_chars") is not None:
                self.compactions += 1
                self.summarizer_ms += _float(data.get("duration_ms"))
        elif event_type in {"guard.block", "policy.block"}:
            self.policy_blocks += 1

    def _observe_tool_end(self, data: Mapping[str, Any]) -> None:
        self.tool_calls += 1
        self.tool_ms += _float(data.get("exec_ms"))
        if data.get("status") == "error":
            self.tool_errors += 1
            kind = _text(data.get("error_kind")) or "unknown"
            self.error_kinds[kind] = self.error_kinds.get(kind, 0) + 1
            if kind == "command_blocked":
                # A guard or policy block surfaces as a failed tool call,
                # not as a ``policy.block`` event (nothing emits one), so
                # this is where the VCR's "no block" condition is counted.
                self.policy_blocks += 1
        for path in _text_list(data.get("edited_files")):
            if path not in self.edited_files:
                self.edited_files.append(path)

    def _observe_agent_end(self, data: Mapping[str, Any]) -> None:
        # The turn ledger already sums main + summarizer billing; split it
        # back apart so ``tokens`` keeps its documented invariants.
        prompt = _int(data.get("prompt_tokens"))
        completion = _int(data.get("completion_tokens"))
        summarizer_prompt = _int(data.get("summarizer_prompt_tokens"))
        summarizer_completion = _int(data.get("summarizer_completion_tokens"))
        self.prompt_tokens = max(0, prompt - summarizer_prompt)
        self.completion_tokens = max(0, completion - summarizer_completion)
        self.cached_tokens = _int(data.get("cached_tokens"))
        self.summarizer_prompt_tokens = summarizer_prompt
        self.summarizer_completion_tokens = summarizer_completion

    def token_usage(self) -> "TokenUsage":
        return TokenUsage(prompt=self.prompt_tokens,
                          completion=self.completion_tokens,
                          cached=self.cached_tokens,
                          summarizer=(self.summarizer_prompt_tokens
                                      + self.summarizer_completion_tokens))

    def latency(self, duration_ms: float) -> "LatencyBreakdown":
        accounted = self.llm_ms + self.tool_ms + self.summarizer_ms
        return LatencyBreakdown(llm=self.llm_ms, tool=self.tool_ms,
                                summarizer=self.summarizer_ms,
                                overhead=max(0.0, duration_ms - accounted))


@dataclass
class TokenUsage:
    """Token counts of one run, kept split so cost can be attributed.

    Two invariants keep readers from double counting:

    - ``cached`` is a subset of ``prompt`` (the OpenAI/DeepSeek shape),
      so it is reported next to it but never added to ``total``.
    - ``summarizer`` is billed on top of the turn's own ``prompt`` and
      ``completion``, so ``total`` adds it once.
    """

    prompt: int = 0
    completion: int = 0
    cached: int = 0
    summarizer: int = 0

    @property
    def total(self) -> int:
        return self.prompt + self.completion + self.summarizer

    def to_dict(self) -> dict[str, int]:
        return {"prompt": self.prompt, "completion": self.completion,
                "cached": self.cached, "summarizer": self.summarizer,
                "total": self.total}

    @classmethod
    def from_dict(cls, record: Any) -> "TokenUsage":
        data = _dict(record)
        return cls(prompt=_int(data.get("prompt")),
                   completion=_int(data.get("completion")),
                   cached=_int(data.get("cached")),
                   summarizer=_int(data.get("summarizer")))


@dataclass
class LatencyBreakdown:
    """Where one run's wall time went, in milliseconds."""

    llm: float = 0.0
    tool: float = 0.0
    summarizer: float = 0.0
    overhead: float = 0.0

    def __post_init__(self) -> None:
        # A wall-clock subtraction can go ``NaN``/``inf``; such a record
        # would serialize to JSON that no strict reader accepts.
        self.llm = _finite(self.llm)
        self.tool = _finite(self.tool)
        self.summarizer = _finite(self.summarizer)
        self.overhead = _finite(self.overhead)

    def to_dict(self) -> dict[str, float]:
        return {"llm": self.llm, "tool": self.tool,
                "summarizer": self.summarizer, "overhead": self.overhead}

    @classmethod
    def from_dict(cls, record: Any) -> "LatencyBreakdown":
        data = _dict(record)
        return cls(llm=_float(data.get("llm")), tool=_float(data.get("tool")),
                   summarizer=_float(data.get("summarizer")),
                   overhead=_float(data.get("overhead")))


@dataclass
class RunConfig:
    """The conditions one run executed under (``run.config``)."""

    run_id: str = ""
    origin: str = DEFAULT_ORIGIN
    harness_id: str = ""
    genes_hash: str = ""
    model: str = ""
    session_id: str | None = None
    task_id: str | None = None
    run_index: int | None = None
    model_params: dict[str, Any] = field(default_factory=dict)
    tools: list[dict[str, Any]] = field(default_factory=list)
    runtime: dict[str, Any] = field(default_factory=dict)
    env: dict[str, Any] = field(default_factory=dict)
    git: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "origin": self.origin,
            "harness_id": self.harness_id,
            "genes_hash": self.genes_hash,
            "model": self.model,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "run_index": self.run_index,
            "model_params": self.model_params,
            "tools": self.tools,
            "runtime": self.runtime,
            "env": self.env,
            "git": self.git,
        }

    @classmethod
    def from_dict(cls, record: Any) -> "RunConfig":
        data = _dict(record)
        tools = data.get("tools")
        return cls(
            run_id=_text(data.get("run_id")) or "",
            origin=_text(data.get("origin")) or DEFAULT_ORIGIN,
            harness_id=_text(data.get("harness_id")) or "",
            genes_hash=_text(data.get("genes_hash")) or "",
            model=_text(data.get("model")) or "",
            session_id=_text(data.get("session_id")),
            task_id=_text(data.get("task_id")),
            run_index=_optional_int(data.get("run_index")),
            model_params=_dict(data.get("model_params")),
            tools=[dict(tool) for tool in tools
                   if isinstance(tool, Mapping)] if isinstance(tools, list) else [],
            runtime=_dict(data.get("runtime")),
            env=_dict(data.get("env")),
            git=_dict(data.get("git")),
        )


@dataclass
class RunMetrics:
    """The measurements of one run (``run.metrics``, one per JSONL line).

    Join keys live here too: the evaluation index reads ``runs.jsonl``
    alone and must be able to slice without opening traces.
    """

    run_id: str = ""
    origin: str = DEFAULT_ORIGIN
    harness_id: str = ""
    genes_hash: str = ""
    model: str = ""
    session_id: str | None = None
    task_id: str | None = None
    run_index: int | None = None
    status: str = "completed"
    has_answer: bool = False
    started_at: float = 0.0
    finished_at: float = 0.0
    duration_ms: float = 0.0
    iterations: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    error_kinds: dict[str, int] = field(default_factory=dict)
    edited_files: list[str] = field(default_factory=list)
    tokens: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: LatencyBreakdown = field(default_factory=LatencyBreakdown)
    ttft_ms: float | None = None
    retries: int = 0
    summary_invalid: int = 0
    summary_attempts: int = 0
    compactions: int = 0
    policy_blocks: int = 0
    error: dict[str, str] | None = None
    trace_bytes: int = 0
    dropped_events: int = 0

    def __post_init__(self) -> None:
        self.started_at = _finite(self.started_at)
        self.finished_at = _finite(self.finished_at)
        self.duration_ms = _finite(self.duration_ms)
        # First-token latency is optional: an unusable value means
        # "unknown", not "instant" (``0`` would drag the p50 down).
        self.ttft_ms = _optional_float(self.ttft_ms)

    @property
    def artifact_evidence(self) -> bool:
        """Whether the run produced a file change (VCR needs this)."""
        return bool(self.edited_files)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "origin": self.origin,
            "harness_id": self.harness_id,
            "genes_hash": self.genes_hash,
            "model": self.model,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "run_index": self.run_index,
            "status": self.status,
            "has_answer": self.has_answer,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "iterations": self.iterations,
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "error_kinds": self.error_kinds,
            "edited_files": list(self.edited_files),
            "tokens": self.tokens.to_dict(),
            "latency_ms": self.latency_ms.to_dict(),
            "ttft_ms": self.ttft_ms,
            "retries": self.retries,
            "summary_invalid": self.summary_invalid,
            "summary_attempts": self.summary_attempts,
            "compactions": self.compactions,
            "policy_blocks": self.policy_blocks,
            "error": self.error,
            "trace_bytes": self.trace_bytes,
            "dropped_events": self.dropped_events,
        }

    @classmethod
    def from_dict(cls, record: Any) -> "RunMetrics":
        data = _dict(record)
        status = _text(data.get("status")) or "completed"
        if status not in RUN_STATUSES:
            status = "error"
        raw_error = data.get("error")
        error: dict[str, str] | None = None
        if isinstance(raw_error, Mapping):
            stage = _text(raw_error.get("stage"))
            code = _text(raw_error.get("code"))
            if stage or code:
                error = {"stage": stage or "", "code": code or ""}
        error_kinds = _dict(data.get("error_kinds"))
        return cls(
            run_id=_text(data.get("run_id")) or "",
            origin=_text(data.get("origin")) or DEFAULT_ORIGIN,
            harness_id=_text(data.get("harness_id")) or "",
            genes_hash=_text(data.get("genes_hash")) or "",
            model=_text(data.get("model")) or "",
            session_id=_text(data.get("session_id")),
            task_id=_text(data.get("task_id")),
            run_index=_optional_int(data.get("run_index")),
            status=status,
            has_answer=_flag(data.get("has_answer")),
            started_at=_float(data.get("started_at")),
            finished_at=_float(data.get("finished_at")),
            duration_ms=_float(data.get("duration_ms")),
            iterations=_int(data.get("iterations")),
            tool_calls=_int(data.get("tool_calls")),
            tool_errors=_int(data.get("tool_errors")),
            error_kinds={str(key): _int(value)
                         for key, value in error_kinds.items()},
            edited_files=_text_list(data.get("edited_files")),
            tokens=TokenUsage.from_dict(data.get("tokens")),
            latency_ms=LatencyBreakdown.from_dict(data.get("latency_ms")),
            ttft_ms=_optional_float(data.get("ttft_ms")),
            retries=_int(data.get("retries")),
            summary_invalid=_int(data.get("summary_invalid")),
            summary_attempts=_int(data.get("summary_attempts")),
            compactions=_int(data.get("compactions")),
            policy_blocks=_int(data.get("policy_blocks")),
            error=error,
            trace_bytes=_int(data.get("trace_bytes")),
            dropped_events=_int(data.get("dropped_events")),
        )


def _trace_bytes(trace: "RunTrace") -> int:
    path = getattr(trace, "output_path", None)
    try:
        return path.stat().st_size if path is not None else 0
    except OSError:
        return 0


def finalize_run(trace: "RunTrace", *, status: str, answer: str = "",
                 error: Mapping[str, Any] | None = None,
                 origin: str | None = None,
                 session_id: str | None = None,
                 task_id: str | None = None,
                 run_index: int | None = None,
                 model: str = "", path: Path | None = None) -> RunMetrics:
    """Close one run: build its metrics, emit ``run.metrics``, append it.

    The single entry point every run owner (webui, CLI, Harbor) calls on
    its way out, so the trace event and the ``runs.jsonl`` line can never
    disagree and an error/cancel path cannot silently skip the record.
    """
    stats = trace.stats
    finished = time.time()
    started = stats.started_at or getattr(trace, "started_at", finished)
    duration_ms = max(0.0, (finished - started) * 1000)
    harness = getattr(trace, "harness", None)
    record = RunMetrics(
        run_id=trace.run_id,
        origin=origin or getattr(trace, "origin", DEFAULT_ORIGIN),
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


async def afinalize_run(trace: "RunTrace", **kwargs: Any) -> RunMetrics:
    """Finalize a run and persist the closing trace events.

    ``finalize_run`` emits ``run.metrics``, and the owning turn has
    usually already flushed in its own ``finally``.  Emitting then starts
    a fresh writer task, so without the second flush that task parks on
    the queue forever (one leaked task per run, holding the trace alive)
    and the record may never reach the file.  Run owners therefore call
    this instead of ``finalize_run``.

    Neither flush may mask the run's own error, so a failure is logged.

    Cancellation is honoured rather than swallowed, but it does not cost
    the record: a ``cancel()`` landing on either flush (a second click on
    Cancel, cancel-then-delete, shutdown) is remembered, the record is
    still built and appended, and the ``CancelledError`` is re-raised
    once the trace tail is on disk -- ``flush``'s own cancel path drains
    the queue synchronously, so even a second cancel persists
    ``run.metrics``.  ``asyncio.shield`` is deliberately not used: it
    hands the cancellation back to the caller anyway and orphans the
    shielded coroutine as a stray task.
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


async def _flush_trace(trace: "RunTrace", phase: str,
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
    stage = _text(error.get("stage")) or ""
    code = _text(error.get("code")) or ""
    if not stage and not code:
        return None
    return {"stage": stage, "code": code}


def append_run_metrics(record: RunMetrics | Mapping[str, Any],
                       path: Path | None = None) -> Path:
    """Append one metrics line to ``metrics/runs.jsonl``.

    One newline-terminated line is appended with ``O_APPEND`` so
    concurrent writers (Harbor runs several trials at once) cannot
    interleave inside a line.  ``allow_nan=False`` keeps the file valid
    JSON; the dataclasses already clamp non-finite floats.

    A failed write is logged and swallowed: this runs in the ``finally``
    path of a run and must never mask the run's own error.  A truncated
    line fails to parse and is skipped on read.  For the same reason a
    record that no longer serializes (a field mutated after construction
    to ``NaN``) is logged rather than raised.
    """
    entry = record if isinstance(record, RunMetrics) else RunMetrics.from_dict(record)
    target = Path(path) if path is not None else storage.metrics_path()
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
    target = Path(path) if path is not None else storage.metrics_path()
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
            # Valid JSON that is not an object (``null``, ``123``, ``[]``)
            # would otherwise default into a fake ``completed`` run.
            if not isinstance(parsed, Mapping):
                logger.warning("metrics.run.line_skipped path=%s line=%d",
                               target, number)
                continue
            records.append(RunMetrics.from_dict(parsed))
    return records


__all__ = [
    "DEFAULT_ORIGIN", "ORIGINS", "RUN_STATUSES",
    "ENV_WHITELIST", "PAYLOAD_EVENT_TYPES",
    "LatencyBreakdown", "RunConfig", "RunMetrics", "RunStats", "TokenUsage",
    "afinalize_run", "append_run_metrics", "finalize_run", "read_run_metrics",
]
