"""Harness genome: the named, versioned configuration that decides how an
agent works, expressed as data instead of code.

The runtime engine (loop, trace, events) stays stable; a harness spec
selects prompt, tools, control flow, context, recovery and verification
behavior.  Specs are loaded from YAML manifests so harness versions can
be diffed, compared, and later mutated one gene at a time:

    baseline-v0  --(enable verification)-->  baseline-v1

Every run records its harness id and a content hash of the genes, so a
trace can always be traced back to the exact harness that produced it.
The harness used when none is requested is ``common-v0``, the generic
task base loaded from ``harnesses/common-v0.yaml``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any


class HarnessError(ValueError):
    """Raised when a harness manifest is missing or invalid."""


ITERATION_LIMIT_NOTICE = (
    "Iteration limit reached. Summarize the progress and give the "
    "best possible final answer. Do not call tools."
)

# The harness a run gets when nothing is requested: the generic
# task base that fits code, analysis, lookup and chat tasks alike.
DEFAULT_HARNESS_ID = "common-v0"


def feed_error_and_continue(exc: Exception, *, tool: str | None = None) -> str:
    """Turn a tool failure into an observation and let the loop continue."""
    return f"Tool error: {exc}"


ToolErrorStrategy = Callable[..., str]

TOOL_ERROR_STRATEGIES: dict[str, ToolErrorStrategy] = {
    "feed_error_and_continue": feed_error_and_continue,
}

# Categories a failed LLM call is classified into; each maps to its own
# retry policy under the recovery.llm_errors gene.  ``context_window`` is
# the hard gate: the provider rejected the request for exceeding the
# context window.  Retrying it in place can never succeed, so the loop
# refreshes (further compacts) the request view before resending.
LLM_RETRY_CATEGORIES = (
    "stream_idle_timeout",
    "stream_truncated",
    "stream_empty",
    "provider_error",
    "context_window",
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
    "full_history", "compact_observations", "llm_summary", "turn_fold")

# Backwards-compatibility alias: the gene is now called ``context``.
# ``memory`` is reserved for future long-term / cross-session recall
# (see ``workspace/memory/``); harness manifests may still use the
# legacy ``memory`` key, which loads as ``context``.
CONTEXT_STRATEGIES: tuple[str, ...] = MEMORY_STRATEGIES

# Transcript-economics defaults: declared once here and consumed by the
# context strategies (agent.context) and the observation boundary
# (agent.observations).  The invariant "archive exactly what compaction
# could truncate" is structural: the observation formatter receives
# archive_min_chars = context.head_chars, so the two can never drift.
DEFAULT_HEAD_CHARS = 200                 # context: compaction keeps this much of an old observation
DEFAULT_CONTEXT_BUDGET = 60_000          # context: request-view char budget that triggers compaction
DEFAULT_WINDOW_ROUNDS = 12               # context: compaction comfort window
DEFAULT_KEEP_RECENT_ROUNDS = 2           # context: rounds never compacted under budget pressure
DEFAULT_SUMMARY_TAIL_ROUNDS = 2          # context: llm_summary verbatim tail
DEFAULT_USER_PRESERVE_BUDGET = 8_000     # context: CHARS (either budget unit) of mid-conversation user messages kept verbatim (newest first). Always chars by design: the preserve pool is a coarse guard, and routing it through the anchor-calibrated char/token ratio would inject conversion noise into a threshold chars serve fine.
DEFAULT_SUMMARY_MAX_TOKENS = 2_048       # context: summarizer output cap; longer output is rejected, falls back
# Cap (chars) for the summarizer's input on the retry attempt. Pathological
# segments (hundreds of k chars after repeated failures stall the boundary)
# make the model return empty/off-schema snapshots; TB-2.1 showed invalid
# inputs at p50 177k / max 548k chars while successes cluster far below.
# Attempt 1 always reads the full segment; only the retry folds first, so
# normal cases never pay the coverage cost of the cap.
DEFAULT_SUMMARIZER_INPUT_CAP_CHARS = 150_000

# Tool names whose observations outlive the comfort window verbatim and
# only fold under real budget pressure (last-folded, never exempt from
# the budget itself).  Retrieval results (grep/read/find) are anchors
# for multi-step reasoning; folding them early forces re-query rounds.
DEFAULT_RETRIEVAL_EXEMPT_TOOLS: tuple[str, ...] = ()

# Units the request-view budget can be measured in.  ``chars`` is the
# legacy cheap proxy; ``tokens`` combines the last measured prompt usage
# with per-message char estimates (see agent.context).
MEMORY_BUDGET_UNITS: tuple[str, ...] = ("chars", "tokens")
CONTEXT_BUDGET_UNITS: tuple[str, ...] = MEMORY_BUDGET_UNITS
DEFAULT_MAX_OBSERVATION_CHARS = 16_000   # control: inline observation budget before windowing
DEFAULT_SPILL_PREVIEW_CHARS = 4_000      # control: head/tail window half-size


@dataclass(frozen=True)
class PromptGenome:
    system: str = ""
    iteration_limit_notice: str = ITERATION_LIMIT_NOTICE


@dataclass(frozen=True)
class ToolGenome:
    enabled: tuple[str, ...] = ("run_command",)


@dataclass(frozen=True)
class BudgetReminder:
    enabled: bool = False
    at_fractions: tuple[float, ...] = (0.6, 0.85)


@dataclass(frozen=True)
class ControlGenome:
    max_iterations: int = 10
    max_observation_chars: int = DEFAULT_MAX_OBSERVATION_CHARS
    spill_preview_chars: int = DEFAULT_SPILL_PREVIEW_CHARS
    # Consecutive finish attempts rejected without interleaving work
    # (plain-text finishes, invalid submissions) tolerated before the
    # turn aborts. 0 disables the fuse (legacy behavior: burn the whole
    # iteration budget on prose).
    finish_violation_limit: int = 3
    budget_reminder: BudgetReminder = field(default_factory=BudgetReminder)


@dataclass(frozen=True)
class ContextGenome:
    strategy: str = "full_history"
    budget_unit: str = "chars"
    head_chars: int = DEFAULT_HEAD_CHARS
    context_budget: int = DEFAULT_CONTEXT_BUDGET
    window_rounds: int = DEFAULT_WINDOW_ROUNDS
    keep_recent_rounds: int = DEFAULT_KEEP_RECENT_ROUNDS
    summary_tail_rounds: int = DEFAULT_SUMMARY_TAIL_ROUNDS
    user_preserve_budget: int = DEFAULT_USER_PRESERVE_BUDGET  # chars, either budget unit (coarse guard; no token conversion)
    summary_max_tokens: int = DEFAULT_SUMMARY_MAX_TOKENS
    retrieval_exempt_tools: tuple[str, ...] = DEFAULT_RETRIEVAL_EXEMPT_TOOLS


# Backwards-compatibility alias: ``memory`` is the legacy gene name.
MemoryGenome = ContextGenome


@dataclass(frozen=True)
class RecoveryGenome:
    tool_error: str = "feed_error_and_continue"
    # Consecutive identical tool failures tolerated before the turn stops
    # early with a manual-handling message. 0 disables the guard (legacy
    # behavior: retry until control.max_iterations is exhausted).
    tool_error_max_retries: int = 0
    llm_errors: dict[str, LLMRetryPolicy] = field(
        default_factory=_default_llm_errors)


# Defaults for the submission contract.  Tool names and the test-target
# pattern are genes because deployments may add write tools, rename the
# submit tool, or verify a non-Python repository.
DEFAULT_SUBMIT_TOOL = "submit_result"
DEFAULT_EDIT_TOOLS: tuple[str, ...] = (
    "edit_file", "apply_patch", "write_file")
DEFAULT_TEST_TARGET_PATTERN = (
    r"[\w\-/]+\.(?:py|sh)\b|tests?/[\w\-/.]+|test_[\w\-/]+")


@dataclass(frozen=True)
class VerificationGenome:
    """Submission contract checked at the turn boundary.

    The model finishes by calling ``submit_tool`` with
    ``solution_description / evidence / command_to_verify``.  The gate
    checks shape (required fields present and non-trivial) and grounding
    (evidence quotes observed output, the declared command was actually
    run, and at least one source edit exists).  The declared command is
    re-executed verbatim and must exit 0.  ``require`` selects the fields;
    ``mode="off"`` disables the gate.
    """

    enabled: bool = False
    mode: str = "off"
    require: tuple[str, ...] = ()
    submit_tool: str = DEFAULT_SUBMIT_TOOL
    edit_tools: tuple[str, ...] = DEFAULT_EDIT_TOOLS
    test_target_pattern: str = DEFAULT_TEST_TARGET_PATTERN


VERIFICATION_MODES: tuple[str, ...] = ("off", "return_contract", "task_result")

DEFAULT_VERIFICATION_REQUIRE: tuple[str, ...] = (
    "solution_description", "evidence", "command_to_verify")


def _hash_genes(genes: Mapping[str, Any]) -> str:
    canonical = json.dumps(genes, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class HarnessSpec:
    """One harness version: lineage metadata plus seven genes."""

    # The neutral built-in genome mirrors harnesses/baseline-v0.yaml.
    id: str = "baseline-v0"
    parent: str | None = None
    mutation: str | None = None
    reason: str | None = None
    prompt: PromptGenome = field(default_factory=PromptGenome)
    tools: ToolGenome = field(default_factory=ToolGenome)
    control: ControlGenome = field(default_factory=ControlGenome)
    context: ContextGenome = field(default_factory=ContextGenome)
    recovery: RecoveryGenome = field(default_factory=RecoveryGenome)
    verification: VerificationGenome = field(default_factory=VerificationGenome)
    skills: tuple[str, ...] = ()
    source: str | None = None
    genes_hash: str = ""

    @property
    def memory(self) -> "ContextGenome":
        """Legacy alias for :attr:`context` (pre-rename manifests/code)."""
        return self.context

    def __post_init__(self) -> None:
        if not self.genes_hash:
            object.__setattr__(self, "genes_hash", _hash_genes(self.genes_dict()))

    def genes_dict(self) -> dict[str, Any]:
        """The behavior-defining genes, excluding lineage and source."""
        return {
            "prompt": asdict(self.prompt),
            "tools": {"enabled": list(self.tools.enabled)},
            "control": asdict(self.control),
            "context": {**asdict(self.context),
                       "retrieval_exempt_tools":
                           list(self.context.retrieval_exempt_tools)},
            "recovery": asdict(self.recovery),
            "verification": {
                **asdict(self.verification),
                "edit_tools": list(self.verification.edit_tools),
            },
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


# The neutral built-in genome (the dataclass defaults). It mirrors
# harnesses/baseline-v0.yaml and is the fallback used when no manifest
# can be found; the harness a run actually gets by default is common-v0.
BUILTIN_HARNESS = HarnessSpec()
