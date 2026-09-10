"""Evaluation records: one JSON file per benchmark run, aggregated later.

A record closes the loop ``harness -> benchmark -> trace -> score``:

* generated from a Harbor ``jobs/`` directory after a run,
* keyed by harness id + genes hash read from the run's own trace
  headers (never hand-typed),
* annotated with failure modes during manual failure analysis,
* aggregated by ``report`` into the family tree with deltas.

Records are immutable: re-running the same benchmark produces a new
record file; existing ones are never rewritten (except the failure
mode annotation, which is its explicit purpose).
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from agent_runtime.harness import HarnessError


DEFAULT_RECORDS_DIR = Path("evaluation") / "records"
DEFAULT_JOBS_DIR = Path("jobs")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _harness_from_trace(trace_path: Path) -> dict[str, Any] | None:
    """Return the harness header record from a run trace, if present."""
    if not trace_path.is_file():
        return None
    with trace_path.open(encoding="utf-8") as stream:
        first = stream.readline()
    if not first:
        return None
    try:
        header = json.loads(first)
    except json.JSONDecodeError:
        return None
    return header if header.get("record_type") == "harness" else None


def _trial_status(trial_result: dict[str, Any], reward: float | None) -> str:
    if trial_result.get("exception_info"):
        return "error"
    if reward is None:
        finished = (trial_result.get("agent_execution") or {}).get("finished_at")
        return "completed" if finished else "cancelled"
    if reward >= 1.0:
        return "passed"
    return "failed"


def _reward_by_trial(top_result: dict[str, Any]) -> dict[str, float]:
    rewards: dict[str, float] = {}
    evals = (top_result.get("stats") or {}).get("evals") or {}
    for entry in evals.values():
        reward_stats = entry.get("reward_stats") or {}
        for raw, trial_names in (reward_stats.get("reward") or {}).items():
            try:
                reward = float(raw)
            except (TypeError, ValueError):
                continue
            for name in trial_names:
                rewards[name] = reward
    return rewards


def extract_record(job_dir: str | Path, *, model: str | None = None,
                   notes: str | None = None,
                   benchmark: str | None = None) -> dict[str, Any]:
    """Build an evaluation record from one Harbor jobs directory."""
    job_path = Path(job_dir)
    if not job_path.is_dir():
        raise HarnessError(f"job directory not found: {job_path}")
    top_result_path = job_path / "result.json"
    if not top_result_path.is_file():
        raise HarnessError(f"not a finished job (no result.json): {job_path}")

    top_result = _read_json(top_result_path)
    config = _read_json(job_path / "config.json") if (job_path / "config.json").is_file() else {}
    rewards = _reward_by_trial(top_result)

    datasets = config.get("datasets") or []
    first = datasets[0] if datasets and isinstance(datasets[0], dict) else {}
    benchmark_name = (benchmark or first.get("name") or first.get("path")
                      or "unknown-benchmark")

    tasks: list[dict[str, Any]] = []
    harness_ids: set[str] = set()
    genes_hashes: set[str] = set()
    for trial_dir in sorted(p for p in job_path.iterdir() if p.is_dir()):
        trial_result_path = trial_dir / "result.json"
        if not trial_result_path.is_file():
            continue
        trial_result = _read_json(trial_result_path)
        trial_name = trial_result.get("trial_name") or trial_dir.name
        reward = rewards.get(trial_name)
        trace_path = trial_dir / "agent" / "agent-runtime.jsonl"
        header = _harness_from_trace(trace_path)
        if header:
            harness_ids.add(header.get("harness_id", "unknown"))
            genes_hashes.add(header.get("genes_hash", ""))
        task_name = trial_result.get("task_name") or trial_name
        tasks.append({
            "task": task_name,
            "trial": trial_name,
            "status": _trial_status(trial_result, reward),
            "reward": reward,
            "trace": str(trace_path),
            "failure_mode": None,
        })

    if len(harness_ids) > 1:
        raise HarnessError(
            f"job {job_path.name} mixes harnesses {sorted(harness_ids)}; "
            "records must cover a single harness")
    harness_id = harness_ids.pop() if harness_ids else "unknown"
    genes_hash = genes_hashes.pop() if genes_hashes else ""

    counts = {"passed": 0, "failed": 0, "error": 0, "cancelled": 0, "completed": 0}
    for task in tasks:
        counts[task["status"]] = counts.get(task["status"], 0) + 1
    judged = counts["passed"] + counts["failed"]
    rate = round(counts["passed"] / judged, 4) if judged else None

    agent_info = {}
    for trial in (_read_json(p / "result.json") for p in job_path.iterdir()
                  if p.is_dir() and (p / "result.json").is_file()):
        info = trial.get("agent_info") or {}
        if info.get("model_info"):
            agent_info = info
            break

    return {
        "record_id": f"{job_path.name}__{harness_id}__{_slug(benchmark_name)}",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "harness_id": harness_id,
        "genes_hash": genes_hash,
        "model": model or (agent_info.get("model_info") or None),
        "benchmark": benchmark_name,
        "job_dir": str(job_path),
        "git_commit": _git_commit(job_path),
        "score": {
            "passed": counts["passed"],
            "failed": counts["failed"],
            "errored": counts["error"],
            "cancelled": counts["cancelled"],
            "unjudged_completed": counts["completed"],
            "total": len(tasks),
            "rate": rate,
        },
        "tasks": tasks,
        "notes": notes,
    }


def _slug(name: str) -> str:
    # Keep the last path segment: "terminal-bench/terminal-bench-2" -> "terminal-bench-2".
    return name.rstrip("/").rsplit("/", 1)[-1].replace(":", "-").replace("@", "-")


def _git_commit(job_path: Path) -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def write_record(record: dict[str, Any], records_dir: str | Path = DEFAULT_RECORDS_DIR) -> Path:
    """Write one record as an immutable JSON file; returns its path."""
    target = Path(records_dir)
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{record['record_id']}.json"
    if path.exists():
        raise HarnessError(f"record already exists: {path} "
                           "(records are immutable; delete it first if intentional)")
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path


def load_records(records_dir: str | Path = DEFAULT_RECORDS_DIR) -> list[dict[str, Any]]:
    """Load every record, oldest first by filename."""
    root = Path(records_dir)
    if not root.is_dir():
        return []
    records = []
    for path in sorted(root.glob("*.json")):
        record = _read_json(path)
        record["_path"] = str(path)
        records.append(record)
    return records


def find_record(reference: str, records_dir: str | Path = DEFAULT_RECORDS_DIR) -> Path:
    """Resolve a record by file path, exact filename, or id prefix."""
    direct = Path(reference)
    if direct.is_file():
        return direct
    root = Path(records_dir)
    exact = root / (reference if reference.endswith(".json") else f"{reference}.json")
    if exact.is_file():
        return exact
    matches = [p for p in root.glob("*.json") if p.stem.startswith(reference)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise HarnessError(f"no record matches {reference!r} in {root}")
    raise HarnessError(
        f"ambiguous record reference {reference!r}: "
        f"{[p.name for p in matches]}")


def annotate_record(reference: str, task: str, mode: str,
                    records_dir: str | Path = DEFAULT_RECORDS_DIR) -> Path:
    """Set the failure mode of one task in a record (manual analysis)."""
    path = find_record(reference, records_dir)
    record = _read_json(path)
    candidates = [t for t in record.get("tasks", [])
                  if task in t.get("task", "") or task in t.get("trial", "")]
    if not candidates:
        known = [t.get("task") for t in record.get("tasks", [])]
        raise HarnessError(f"task {task!r} not in record {path.name}; tasks: {known}")
    if len(candidates) > 1:
        raise HarnessError(
            f"task reference {task!r} matches several entries: "
            f"{[t['task'] for t in candidates]}")
    candidates[0]["failure_mode"] = mode
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path


def latest_rate_by_harness(records: list[dict[str, Any]]) -> dict[str, float | None]:
    """Latest record's judged pass rate per harness id."""
    rates: dict[str, float | None] = {}
    for record in records:  # load_records sorts oldest first
        rates[record.get("harness_id", "unknown")] = record.get("score", {}).get("rate")
    return rates


def build_report(specs_by_id: dict[str, Any], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per harness: latest score and delta vs parent."""
    rates = latest_rate_by_harness(records)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def emit(harness_id: str) -> None:
        if harness_id in seen or harness_id not in specs_by_id:
            return
        spec = specs_by_id[harness_id]
        if spec.parent:
            emit(spec.parent)
        seen.add(harness_id)
        rate = rates.get(harness_id)
        parent_rate = rates.get(spec.parent) if spec.parent else None
        delta = None
        if rate is not None and parent_rate is not None:
            delta = round(rate - parent_rate, 4)
        rows.append({
            "id": harness_id,
            "parent": spec.parent,
            "mutation": spec.mutation,
            "rate": rate,
            "delta": delta,
        })

    for harness_id in specs_by_id:
        emit(harness_id)
    for harness_id in sorted(set(rates) - set(specs_by_id)):
        rows.append({
            "id": harness_id,
            "parent": None,
            "mutation": "(manifest missing)",
            "rate": rates[harness_id],
            "delta": None,
        })
    return rows


def format_report(rows: list[dict[str, Any]]) -> str:
    """Render report rows as a fixed-width table."""
    id_width = max([len(row["id"]) for row in rows] + [len("ID")])
    mutation_width = max([len(row["mutation"] or "-") for row in rows] + [len("MUTATION")])
    header = (f"{'ID':<{id_width}}  {'PARENT':<{id_width}}  "
              f"{'MUTATION':<{mutation_width}}  {'RATE':>5}  {'DELTA':>6}")
    lines = [header, "-" * len(header)]
    for row in rows:
        parent = row["parent"] or "-"
        mutation = row["mutation"] or "-"
        rate = f"{row['rate']:.0%}" if row["rate"] is not None else "-"
        if row["delta"] is not None:
            delta = f"{row['delta']:+.0%}"
        else:
            delta = "-"
        lines.append(
            f"{row['id']:<{id_width}}  {parent:<{id_width}}  "
            f"{mutation:<{mutation_width}}  {rate:>5}  {delta:>6}")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_JOBS_DIR", "DEFAULT_RECORDS_DIR", "annotate_record", "build_report",
    "extract_record", "find_record", "format_report", "latest_rate_by_harness",
    "load_records", "write_record",
]
