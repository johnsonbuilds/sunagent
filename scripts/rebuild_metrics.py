"""Rebuild ``metrics/runs.jsonl`` from the trace files.

Every run writes exactly one ``run.metrics`` trace event just before it
ends (``agent_runtime.metrics.finalize_run``), and the index line is that
same payload.  The index is therefore derived data: deleting ``metrics/``
loses nothing the traces still hold, and a torn or hand-edited line can be
replaced from the authoritative log.

Usage:
    uv run python scripts/rebuild_metrics.py                  # dry run
    uv run python scripts/rebuild_metrics.py --write           # replace the index
    uv run python scripts/rebuild_metrics.py --write --keep-existing

A dry run prints what it would write and touches nothing.  ``--write``
replaces the file atomically (temp file + ``os.replace``), so an
interrupted rebuild cannot leave a half-written index behind.  Without
``--keep-existing`` the traces are the only source; with it the current
lines are merged in and an existing line wins, because it was written when
the run finished while the trace may have been truncated since.

Traces older than the Phase 1 instrumentation carry no ``run.metrics``
event.  They are reported and skipped: replaying their events would only
approximate the numbers, and a guessed line is worse than a missing one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SOURCE_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SOURCE_DIR))

from agent_runtime import storage
from agent_runtime.metrics import read_run_metrics
from agent_runtime.run_records import RunMetrics


def trace_metrics(path: Path) -> dict[str, Any] | None:
    """The last ``run.metrics`` payload of one trace, if it has one."""
    found: dict[str, Any] | None = None
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if "run.metrics" not in line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if not isinstance(parsed, Mapping):
                continue
            if parsed.get("event_type") != "run.metrics":
                continue
            payload = parsed.get("data")
            if isinstance(payload, Mapping) and payload.get("run_id"):
                found = dict(payload)
    return found


@dataclass
class RebuildPlan:
    """What a rebuild would write; nothing touches disk until ``write``."""

    records: list[RunMetrics] = field(default_factory=list)
    traces: int = 0
    skipped: list[str] = field(default_factory=list)
    duplicate_ids: list[str] = field(default_factory=list)
    replaced: int = 0


def plan(traces_dir: Path, metrics_path: Path, *,
         keep_existing: bool = False) -> RebuildPlan:
    """Collect one record per trace, plus what the current index holds."""
    result = RebuildPlan()
    rebuilt: dict[str, RunMetrics] = {}
    for path in sorted(traces_dir.glob("*.jsonl")):
        result.traces += 1
        payload = trace_metrics(path)
        if payload is None:
            result.skipped.append(path.name)
            continue
        record = RunMetrics.from_dict(payload)
        previous = rebuilt.get(record.run_id)
        if previous is not None:
            # Two files claim the same run: keep the later finish.
            result.duplicate_ids.append(record.run_id)
            if record.finished_at <= previous.finished_at:
                continue
        rebuilt[record.run_id] = record
    existing = read_run_metrics(metrics_path)
    if keep_existing:
        for record in existing:
            rebuilt[record.run_id] = record
    elif existing:
        result.replaced = len(existing)
    result.records = sorted(rebuilt.values(),
                            key=lambda record: (record.finished_at, record.run_id))
    return result


def write(records: Sequence[RunMetrics], metrics_path: Path) -> Path:
    """Replace the index atomically; the line format matches the appender."""
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = metrics_path.with_name(metrics_path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record.to_dict(), ensure_ascii=False,
                                    separators=(",", ":"), default=str,
                                    allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, metrics_path)
    return metrics_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--traces", type=Path, default=None,
                        help="trace directory (default: <home>/traces)")
    parser.add_argument("--metrics", type=Path, default=None,
                        help="index to write (default: <home>/metrics/runs.jsonl)")
    parser.add_argument("--write", action="store_true",
                        help="replace the index; without it this is a dry run")
    parser.add_argument("--keep-existing", action="store_true",
                        help="merge the current lines, letting them win")
    args = parser.parse_args(argv)

    traces_dir = args.traces or storage.traces_dir()
    metrics_path = args.metrics or storage.metrics_path()
    if not traces_dir.is_dir():
        print(f"no trace directory at {traces_dir}", file=sys.stderr)
        return 1
    result = plan(traces_dir, metrics_path, keep_existing=args.keep_existing)

    print(f"scanned {result.traces} traces in {traces_dir}")
    print(f"  {len(result.records)} records, "
          f"{len(result.skipped)} without run.metrics, "
          f"{len(result.duplicate_ids)} duplicate run_ids")
    for name in result.skipped[:5]:
        print(f"  skipped {name}")
    if len(result.skipped) > 5:
        print(f"  ... and {len(result.skipped) - 5} more")
    if result.replaced:
        print(f"  replacing {result.replaced} existing lines "
              f"(--keep-existing merges them instead)")
    if not args.write:
        print("dry run: nothing written (pass --write to rebuild)")
        return 0
    write(result.records, metrics_path)
    print(f"wrote {len(result.records)} lines to {metrics_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
