"""Evidence bundles and read-only readers for model-driven diagnosis.

A diagnosis is one ordinary run turn on the ``evaluation-v1`` harness: the
model reads a compressed case bundle, verifies the claims against the raw
trace with read-only tools, and writes Markdown with four fixed headings
(``docs/evaluation.md`` §7).  This module owns the parts that belong to the
runtime rather than to the web UI:

* :func:`build_bundle` — the bundle document (task / runs / environment /
  human feedback / aggregate) from data the caller already indexed;
* :func:`write_bundle` — ``<runtime_home>/diagnosis/<id>/bundle.json`` and
  ``summary.md``, capped at 1 MB;
* :func:`collect_run_evidence` — per-iteration evidence carrying
  ``event_id``s, read straight from ``traces/``;
* :func:`parse_report` / :func:`verify_report` — the four-heading contract
  and the best-effort citation check (annotation only, never a gate);
* :func:`diagnosis_tool_specs` — ``read_diagnosis`` / ``read_trace`` /
  ``search_trace``.

The tools read ``storage`` directly instead of going through the sandbox:
``AGENT_RUNTIME_HOME`` and ``AGENT_RUNTIME_WORKSPACE`` are different roots,
and a sandbox narrowed to a subdirectory cannot see the traces at all.
Nothing here imports the web UI — diagnosis data is prepared by the caller.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_runtime import storage
from agent_runtime.tools.spec import ToolSpec


logger = logging.getLogger(__name__)


# The harness that carries the diagnosis prompt and the read-only tools.
EVALUATION_HARNESS_ID = "evaluation-v1"

# The four headings, in the order the report must use them.
REPORT_HEADINGS: tuple[str, ...] = ("Conclusion", "Root cause", "Evidence",
                                    "Top 5 improvements")

# Reports written before the English contract (and the wording the web UI used
# for the label) keep rendering: aliases normalize onto the canonical heading
# instead of turning into unknown body text.
REPORT_HEADING_ALIASES: dict[str, str] = {
    "结论": REPORT_HEADINGS[0],
    "根因": REPORT_HEADINGS[1],
    "证据": REPORT_HEADINGS[2],
    "top 5 改进建议": REPORT_HEADINGS[3],
}

REPORT_HEADING_RE = re.compile(
    r"^#{1,3}\s*(conclusion|root\s*cause|evidence|"
    r"top\s*5\s*(?:improvements?|suggestions?)|结论|根因|证据|"
    r"top\s*5\s*改进建议)\s*$",
    re.IGNORECASE | re.MULTILINE)
NUMBERED_ITEM_RE = re.compile(r"^\s{0,3}(\d{1,2})[.、)]\s+(.+?)\s*$")
EVENT_ID_RE = re.compile(r"\bevt-[0-9a-f]{6,}\b")
RUN_ID_RE = re.compile(r"\brun-[0-9a-z]{6,}\b")
ITERATION_RE = re.compile(r"iteration\s*`?(\d+)`?", re.IGNORECASE)

# Bundle budget (docs/harness-evaluation.md §5.2): one file, ≤ 1 MB.
MAX_BUNDLE_BYTES = 1_000_000
MAX_RUNS = 5
MAX_ITERATIONS_PER_RUN = 40
MAX_TOOL_ERRORS = 20
MAX_COHORT_CASES = 20
MAX_TOOL_OUTPUT_CHARS = 20_000
MAX_SEARCH_MATCHES = 40
_DIAGNOSIS_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

DIAGNOSIS_FILES: tuple[str, ...] = ("bundle.json", "summary.md")


class DiagnosisError(ValueError):
    """The bundle or the diagnosis id is invalid."""


# -- bundle -------------------------------------------------------------


def bundle_dir(diagnosis_id: str, base_dir: Path | None = None) -> Path:
    """Where one diagnosis bundle lives (``<home>/diagnosis/<id>/``)."""
    root = Path(base_dir) if base_dir is not None else storage.diagnosis_dir()
    return root / _check_id(diagnosis_id)


def bundle_path(diagnosis_id: str, base_dir: Path | None = None) -> Path:
    return bundle_dir(diagnosis_id, base_dir) / "bundle.json"


def summary_path(diagnosis_id: str, base_dir: Path | None = None) -> Path:
    return bundle_dir(diagnosis_id, base_dir) / "summary.md"


def _check_id(diagnosis_id: Any) -> str:
    name = diagnosis_id if isinstance(diagnosis_id, str) else ""
    if not _DIAGNOSIS_ID_RE.match(name) or name in (".", ".."):
        raise DiagnosisError(f"invalid diagnosis id: {diagnosis_id!r}")
    return name


def compute_hash(bundle: Mapping[str, Any]) -> str:
    """Fingerprint of a bundle's *evidence*, not of when it was built.

    ``generated_at`` is excluded so re-running a diagnosis minutes later
    over unchanged evidence hits the duplicate check instead of paying
    for the same analysis twice.
    """
    evidence = {key: value for key, value in bundle.items()
                if key != "generated_at"}
    canonical = json.dumps(evidence, sort_keys=True, ensure_ascii=False,
                           default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def build_bundle(*, target: Mapping[str, Any],
                 task: Mapping[str, Any] | None = None,
                 runs: Sequence[Mapping[str, Any]] = (),
                 cases: Sequence[Mapping[str, Any]] = (),
                 environment: Mapping[str, Any] | None = None,
                 human_feedback: Mapping[str, Any] | None = None,
                 aggregate: Mapping[str, Any] | None = None,
                 generated_at: float | None = None) -> dict[str, Any]:
    """Assemble one bundle document (deep-copied, JSON-safe)."""
    bundle: dict[str, Any] = {
        "target": json.loads(json.dumps(dict(target), default=str)),
        "generated_at": generated_at,
    }
    if task is not None:
        bundle["task"] = json.loads(json.dumps(dict(task), default=str))
    if cases:
        bundle["cases"] = json.loads(json.dumps(list(cases[:MAX_COHORT_CASES]),
                                                default=str))
    else:
        bundle["runs"] = json.loads(json.dumps(list(runs[:MAX_RUNS]),
                                               default=str))
    bundle["environment"] = json.loads(json.dumps(dict(environment or {}),
                                                  default=str))
    bundle["human_feedback"] = json.loads(json.dumps(dict(human_feedback or {}),
                                                     default=str))
    bundle["aggregate"] = json.loads(json.dumps(dict(aggregate or {}),
                                                 default=str))
    return bundle


def compact_bundle(bundle: dict[str, Any],
                   limit: int = MAX_BUNDLE_BYTES) -> dict[str, Any]:
    """Trim a bundle to ``limit`` bytes, richest evidence last to go.

    Priority is failure run > rework run > passed run (docs §5.2), so a
    passing run loses its per-iteration detail first and the model still
    sees every failure in full.
    """
    if _size(bundle) <= limit:
        # Never clear a flag that is already set: ``write_bundle`` compacts a
        # bundle its caller may already have compacted, and forgetting the
        # truncation would change the hash of evidence that *is* truncated.
        bundle["truncated_input"] = bool(bundle.get("truncated_input"))
        return bundle
    bundle["truncated_input"] = True
    runs = bundle.get("runs")
    if isinstance(runs, list):
        # Passing runs lose their detail first (highest priority number),
        # so a truncated bundle still shows every failure in full.
        ordered = sorted(runs, key=_run_priority, reverse=True)
        for run in ordered:
            if _size(bundle) <= limit:
                break
            if isinstance(run, dict) and run.get("iterations"):
                run["iterations"] = []
                run["detail_dropped"] = True
        for run in ordered:
            if _size(bundle) <= limit:
                break
            for key in ("tool_errors", "context_events"):
                if isinstance(run, dict) and run.get(key):
                    run[key] = []
                    run["detail_dropped"] = True
    cases = bundle.get("cases")
    if isinstance(cases, list):
        while len(cases) > 1 and _size(bundle) > limit:
            cases.pop()
    return bundle


def _run_priority(run: Mapping[str, Any]) -> tuple[int, int]:
    status = str(run.get("status") or "")
    metrics = run.get("metrics") if isinstance(run.get("metrics"), Mapping) else {}
    failed = status == "error" or bool(run.get("failure"))
    reworked = bool(metrics.get("reworked"))
    # Sorting ascending: 0 = keep detail longest.
    return (0 if failed else (1 if reworked else 2), 0)


def _size(bundle: Mapping[str, Any]) -> int:
    """Bytes the bundle occupies on disk, indentation included."""
    return len(json.dumps(bundle, ensure_ascii=False, indent=2,
                          default=str).encode("utf-8"))


def write_bundle(bundle: dict[str, Any], diagnosis_id: str,
                 base_dir: Path | None = None) -> dict[str, Any]:
    """Write ``bundle.json`` and ``summary.md``; return their paths and hash."""
    name = _check_id(diagnosis_id)
    compacted = compact_bundle(bundle)
    directory = bundle_dir(name, base_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "bundle.json"
    path.write_text(json.dumps(compacted, ensure_ascii=False, indent=2,
                               default=str), encoding="utf-8")
    summary = directory / "summary.md"
    summary.write_text(summary_markdown(compacted), encoding="utf-8")
    return {"diagnosis_id": name, "bundle_hash": compute_hash(compacted),
            "path": str(path), "summary_path": str(summary),
            "bytes": path.stat().st_size,
            "truncated_input": bool(compacted.get("truncated_input"))}


def read_bundle(diagnosis_id: str,
                base_dir: Path | None = None) -> dict[str, Any] | None:
    """The bundle of one diagnosis, or ``None`` when it is not on disk."""
    try:
        raw = bundle_path(diagnosis_id, base_dir).read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, ValueError, DiagnosisError):
        return None
    return parsed if isinstance(parsed, dict) else None


def delete_bundle(diagnosis_id: str, base_dir: Path | None = None) -> bool:
    """Remove one bundle directory; ``False`` when it was already gone."""
    try:
        directory = bundle_dir(diagnosis_id, base_dir)
    except DiagnosisError:
        return False
    if not directory.is_dir():
        return False
    try:
        for name in DIAGNOSIS_FILES:
            (directory / name).unlink(missing_ok=True)
        directory.rmdir()
    except OSError as exc:  # pragma: no cover - permissions
        logger.warning("diagnosis.bundle.delete_failed id=%s error=%s",
                       diagnosis_id, exc)
        return False
    return True


def summary_markdown(bundle: Mapping[str, Any]) -> str:
    """The model's quick map: what was measured and where to look."""
    target = bundle.get("target") if isinstance(bundle.get("target"),
                                                Mapping) else {}
    lines = [
        "# Diagnosis evidence summary",
        "",
        f"- Target type: {target.get('type') or 'unknown'}",
        f"- Target id: {target.get('id') or '-'}",
        f"- Evidence truncated: {'yes' if bundle.get('truncated_input') else 'no'}",
        "",
        "## Metric definitions (docs/evaluation.md §6)",
        "",
        "- TCR = normally finished runs / launched runs "
        "(`run.metrics.status == completed`)",
        "- VCR = normally finished + non-empty answer + artifact evidence + "
        "no policy block (only for tasks that declare artifacts)",
        "- Rework = `review → todo/doing` after the first execution, or a "
        "comment added while in doing/review",
        "- Artifact evidence comes from successful editing tool calls "
        "(`tool.end.edited_files`), not from a post-hoc git diff",
        "",
    ]
    aggregate = bundle.get("aggregate")
    if isinstance(aggregate, Mapping) and aggregate:
        lines += ["## Aggregate metrics", "", "```json",
                  json.dumps(aggregate, ensure_ascii=False, indent=2,
                             default=str), "```", ""]
    cases = bundle.get("cases")
    if isinstance(cases, list) and cases:
        lines += ["## Cases", ""]
        for case in cases:
            if not isinstance(case, Mapping):
                continue
            metrics = case.get("metrics") if isinstance(case.get("metrics"),
                                                        Mapping) else {}
            lines.append(
                f"- `{case.get('task_id')}` status={case.get('status')} "
                f"runs={case.get('runs')} tsr={metrics.get('tsr')} "
                f"tokens={metrics.get('tokens')} "
                f"top_signal={case.get('top_signal') or '-'}")
        lines.append("")
    runs = bundle.get("runs")
    if isinstance(runs, list) and runs:
        lines += ["## Runs", ""]
        for run in runs:
            if not isinstance(run, Mapping):
                continue
            metrics = run.get("metrics") if isinstance(run.get("metrics"),
                                                      Mapping) else {}
            failure = run.get("failure") if isinstance(run.get("failure"),
                                                      Mapping) else {}
            lines.append(
                f"- `{run.get('run_id')}` status={metrics.get('status')} "
                f"iterations={metrics.get('iterations')} "
                f"tokens={metrics.get('tokens')} "
                f"tool_errors={metrics.get('tool_errors')} "
                f"failure={failure.get('stage') or '-'}/"
                f"{failure.get('code') or '-'} "
                f"iterations_captured={len(run.get('iterations') or [])}")
        lines.append("")
    lines += [
        "## How to use",
        "",
        "1. `read_diagnosis(diagnosis_id, file=\"bundle.json\")` reads the "
        "structured evidence.",
        "2. `read_trace(run_id=..., event_type=...|event_id=...|iteration=...)` "
        "pulls the raw events.",
        "3. `search_trace(run_id=..., pattern=...)` finds error text in the "
        "raw trace.",
        "4. `read_file` / `list_dir` are only for checking workspace source, "
        "never for reading diagnosis output.",
        "",
        "Output Markdown with these four level-2 headings, fixed and in this "
        "order: `## Conclusion` / `## Root cause` / `## Evidence` / "
        "`## Top 5 improvements`.",
        "",
    ]
    return "\n".join(lines)


def first_message(diagnosis_id: str, bundle: Mapping[str, Any]) -> str:
    """The short instruction that starts a diagnosis conversation."""
    target = bundle.get("target") if isinstance(bundle.get("target"),
                                                Mapping) else {}
    label = target.get("id") or target.get("type") or "target"
    return "\n".join([
        f"Diagnose this run: {label} (diagnosis_id={diagnosis_id}).",
        "",
        f"1. Start with read_diagnosis(diagnosis_id=\"{diagnosis_id}\", "
        "file=\"summary.md\") for the metric definitions and the aggregate, "
        "then read file=\"bundle.json\" for the evidence.",
        "2. Verify the load-bearing claims with read_trace / search_trace; "
        "do not trust the summary alone.",
        "3. Output Markdown with these four level-2 headings, fixed and in "
        "this order: ## Conclusion / ## Root cause / ## Evidence / "
        "## Top 5 improvements.",
        "4. Cite run_id / iteration / event_id for every piece of evidence; "
        "when you cannot cite one, say plainly that it is an inference.",
        "5. At most 5 suggestions; if you have fewer, report the actual number "
        "instead of padding. Do not modify any file.",
    ])


# -- trace evidence -----------------------------------------------------


def iter_trace(path: Path) -> Iterator[dict[str, Any]]:
    """Yield the run events of one trace file (harness header skipped)."""
    try:
        stream = path.open("r", encoding="utf-8")
    except OSError:
        return
    with stream:
        for raw in stream:
            text = raw.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            if record.get("record_type") == "harness":
                continue
            yield record


def collect_run_evidence(run_id: str, *, trace_path: Path | None = None,
                         max_iterations: int = MAX_ITERATIONS_PER_RUN,
                         max_tool_errors: int = MAX_TOOL_ERRORS,
                         max_context_events: int = MAX_TOOL_ERRORS
                         ) -> dict[str, Any]:
    """Per-iteration evidence of one run, with ``event_id``s to cite."""
    path = trace_path if trace_path is not None else storage.trace_path(run_id)
    evidence: dict[str, Any] = {
        "run_id": run_id,
        "trace_available": path.is_file(),
        "events": 0,
        "iterations": [],
        "tool_errors": [],
        "context_events": [],
        "failure": None,
        "truncated": False,
        "trace_bytes": 0,
    }
    if not path.is_file():
        return evidence
    try:
        evidence["trace_bytes"] = path.stat().st_size
    except OSError:  # pragma: no cover - races with a rotation
        pass
    by_iteration: dict[int, dict[str, Any]] = {}
    edits: set[str] = set()

    def bucket(iteration: Any) -> dict[str, Any]:
        number = iteration if isinstance(iteration, int) and not isinstance(
            iteration, bool) else 0
        row = by_iteration.get(number)
        if row is None:
            row = by_iteration[number] = {
                "n": number,
                "llm": {"tokens": 0, "duration_ms": 0.0},
                "tools": [],
                "context": [],
            }
        return row

    for event in iter_trace(path):
        evidence["events"] += 1
        event_type = event.get("event_type")
        if not isinstance(event_type, str):
            continue
        data = event.get("data") if isinstance(event.get("data"),
                                                Mapping) else {}
        iteration = event.get("iteration")
        event_id = event.get("event_id")
        if event_type == "llm.end":
            row = bucket(iteration)["llm"]
            usage = data.get("usage") if isinstance(data.get("usage"),
                                                    Mapping) else {}
            row["tokens"] += int(usage.get("prompt_tokens") or 0) + int(
                usage.get("completion_tokens") or 0)
            row["duration_ms"] = round(
                row["duration_ms"] + float(data.get("duration_ms") or 0.0), 3)
        elif event_type == "tool.end":
            _collect_tool(evidence, bucket(iteration), data, iteration,
                          event_id, edits)
        elif event_type.startswith("context.") and data.get("request_chars") \
                is not None:
            item = {"trigger": event_type,
                    "before": data.get("original_chars"),
                    "after": data.get("request_chars"),
                    "event_id": event_id}
            bucket(iteration)["context"].append(item)
            if len(evidence["context_events"]) < max_context_events:
                evidence["context_events"].append({**item, "iteration": iteration})
        elif event_type.startswith("context.") and (
                event_type.endswith(".invalid")
                or event_type.endswith(".error")):
            # A failed compaction / summarizer is the core ``context_loss``
            # signal, and it carries no ``request_chars`` to match above.
            item = {"trigger": event_type,
                    "before": data.get("original_chars"),
                    "after": data.get("request_chars"),
                    "error": _clip(data.get("error") or data.get("reason"), 400),
                    "event_id": event_id}
            bucket(iteration)["context"].append(item)
            if len(evidence["context_events"]) < max_context_events:
                evidence["context_events"].append({**item, "iteration": iteration})
        elif event_type in ("agent.error", "runtime.error"):
            evidence["failure"] = {
                "stage": str(data.get("stage") or "runtime"),
                "code": str(data.get("error") or "run failed"),
                "event_id": event_id,
            }
    ordered = [by_iteration[key] for key in sorted(by_iteration)]
    if len(ordered) > max_iterations:
        evidence["truncated"] = True
        ordered = ordered[-max_iterations:]
    if len(evidence["tool_errors"]) > max_tool_errors:
        evidence["truncated"] = True
        evidence["tool_errors"] = evidence["tool_errors"][:max_tool_errors]
    evidence["iterations"] = ordered
    evidence["edited_files"] = sorted(edits)
    return evidence


def _collect_tool(evidence: dict[str, Any], row: dict[str, Any],
                  data: Mapping[str, Any], iteration: Any, event_id: Any,
                  edits: set[str]) -> None:
    name = str(data.get("tool") or "unknown")
    status = str(data.get("status") or "")
    row["tools"].append({"tool": name, "status": status,
                         "event_id": event_id})
    for path in data.get("edited_files") or []:
        if isinstance(path, str) and path.strip():
            edits.add(path.strip())
    if status != "error":
        return
    result = data.get("result")
    evidence["tool_errors"].append({
        "tool": name,
        "error_kind": data.get("error_kind"),
        "error": _clip(result, 400) if result is not None else None,
        "exit_code": data.get("exit_code"),
        "iteration": iteration,
        "event_id": event_id,
    })


def _clip(value: Any, limit: int) -> Any:
    """Shorten long strings, recursively, for bundle-sized evidence."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, Mapping):
        return {str(key): _clip(item, limit) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clip(item, limit) for item in value[:50]]
    return value


# -- report parsing -----------------------------------------------------


def _normalize_heading(raw: str) -> str:
    text = " ".join(raw.split()).lower()
    for heading in REPORT_HEADINGS:
        if text == heading.lower():
            return heading
    alias = REPORT_HEADING_ALIASES.get(text)
    if alias is not None:
        return alias
    return REPORT_HEADINGS[3] if text.startswith("top") else raw.strip()


def parse_report(markdown: str | None) -> dict[str, Any]:
    """Split a report by the four fixed headings and extract suggestions."""
    text = markdown if isinstance(markdown, str) else ""
    matches = list(REPORT_HEADING_RE.finditer(text))
    sections: dict[str, str] = {}
    found: list[str] = []
    for index, match in enumerate(matches):
        heading = _normalize_heading(match.group(1))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[heading] = text[match.end():end].strip()
        if heading not in found:
            found.append(heading)
    expected = [heading for heading in REPORT_HEADINGS if heading in found]
    suggestions: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in sections.get(REPORT_HEADINGS[3], "").splitlines():
        item = NUMBERED_ITEM_RE.match(line)
        if item:
            current = {"index": int(item.group(1)),
                       "text": item.group(2).strip()}
            suggestions.append(current)
        elif current is not None and line.strip():
            # The model may write `Metric / Verification` on the lines after
            # the numbered item; keep them with it instead of dropping them.
            current["text"] = f"{current['text']}\n{line.strip()}".strip()
    return {
        "available": bool(text.strip()),
        "sections": sections,
        "headings": found,
        "missing": [heading for heading in REPORT_HEADINGS
                    if heading not in sections],
        "order_ok": found == expected,
        "suggestions": suggestions,
        "suggestion_count": len(suggestions),
        "complete": len(found) == len(REPORT_HEADINGS) and found == expected,
    }


def verify_report(parsed: Mapping[str, Any], *, event_ids: Iterable[str] = (),
                  run_ids: Iterable[str] = ()) -> dict[str, Any]:
    """Best-effort citation check: mark, never reject (docs §5.5)."""
    known_events = {str(item) for item in event_ids}
    known_runs = {str(item) for item in run_ids}
    body = "\n".join(str(section) for section in
                     (parsed.get("sections") or {}).values())
    citations: list[dict[str, Any]] = []
    for value in sorted(set(EVENT_ID_RE.findall(body))):
        citations.append({"kind": "event", "value": value,
                          "verified": value in known_events})
    for value in sorted(set(RUN_ID_RE.findall(body))):
        citations.append({"kind": "run", "value": value,
                          "verified": value in known_runs})
    for value in sorted(set(ITERATION_RE.findall(body))):
        citations.append({"kind": "iteration", "value": value,
                          "verified": True})
    return {
        "citations": citations,
        "unverified": [item["value"] for item in citations
                       if not item["verified"]],
        "missing_sections": list(parsed.get("missing") or []),
        "order_ok": bool(parsed.get("order_ok")),
        "complete": bool(parsed.get("complete")),
        "suggestion_count": int(parsed.get("suggestion_count") or 0),
    }


def collect_event_ids(bundle: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    """Every ``event_id`` / ``run_id`` a bundle can legitimately cite."""
    events: set[str] = set()
    runs: set[str] = set()
    text = json.dumps(bundle, ensure_ascii=False, default=str)
    for value in EVENT_ID_RE.findall(text):
        events.add(value)
    for value in RUN_ID_RE.findall(text):
        runs.add(value)
    for item in _walk(bundle):
        if not isinstance(item, Mapping):
            continue
        event_id = item.get("event_id")
        if isinstance(event_id, str):
            events.add(event_id)
        run_id = item.get("run_id")
        if isinstance(run_id, str):
            runs.add(run_id)
    return events, runs


def _walk(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


# -- read-only tools ----------------------------------------------------


def _dump(payload: Any, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… truncated at {limit} chars"


def _safe_run_path(run_id: Any) -> Path | None:
    name = run_id if isinstance(run_id, str) else ""
    if not name or name in (".", "..") or Path(name).name != name:
        return None
    return storage.traces_dir() / f"{name}.jsonl"


async def read_diagnosis(diagnosis_id: str, file: str = "bundle.json",
                         offset: int = 0,
                         limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    """Read one file of a diagnosis bundle (never an arbitrary path)."""
    if file not in DIAGNOSIS_FILES:
        return _dump({"error": f"file must be one of {list(DIAGNOSIS_FILES)}"})
    try:
        path = bundle_dir(diagnosis_id) / file
    except DiagnosisError as exc:
        return _dump({"error": str(exc)})
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return _dump({"error": f"cannot read {file}: {exc}"})
    start = max(0, int(offset or 0))
    size = max(1, min(int(limit or MAX_TOOL_OUTPUT_CHARS),
                      MAX_TOOL_OUTPUT_CHARS))
    chunk = text[start:start + size]
    return chunk + ("" if start + size >= len(text)
                    else f"\n… {len(text) - start - size} chars left")


async def read_trace(run_id: str, event_type: str | None = None,
                     event_id: str | None = None,
                     iteration: int | None = None,
                     limit: int = 25) -> str:
    """Read events of one trace, filtered and clipped, never the payload bulk."""
    path = _safe_run_path(run_id)
    if path is None:
        return _dump({"error": f"invalid run id: {run_id!r}"})
    if not path.is_file():
        return _dump({"error": f"trace not found for {run_id}",
                      "path": str(path)})
    wanted = max(1, min(int(limit or 25), 100))
    events: list[dict[str, Any]] = []
    for event in iter_trace(path):
        if event_type and event.get("event_type") != event_type:
            continue
        if event_id and event.get("event_id") != event_id:
            continue
        if iteration is not None and event.get("iteration") != iteration:
            continue
        events.append({
            "event_id": event.get("event_id"),
            "event_type": event.get("event_type"),
            "iteration": event.get("iteration"),
            "timestamp": event.get("timestamp"),
            "data": _clip(event.get("data"), 600),
        })
        if len(events) >= wanted:
            break
    return _dump({"run_id": run_id, "path": str(path), "events": events,
                  "matched": len(events), "limit": wanted})


async def search_trace(run_id: str, pattern: str,
                       limit: int = 20) -> str:
    """Case-insensitive substring search over one trace."""
    path = _safe_run_path(run_id)
    if path is None:
        return _dump({"error": f"invalid run id: {run_id!r}"})
    needle = (pattern or "").strip().lower()
    if not needle:
        return _dump({"error": "pattern is empty"})
    if not path.is_file():
        return _dump({"error": f"trace not found for {run_id}"})
    wanted = max(1, min(int(limit or 20), MAX_SEARCH_MATCHES))
    matches: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for raw in stream:
                index = raw.lower().find(needle)
                if index < 0:
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    record = {}
                record = record if isinstance(record, dict) else {}
                start = max(0, index - 120)
                matches.append({
                    "event_id": record.get("event_id"),
                    "event_type": record.get("event_type"),
                    "iteration": record.get("iteration"),
                    "snippet": raw[start:index + len(needle) + 120].strip(),
                })
                if len(matches) >= wanted:
                    break
    except OSError as exc:
        return _dump({"error": f"cannot read trace: {exc}"})
    return _dump({"run_id": run_id, "pattern": pattern, "matches": matches,
                  "matched": len(matches), "limit": wanted})


def diagnosis_tool_specs() -> list[ToolSpec]:
    """The read-only tools only the diagnosis harness may call."""
    return [
        ToolSpec(
            name="read_diagnosis",
            description=("Read a diagnosis evidence bundle. ``file`` is "
                         "``bundle.json`` (structured evidence) or "
                         "``summary.md`` (metric definitions and aggregate)."),
            parameters={"type": "object", "properties": {
                "diagnosis_id": {"type": "string"},
                "file": {"type": "string", "enum": list(DIAGNOSIS_FILES)},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1,
                          "maximum": MAX_TOOL_OUTPUT_CHARS},
            }, "required": ["diagnosis_id"]},
            handler=read_diagnosis,
        ),
        ToolSpec(
            name="read_trace",
            description=("Read events of one run trace (run_id), filtered by "
                         "event_type / event_id / iteration. Payload strings "
                         "are clipped; use search_trace to locate an error."),
            parameters={"type": "object", "properties": {
                "run_id": {"type": "string"},
                "event_type": {"type": "string"},
                "event_id": {"type": "string"},
                "iteration": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            }, "required": ["run_id"]},
            handler=read_trace,
        ),
        ToolSpec(
            name="search_trace",
            description=("Case-insensitive substring search inside one run "
                         "trace; returns event ids and a surrounding snippet."),
            parameters={"type": "object", "properties": {
                "run_id": {"type": "string"},
                "pattern": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1,
                          "maximum": MAX_SEARCH_MATCHES},
            }, "required": ["run_id", "pattern"]},
            handler=search_trace,
        ),
    ]


__all__ = [
    "DIAGNOSIS_FILES",
    "DiagnosisError",
    "EVALUATION_HARNESS_ID",
    "MAX_BUNDLE_BYTES",
    "REPORT_HEADINGS",
    "build_bundle",
    "bundle_dir",
    "bundle_path",
    "collect_event_ids",
    "collect_run_evidence",
    "compact_bundle",
    "compute_hash",
    "delete_bundle",
    "diagnosis_tool_specs",
    "first_message",
    "parse_report",
    "read_bundle",
    "read_diagnosis",
    "read_trace",
    "search_trace",
    "summary_markdown",
    "summary_path",
    "verify_report",
    "write_bundle",
]
