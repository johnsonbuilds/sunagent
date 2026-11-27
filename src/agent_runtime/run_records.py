"""Run-level record contracts: ``run.config`` and ``run.metrics``.

``RunConfig`` records the conditions of a run (model params, tool schemas,
runtime settings, env whitelist, git state) — without it a trace is not
reproducible.  ``RunMetrics`` records the measurements.  Both carry the join
keys so the ``metrics/runs.jsonl`` index can be sliced without re-reading
traces, and a line can be rebuilt from its trace, so deleting ``metrics/``
loses no information.

This module is deliberately a leaf: it owns the record shapes and their
tolerant parsing, but knows nothing about tracing, storage, or run lifecycle.
Both :mod:`agent_runtime.trace` and :mod:`agent_runtime.metrics` depend on it.

Add fields by giving them a default: readers ignore unknown keys and fall back
to the default for missing ones, so old lines stay readable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping


# Which surface started the run.  A slicing dimension, not a filter: diagnosis
# and improvement runs stay in ``runs.jsonl`` but are excluded from harness and
# board metrics.
ORIGINS = ("cli", "webui", "harbor", "diagnosis", "improvement")
DEFAULT_ORIGIN = "cli"

RUN_STATUSES = ("completed", "error", "cancelled")

# Events whose payload is large enough that keeping them in ``trace.events``
# costs more memory than the diagnosis value they add.  They are still written
# to the trace file and still counted in ``RunStats``.
PAYLOAD_EVENT_TYPES = frozenset({
    "llm.start", "llm.end", "llm.chunk", "tool.start", "tool.end",
    "tool.error", "context.llm_summary.text",
})

# Environment keys that change how a run behaves.  Read explicitly rather than
# "every AGENT_RUNTIME_*" so credentials can never leak into a trace.
ENV_WHITELIST = (
    "AGENT_RUNTIME_HARNESS", "AGENT_RUNTIME_WORKSPACE", "AGENT_RUNTIME_HOME",
    "AGENT_RUNTIME_MEMORY", "AGENT_RUNTIME_MEMORY_DIR",
    "AGENT_RUNTIME_SKILLS_DIR",
    "AGENT_RUNTIME_STREAM_IDLE_TIMEOUT", "AGENT_RUNTIME_MAX_REASONING_CHARS",
    "AGENT_RUNTIME_RETRY_4XX", "AGENT_RUNTIME_REASONING_EFFORT",
    "AGENT_RUNTIME_REASONING_ECHO", "AGENT_RUNTIME_STREAM",
    "AGENT_RUNTIME_STREAM_USAGE", "AGENT_RUNTIME_COMMAND_GUARD",
)


def parse_text(value: Any) -> str | None:
    """Read a non-blank string, trimmed the way every writer trims it."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def parse_int(value: Any, default: int = 0) -> int:
    # ``bool`` is an ``int`` subclass, but ``{"iterations": true}`` must not
    # turn into a count of one.
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def parse_optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def parse_float(value: Any, default: float = 0.0) -> float:
    """Read a finite float; JSON has no ``NaN`` or ``Infinity``."""
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def parse_optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_flag(value: Any) -> bool:
    """Read a boolean flag; a string ``"false"`` is not ``False``."""
    return value if isinstance(value, bool) else False


def parse_finite_float(value: Any, default: float = 0.0) -> float:
    """Clamp a computed float to something JSON can carry."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    return number if math.isfinite(number) else default


def parse_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def parse_text_list(value: Any) -> list[str]:
    """Read a list of paths, dropping blanks so evidence stays truthful."""
    if not isinstance(value, list):
        return []
    paths: list[str] = []
    for item in value:
        text = parse_text(item)
        if text is not None and text not in paths:
            paths.append(text)
    return paths


@dataclass
class TokenUsage:
    """Token counts of one run, kept split so cost can be attributed.

    ``cached`` is a subset of ``prompt``, so it is reported next to it but never
    added to ``total``.  ``summarizer`` is billed on top of the turn's own
    ``prompt``/``completion``, so ``total`` adds it once.
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
        data = parse_dict(record)
        return cls(prompt=parse_int(data.get("prompt")),
                   completion=parse_int(data.get("completion")),
                   cached=parse_int(data.get("cached")),
                   summarizer=parse_int(data.get("summarizer")))


@dataclass
class LatencyBreakdown:
    """Where one run's wall time went, in milliseconds."""

    llm: float = 0.0
    tool: float = 0.0
    summarizer: float = 0.0
    overhead: float = 0.0

    def __post_init__(self) -> None:
        # A wall-clock subtraction can go ``NaN``/``inf``; such a record would
        # serialize to JSON that no strict reader accepts.
        self.llm = parse_finite_float(self.llm)
        self.tool = parse_finite_float(self.tool)
        self.summarizer = parse_finite_float(self.summarizer)
        self.overhead = parse_finite_float(self.overhead)

    def to_dict(self) -> dict[str, float]:
        return {"llm": self.llm, "tool": self.tool,
                "summarizer": self.summarizer, "overhead": self.overhead}

    @classmethod
    def from_dict(cls, record: Any) -> "LatencyBreakdown":
        data = parse_dict(record)
        return cls(llm=parse_float(data.get("llm")), tool=parse_float(data.get("tool")),
                   summarizer=parse_float(data.get("summarizer")),
                   overhead=parse_float(data.get("overhead")))


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
        data = parse_dict(record)
        tools = data.get("tools")
        return cls(
            run_id=parse_text(data.get("run_id")) or "",
            origin=parse_text(data.get("origin")) or DEFAULT_ORIGIN,
            harness_id=parse_text(data.get("harness_id")) or "",
            genes_hash=parse_text(data.get("genes_hash")) or "",
            model=parse_text(data.get("model")) or "",
            session_id=parse_text(data.get("session_id")),
            task_id=parse_text(data.get("task_id")),
            run_index=parse_optional_int(data.get("run_index")),
            model_params=parse_dict(data.get("model_params")),
            tools=[dict(tool) for tool in tools
                   if isinstance(tool, Mapping)] if isinstance(tools, list) else [],
            runtime=parse_dict(data.get("runtime")),
            env=parse_dict(data.get("env")),
            git=parse_dict(data.get("git")),
        )


@dataclass
class RunMetrics:
    """The measurements of one run (``run.metrics``, one per JSONL line).

    Join keys live here too: the evaluation index reads ``runs.jsonl`` alone and
    must be able to slice without opening traces.
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
        self.started_at = parse_finite_float(self.started_at)
        self.finished_at = parse_finite_float(self.finished_at)
        self.duration_ms = parse_finite_float(self.duration_ms)
        # First-token latency is optional: an unusable value means "unknown",
        # not "instant" (``0`` would drag the p50 down).
        self.ttft_ms = parse_optional_float(self.ttft_ms)

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
        data = parse_dict(record)
        status = parse_text(data.get("status")) or "completed"
        if status not in RUN_STATUSES:
            status = "error"
        raw_error = data.get("error")
        error: dict[str, str] | None = None
        if isinstance(raw_error, Mapping):
            stage = parse_text(raw_error.get("stage"))
            code = parse_text(raw_error.get("code"))
            if stage or code:
                error = {"stage": stage or "", "code": code or ""}
        error_kinds = parse_dict(data.get("error_kinds"))
        return cls(
            run_id=parse_text(data.get("run_id")) or "",
            origin=parse_text(data.get("origin")) or DEFAULT_ORIGIN,
            harness_id=parse_text(data.get("harness_id")) or "",
            genes_hash=parse_text(data.get("genes_hash")) or "",
            model=parse_text(data.get("model")) or "",
            session_id=parse_text(data.get("session_id")),
            task_id=parse_text(data.get("task_id")),
            run_index=parse_optional_int(data.get("run_index")),
            status=status,
            has_answer=parse_flag(data.get("has_answer")),
            started_at=parse_float(data.get("started_at")),
            finished_at=parse_float(data.get("finished_at")),
            duration_ms=parse_float(data.get("duration_ms")),
            iterations=parse_int(data.get("iterations")),
            tool_calls=parse_int(data.get("tool_calls")),
            tool_errors=parse_int(data.get("tool_errors")),
            error_kinds={str(key): parse_int(value)
                         for key, value in error_kinds.items()},
            edited_files=parse_text_list(data.get("edited_files")),
            tokens=TokenUsage.from_dict(data.get("tokens")),
            latency_ms=LatencyBreakdown.from_dict(data.get("latency_ms")),
            ttft_ms=parse_optional_float(data.get("ttft_ms")),
            retries=parse_int(data.get("retries")),
            summary_invalid=parse_int(data.get("summary_invalid")),
            summary_attempts=parse_int(data.get("summary_attempts")),
            compactions=parse_int(data.get("compactions")),
            policy_blocks=parse_int(data.get("policy_blocks")),
            error=error,
            trace_bytes=parse_int(data.get("trace_bytes")),
            dropped_events=parse_int(data.get("dropped_events")),
        )


__all__ = [
    "DEFAULT_ORIGIN", "ORIGINS", "RUN_STATUSES",
    "ENV_WHITELIST", "PAYLOAD_EVENT_TYPES",
    "LatencyBreakdown", "RunConfig", "RunMetrics", "TokenUsage",
    "parse_dict", "parse_finite_float", "parse_flag", "parse_float",
    "parse_int", "parse_optional_float", "parse_optional_int", "parse_text",
    "parse_text_list",
]
