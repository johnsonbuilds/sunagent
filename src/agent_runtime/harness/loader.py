"""Load and validate harness manifests.

The loader owns manifest parsing, validation, and resolution.  It has no
dependency on the CLI or on lineage/evaluation, so importing it never
pulls the research tooling into a runtime process.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .schema import (
    BACKOFF_POLICIES,
    BUILTIN_HARNESS,
    BudgetReminder,
    CONTEXT_BUDGET_UNITS,
    CONTEXT_STRATEGIES,
    ControlGenome,
    ContextGenome,
    DEFAULT_CONTEXT_BUDGET,
    DEFAULT_EDIT_TOOLS,
    DEFAULT_HARNESS_ID,
    DEFAULT_HEAD_CHARS,
    DEFAULT_KEEP_RECENT_ROUNDS,
    DEFAULT_MAX_OBSERVATION_CHARS,
    DEFAULT_SPILL_PREVIEW_CHARS,
    DEFAULT_SUBMIT_TOOL,
    DEFAULT_SUMMARY_MAX_TOKENS,
    DEFAULT_SUMMARY_TAIL_ROUNDS,
    DEFAULT_TEST_TARGET_PATTERN,
    DEFAULT_USER_PRESERVE_BUDGET,
    DEFAULT_VERIFICATION_REQUIRE,
    DEFAULT_WINDOW_ROUNDS,
    HarnessError,
    HarnessSpec,
    ITERATION_LIMIT_NOTICE,
    LLM_RETRY_CATEGORIES,
    LLMRetryPolicy,
    PromptGenome,
    RecoveryGenome,
    SUMMARY_REASONING_EFFORTS,
    TOOL_ERROR_STRATEGIES,
    ToolGenome,
    VERIFICATION_MODES,
    VerificationGenome,
)


# The manifests live beside the package source, not inside it, so they
# are also looked up relative to the repository root: the package can
# run from a directory other than the project root.
REPO_HARNESSES_DIR = Path(__file__).resolve().parents[3] / "harnesses"


def _positive_int(value: Any, label: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise HarnessError(f"{label} must be an integer >= 1")
    return value


@lru_cache(maxsize=1)
def default_harness() -> HarnessSpec:
    """The default harness: ``harnesses/common-v0.yaml``.

    Falls back to the built-in neutral genome (baseline-v0) when the
    manifest is unavailable, e.g. a wheel install without the repo.
    """
    try:
        return load_named_harness(DEFAULT_HARNESS_ID)
    except HarnessError:
        return BUILTIN_HARNESS


def _check_keys(section: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(section) - allowed
    if unknown:
        raise HarnessError(f"unknown {where} keys: {sorted(unknown)}")


def _optional_text(data: Mapping[str, Any], key: str, where: str) -> str | None:
    value = data.get(key)
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise HarnessError(f"{where}.{key} must be non-empty text or null")
    return value


def _summary_reasoning_effort(section: Mapping[str, Any]) -> str | None:
    """Parse ``context.summary_reasoning_effort`` as a normalized token.

    The value is passed verbatim to the provider as ``reasoning_effort``;
    validating the union of values the supported endpoints document keeps
    a typo from turning every compaction into a failed summarizer call.
    """
    value = _optional_text(section, "summary_reasoning_effort", "context")
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized not in SUMMARY_REASONING_EFFORTS:
        raise HarnessError(
            f"context.summary_reasoning_effort must be one of "
            f"{list(SUMMARY_REASONING_EFFORTS)} or null, got {value!r}")
    return normalized


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
                          "spill_preview_chars", "finish_violation_limit",
                          "budget_reminder"},
                "control")
    value = section.get("max_iterations", 10)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise HarnessError("control.max_iterations must be an integer >= 1")
    violation_limit = section.get("finish_violation_limit", 3)
    if (not isinstance(violation_limit, int)
            or isinstance(violation_limit, bool) or violation_limit < 0):
        raise HarnessError(
            "control.finish_violation_limit must be an integer >= 0")
    return ControlGenome(
        max_iterations=value,
        max_observation_chars=_positive_int(
            section.get("max_observation_chars"), "control.max_observation_chars",
            DEFAULT_MAX_OBSERVATION_CHARS),
        spill_preview_chars=_positive_int(
            section.get("spill_preview_chars"), "control.spill_preview_chars",
            DEFAULT_SPILL_PREVIEW_CHARS),
        finish_violation_limit=violation_limit,
        budget_reminder=_budget_reminder(section.get("budget_reminder")))


def _budget_reminder(value: Any) -> BudgetReminder:
    if value is None:
        return BudgetReminder()
    if not isinstance(value, Mapping):
        raise HarnessError("control.budget_reminder must be a mapping")
    _check_keys(value, {"enabled", "at_fractions"}, "control.budget_reminder")
    enabled = value.get("enabled", False)
    if not isinstance(enabled, bool):
        raise HarnessError("control.budget_reminder.enabled must be a boolean")
    raw = value.get("at_fractions", (0.6, 0.85))
    if not isinstance(raw, (list, tuple)) or not raw:
        raise HarnessError("control.budget_reminder.at_fractions must be a non-empty list")
    fractions: list[float] = []
    for item in raw:
        if not isinstance(item, (int, float)) or isinstance(item, bool) \
                or not 0 < item < 1:
            raise HarnessError(
                "control.budget_reminder.at_fractions entries must be in (0, 1)")
        fractions.append(float(item))
    return BudgetReminder(enabled=enabled, at_fractions=tuple(sorted(fractions)))


def _context_genome(data: Mapping[str, Any]) -> ContextGenome:
    # ``memory`` is the legacy gene name: old manifests still carry it.
    # New manifests use ``context``. Both present is ambiguous -> reject.
    if "context" in data and "memory" in data:
        if data.get("context") is not None and data.get("memory") is not None:
            raise HarnessError("use 'context', not both 'context' and legacy 'memory'")
    section = data.get("context")
    if section is None:
        section = data.get("memory") or {}
    if not isinstance(section, Mapping):
        raise HarnessError("context must be a mapping")
    _check_keys(section, {"strategy", "budget_unit", "head_chars",
                          "context_budget", "window_rounds",
                          "keep_recent_rounds", "summary_tail_rounds",
                          "user_preserve_budget", "summary_max_tokens",
                          "summary_reasoning_effort",
                          "retrieval_exempt_tools"},
                "context")
    strategy = _required_text(section, "strategy", "context", "full_history")
    if strategy not in CONTEXT_STRATEGIES:
        raise HarnessError(
            f"context.strategy must be one of {list(CONTEXT_STRATEGIES)}, "
            f"got {strategy!r}")
    budget_unit = section.get("budget_unit") or "chars"
    if budget_unit not in CONTEXT_BUDGET_UNITS:
        raise HarnessError(
            f"context.budget_unit must be one of "
            f"{list(CONTEXT_BUDGET_UNITS)}, got {budget_unit!r}")
    return ContextGenome(
        strategy=strategy,
        budget_unit=budget_unit,
        head_chars=_positive_int(section.get("head_chars"),
                                 "context.head_chars", DEFAULT_HEAD_CHARS),
        context_budget=_positive_int(section.get("context_budget"),
                                     "context.context_budget",
                                     DEFAULT_CONTEXT_BUDGET),
        window_rounds=_positive_int(section.get("window_rounds"),
                                    "context.window_rounds",
                                    DEFAULT_WINDOW_ROUNDS),
        keep_recent_rounds=_positive_int(section.get("keep_recent_rounds"),
                                         "context.keep_recent_rounds",
                                         DEFAULT_KEEP_RECENT_ROUNDS),
        summary_tail_rounds=_positive_int(section.get("summary_tail_rounds"),
                                          "context.summary_tail_rounds",
                                          DEFAULT_SUMMARY_TAIL_ROUNDS),
        user_preserve_budget=_positive_int(
            section.get("user_preserve_budget"),
            "context.user_preserve_budget", DEFAULT_USER_PRESERVE_BUDGET),
        summary_max_tokens=_positive_int(
            section.get("summary_max_tokens"),
            "context.summary_max_tokens", DEFAULT_SUMMARY_MAX_TOKENS),
        summary_reasoning_effort=_summary_reasoning_effort(section),
        retrieval_exempt_tools=_name_list(
            section, "retrieval_exempt_tools", "context"))


def _memory_genome(data: Mapping[str, Any]) -> ContextGenome:
    """Legacy alias: old code path, now loads via :func:`_context_genome`."""
    return _context_genome(data)


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
                          "rerun_declared_command", "submit_tool",
                          "edit_tools", "test_target_pattern"},
                "verification")
    # Legacy (code-v10..v12): accepted so old manifests load for lineage,
    # otherwise ignored — since code-v13 a declared command is always
    # re-executed, no flag needed.
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
    if mode in ("return_contract", "task_result") and not require:
        require = list(DEFAULT_VERIFICATION_REQUIRE)
    submit_tool = _required_text(section, "submit_tool", "verification",
                                 DEFAULT_SUBMIT_TOOL)
    edit_tools = (DEFAULT_EDIT_TOOLS if "edit_tools" not in section
                  else _name_list(section, "edit_tools", "verification"))
    test_target_pattern = _required_text(
        section, "test_target_pattern", "verification",
        DEFAULT_TEST_TARGET_PATTERN)
    try:
        re.compile(test_target_pattern)
    except re.error as exc:
        raise HarnessError(
            "verification.test_target_pattern must be a valid regular "
            f"expression: {exc}") from exc
    return VerificationGenome(enabled=enabled, mode=mode,
                              require=tuple(require),
                              submit_tool=submit_tool,
                              edit_tools=tuple(edit_tools),
                              test_target_pattern=test_target_pattern)


def from_dict(data: Mapping[str, Any]) -> HarnessSpec:
    """Validate manifest data and build a harness spec."""
    if not isinstance(data, Mapping):
        raise HarnessError("harness manifest must be a mapping")
    # "source" and "genes_hash" are outputs of to_dict(); accepted so a
    # spec can round-trip, but ignored and recomputed here.
    _check_keys(data, {"id", "parent", "mutation", "reason", "prompt", "tools",
                       "control", "context", "memory", "recovery", "verification", "skills",
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
        context=_context_genome(data),
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


def manifest_candidates(harness_id: str) -> list[Path]:
    """Where a harness id may live: the working directory's ``harnesses/``
    first, then the repository's own ``harnesses/``."""
    return [Path("harnesses") / f"{harness_id}.yaml",
            REPO_HARNESSES_DIR / f"{harness_id}.yaml"]


def load_named_harness(harness_id: str) -> HarnessSpec:
    """Load a harness by id from the ``harnesses/`` directory."""
    candidates = manifest_candidates(harness_id)
    for path in candidates:
        if path.is_file():
            return load_harness(path)
    raise HarnessError(
        f"harness {harness_id!r} not found in "
        + " or ".join(str(path) for path in candidates))


def resolve_harness(value: str | None) -> HarnessSpec:
    """Resolve a CLI/env harness reference to a spec.

    Accepts a file path or a bare id resolved against ``harnesses/``;
    falls back to the default harness (common-v0) when nothing is given.
    """
    if not value:
        return default_harness()
    direct = Path(value)
    if direct.is_file():
        return load_harness(direct)
    try:
        return load_named_harness(value)
    except HarnessError as exc:
        raise HarnessError(
            f"harness {value!r} not found as a file or as "
            f"{Path('harnesses') / f'{value}.yaml'}") from exc

