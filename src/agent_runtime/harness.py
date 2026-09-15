"""Harness genome: the named, versioned configuration that decides how an
agent works, expressed as data instead of code.

The runtime engine (loop, trace, events) stays stable; a harness spec
selects prompt, tools, control flow, memory, recovery and verification
behavior.  Specs are loaded from YAML manifests so harness versions can
be diffed, compared, and later mutated one gene at a time:

    baseline-v0  --(enable verification)-->  baseline-v1

Every run records its harness id and a content hash of the genes, so a
trace can always be traced back to the exact harness that produced it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml


class HarnessError(ValueError):
    """Raised when a harness manifest is missing or invalid."""


ITERATION_LIMIT_NOTICE = (
    "Iteration limit reached. Summarize the progress and give the "
    "best possible final answer. Do not call tools."
)


def feed_error_and_continue(exc: Exception, *, tool: str | None = None) -> str:
    """Turn a tool failure into an observation and let the loop continue."""
    return f"Tool error: {exc}"


ToolErrorStrategy = Callable[..., str]

TOOL_ERROR_STRATEGIES: dict[str, ToolErrorStrategy] = {
    "feed_error_and_continue": feed_error_and_continue,
}

# Categories a failed LLM call is classified into; each maps to its own
# retry policy under the recovery.llm_errors gene.
LLM_RETRY_CATEGORIES = (
    "stream_idle_timeout",
    "stream_truncated",
    "stream_empty",
    "provider_error",
)


def backoff_none(base_delay: float, attempt: int) -> float:
    """Retry immediately, ignoring the base delay."""
    return 0.0


def backoff_fixed(base_delay: float, attempt: int) -> float:
    """Same delay before every retry."""
    return base_delay


def backoff_exponential(base_delay: float, attempt: int) -> float:
    """Double the delay with every subsequent retry."""
    return base_delay * 2 ** (attempt - 1)


BackoffPolicy = Callable[[float, int], float]

BACKOFF_POLICIES: dict[str, BackoffPolicy] = {
    "none": backoff_none,
    "fixed": backoff_fixed,
    "exponential": backoff_exponential,
}


@dataclass(frozen=True)
class LLMRetryPolicy:
    """Retry knobs for one LLM-error category.

    ``max_retries`` counts retries on top of the initial attempt;
    ``attempt`` in :meth:`delay` is 1-based (first retry = 1).
    """

    max_retries: int = 0
    backoff: str = "exponential"
    base_delay: float = 0.5

    def delay(self, attempt: int) -> float:
        try:
            policy = BACKOFF_POLICIES[self.backoff]
        except KeyError as missing:
            raise HarnessError(
                f"unknown backoff policy: {missing.args[0]!r}") from missing
        return policy(self.base_delay, attempt)


def _default_llm_errors() -> dict[str, LLMRetryPolicy]:
    # Zero-retry everywhere so existing manifests keep their exact
    # behavior; retries only happen when a harness opts in per category.
    return {category: LLMRetryPolicy() for category in LLM_RETRY_CATEGORIES}

MEMORY_STRATEGIES: tuple[str, ...] = (
    "full_history", "compact_observations", "llm_summary")

# Transcript-economics defaults: declared once here and consumed by the
# memory strategies (agent.memory) and the observation boundary
# (agent.observations).  The invariant "archive exactly what compaction
# could truncate" is structural: the observation formatter receives
# archive_min_chars = memory.head_chars, so the two can never drift.
DEFAULT_HEAD_CHARS = 200                 # memory: compaction keeps this much of an old observation
DEFAULT_CONTEXT_BUDGET = 60_000          # memory: request-view char budget that triggers compaction
DEFAULT_WINDOW_ROUNDS = 12               # memory: compaction comfort window
DEFAULT_KEEP_RECENT_ROUNDS = 2           # memory: rounds never compacted under budget pressure
DEFAULT_SUMMARY_TAIL_ROUNDS = 2          # memory: llm_summary verbatim tail
DEFAULT_MAX_OBSERVATION_CHARS = 16_000   # control: inline observation budget before windowing
DEFAULT_SPILL_PREVIEW_CHARS = 4_000      # control: head/tail window half-size


def _positive_int(value: Any, label: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise HarnessError(f"{label} must be an integer >= 1")
    return value


@dataclass(frozen=True)
class PromptGenome:
    system: str = ""
    iteration_limit_notice: str = ITERATION_LIMIT_NOTICE


@dataclass(frozen=True)
class ToolGenome:
    enabled: tuple[str, ...] = ("run_command",)


@dataclass(frozen=True)
class ControlGenome:
    max_iterations: int = 10
    max_observation_chars: int = DEFAULT_MAX_OBSERVATION_CHARS
    spill_preview_chars: int = DEFAULT_SPILL_PREVIEW_CHARS


@dataclass(frozen=True)
class MemoryGenome:
    strategy: str = "full_history"
    head_chars: int = DEFAULT_HEAD_CHARS
    context_budget: int = DEFAULT_CONTEXT_BUDGET
    window_rounds: int = DEFAULT_WINDOW_ROUNDS
    keep_recent_rounds: int = DEFAULT_KEEP_RECENT_ROUNDS
    summary_tail_rounds: int = DEFAULT_SUMMARY_TAIL_ROUNDS


@dataclass(frozen=True)
class RecoveryGenome:
    tool_error: str = "feed_error_and_continue"
    # Consecutive identical tool failures tolerated before the turn stops
    # early with a manual-handling message. 0 disables the guard (legacy
    # behavior: retry until control.max_iterations is exhausted).
    tool_error_max_retries: int = 0
    llm_errors: dict[str, LLMRetryPolicy] = field(
        default_factory=_default_llm_errors)


@dataclass(frozen=True)
class VerificationGenome:
    """Return-contract gate enforced at the turn boundary, not in the loop.

    Mirrors the labs-OO-Agents BenchAgent pattern (structured TaskResult):
    the model declares ``solution_description / evidence / command_to_verify``
    as free text; the harness checks *shape* (field present + minimal
    content) plus *grounding* (evidence quotes observed output, the declared
    command was run, at least one source edit exists). The declared command
    is model-authored per task, so the gate stays generic across
    SWE-bench / terminal-bench. When ``rerun_declared_command`` is true
    (code-v12), a passing answer's declared command is re-executed once
    via ``run_command``: the canonical history command grounding the
    declaration runs (never the raw declared string, which may carry
    fences or trailing prose), exit 0 accepts, anything else rejects as
    a ``command_to_verify`` gap with the rerun output quoted.
    ``task_result`` mode (code-v13) moves the declaration into the
    ``submit_result`` tool: finishing requires one call with all three
    parameters as typed arguments — no answer-string parsing — while the
    same shape/grounding/rerun pipeline validates the submission.
    """

    enabled: bool = False
    mode: str = "off"
    require: tuple[str, ...] = ()
    rerun_declared_command: bool = False


VERIFICATION_MODES: tuple[str, ...] = ("off", "return_contract", "task_result")

DEFAULT_VERIFICATION_REQUIRE: tuple[str, ...] = (
    "solution_description", "evidence", "command_to_verify")


def _hash_genes(genes: Mapping[str, Any]) -> str:
    canonical = json.dumps(genes, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class HarnessSpec:
    """One harness version: lineage metadata plus seven genes."""

    id: str = "baseline-v0"
    parent: str | None = None
    mutation: str | None = None
    reason: str | None = None
    prompt: PromptGenome = field(default_factory=PromptGenome)
    tools: ToolGenome = field(default_factory=ToolGenome)
    control: ControlGenome = field(default_factory=ControlGenome)
    memory: MemoryGenome = field(default_factory=MemoryGenome)
    recovery: RecoveryGenome = field(default_factory=RecoveryGenome)
    verification: VerificationGenome = field(default_factory=VerificationGenome)
    skills: tuple[str, ...] = ()
    source: str | None = None
    genes_hash: str = ""

    def __post_init__(self) -> None:
        if not self.genes_hash:
            object.__setattr__(self, "genes_hash", _hash_genes(self.genes_dict()))

    def genes_dict(self) -> dict[str, Any]:
        """The behavior-defining genes, excluding lineage and source."""
        return {
            "prompt": asdict(self.prompt),
            "tools": {"enabled": list(self.tools.enabled)},
            "control": asdict(self.control),
            "memory": asdict(self.memory),
            "recovery": asdict(self.recovery),
            "verification": asdict(self.verification),
            "skills": list(self.skills),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "parent": self.parent,
            "mutation": self.mutation,
            "reason": self.reason,
            **self.genes_dict(),
            "source": self.source,
            "genes_hash": self.genes_hash,
        }

    def tool_error_observation(self, exc: Exception, **context: Any) -> str:
        """Apply the recovery gene to a tool failure."""
        try:
            strategy = TOOL_ERROR_STRATEGIES[self.recovery.tool_error]
        except KeyError as missing:
            raise HarnessError(
                f"unknown tool_error strategy: {missing.args[0]!r}") from missing
        return strategy(exc, **context)


DEFAULT_HARNESS = HarnessSpec()


def default_harness() -> HarnessSpec:
    """The built-in baseline; mirrors harnesses/baseline-v0.yaml."""
    return DEFAULT_HARNESS


def _check_keys(section: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(section) - allowed
    if unknown:
        raise HarnessError(f"unknown {where} keys: {sorted(unknown)}")


def _optional_text(data: Mapping[str, Any], key: str, where: str) -> str | None:
    value = data.get(key)
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise HarnessError(f"{where}.{key} must be non-empty text or null")
    return value


def _required_text(section: Mapping[str, Any], key: str, where: str,
                   default: Any = None) -> Any:
    value = section.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise HarnessError(f"{where}.{key} must be non-empty text")
    return value


def _name_list(section: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = section.get(key, [])
    if not isinstance(value, list):
        raise HarnessError(f"{where}.{key} must be a list")
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise HarnessError(f"{where}.{key} entries must be non-empty text")
    return tuple(value)


def _prompt_genome(data: Mapping[str, Any]) -> PromptGenome:
    section = data.get("prompt") or {}
    if not isinstance(section, Mapping):
        raise HarnessError("prompt must be a mapping")
    _check_keys(section, {"system", "iteration_limit_notice"}, "prompt")
    system = section.get("system", "")
    if not isinstance(system, str):
        raise HarnessError("prompt.system must be text")
    return PromptGenome(
        system=system,
        iteration_limit_notice=_required_text(
            section, "iteration_limit_notice", "prompt", ITERATION_LIMIT_NOTICE),
    )


def _tools_genome(data: Mapping[str, Any]) -> ToolGenome:
    section = data.get("tools") or {}
    if not isinstance(section, Mapping):
        raise HarnessError("tools must be a mapping")
    _check_keys(section, {"enabled"}, "tools")
    return ToolGenome(enabled=_name_list(section, "enabled", "tools"))


def _control_genome(data: Mapping[str, Any]) -> ControlGenome:
    section = data.get("control") or {}
    if not isinstance(section, Mapping):
        raise HarnessError("control must be a mapping")
    _check_keys(section, {"max_iterations", "max_observation_chars",
                          "spill_preview_chars"}, "control")
    value = section.get("max_iterations", 10)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise HarnessError("control.max_iterations must be an integer >= 1")
    return ControlGenome(
        max_iterations=value,
        max_observation_chars=_positive_int(
            section.get("max_observation_chars"), "control.max_observation_chars",
            DEFAULT_MAX_OBSERVATION_CHARS),
        spill_preview_chars=_positive_int(
            section.get("spill_preview_chars"), "control.spill_preview_chars",
            DEFAULT_SPILL_PREVIEW_CHARS))


def _memory_genome(data: Mapping[str, Any]) -> MemoryGenome:
    section = data.get("memory") or {}
    if not isinstance(section, Mapping):
        raise HarnessError("memory must be a mapping")
    _check_keys(section, {"strategy", "head_chars", "context_budget",
                          "window_rounds", "keep_recent_rounds",
                          "summary_tail_rounds"}, "memory")
    strategy = _required_text(section, "strategy", "memory", "full_history")
    if strategy not in MEMORY_STRATEGIES:
        raise HarnessError(
            f"memory.strategy must be one of {list(MEMORY_STRATEGIES)}, "
            f"got {strategy!r}")
    return MemoryGenome(
        strategy=strategy,
        head_chars=_positive_int(section.get("head_chars"),
                                 "memory.head_chars", DEFAULT_HEAD_CHARS),
        context_budget=_positive_int(section.get("context_budget"),
                                     "memory.context_budget",
                                     DEFAULT_CONTEXT_BUDGET),
        window_rounds=_positive_int(section.get("window_rounds"),
                                    "memory.window_rounds",
                                    DEFAULT_WINDOW_ROUNDS),
        keep_recent_rounds=_positive_int(section.get("keep_recent_rounds"),
                                         "memory.keep_recent_rounds",
                                         DEFAULT_KEEP_RECENT_ROUNDS),
        summary_tail_rounds=_positive_int(section.get("summary_tail_rounds"),
                                          "memory.summary_tail_rounds",
                                          DEFAULT_SUMMARY_TAIL_ROUNDS))


def _recovery_genome(data: Mapping[str, Any]) -> RecoveryGenome:
    section = data.get("recovery") or {}
    if not isinstance(section, Mapping):
        raise HarnessError("recovery must be a mapping")
    _check_keys(section, {"tool_error", "tool_error_max_retries",
                          "llm_errors"}, "recovery")
    tool_error = _required_text(section, "tool_error", "recovery",
                                "feed_error_and_continue")
    if tool_error not in TOOL_ERROR_STRATEGIES:
        raise HarnessError(
            f"recovery.tool_error must be one of "
            f"{sorted(TOOL_ERROR_STRATEGIES)}, got {tool_error!r}")
    max_consecutive = section.get("tool_error_max_retries", 0)
    if (not isinstance(max_consecutive, int) or isinstance(max_consecutive, bool)
            or max_consecutive < 0):
        raise HarnessError(
            "recovery.tool_error_max_retries must be an integer >= 0")
    llm_section = section.get("llm_errors") or {}
    if not isinstance(llm_section, Mapping):
        raise HarnessError("recovery.llm_errors must be a mapping")
    _check_keys(llm_section, set(LLM_RETRY_CATEGORIES), "recovery.llm_errors")
    llm_errors: dict[str, LLMRetryPolicy] = {}
    for category in LLM_RETRY_CATEGORIES:
        entry = llm_section.get(category) or {}
        if not isinstance(entry, Mapping):
            raise HarnessError(
                f"recovery.llm_errors.{category} must be a mapping")
        _check_keys(entry, {"max_retries", "backoff", "base_delay"},
                    f"recovery.llm_errors.{category}")
        max_retries = entry.get("max_retries", 0)
        if (not isinstance(max_retries, int) or isinstance(max_retries, bool)
                or max_retries < 0):
            raise HarnessError(
                f"recovery.llm_errors.{category}.max_retries must be an "
                f"integer >= 0")
        backoff = entry.get("backoff", "exponential")
        if backoff not in BACKOFF_POLICIES:
            raise HarnessError(
                f"recovery.llm_errors.{category}.backoff must be one of "
                f"{sorted(BACKOFF_POLICIES)}, got {backoff!r}")
        base_delay = entry.get("base_delay", 0.5)
        if (not isinstance(base_delay, (int, float))
                or isinstance(base_delay, bool) or base_delay < 0):
            raise HarnessError(
                f"recovery.llm_errors.{category}.base_delay must be a "
                f"non-negative number")
        llm_errors[category] = LLMRetryPolicy(max_retries=max_retries,
                                              backoff=backoff,
                                              base_delay=float(base_delay))
    return RecoveryGenome(tool_error=tool_error,
                            tool_error_max_retries=max_consecutive,
                            llm_errors=llm_errors)


def _verification_genome(data: Mapping[str, Any]) -> VerificationGenome:
    section = data.get("verification") or {}
    if not isinstance(section, Mapping):
        raise HarnessError("verification must be a mapping")
    _check_keys(section, {"enabled", "mode", "require",
                          "rerun_declared_command"}, "verification")
    enabled = section.get("enabled", False)
    if not isinstance(enabled, bool):
        raise HarnessError("verification.enabled must be a boolean")
    mode = section.get("mode", "off")
    if not isinstance(mode, str) or mode not in VERIFICATION_MODES:
        raise HarnessError(
            f"verification.mode must be one of {list(VERIFICATION_MODES)}, "
            f"got {mode!r}")
    require_raw = section.get("require", [])
    if require_raw is None:
        require_raw = []
    if not isinstance(require_raw, (list, tuple)):
        raise HarnessError("verification.require must be a list")
    require: list[str] = []
    for item in require_raw:
        if not isinstance(item, str) or not item.strip():
            raise HarnessError(
                "verification.require entries must be non-empty text")
        require.append(item.strip())
    rerun = section.get("rerun_declared_command", False)
    if not isinstance(rerun, bool):
        raise HarnessError(
            "verification.rerun_declared_command must be a boolean")
    if mode in ("return_contract", "task_result") and not require:
        require = list(DEFAULT_VERIFICATION_REQUIRE)
    if rerun and mode not in ("return_contract", "task_result"):
        raise HarnessError(
            "verification.rerun_declared_command requires "
            "verification.mode=return_contract or task_result")
    return VerificationGenome(enabled=enabled, mode=mode,
                              require=tuple(require),
                              rerun_declared_command=rerun)


def from_dict(data: Mapping[str, Any]) -> HarnessSpec:
    """Validate manifest data and build a harness spec."""
    if not isinstance(data, Mapping):
        raise HarnessError("harness manifest must be a mapping")
    # "source" and "genes_hash" are outputs of to_dict(); accepted so a
    # spec can round-trip, but ignored and recomputed here.
    _check_keys(data, {"id", "parent", "mutation", "reason", "prompt", "tools",
                       "control", "memory", "recovery", "verification", "skills",
                       "source", "genes_hash"},
                "manifest")
    harness_id = data.get("id", "baseline-v0")
    if not isinstance(harness_id, str) or not harness_id.strip():
        raise HarnessError("manifest.id must be non-empty text")
    return HarnessSpec(
        id=harness_id,
        parent=_optional_text(data, "parent", "manifest"),
        mutation=_optional_text(data, "mutation", "manifest"),
        reason=_optional_text(data, "reason", "manifest"),
        prompt=_prompt_genome(data),
        tools=_tools_genome(data),
        control=_control_genome(data),
        memory=_memory_genome(data),
        recovery=_recovery_genome(data),
        verification=_verification_genome(data),
        skills=_name_list(data, "skills", "manifest"),
    )


def load_harness(path: str | Path) -> HarnessSpec:
    """Load and validate a harness manifest from a YAML file."""
    manifest_path = Path(path)
    if not manifest_path.is_file():
        raise HarnessError(f"harness manifest not found: {manifest_path}")
    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise HarnessError(f"invalid harness YAML in {manifest_path}: {exc}") from exc
    spec = from_dict(data or {})
    object.__setattr__(spec, "source", str(manifest_path))
    return spec


def resolve_harness(value: str | None) -> HarnessSpec:
    """Resolve a CLI/env harness reference to a spec.

    Accepts a file path or a bare id resolved against ``harnesses/``;
    falls back to the built-in baseline when nothing is given.
    """
    if not value:
        return DEFAULT_HARNESS
    direct = Path(value)
    if direct.is_file():
        return load_harness(direct)
    as_id = Path("harnesses") / f"{value}.yaml"
    if as_id.is_file():
        return load_harness(as_id)
    raise HarnessError(
        f"harness {value!r} not found as a file or as {as_id}")


def _main(argv: list[str] | None = None) -> int:
    """Harness management CLI: tree, derive, record, report, annotate."""
    import argparse

    from agent_runtime import evaluation, lineage

    parser = argparse.ArgumentParser(
        prog="python -m agent_runtime.harness",
        description="Manage harness manifests, evaluation records, and reports.")
    commands = parser.add_subparsers(dest="command", required=True)

    tree_parser = commands.add_parser("tree", help="show the harness family tree")
    tree_parser.add_argument("--harnesses-dir", type=Path,
                             default=lineage.DEFAULT_HARNESSES_DIR)

    derive_parser = commands.add_parser(
        "derive", help="create a child manifest from a parent")
    derive_parser.add_argument("child_id")
    derive_parser.add_argument("--from", dest="parent", required=True,
                               metavar="PARENT_ID")
    derive_parser.add_argument("--mutation", required=True,
                               help="one-line description of the gene change")
    derive_parser.add_argument("--reason", default=None)
    derive_parser.add_argument("--set", action="append", default=[], metavar="PATH=VALUE",
                               help="gene override, e.g. verification.enabled=true "
                                    "(repeatable)")
    derive_parser.add_argument("--harnesses-dir", type=Path,
                               default=lineage.DEFAULT_HARNESSES_DIR)
    derive_parser.add_argument("--yes", action="store_true",
                               help="write without showing the diff first")

    record_parser = commands.add_parser(
        "record", help="extract an evaluation record from a jobs directory")
    record_parser.add_argument("job_dir", type=Path)
    record_parser.add_argument("--model", default=None)
    record_parser.add_argument("--benchmark", default=None,
                               help="override the benchmark name")
    record_parser.add_argument("--notes", default=None)
    record_parser.add_argument("--records-dir", type=Path,
                               default=evaluation.DEFAULT_RECORDS_DIR)

    report_parser = commands.add_parser(
        "report", help="aggregate records along the harness lineage")
    report_parser.add_argument("--harnesses-dir", type=Path,
                               default=lineage.DEFAULT_HARNESSES_DIR)
    report_parser.add_argument("--records-dir", type=Path,
                               default=evaluation.DEFAULT_RECORDS_DIR)

    annotate_parser = commands.add_parser(
        "annotate", help="set the failure mode of one task in a record")
    annotate_parser.add_argument("record")
    annotate_parser.add_argument("task")
    annotate_parser.add_argument("--mode", required=True)
    annotate_parser.add_argument("--records-dir", type=Path,
                                 default=evaluation.DEFAULT_RECORDS_DIR)

    args = parser.parse_args(argv)

    if args.command == "tree":
        specs = lineage.load_all_harnesses(args.harnesses_dir)
        problems = lineage.validate_lineage(specs)
        print(lineage.format_tree(specs))
        if problems:
            print()
            for problem in problems:
                print(f"warning: {problem}")
            return 1
        return 0

    if args.command == "derive":
        specs = lineage.load_all_harnesses(args.harnesses_dir)
        parent = specs.get(args.parent)
        if parent is None:
            raise HarnessError(
                f"parent harness {args.parent!r} not found in {args.harnesses_dir}")
        if args.child_id in specs:
            raise HarnessError(f"harness id {args.child_id!r} already exists")
        sets = []
        for item in args.set:
            path, sep, raw = item.partition("=")
            if not sep or not path or not raw:
                raise HarnessError(f"--set expects PATH=VALUE, got {item!r}")
            sets.append((path, raw))
        child, diff = lineage.derive(parent, args.child_id, args.mutation,
                                     args.reason, sets)
        target = args.harnesses_dir / f"{child.id}.yaml"
        print(f"deriving {child.id} from {parent.id}")
        print(diff)
        if not args.yes:
            try:
                answer = input(f"\nwrite {target}? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            if answer not in {"y", "yes"}:
                print("aborted")
                return 1
        target.write_text(lineage.manifest_text(child), encoding="utf-8")
        print(f"wrote {target}")
        return 0

    if args.command == "record":
        record = evaluation.extract_record(args.job_dir, model=args.model,
                                           notes=args.notes,
                                           benchmark=args.benchmark)
        path = evaluation.write_record(record, args.records_dir)
        score = record["score"]
        print(f"wrote {path}")
        print(f"harness={record['harness_id']} benchmark={record['benchmark']} "
              f"passed={score['passed']}/{score['total']} rate={score['rate']}")
        return 0

    if args.command == "report":
        specs = lineage.load_all_harnesses(args.harnesses_dir)
        problems = lineage.validate_lineage(specs)
        records = evaluation.load_records(args.records_dir)
        rows = evaluation.build_report(specs, records)
        print(evaluation.format_report(rows))
        if problems:
            print()
            for problem in problems:
                print(f"warning: {problem}")
            return 1
        return 0

    if args.command == "annotate":
        path = evaluation.annotate_record(args.record, args.task, args.mode,
                                          args.records_dir)
        print(f"annotated {path}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "DEFAULT_HARNESS", "HarnessError", "HarnessSpec", "ITERATION_LIMIT_NOTICE",
    "PromptGenome", "ToolGenome", "ControlGenome", "MemoryGenome",
    "RecoveryGenome", "VerificationGenome", "DEFAULT_VERIFICATION_REQUIRE",
    "VERIFICATION_MODES", "TOOL_ERROR_STRATEGIES",
    "LLM_RETRY_CATEGORIES", "LLMRetryPolicy", "BACKOFF_POLICIES",
    "MEMORY_STRATEGIES", "default_harness", "feed_error_and_continue",
    "from_dict", "load_harness", "resolve_harness", "_main",
]
