"""Evaluation aggregation for the web UI.

The evaluation page has to answer "how are the runs doing?" without
re-reading traces on every request, and without mixing three different
kinds of success.  This module keeps one in-memory index over the three
machine-written sources and computes the metric families of
``docs/evaluation.md`` §6:

``overview``
    Run level, from ``metrics/runs.jsonl``: TCR, tool errors, argument
    validity, tokens, latency/TTFT percentiles, retries, summary invalid.
    Sliced by ``origin`` / ``harness`` / ``model`` / day and **not** tied
    to ``task_id``, so CLI and Harbor runs count the same as chat runs.

``board_overview``
    Board outcome, from ``board_events.jsonl`` plus ``board.json``: TSR,
    first-pass acceptance, rework, funnel, queue wait, and the artifact
    metrics (VCR / hallucinated completion) that need
    ``BoardTask.expects_artifact``.

``benchmarks``
    Harbor outcome, from ``evaluation/records/``: one record per benchmark
    run, kept in its own view.

The three are never blended into one success rate; a run that ends
cleanly, a task a human accepted, and a benchmark trial that scored a
reward are different claims.

Diagnosis sessions are indexed from ``SessionStore`` (``origin`` +
``target``) because that is the only task -> diagnosis link.  They never
enter the board view: the board event stream deliberately excludes them.

Refresh is incremental: both JSONL files are append-only, so only the new
bytes are parsed, and a file that shrank (rotation, truncation) is
reloaded from scratch.  Reads are guarded by one lock; the index is
rebuilt lazily on the first query after a write.
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4

from agent_runtime import storage
from agent_runtime import diagnosis as diagnosis_mod
from agent_runtime.diagnosis import EVALUATION_HARNESS_ID
from agent_runtime.evaluation import DEFAULT_RECORDS_DIR, load_records
from agent_runtime.run_records import ORIGINS, RunMetrics

from .board import BoardStore
from .sessions import Session, SessionStore


logger = logging.getLogger(__name__)


class EvaluationError(Exception):
    """Base class for evaluation query failures."""


class EvaluationNotFound(EvaluationError):
    """The requested task, run, or diagnosis does not exist."""


class EvaluationRequestError(EvaluationError):
    """The query is malformed (unknown range, page size, ...)."""


# Run-level metrics count real work; diagnosis/improvement runs are kept
# in ``runs.jsonl`` but are a separate experiment, never mixed in.
RUN_ORIGINS: tuple[str, ...] = ("cli", "webui", "harbor")

DIAGNOSIS_ORIGINS: tuple[str, ...] = ("diagnosis", "improvement")

# Harnesses whose tasks are judged on leaving a file behind.  ``general-v1``
# and ``readonly-v1`` have no artifact semantics at all, so a task on them
# reports ``None`` (not judged) rather than failing VCR.
ARTIFACT_HARNESSES = frozenset({"coding-v1", "terminal-v1"})

RANGES: dict[str, float | None] = {
    "24h": 86_400.0,
    "7d": 7 * 86_400.0,
    "30d": 30 * 86_400.0,
    "90d": 90 * 86_400.0,
    "all": None,
}
DEFAULT_RANGE = "7d"

QUEUED_STATUS = "todo"
REVIEW_STATUS = "review"
DONE_STATUS = "done"
EXECUTING_STATUS = "doing"

_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200
_MAX_TOOL_ROWS = 50
_MAX_DIGEST_BYTES = 32 * 1024 * 1024
_DIGEST_CACHE_ENTRIES = 256
# Event ids read from a trace for citation checks: one report cites a handful,
# and a cohort bundle cites two runs per case.
_MAX_TRACE_EVENT_IDS = 50_000
_TRACE_ID_CACHE_ENTRIES = 32
# Traces scanned to verify one report's citations; a bundle names at most a
# handful of runs, and a cohort report rarely cites more.
_MAX_CITATION_TRACES = 8
_MAX_REPORT_CHARS = 40_000

# Targets a diagnosis can be started for; task and run name one case, a
# cohort names a filter over the board (docs/evaluation.md §7).
DIAGNOSIS_TARGET_TYPES: tuple[str, ...] = ("task", "run", "cohort")


@dataclass(frozen=True)
class DiagnosisPlan:
    """What the API needs to start a diagnosis turn.

    ``duplicate`` plans carry no session: the same target produced the same
    evidence before, so the caller reopens the existing conversation
    instead of paying for a second run.
    """

    bundle_hash: str
    diagnosis_id: str | None = None
    session: Session | None = None
    message: str = ""
    bundle_path: str | None = None
    duplicate: bool = False
    existing_session_id: str | None = None


def _percentile_sorted(ordered: Sequence[float],
                       fraction: float) -> float | None:
    """Linear-interpolated percentile of an already sorted sample.

    Taking the sorted list lets one sample serve several percentiles, so a
    request sorts each run-length list once instead of once per p50/p95.
    """
    if not ordered:
        return None
    if len(ordered) == 1:
        return round(float(ordered[0]), 3)
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return round(float(ordered[low]), 3)
    span = ordered[high] - ordered[low]
    return round(float(ordered[low]) + span * (position - low), 3)


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    """Linear-interpolated percentile; ``None`` for an empty sample."""
    return _percentile_sorted(sorted(values), fraction)


def _ratio(part: float, whole: float) -> float | None:
    """A rate, or ``None`` when the denominator is empty.

    ``None`` is not ``0.0``: "no VCR-eligible run" and "every eligible run
    failed" must not render as the same number.
    """
    if not whole:
        return None
    return round(part / whole, 4)


def _mean(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 3)


class _DayNames:
    """Timestamp -> local calendar day, memoised per minute.

    Formatting a day per run was the single most expensive step of the
    10k-run overview.  Local UTC offsets are whole minutes, so a minute
    never straddles local midnight and a run's day is the day of its
    minute start; one week of minutes is cached at a time.
    """

    __slots__ = ("_cache",)
    _LIMIT = 10_080

    def __init__(self) -> None:
        self._cache: dict[int, str] = {}

    def __call__(self, timestamp: float) -> str:
        minute = int(timestamp) // 60
        day = self._cache.get(minute)
        if day is None:
            moment = time.localtime(minute * 60)
            day = "%04d-%02d-%02d" % (moment.tm_year, moment.tm_mon,
                                       moment.tm_mday)
            if len(self._cache) >= self._LIMIT:
                self._cache.clear()
            self._cache[minute] = day
        return day


_day = _DayNames()


class _RunBucket:
    """Accumulator for one day or one slice key of ``overview()``."""

    __slots__ = ("runs", "completed", "errors", "tokens", "durations")

    def __init__(self) -> None:
        self.runs = 0
        self.completed = 0
        self.errors = 0
        self.tokens = 0
        self.durations: list[float] = []


def _bucket_into(buckets: dict[str, _RunBucket], key: str, completed: bool,
                 errored: bool, tokens: int, duration: float) -> None:
    bucket = buckets.get(key)
    if bucket is None:
        bucket = buckets[key] = _RunBucket()
    bucket.runs += 1
    if completed:
        bucket.completed += 1
    if errored:
        bucket.errors += 1
    bucket.tokens += tokens
    bucket.durations.append(duration)


def _trend_row(day: str, bucket: _RunBucket) -> dict[str, Any]:
    ordered = sorted(bucket.durations)
    return {
        "day": day,
        "runs": bucket.runs,
        "tcr": _ratio(bucket.completed, bucket.runs),
        "tokens": bucket.tokens,
        "errors": bucket.errors,
        "duration_p50_ms": _percentile_sorted(ordered, 0.5),
        "duration_p95_ms": _percentile_sorted(ordered, 0.95),
    }


def _slice_row(key: str, bucket: _RunBucket) -> dict[str, Any]:
    return {
        "key": key,
        "runs": bucket.runs,
        "tcr": _ratio(bucket.completed, bucket.runs),
        "tokens": bucket.tokens,
        "duration_p95_ms": _percentile_sorted(sorted(bucket.durations), 0.95),
    }


# Fixed lower bounds of the overview histograms.  The labels are language
# neutral so the front end can render them verbatim; a histogram over a
# fixed grid also keeps ``overview()`` at one pass over the runs.
ITERATION_BUCKETS: tuple[tuple[str, float], ...] = (
    ("0–1", 0.0), ("2", 2.0), ("3–4", 3.0), ("5–8", 5.0),
    ("9–16", 9.0), ("17–32", 17.0), ("33+", 33.0))
DURATION_BUCKETS: tuple[tuple[str, float], ...] = (
    ("<1s", 0.0), ("1–5s", 1_000.0), ("5–15s", 5_000.0),
    ("15–60s", 15_000.0), ("1–5m", 60_000.0), ("5m+", 300_000.0))
_ITERATION_BOUNDS = tuple(bound for _, bound in ITERATION_BUCKETS)
_DURATION_BOUNDS = tuple(bound for _, bound in DURATION_BUCKETS)


def _bucket_of(bounds: Sequence[float], value: float) -> int:
    """Index of the fixed bucket that holds ``value``."""
    return max(0, bisect.bisect_right(bounds, value) - 1)


def _histogram(buckets: Sequence[tuple[str, float]],
               counts: Sequence[int]) -> list[dict[str, Any]]:
    return [{"label": label, "count": count}
            for (label, _), count in zip(buckets, counts)]


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _first_text(record: Mapping[str, Any], *keys: str) -> str | None:
    """First non-blank spelling of one field (events use two of them)."""
    for key in keys:
        text = _text(record.get(key))
        if text is not None:
            return text
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_range(value: str | None) -> tuple[str, float | None]:
    """Validate a ``range`` query value and return its window length."""
    name = (value or DEFAULT_RANGE).strip().lower()
    if name not in RANGES:
        raise EvaluationRequestError(
            "range must be one of " + ", ".join(RANGES))
    return name, RANGES[name]


@dataclass(frozen=True)
class BoardEvent:
    """One record of ``board_events.jsonl`` (board lifecycle only)."""

    type: str
    at: float
    task_id: str
    run_id: str | None = None
    from_status: str | None = None
    to_status: str | None = None
    queue_wait_ms: float | None = None
    run_index: int | None = None
    status: str | None = None
    deferred: bool | None = None
    resume: bool | None = None

    @classmethod
    def from_record(cls, record: Any) -> "BoardEvent | None":
        """Read one event, skipping records we cannot attribute."""
        if not isinstance(record, Mapping):
            return None
        event_type = _text(record.get("type"))
        task_id = _text(record.get("task_id"))
        if event_type is None or task_id is None:
            return None
        run_index = record.get("run_index")
        return cls(
            type=event_type,
            at=_number(record.get("at")) or 0.0,
            task_id=task_id,
            run_id=_text(record.get("run_id")),
            # ``status_changed`` writes ``from``/``to``; ``comment_added``
            # writes the spelled-out keys.  Reading only one spelling made
            # every comment-triggered rework invisible.
            from_status=_first_text(record, "from", "from_status"),
            to_status=_first_text(record, "to", "to_status"),
            queue_wait_ms=_number(record.get("queue_wait_ms")),
            run_index=run_index if isinstance(run_index, int) else None,
            status=_text(record.get("status")),
            deferred=record.get("deferred") if isinstance(
                record.get("deferred"), bool) else None,
            resume=record.get("resume") if isinstance(
                record.get("resume"), bool) else None,
        )


@dataclass
class TaskFacts:
    """What the event stream says happened to one card."""

    task_id: str
    title: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    status: str = "backlog"
    harness: str | None = None
    model: str | None = None
    expects_artifact: bool | None = None
    runs: list[dict[str, Any]] = field(default_factory=list)
    comments: int = 0
    last_error: str | None = None
    executed: bool = False
    first_run_at: float | None = None
    run_count: int = 0
    completed_runs: int = 0
    reached_review: bool = False
    done: bool = False
    rework_count: int = 0
    rework_reasons: Counter[str] = field(default_factory=Counter)
    queue_waits: list[float] = field(default_factory=list)

    @property
    def reworked(self) -> bool:
        return self.rework_count > 0

    @property
    def expects_artifact_resolved(self) -> bool | None:
        """``None`` means the task is not judged on producing files."""
        return self.expects_artifact

    def completed_responses(self) -> list[dict[str, Any]]:
        """Runs that claimed to be done and said something about it."""
        return [run for run in self.runs
                if run.get("status") == "completed"
                and str(run.get("response") or "").strip()]


def _expects_artifact(task: Mapping[str, Any]) -> bool | None:
    """Resolve the artifact requirement: explicit value first, then harness.

    The rule lives here, in one place, so ``VCR`` and ``hallucinated
    completion`` cannot disagree about which tasks are judged at all.
    """
    explicit = task.get("expects_artifact")
    if isinstance(explicit, bool):
        return explicit
    harness = _text(task.get("harness"))
    if harness in ARTIFACT_HARNESSES:
        return True
    return None


def _stat_of(path: Path) -> tuple[int, int | None]:
    """Size and inode; ``(0, None)`` when the file is missing."""
    try:
        stat = path.stat()
    except OSError:
        return 0, None
    return stat.st_size, stat.st_ino


def _rotated(inode: int | None, size: int,
             known_inode: int | None, offset: int) -> bool:
    """Whether the bytes after ``offset`` no longer belong to this file.

    A shrink catches truncation; an inode change catches the atomic
    replace a log rotation does (the replacement may well be larger).
    """
    if known_inode is not None and inode is not None and inode != known_inode:
        return True
    return size < offset


def _read_appended(path: Path, offset: int) -> tuple[list[bytes], int]:
    """Read whole lines from ``offset``; leave a trailing partial line behind.

    A concurrent writer appends with ``O_APPEND``, so the reader can catch
    a half-written line.  Returning the offset of the last complete
    newline means that line is re-read once it is finished instead of
    being parsed (and logged) as corrupt.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return [], offset
    if size <= offset:
        return [], offset
    try:
        with path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(size - offset)
    except OSError as exc:
        logger.warning("webui.evaluation.read_failed path=%s error=%s",
                       path, exc)
        return [], offset
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    complete = data[:end + 1]
    return complete.splitlines(), offset + len(complete)


class EvaluationStore:
    """In-memory index over runs, board events, sessions, benchmark records."""

    def __init__(self, sessions: SessionStore, board: BoardStore, *,
                 root: Path | None = None,
                 records_dir: str | Path | None = None) -> None:
        self._sessions = sessions
        self._board = board
        if root is None:
            self._runs_path = storage.metrics_path()
            self._events_path = storage.board_events_path()
            self._traces_dir = storage.traces_dir()
            self._diagnosis_dir = storage.diagnosis_dir()
        else:
            base = Path(root)
            self._runs_path = base / "metrics" / "runs.jsonl"
            self._events_path = base / "board_events.jsonl"
            self._traces_dir = base / "traces"
            self._diagnosis_dir = base / "diagnosis"
        self._records_dir = (Path(records_dir) if records_dir is not None
                             else Path(DEFAULT_RECORDS_DIR))
        self._lock = RLock()
        # Keyed by ``run_id`` (dicts keep insertion order): a duplicate row
        # must not be counted twice by ``overview()``.
        self._runs_by_id: dict[str, RunMetrics] = {}
        self._runs_offset = 0
        self._runs_ino: int | None = None
        self._events: list[BoardEvent] = []
        self._events_offset = 0
        self._events_ino: int | None = None
        self._diagnosis_sessions: list[dict[str, Any]] = []
        self._diagnoses_by_target: dict[str, list[dict[str, Any]]] = {}
        self._session_signature: tuple[Any, ...] | None = None
        self._records: list[dict[str, Any]] = []
        self._records_signature: tuple[Any, ...] | None = None
        self._digests: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
        # Event ids per trace, for report citation checks (bounded cache).
        self._trace_ids: dict[str, tuple[tuple[Any, ...], set[str]]] = {}
        self._skipped_lines = 0
        self.last_refresh_at = 0.0

    # -- loading ---------------------------------------------------------

    @property
    def runs_path(self) -> Path:
        return self._runs_path

    @property
    def events_path(self) -> Path:
        return self._events_path

    @property
    def diagnosis_dir(self) -> Path:
        """Where this store reads and writes diagnosis bundles."""
        return self._diagnosis_dir

    @property
    def run_count(self) -> int:
        with self._lock:
            return len(self._runs_by_id)

    @property
    def event_count(self) -> int:
        with self._lock:
            return len(self._events)

    def refresh(self, *, force: bool = False) -> dict[str, Any]:
        """Bring the index up to date; only new bytes are parsed."""
        with self._lock:
            if force:
                # The counter describes the index, so a rebuild resets it;
                # otherwise every forced refresh would keep adding up.
                self._skipped_lines = 0
            runs_added = self._refresh_runs(force)
            events_added = self._refresh_events(force)
            self._refresh_sessions(force)
            if force:
                self._digests.clear()
                self._records = []
                self._records_signature = None
            self.last_refresh_at = time.time()
            return {"runs": len(self._runs_by_id),
                    "events": len(self._events),
                    "runs_added": runs_added, "events_added": events_added,
                    "skipped_lines": self._skipped_lines}

    def _refresh_runs(self, force: bool) -> int:
        size, inode = _stat_of(self._runs_path)
        if force or _rotated(inode, size, self._runs_ino, self._runs_offset):
            # Rotation or truncation: the offset no longer refers to bytes
            # of this file, so the index is rebuilt from scratch.
            self._runs_by_id = {}
            self._runs_offset = 0
        lines, offset = _read_appended(self._runs_path, self._runs_offset)
        added = 0
        for line in lines:
            record = self._parse_run(line)
            if record is None:
                continue
            # Last line wins, but a repeated id keeps one entry: a copied
            # or re-appended file must not inflate the totals.
            self._runs_by_id[record.run_id] = record
            added += 1
        self._runs_ino = inode
        self._runs_offset = offset
        return added

    def _parse_run(self, raw: bytes) -> RunMetrics | None:
        text = raw.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except ValueError:
            self._skipped_lines += 1
            logger.warning("webui.evaluation.run_line_skipped path=%s",
                           self._runs_path)
            return None
        if not isinstance(parsed, Mapping):
            self._skipped_lines += 1
            logger.warning("webui.evaluation.run_line_skipped path=%s",
                           self._runs_path)
            return None
        record = RunMetrics.from_dict(parsed)
        if not record.run_id:
            # Without an id the row can never be joined or drilled into.
            self._skipped_lines += 1
            return None
        return record

    def _refresh_events(self, force: bool) -> int:
        size, inode = _stat_of(self._events_path)
        if force or _rotated(inode, size, self._events_ino, self._events_offset):
            self._events = []
            self._events_offset = 0
        lines, offset = _read_appended(self._events_path, self._events_offset)
        added = 0
        for line in lines:
            text = line.strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except ValueError:
                self._skipped_lines += 1
                logger.warning("webui.evaluation.event_line_skipped path=%s",
                               self._events_path)
                continue
            event = BoardEvent.from_record(parsed)
            if event is None:
                self._skipped_lines += 1
                continue
            self._events.append(event)
            added += 1
        self._events_ino = inode
        self._events_offset = offset
        return added

    def _refresh_sessions(self, force: bool) -> None:
        """Index diagnosis sessions (``origin`` + ``target``)."""
        sessions = self._sessions.list()
        signature = tuple(
            (session.id, session.origin, session.updated_at, session.busy,
             session.report_segment_id, json.dumps(session.target,
                                                  sort_keys=True, default=str))
            for session in sessions
        )
        if not force and signature == self._session_signature:
            return
        self._session_signature = signature
        rows: list[dict[str, Any]] = []
        by_target: dict[str, list[dict[str, Any]]] = {}
        for session in sessions:
            if session.origin not in DIAGNOSIS_ORIGINS:
                continue
            target = session.target if isinstance(session.target, dict) else {}
            row = {
                "session_id": session.id,
                "title": session.title,
                "origin": session.origin,
                "target": target or None,
                "target_type": _text(target.get("type")),
                "target_id": _text(target.get("id")),
                "report_segment_id": session.report_segment_id,
                "busy": bool(session.busy),
                "bundle_hash": session.bundle_hash,
                "diagnosis_id": session.diagnosis_id,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
            }
            rows.append(row)
            if row["target_id"]:
                by_target.setdefault(row["target_id"], []).append(row)
        self._diagnosis_sessions = rows
        self._diagnoses_by_target = by_target

    # -- queries ---------------------------------------------------------

    def overview(self, *, origins: Iterable[str] | None = None,
                 range_: str | None = None, harness: str | None = None,
                 model: str | None = None,
                 now: float | None = None) -> dict[str, Any]:
        """Run-level KPIs, trends, and slices (``jq``-able dicts only)."""
        name, window = parse_range(range_)
        with self._lock:
            self.refresh()
            cutoff = (now if now is not None else time.time()) - window \
                if window is not None else None
            scope = self._run_scope(origins)
            selected: list[RunMetrics] = []
            excluded_origin = 0
            for record in self._runs_by_id.values():
                # Inlined run time (``finished_at`` else ``started_at``):
                # this walk touches every recorded run.
                if cutoff is not None and \
                        (record.finished_at or record.started_at) < cutoff:
                    continue
                if scope is not None and record.origin not in scope:
                    excluded_origin += 1
                    continue
                if harness and record.harness_id != harness:
                    continue
                if model and record.model != model:
                    continue
                selected.append(record)
            return self._overview_payload(selected, name=name,
                                          origins=sorted(scope) if scope
                                          else None,
                                          excluded_origin=excluded_origin,
                                          cutoff=cutoff)

    @staticmethod
    def _run_scope(origins: Iterable[str] | None) -> set[str] | None:
        if origins is None:
            return set(RUN_ORIGINS)
        wanted = {origin for origin in origins if origin}
        if not wanted:
            return set(RUN_ORIGINS)
        unknown = wanted - set(ORIGINS)
        if unknown:
            raise EvaluationRequestError(
                "unknown origin: " + ", ".join(sorted(unknown)))
        return wanted

    def _overview_payload(self, runs: Sequence[RunMetrics], *, name: str,
                          origins: Sequence[str] | None,
                          excluded_origin: int,
                          cutoff: float | None) -> dict[str, Any]:
        # One pass feeds every KPI, and each percentile receives an already
        # sorted sample: at 10k runs the old per-KPI walks plus repeated
        # sorting dominated the request, so the loop keeps its counters in
        # locals and folds the trend/slice buckets as it goes.
        total = len(runs)
        completed = errored = cancelled = 0
        tool_calls = tool_errors = retried = retries = 0
        summary_attempts = summary_invalid = compactions = 0
        policy_blocks = dropped_events = trace_bytes = 0
        prompt_tokens = completion_tokens = cached_tokens = 0
        summarizer_tokens = total_tokens = 0
        llm_ms = tool_ms = summarizer_ms = overhead_ms = 0.0
        error_kinds: dict[str, int] = {}
        stages: Counter[tuple[str, str]] = Counter()
        durations: list[float] = []
        ttfts: list[float] = []
        iteration_counts = [0] * len(ITERATION_BUCKETS)
        duration_counts = [0] * len(DURATION_BUCKETS)
        days: dict[str, _RunBucket] = {}
        slices: dict[str, dict[str, _RunBucket]] = {
            "origin": {}, "harness": {}, "model": {}}

        for record in runs:
            status = record.status
            accepted = status == "completed"
            if accepted:
                completed += 1
            elif status == "error":
                errored += 1
            elif status == "cancelled":
                cancelled += 1
            tokens = record.tokens
            run_tokens = tokens.prompt + tokens.completion + tokens.summarizer
            prompt_tokens += tokens.prompt
            completion_tokens += tokens.completion
            cached_tokens += tokens.cached
            summarizer_tokens += tokens.summarizer
            total_tokens += run_tokens
            latency = record.latency_ms
            llm_ms += latency.llm
            tool_ms += latency.tool
            summarizer_ms += latency.summarizer
            overhead_ms += latency.overhead
            duration = record.duration_ms
            durations.append(duration)
            iteration_counts[_bucket_of(_ITERATION_BOUNDS,
                                        float(record.iterations))] += 1
            duration_counts[_bucket_of(_DURATION_BOUNDS, duration)] += 1
            if record.ttft_ms is not None:
                ttfts.append(record.ttft_ms)
            tool_calls += record.tool_calls
            tool_errors += record.tool_errors
            # A plain ``dict`` beats ``Counter.update`` per record; the
            # ordered top-N view is built once at the end.
            for kind, count in record.error_kinds.items():
                error_kinds[kind] = error_kinds.get(kind, 0) + count
            error = record.error
            if error:
                stages[(error.get("stage") or "",
                        error.get("code") or "")] += 1
            summary_attempts += record.summary_attempts
            summary_invalid += record.summary_invalid
            compactions += record.compactions
            policy_blocks += record.policy_blocks
            dropped_events += record.dropped_events
            trace_bytes += record.trace_bytes
            if record.retries > 0:
                retried += 1
                retries += record.retries
            stamp = record.finished_at or record.started_at
            if stamp > 0:
                _bucket_into(days, _day(stamp), accepted,
                             status == "error", run_tokens, duration)
            for dimension, key in (("origin", record.origin),
                                   ("harness", record.harness_id),
                                   ("model", record.model)):
                _bucket_into(slices[dimension], key or "", accepted,
                             status == "error", run_tokens, duration)

        # ``trace_tool_rejection`` stamps schema rejections as
        # ``error_kind="validation"``; ``schema`` is kept for older rows.
        schema_errors = (error_kinds.get("validation", 0)
                         + error_kinds.get("schema", 0))
        schema_pass_rate = (None if not tool_calls
                            else round(max(0.0, 1 - schema_errors / tool_calls),
                                       4))
        ordered = sorted(durations)
        ordered_ttft = sorted(ttfts)
        return {
            "range": name,
            "origins": list(origins) if origins else None,
            "since": cutoff,
            "kpis": {
                "runs": total,
                "tcr": _ratio(completed, total),
                "error_rate": _ratio(errored, total),
                "cancelled_rate": _ratio(cancelled, total),
                "tool_calls": tool_calls,
                "tool_error_rate": _ratio(tool_errors, tool_calls),
                "schema_pass_rate": schema_pass_rate,
                "retry_rate": _ratio(retried, total),
                "summary_invalid_rate": _ratio(summary_invalid,
                                               summary_attempts),
                "policy_blocks": policy_blocks,
                "tokens_total": total_tokens,
                "tokens_per_run": (None if not total
                                   else round(total_tokens / total, 1)),
                "duration_p50_ms": _percentile_sorted(ordered, 0.5),
                "duration_p95_ms": _percentile_sorted(ordered, 0.95),
                "ttft_p50_ms": _percentile_sorted(ordered_ttft, 0.5),
                "ttft_p95_ms": _percentile_sorted(ordered_ttft, 0.95),
                "dropped_events": dropped_events,
                "trace_bytes": trace_bytes,
            },
            "tokens": {
                "prompt": prompt_tokens,
                "completion": completion_tokens,
                "cached": cached_tokens,
                "summarizer": summarizer_tokens,
                "total": total_tokens,
            },
            "latency_ms": {
                "llm": round(llm_ms, 3),
                "tool": round(tool_ms, 3),
                "summarizer": round(summarizer_ms, 3),
                "overhead": round(overhead_ms, 3),
            },
            "retries": {"runs": retried, "total": retries},
            "summary": {
                "attempts": summary_attempts,
                "invalid": summary_invalid,
                "compactions": compactions,
            },
            "errors": [
                {"stage": stage, "code": code, "count": count}
                for (stage, code), count in stages.most_common(20)
            ],
            "error_kinds": [
                {"kind": kind, "count": count}
                for kind, count in sorted(error_kinds.items(),
                                          key=lambda item: item[1],
                                          reverse=True)[:20]
            ],
            "trends": [_trend_row(day, days[day]) for day in sorted(days)],
            "histograms": {
                "iterations": _histogram(ITERATION_BUCKETS, iteration_counts),
                "duration_ms": _histogram(DURATION_BUCKETS, duration_counts),
            },
            "slices": {dimension: [_slice_row(key, group[key])
                                   for key in sorted(group)]
                       for dimension, group in slices.items()},
            "excluded": {"runs_by_origin": excluded_origin},
            "generated_at": time.time(),
        }

    def board_overview(self, *, range_: str | None = None,
                       now: float | None = None) -> dict[str, Any]:
        """Board outcome: funnel, TSR, first pass, rework, queue, artifacts."""
        name, window = parse_range(range_)
        with self._lock:
            self.refresh()
            moment = now if now is not None else time.time()
            cutoff = moment - window if window is not None else None
            facts = self._task_facts()
            scoped = [fact for fact in facts
                      if cutoff is None or fact.updated_at >= cutoff]
            return self._board_payload(scoped, name=name, cutoff=cutoff,
                                       moment=moment)

    def _board_payload(self, facts: Sequence[TaskFacts], *, name: str,
                       cutoff: float | None, moment: float) -> dict[str, Any]:
        executed = [fact for fact in facts if fact.executed]
        reviewed = [fact for fact in facts if fact.reached_review]
        done = [fact for fact in facts if fact.done]
        reworked = [fact for fact in reviewed if fact.reworked]
        accepted = [fact for fact in executed if fact.done]
        first_pass = [fact for fact in accepted if not fact.reworked]
        waits = [wait for fact in facts for wait in fact.queue_waits]
        reasons: Counter[str] = Counter()
        for fact in facts:
            reasons.update(fact.rework_reasons)
        artifact = self._artifact_payload(executed)
        executed_tokens = sum(record.tokens.total
                              for fact in executed
                              for record in self._fact_metrics(fact))
        return {
            "range": name,
            "since": cutoff,
            "funnel": {
                "created": len(facts),
                "executed": len(executed),
                "run_completed": len([fact for fact in facts
                                      if fact.completed_runs > 0]),
                "review": len(reviewed),
                "done": len(done),
            },
            "kpis": {
                "tsr": _ratio(len(accepted), len(executed)),
                "first_pass_rate": _ratio(len(first_pass), len(executed)),
                "rework_rate": _ratio(len(reworked), len(reviewed)),
                "queue_wait_p50_ms": _percentile(waits, 0.5),
                "queue_wait_p95_ms": _percentile(waits, 0.95),
                "queue_wait_mean_ms": _mean(waits),
                "tokens_per_task": (None if not executed
                                    else round(executed_tokens / len(executed), 1)),
                "runs_per_task": (None if not executed
                                  else round(sum(fact.run_count
                                                 for fact in executed)
                                             / len(executed), 2)),
                "vcr": artifact["vcr"],
                "hallucinated_rate": artifact["hallucinated_rate"],
            },
            "counts": {
                "tasks": len(facts),
                "executed": len(executed),
                "accepted": len(accepted),
                "reworked": len(reworked),
                "first_pass": len(first_pass),
                "rework_events": sum(fact.rework_count for fact in facts),
                "runs": sum(fact.run_count for fact in facts),
                "completed_runs": sum(fact.completed_runs for fact in facts),
            },
            "rework_reasons": [
                {"reason": reason, "count": count}
                for reason, count in reasons.most_common()
            ],
            "artifact": artifact,
            "tsr_trend": self._board_trends(facts),
            "top_offenders": self._top_offenders(facts),
            "generated_at": time.time(),
        }

    def _board_trends(self, facts: Sequence[TaskFacts]) -> list[dict[str, Any]]:
        """Per-day acceptance, bucketed by when the task was last touched."""
        buckets: dict[str, list[TaskFacts]] = {}
        for fact in facts:
            stamp = fact.updated_at or fact.created_at
            if stamp <= 0:
                continue
            buckets.setdefault(_day(stamp), []).append(fact)
        rows: list[dict[str, Any]] = []
        for day in sorted(buckets):
            group = buckets[day]
            executed = [fact for fact in group if fact.executed]
            accepted = [fact for fact in executed if fact.done]
            rows.append({
                "day": day,
                "tasks": len(group),
                "executed": len(executed),
                "done": len(accepted),
                "tsr": _ratio(len(accepted), len(executed)),
            })
        return rows

    def _fact_metrics(self, fact: TaskFacts) -> list[RunMetrics]:
        """Metrics rows of one task's runs, in board order."""
        return [self._runs_by_id[run["run_id"]] for run in fact.runs
                if run.get("run_id") in self._runs_by_id]

    def _top_offenders(self, facts: Sequence[TaskFacts],
                       limit: int = 5) -> dict[str, list[dict[str, Any]]]:
        rows = []
        for fact in facts:
            metrics = self._fact_metrics(fact)
            rows.append({
                "task_id": fact.task_id,
                "status": fact.status,
                "runs": fact.run_count,
                "tokens": sum(record.tokens.total for record in metrics),
                "wall_ms": round(sum(record.duration_ms for record in metrics), 3),
                "rework": fact.rework_count,
            })

        def top(key: str) -> list[dict[str, Any]]:
            ordered = sorted(rows, key=lambda row: row[key], reverse=True)
            return [row for row in ordered[:limit] if row[key]]

        return {"tokens": top("tokens"), "wall_ms": top("wall_ms"),
                "rework": top("rework")}

    def _artifact_payload(self, executed: Sequence[TaskFacts]) -> dict[str, Any]:
        """VCR and hallucinated completion over tasks that need a file.

        Both are ``None`` when no task in scope declares an artifact
        requirement: "nothing to judge" is not "0% success".
        """
        judged = [fact for fact in executed
                  if fact.expects_artifact_resolved is True]
        vcr_total = 0
        vcr_passed = 0
        hallucinated: list[dict[str, Any]] = []
        claiming = 0
        unmeasured = 0
        for fact in judged:
            metrics = self._fact_metrics(fact)
            # Only a claim backed by a ``run.metrics`` row can be judged.  A
            # run from before the index existed is *unknown*, and counting
            # it as a hallucination would accuse the data of what it cannot
            # show (`task_detail` reports such a run as ``evidence: missing``).
            claims = fact.completed_responses()
            sampled = [run for run in claims
                       if run.get("run_id") in self._runs_by_id]
            if sampled:
                claiming += 1
            elif claims:
                unmeasured += 1
            for record in metrics:
                vcr_total += 1
                if (record.status == "completed" and record.has_answer
                        and record.artifact_evidence
                        and record.policy_blocks == 0):
                    vcr_passed += 1
            empty = [run for run in sampled
                     if not self._runs_by_id[run["run_id"]].artifact_evidence]
            if empty and fact.reworked:
                hallucinated.append({
                    "task_id": fact.task_id,
                    "run_id": empty[-1].get("run_id"),
                    "status": fact.status,
                    "runs": fact.run_count,
                    "rework_count": fact.rework_count,
                })
        return {
            "judged_tasks": len(judged),
            "claiming_tasks": claiming,
            "unmeasured_claiming_tasks": unmeasured,
            "vcr_runs": vcr_total,
            "vcr_passed": vcr_passed,
            "vcr": _ratio(vcr_passed, vcr_total),
            "hallucinated_count": len(hallucinated),
            "hallucinated_rate": _ratio(len(hallucinated), claiming),
            "hallucinated_tasks": hallucinated[:20],
        }

    def _task_facts(self) -> list[TaskFacts]:
        """Join ``board.json`` (current state) with the event history."""
        by_task: dict[str, list[BoardEvent]] = {}
        for event in self._events:
            by_task.setdefault(event.task_id, []).append(event)
        facts: list[TaskFacts] = []
        for record in self._board.list_all():
            task_id = record.get("id")
            if not isinstance(task_id, str):
                continue
            fact = TaskFacts(
                task_id=task_id,
                title=str(record.get("title") or task_id),
                created_at=_number(record.get("created_at")) or 0.0,
                updated_at=_number(record.get("updated_at")) or 0.0,
                status=str(record.get("status") or "backlog"),
                harness=_text(record.get("harness")),
                model=_text(record.get("model")),
                expects_artifact=_expects_artifact(record),
                runs=[run for run in record.get("runs") or []
                      if isinstance(run, Mapping)],
                comments=len(record.get("comments") or []),
                last_error=_text(record.get("last_error")),
            )
            events = sorted(by_task.get(task_id, []), key=lambda item: item.at)
            self._apply_events(fact, events)
            facts.append(fact)
        return facts

    def _apply_events(self, fact: TaskFacts,
                      events: Sequence[BoardEvent]) -> None:
        claimed_at: float | None = None
        for event in events:
            if event.type == "task_created" and not fact.created_at:
                fact.created_at = event.at
            elif event.type == "run_claimed":
                fact.executed = True
                if claimed_at is None or event.at < claimed_at:
                    claimed_at = event.at
                if event.queue_wait_ms is not None:
                    fact.queue_waits.append(event.queue_wait_ms)
            elif event.type == "status_changed":
                if event.to_status == REVIEW_STATUS:
                    fact.reached_review = True
                if event.to_status == DONE_STATUS:
                    fact.reached_review = True
            elif event.type == "run_finished":
                if event.to_status in (REVIEW_STATUS, DONE_STATUS):
                    fact.reached_review = True
        # A card that is in Review/Done now was reviewed even if the event
        # that moved it predates the event stream.
        if fact.status in (REVIEW_STATUS, DONE_STATUS):
            fact.reached_review = True
        # ``done`` is the *current* column, never history: a card sent back
        # to Todo or Review must stop counting as accepted.
        fact.done = fact.status == DONE_STATUS
        fact.run_count = len(fact.runs)
        fact.completed_runs = sum(1 for run in fact.runs
                                  if run.get("status") == "completed")
        fact.first_run_at = claimed_at
        if not fact.executed and (fact.runs or fact.status == EXECUTING_STATUS):
            # Rows written before the event stream existed still count.
            fact.executed = True
        if not fact.executed:
            return
        start = claimed_at
        if start is None:
            stamps = [_number(run.get("started_at")) or 0.0
                      for run in fact.runs]
            start = min(stamps) if stamps else None
        for event in events:
            if start is not None and event.at and event.at < start:
                # Rework only counts after the task was actually executed.
                continue
            reason = None
            if event.type == "status_changed":
                if (event.from_status == REVIEW_STATUS
                        and event.to_status in (QUEUED_STATUS,
                                                EXECUTING_STATUS)):
                    reason = "review_to_todo"
            elif event.type == "comment_added":
                if event.from_status == REVIEW_STATUS:
                    reason = "comment_on_review"
                elif event.from_status == EXECUTING_STATUS:
                    reason = "comment_during_run"
            if reason is not None:
                fact.rework_count += 1
                fact.rework_reasons[reason] += 1

    def tasks(self, *, range_: str | None = None, status: str | None = None,
              harness: str | None = None, model: str | None = None,
              origins: Iterable[str] | None = None, query: str | None = None,
              sort: str = "updated_at", order: str = "desc",
              page: int = 1, page_size: int = _DEFAULT_PAGE_SIZE,
              now: float | None = None) -> dict[str, Any]:
        """Task rows for the table view, with run-derived totals."""
        _, window = parse_range(range_)
        if page < 1:
            raise EvaluationRequestError("page must be >= 1")
        if page_size < 1 or page_size > _MAX_PAGE_SIZE:
            raise EvaluationRequestError(
                f"page_size must be between 1 and {_MAX_PAGE_SIZE}")
        if sort not in ("updated_at", "created_at", "status", "runs",
                        "tokens", "wall_ms", "rework", "title"):
            raise EvaluationRequestError("unknown sort field")
        if order not in ("asc", "desc"):
            raise EvaluationRequestError("order must be asc or desc")
        wanted_origins = [origin for origin in (origins or []) if origin]
        scope = self._run_scope(wanted_origins or None)
        explicit_origins = bool(wanted_origins)
        with self._lock:
            self.refresh()
            moment = now if now is not None else time.time()
            cutoff = moment - window if window is not None else None
            needle = (query or "").strip().lower()
            rows: list[dict[str, Any]] = []
            for fact in self._task_facts():
                if cutoff is not None and fact.updated_at < cutoff:
                    continue
                if status and fact.status != status:
                    continue
                if harness and fact.harness != harness:
                    continue
                if model and fact.model != model:
                    continue
                if needle and needle not in fact.title.lower() \
                        and needle not in fact.task_id.lower():
                    continue
                if explicit_origins and not self._task_metrics(fact, scope):
                    # An explicit origin asks "what did these runs work
                    # on?"; a backlog card has nothing to match.
                    continue
                rows.append(self._task_row(fact, scope=scope))
            reverse = order == "desc"
            rows.sort(key=lambda row: (row[sort] is not None, row[sort]),
                      reverse=reverse)
            start = (page - 1) * page_size
            return {
                "total": len(rows),
                "page": page,
                "page_size": page_size,
                "rows": rows[start:start + page_size],
                "generated_at": time.time(),
            }

    def _task_row(self, fact: TaskFacts,
                  scope: set[str] | None) -> dict[str, Any]:
        metrics = self._task_metrics(fact, scope)
        return {
            "task_id": fact.task_id,
            "title": fact.title,
            "status": fact.status,
            "harness": fact.harness,
            "model": fact.model,
            "runs": fact.run_count,
            "completed_runs": fact.completed_runs,
            "tokens": sum(record.tokens.total for record in metrics),
            "wall_ms": round(sum(record.duration_ms for record in metrics), 3),
            "rework": fact.rework_count,
            "expects_artifact": fact.expects_artifact,
            "queue_wait_ms": _mean(fact.queue_waits),
            "last_error": fact.last_error,
            "updated_at": fact.updated_at,
            "created_at": fact.created_at,
        }

    def _task_metrics(self, fact: TaskFacts,
                      scope: set[str] | None) -> list[RunMetrics]:
        return [record for record in self._fact_metrics(fact)
                if scope is None or record.origin in scope]

    def task_detail(self, task_id: str) -> dict[str, Any]:
        """One task: lifecycle, runs with metrics, evidence, diagnoses."""
        with self._lock:
            self.refresh()
            fact = next((item for item in self._task_facts()
                         if item.task_id == task_id), None)
            if fact is None:
                raise EvaluationNotFound("task not found")
            record = self._board.get(task_id)
            runs: list[dict[str, Any]] = []
            for run in fact.runs:
                run_id = run.get("run_id")
                metrics = self._runs_by_id.get(run_id) \
                    if isinstance(run_id, str) else None
                runs.append({
                    "run_id": run_id,
                    "status": run.get("status"),
                    "started_at": run.get("started_at"),
                    "finished_at": run.get("finished_at"),
                    "error": run.get("error"),
                    "response_chars": len(str(run.get("response") or "")),
                    "response": str(run.get("response") or "")[:2000],
                    "metrics": metrics.to_dict() if metrics is not None else None,
                    "evidence": ("missing" if metrics is None
                                 else ("present" if metrics.artifact_evidence
                                       else "none")),
                    "edited_files": list(metrics.edited_files)
                    if metrics is not None else [],
                    "trace_available": self._has_trace(run_id),
                })
            timeline = [
                {"type": event.type, "at": event.at,
                 "run_id": event.run_id,
                 "from": event.from_status, "to": event.to_status,
                 "status": event.status,
                 "queue_wait_ms": event.queue_wait_ms}
                for event in sorted(
                    (event for event in self._events
                     if event.task_id == task_id), key=lambda item: item.at)
            ]
            return {
                "task_id": fact.task_id,
                "title": fact.title,
                "status": fact.status,
                "harness": fact.harness,
                "model": fact.model,
                "description": record.description if record is not None else "",
                "expects_artifact": fact.expects_artifact,
                "expects_artifact_source": (
                    "explicit" if record is not None
                    and record.expects_artifact is not None else "harness"),
                "executed": fact.executed,
                "done": fact.done,
                "reached_review": fact.reached_review,
                "rework_count": fact.rework_count,
                "rework_reasons": dict(fact.rework_reasons),
                "queue_waits_ms": list(fact.queue_waits),
                "last_error": fact.last_error,
                "created_at": fact.created_at,
                "updated_at": fact.updated_at,
                "runs": runs,
                "timeline": timeline,
                "diagnoses": self._decorate_diagnoses(
                    self._diagnoses_by_target.get(task_id, [])),
                "generated_at": time.time(),
            }

    def run_detail(self, run_id: str) -> dict[str, Any]:
        """``run.metrics`` plus a bounded digest of the run's trace."""
        with self._lock:
            self.refresh()
            record = self._runs_by_id.get(run_id)
            if record is None:
                raise EvaluationNotFound("run not found")
            board_run = self._board_run(run_id)
            trace_path = self._trace_path(run_id)
            return {
                "run_id": run_id,
                "metrics": record.to_dict(),
                "board": board_run,
                "digest": self._digest(run_id),
                "trace_path": str(trace_path) if trace_path else None,
                "generated_at": time.time(),
            }

    def _has_trace(self, run_id: Any) -> bool:
        path = self._trace_path(run_id)
        return path is not None and path.is_file()

    def _board_run(self, run_id: str) -> dict[str, Any] | None:
        for record in self._board.list_all():
            for run in record.get("runs") or []:
                if isinstance(run, Mapping) and run.get("run_id") == run_id:
                    return {
                        "task_id": record.get("id"),
                        "title": record.get("title"),
                        "status": record.get("status"),
                        "run": dict(run),
                    }
        return None

    def _trace_path(self, run_id: Any) -> Path | None:
        """Trace file of one run; ``None`` for an id that cannot be one."""
        name = run_id if isinstance(run_id, str) else ""
        if not name or name in (".", "..") or Path(name).name != name:
            return None
        return self._traces_dir / f"{name}.jsonl"

    def _digest(self, run_id: str) -> dict[str, Any]:
        """Aggregate a trace without returning its payloads.

        Cached on ``(mtime, size)``: the detail drawer polls, and a long
        run's trace is the largest file the runtime writes.
        """
        path = self._trace_path(run_id)
        if path is None:
            return {"available": False, "reason": "invalid run id"}
        try:
            stat = path.stat()
        except OSError:
            return {"available": False, "reason": "trace missing"}
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        cached = self._digests.get(run_id)
        if cached is not None and cached[0] == key:
            return cached[1]
        digest = self._build_digest(path, stat.st_size)
        if len(self._digests) >= _DIGEST_CACHE_ENTRIES:
            self._digests.pop(next(iter(self._digests)))
        self._digests[run_id] = (key, digest)
        return digest

    def _build_digest(self, path: Path, size: int) -> dict[str, Any]:
        counts: Counter[str] = Counter()
        tools: dict[str, dict[str, Any]] = {}
        finish_reasons: Counter[str] = Counter()
        errors: list[dict[str, Any]] = []
        llm_requests = 0
        retries = 0
        chunks = 0
        compactions = 0
        summary_invalid = 0
        iterations = 0
        config: dict[str, Any] = {}
        metrics: dict[str, Any] = {}
        read = 0
        truncated = False
        try:
            with path.open("rb") as stream:
                for raw in stream:
                    read += len(raw)
                    if read > _MAX_DIGEST_BYTES:
                        truncated = True
                        break
                    text = raw.strip()
                    if not text:
                        continue
                    try:
                        event = json.loads(text)
                    except ValueError:
                        continue
                    if not isinstance(event, Mapping):
                        continue
                    event_type = _text(event.get("event_type"))
                    if event_type is None:
                        continue
                    data = event.get("data")
                    payload = data if isinstance(data, Mapping) else {}
                    counts[event_type] += 1
                    iteration = event.get("iteration")
                    if isinstance(iteration, int) and not isinstance(iteration,
                                                                  bool):
                        iterations = max(iterations, iteration)
                    if event_type == "run.config":
                        config = {
                            "model": payload.get("model"),
                            "harness_id": payload.get("harness_id"),
                            "tools": len(payload.get("tools") or []),
                            "model_params": payload.get("model_params"),
                            "git": payload.get("git"),
                        }
                    elif event_type == "run.metrics":
                        metrics = {"status": payload.get("status"),
                                   "dropped_events": payload.get("dropped_events")}
                    elif event_type == "llm.end":
                        llm_requests += 1
                        reason = _text(payload.get("finish_reason"))
                        if reason:
                            finish_reasons[reason] += 1
                    elif event_type == "llm.retry":
                        retries += 1
                    elif event_type == "llm.chunk":
                        chunks += 1
                    elif event_type == "context.llm_summary.invalid":
                        summary_invalid += 1
                    elif (event_type.startswith("context.")
                          and not event_type.endswith(".text")
                          and payload.get("request_chars") is not None):
                        # Same rule as ``RunStats.observe``: a compaction is
                        # the event that carries a request size, not any
                        # ``context.*`` event.
                        compactions += 1
                    elif event_type == "tool.end":
                        name = _text(payload.get("tool")) or "unknown"
                        row = tools.setdefault(
                            name, {"tool": name, "calls": 0, "errors": 0,
                                   "exec_ms": 0.0})
                        row["calls"] += 1
                        row["exec_ms"] += _number(payload.get("exec_ms")) or 0.0
                        if payload.get("status") == "error":
                            row["errors"] += 1
                            if len(errors) < 20:
                                errors.append({
                                    "tool": name,
                                    "error_kind": payload.get("error_kind"),
                                    "iteration": iteration,
                                    "edits": len(payload.get("edited_files") or []),
                                })
        except OSError as exc:
            return {"available": False, "reason": str(exc)}
        tool_rows = sorted(tools.values(), key=lambda row: row["calls"],
                           reverse=True)
        return {
            "available": True,
            "bytes": size,
            "truncated": truncated,
            "events": sum(counts.values()),
            "event_counts": dict(counts.most_common()),
            "iterations": iterations,
            "llm": {"requests": llm_requests, "retries": retries,
                    "chunks": chunks,
                    "finish_reasons": dict(finish_reasons)},
            "context": {"compactions": compactions,
                        "summary_invalid": summary_invalid},
            "tools": [{**row, "exec_ms": round(row["exec_ms"], 3)}
                      for row in tool_rows[:_MAX_TOOL_ROWS]],
            "tool_calls": sum(row["calls"] for row in tool_rows),
            "tool_errors": sum(row["errors"] for row in tool_rows),
            "error_samples": errors,
            "config": config,
            "metrics": metrics,
        }

    def benchmarks(self) -> dict[str, Any]:
        """Harbor benchmark records (its own view, never merged with board)."""
        with self._lock:
            records = self._load_records()
        rows: list[dict[str, Any]] = []
        for record in records:
            score = record.get("score") if isinstance(record.get("score"),
                                                     Mapping) else {}
            rows.append({
                "record_id": record.get("record_id"),
                "created_at": record.get("created_at"),
                "harness_id": record.get("harness_id"),
                "genes_hash": record.get("genes_hash"),
                "model": record.get("model"),
                "benchmark": record.get("benchmark"),
                "job_dir": record.get("job_dir"),
                "accuracy": _number(score.get("rate")),
                "score": dict(score),
                "tasks": len(record.get("tasks") or []),
            })
        rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        by_harness: dict[str, list[float]] = {}
        for row in rows:
            if row["accuracy"] is not None and row["harness_id"]:
                by_harness.setdefault(str(row["harness_id"]), []).append(
                    row["accuracy"])
        return {
            "records": rows,
            "latest_accuracy": {harness: values[0]
                                for harness, values in by_harness.items()},
            "generated_at": time.time(),
        }

    def _load_records(self) -> list[dict[str, Any]]:
        directory = self._records_dir
        try:
            paths = sorted(directory.glob("*.json"))
        except OSError:
            return []
        # ``(name, mtime, size)`` per file: a record overwritten in place
        # keeps its name, so names alone would serve a stale cache.
        parts: list[tuple[str, int, int]] = []
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            parts.append((path.name, stat.st_mtime_ns, stat.st_size))
        signature: tuple[Any, ...] = (str(directory), tuple(parts))
        if signature == self._records_signature:
            return self._records
        try:
            records = load_records(directory)
        except (OSError, ValueError) as exc:
            logger.warning("webui.evaluation.records_failed dir=%s error=%s",
                           directory, exc)
            records = []
        self._records = [record for record in records
                         if isinstance(record, Mapping)]
        self._records_signature = signature
        return self._records

    # -- diagnosis -------------------------------------------------------

    def diagnose(self, target: Mapping[str, Any], *, model: str | None,
                 force: bool = False,
                 now: float | None = None) -> DiagnosisPlan:
        """Build the evidence bundle and open a diagnosis session.

        Never starts the turn itself: a diagnosis is one ordinary run, so
        the caller owns the turn machinery.  ``force=False`` reuses an
        existing session when the target produced the same evidence
        (same ``bundle_hash``).
        """
        normalized = _normalize_target(target)
        with self._lock:
            self.refresh()
            bundle = self._diagnosis_bundle(normalized, now=now)
            compacted = diagnosis_mod.compact_bundle(bundle)
            fingerprint = diagnosis_mod.compute_hash(compacted)
            if not force:
                existing = self._existing_diagnosis(normalized, fingerprint)
                if existing is not None:
                    return DiagnosisPlan(
                        bundle_hash=fingerprint, duplicate=True,
                        existing_session_id=existing["session_id"],
                        diagnosis_id=existing.get("diagnosis_id"))
            diagnosis_id = f"diag-{uuid4().hex[:12]}"
            written = diagnosis_mod.write_bundle(
                compacted, diagnosis_id, self._diagnosis_dir)
            session = self._sessions.create(
                title=_diagnosis_title(normalized),
                model=model,
                harness=EVALUATION_HARNESS_ID,
                origin="diagnosis",
                target=normalized,
                diagnosis_id=diagnosis_id,
                bundle_hash=written["bundle_hash"],
            )
            self._sessions.save(session)
            self._refresh_sessions(force=True)
        return DiagnosisPlan(
            bundle_hash=written["bundle_hash"], diagnosis_id=diagnosis_id,
            session=session, bundle_path=written["path"],
            message=diagnosis_mod.first_message(diagnosis_id, compacted))

    def _existing_diagnosis(self, target: Mapping[str, Any],
                            fingerprint: str) -> dict[str, Any] | None:
        """An earlier session over the same evidence that is worth reusing.

        A session that errored or was cancelled before producing a report
        left the same evidence undiagnosed: reusing it would make the
        target undiagnosable, since only a report answers the question.
        """
        for row in self._diagnosis_sessions:
            if row.get("bundle_hash") != fingerprint:
                continue
            if _target_key(row.get("target")) != _target_key(target):
                continue
            if not (row.get("busy") or row.get("report_segment_id")):
                continue
            return row
        return None

    def _diagnosis_bundle(self, target: Mapping[str, Any], *,
                          now: float | None = None) -> dict[str, Any]:
        """The evidence document of one target (docs/evaluation.md §7)."""
        moment = now if now is not None else time.time()
        kind = target["type"]
        if kind == "run":
            run_id = str(target["id"])
            record = self._runs_by_id.get(run_id)
            if record is None:
                raise EvaluationNotFound(f"run not found: {run_id}")
            fact = self._fact_for_run(run_id)
            run_row = next((run for run in (fact.runs if fact else [])
                            if run.get("run_id") == run_id), {"run_id": run_id})
            runs = [self._run_evidence(run_row, fact)]
            return diagnosis_mod.build_bundle(
                target=target,
                task=self._task_section(fact),
                runs=runs,
                environment=_environment(runs),
                human_feedback=_human_feedback(fact),
                aggregate=self._aggregate_section(runs, facts=[fact] if fact else []),
                generated_at=moment)
        if kind == "task":
            task_id = str(target["id"])
            fact = self._fact(task_id)
            runs = [self._run_evidence(run, fact) for run in fact.runs]
            # Failure first: a truncated bundle must keep what went wrong.
            runs.sort(key=lambda row: 0 if row.get("failure") else 1)
            return diagnosis_mod.build_bundle(
                target=target,
                task=self._task_section(fact),
                runs=runs,
                environment=_environment(runs),
                human_feedback=_human_feedback(fact),
                aggregate=self._aggregate_section(runs, facts=[fact]),
                generated_at=moment)
        return self._cohort_bundle(target, now=moment)

    def _cohort_bundle(self, target: Mapping[str, Any], *,
                       now: float) -> dict[str, Any]:
        """One compact card per task, never the traces of every case."""
        filters = target.get("filters") if isinstance(target.get("filters"),
                                                       Mapping) else {}
        _, window = parse_range(filters.get("range"))
        cutoff = now - window if window is not None else None
        status = _text(filters.get("status"))
        harness = _text(filters.get("harness"))
        model = _text(filters.get("model"))
        origin = _text(filters.get("origin"))
        scope = self._run_scope([origin] if origin else None)
        facts = self._task_facts()
        selected = [fact for fact in facts
                    if (cutoff is None or fact.updated_at >= cutoff)
                    and (status is None or fact.status == status)
                    and (harness is None or fact.harness == harness)
                    and (model is None or fact.model == model)
                    # An explicit origin names the runs at fault; a card no
                    # such run ever worked on is not part of the cohort.
                    and (scope is None or self._task_metrics(fact, scope))]
        cases = [self._case_card(fact) for fact in selected
                 if fact.executed or fact.runs]
        cases.sort(key=lambda case: (case.get("tsr") is not None,
                                     case.get("tsr") or 0.0))
        cases = cases[:diagnosis_mod.MAX_COHORT_CASES]
        executed = [fact for fact in selected if fact.executed]
        accepted = [fact for fact in executed if fact.done]
        reviewed = [fact for fact in selected if fact.reached_review]
        reworked = [fact for fact in reviewed if fact.reworked]
        signals: Counter[str] = Counter()
        for case in cases:
            signal = _text(case.get("top_signal"))
            if signal:
                signals[signal] += 1
        return diagnosis_mod.build_bundle(
            target=target,
            cases=cases,
            aggregate={
                "cohort_size": len(cases),
                "selected_tasks": len(selected),
                "tsr": _ratio(len(accepted), len(executed)),
                "rework_rate": _ratio(len(reworked), len(reviewed)),
                "top_failure_signals": [
                    {"signal": signal, "count": count}
                    for signal, count in signals.most_common(10)],
            },
            generated_at=now)

    def _case_card(self, fact: TaskFacts) -> dict[str, Any]:
        metrics = self._fact_metrics(fact)
        runs = [row for row in fact.runs if row.get("run_id") in
                self._runs_by_id]
        errors: Counter[str] = Counter()
        for record in metrics:
            for kind, count in record.error_kinds.items():
                errors[kind] += count
        tool_errors = sum(record.tool_errors for record in metrics)
        return {
            "task_id": fact.task_id,
            "title": fact.title,
            "status": fact.status,
            "harness": fact.harness,
            "model": fact.model,
            "runs": fact.run_count,
            "tsr": 1.0 if fact.done and fact.executed else 0.0,
            "rework_count": fact.rework_count,
            "metrics": {
                "tsr": 1.0 if fact.done and fact.executed else 0.0,
                "tokens": sum(record.tokens.total for record in metrics),
                "tool_errors": tool_errors,
                "iterations": sum(record.iterations for record in metrics),
            },
            "top_signal": (("%s (x%d)" % errors.most_common(1)[0])
                           if errors else fact.last_error),
            "run_ids": [str(run.get("run_id") or "") for run in runs[-2:]],
            # The ids a report is allowed to cite: a cohort bundle carries no
            # per-iteration detail, so only the trace knows them.
            "evidence": [self._trace_evidence(run) for run in runs[-2:]],
        }

    def _trace_evidence(self, run_row: Mapping[str, Any]) -> dict[str, Any]:
        """One run's citable event ids, read from its trace."""
        run_id = str(run_row.get("run_id") or "")
        return {"run_id": run_id,
                "event_ids": sorted(self._trace_event_ids(run_id))}

    def _trace_event_ids(self, run_id: str, *,
                         wanted: set[str] | None = None,
                         max_events: int = _MAX_TRACE_EVENT_IDS) -> set[str]:
        """``event_id``s of one trace, cached on ``(mtime, size)``.

        ``wanted`` stops the scan early (a report usually cites a handful
        of ids); a partial scan is not cached, so the next call is honest.
        Only complete scans enter the cache, hence a hit answers any
        ``wanted`` set -- including ids the trace does not have at all.
        """
        path = self._trace_path(run_id)
        if path is None:
            return set()
        try:
            stat = path.stat()
        except OSError:
            return set()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        cached = self._trace_ids.get(run_id)
        if cached is not None and cached[0] == key:
            return set(cached[1])
        found: set[str] = set()
        complete = True
        for event in diagnosis_mod.iter_trace(path):
            event_id = event.get("event_id")
            if isinstance(event_id, str) and event_id:
                found.add(event_id)
            if len(found) >= max_events or (wanted is not None
                                            and wanted <= found):
                complete = False
                break
        if complete:
            if len(self._trace_ids) >= _TRACE_ID_CACHE_ENTRIES:
                self._trace_ids.pop(next(iter(self._trace_ids)))
            self._trace_ids[run_id] = (key, found)
        return set(found)

    def _run_evidence(self, run_row: Mapping[str, Any],
                      fact: TaskFacts | None) -> dict[str, Any]:
        """One run's metrics plus per-iteration evidence with event ids."""
        run_id = str(run_row.get("run_id") or "")
        record = self._runs_by_id.get(run_id)
        evidence = diagnosis_mod.collect_run_evidence(
            run_id, trace_path=self._trace_path(run_id))
        failure = evidence.pop("failure", None)
        edited = evidence.pop("edited_files", [])
        metrics = {
            "status": (record.status if record else run_row.get("status")),
            "iterations": record.iterations if record else None,
            "tokens": record.tokens.total if record else 0,
            "prompt_tokens": record.tokens.prompt if record else 0,
            "completion_tokens": record.tokens.completion if record else 0,
            "cached_tokens": record.tokens.cached if record else 0,
            "tool_calls": record.tool_calls if record else 0,
            "tool_errors": record.tool_errors if record else 0,
            "error_kinds": dict(record.error_kinds) if record else {},
            "ttft_ms": record.ttft_ms if record else None,
            "duration_ms": record.duration_ms if record else None,
            "policy_blocks": record.policy_blocks if record else 0,
            "summary_invalid": record.summary_invalid if record else 0,
            "summary_attempts": record.summary_attempts if record else 0,
            "has_answer": record.has_answer if record else False,
            "edited_files": list(record.edited_files) if record else list(edited),
            "reworked": bool(fact.reworked) if fact is not None else False,
            "tsr": (1.0 if fact is not None and fact.done and fact.executed
                    else 0.0),
        }
        return {
            "run_id": run_id,
            "harness": record.harness_id if record else None,
            "model": record.model if record else None,
            "status": metrics["status"],
            "metrics": metrics,
            "failure": failure or (record.error if record else None),
            "response_chars": len(str(run_row.get("response") or "")),
            "iterations": evidence.get("iterations") or [],
            "tool_errors": evidence.get("tool_errors") or [],
            "context_events": evidence.get("context_events") or [],
            "trace": {"available": evidence.get("trace_available"),
                      "events": evidence.get("events"),
                      "bytes": evidence.get("trace_bytes"),
                      "truncated": evidence.get("truncated")},
        }

    def _task_section(self, fact: TaskFacts | None) -> dict[str, Any] | None:
        if fact is None:
            return None
        record = self._board.get(fact.task_id)
        comments = [str(item.get("body") or "")[:500]
                    for item in (record.comments if record is not None else [])
                    if isinstance(item, Mapping)]
        return {
            "task_id": fact.task_id,
            "title": fact.title,
            "description": str(record.description if record else "")[:4000],
            "status": fact.status,
            "harness": fact.harness,
            "model": fact.model,
            "expects_artifact": fact.expects_artifact,
            "tags": [str(tag) for tag in (record.tags if record else [])],
            "comments": comments[-5:],
            "runs": fact.run_count,
            "rework_count": fact.rework_count,
            "last_error": fact.last_error,
        }

    def _aggregate_section(self, runs: Sequence[Mapping[str, Any]], *,
                           facts: Sequence[TaskFacts]) -> dict[str, Any]:
        metrics = [run.get("metrics") or {} for run in runs]
        signals: Counter[str] = Counter()
        for entry in metrics:
            for kind, count in (entry.get("error_kinds") or {}).items():
                signals[str(kind)] += int(count)
            failure = entry.get("status")
            if failure == "error":
                signals["run_error"] += 1
            elif failure == "cancelled":
                signals["cancelled"] += 1
        executed = [fact for fact in facts if fact.executed]
        accepted = [fact for fact in executed if fact.done]
        return {
            "runs": len(runs),
            "tsr": (1.0 if facts and len(accepted) == len(executed) and executed
                    else (0.0 if facts else None)),
            "rework_count": sum(fact.rework_count for fact in facts),
            "tokens": sum(int((entry.get("tokens") or 0))
                          for entry in metrics),
            "tool_errors": sum(int(entry.get("tool_errors") or 0)
                               for entry in metrics),
            "top_failure_signals": [{"signal": signal, "count": count}
                                    for signal, count in signals.most_common(10)],
        }

    def _fact(self, task_id: str) -> TaskFacts:
        fact = next((item for item in self._task_facts()
                     if item.task_id == task_id), None)
        if fact is None:
            raise EvaluationNotFound(f"task not found: {task_id}")
        return fact

    def _fact_for_run(self, run_id: str) -> TaskFacts | None:
        for fact in self._task_facts():
            if any(run.get("run_id") == run_id for run in fact.runs):
                return fact
        return None

    def _decorate_diagnoses(self,
                            rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Attach the frozen report and its best-effort verification."""
        return [{**row, "report": self._report_payload(row)} for row in rows]

    def _report_payload(self, row: Mapping[str, Any]) -> dict[str, Any]:
        session = self._sessions.get(str(row.get("session_id") or ""))
        if session is None:
            return {"available": False, "markdown": ""}
        markdown = ""
        segment_id = session.report_segment_id
        if segment_id:
            for segment in session.segments:
                if segment.get("id") == segment_id:
                    markdown = str(segment.get("content") or "")
                    break
        parsed = diagnosis_mod.parse_report(markdown)
        bundle = (diagnosis_mod.read_bundle(str(row.get("diagnosis_id")),
                                           self._diagnosis_dir)
                  if row.get("diagnosis_id") else None)
        events, runs = diagnosis_mod.collect_event_ids(bundle or {})
        # The harness tells the model to verify claims against the *trace*, and
        # a compacted bundle no longer carries every ``event_id``.  A citation
        # that exists in the trace is verified, not invented.
        missing = set(diagnosis_mod.EVENT_ID_RE.findall(markdown)) - events
        candidates = sorted(set(runs)
                            | set(diagnosis_mod.RUN_ID_RE.findall(markdown)))
        for run_id in candidates[:_MAX_CITATION_TRACES]:
            if not missing:
                break
            missing -= self._trace_event_ids(run_id, wanted=missing)
        events |= (set(diagnosis_mod.EVENT_ID_RE.findall(markdown)) - missing)
        return {
            "available": bool(markdown),
            "markdown": markdown[:_MAX_REPORT_CHARS],
            "verification": diagnosis_mod.verify_report(
                parsed, event_ids=events, run_ids=runs),
            "suggestions": parsed.get("suggestions") or [],
        }

    def diagnoses(self, *, task_id: str | None = None) -> dict[str, Any]:
        """Diagnosis sessions of one task (or all of them)."""
        with self._lock:
            self.refresh()
            if task_id is not None:
                return {"task_id": task_id,
                        "sessions": self._decorate_diagnoses(
                            self._diagnoses_by_target.get(task_id, []))}
            return {"sessions": self._decorate_diagnoses(
                self._diagnosis_sessions)}

    def run_metrics(self, run_ids: Iterable[str]) -> list[RunMetrics]:
        """Metrics records by id, in the given order (missing ones dropped)."""
        with self._lock:
            self.refresh()
            found: list[RunMetrics] = []
            for run_id in run_ids:
                record = self._runs_by_id.get(run_id)
                if record is not None:
                    found.append(record)
            return found


def _normalize_target(target: Any) -> dict[str, Any]:
    """Validate a diagnosis target; only the documented shapes pass.

    ``task`` / ``run`` name one case; ``cohort`` names a filter over the
    board.  Unknown keys are dropped so a stale client cannot smuggle a
    path or an evaluator into the bundle.
    """
    if not isinstance(target, Mapping):
        raise EvaluationRequestError("target must be an object")
    kind = _text(target.get("type"))
    if kind not in DIAGNOSIS_TARGET_TYPES:
        raise EvaluationRequestError(
            "target.type must be one of " + ", ".join(DIAGNOSIS_TARGET_TYPES))
    if kind == "cohort":
        raw = target.get("filters")
        filters: dict[str, str] = {}
        if isinstance(raw, Mapping):
            for key in ("range", "status", "harness", "model", "origin"):
                value = _text(raw.get(key))
                if value:
                    filters[key] = value
        parse_range(filters.get("range"))
        return {"type": "cohort", "filters": filters}
    target_id = _text(target.get("id"))
    if target_id is None:
        raise EvaluationRequestError("target.id is required")
    return {"type": kind, "id": target_id}


def _target_key(target: Any) -> str:
    """Stable comparison key for two targets."""
    return json.dumps(target if isinstance(target, Mapping) else {},
                      sort_keys=True, ensure_ascii=False, default=str)


def _diagnosis_title(target: Mapping[str, Any]) -> str:
    kind = target.get("type")
    if kind == "cohort":
        filters = target.get("filters") or {}
        shown = ", ".join(f"{key}={value}" for key, value in filters.items())
        return f"诊断 cohort: {shown or 'board'}"[:80]
    return f"诊断 {kind}: {target.get('id')}"[:80]


def _environment(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """What the run left behind inside the workspace (docs §5.2).

    ``diff_summary`` stays ``None``: a shared workspace makes a posteriori
    ``git diff`` wrong (9.2), so product evidence is ``edited_files``.
    """
    edited: set[str] = set()
    for run in runs:
        metrics = run.get("metrics") if isinstance(run.get("metrics"),
                                                  Mapping) else {}
        for path in metrics.get("edited_files") or []:
            if isinstance(path, str) and path.strip():
                edited.add(path.strip())
    return {"edited_files": sorted(edited), "diff_summary": None,
            "tests_seen": []}


def _human_feedback(fact: TaskFacts | None) -> dict[str, Any]:
    """The human verdict the board already encodes."""
    if fact is None:
        return {"verdict": None, "comment": None}
    if fact.reworked:
        verdict = "rework"
    elif fact.done:
        verdict = "accepted"
    else:
        verdict = fact.status
    return {"verdict": verdict, "comment": fact.last_error}


__all__ = [
    "ARTIFACT_HARNESSES",
    "BoardEvent",
    "DEFAULT_RANGE",
    "DIAGNOSIS_TARGET_TYPES",
    "DiagnosisPlan",
    "EvaluationError",
    "EvaluationNotFound",
    "EvaluationRequestError",
    "EvaluationStore",
    "DURATION_BUCKETS",
    "ITERATION_BUCKETS",
    "RANGES",
    "RUN_ORIGINS",
    "TaskFacts",
    "parse_range",
]
