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

from .loader import (
    REPO_HARNESSES_DIR,
    default_harness,
    from_dict,
    load_harness,
    load_named_harness,
    manifest_candidates,
    resolve_harness,
)
from .schema import (
    BACKOFF_POLICIES,
    BUILTIN_HARNESS,
    BudgetReminder,
    CONTEXT_BUDGET_UNITS,
    CONTEXT_STRATEGIES,
    ContextGenome,
    ControlGenome,
    DEFAULT_CONTEXT_BUDGET,
    DEFAULT_EDIT_TOOLS,
    DEFAULT_HARNESS_ID,
    DEFAULT_HEAD_CHARS,
    DEFAULT_KEEP_RECENT_ROUNDS,
    DEFAULT_MAX_OBSERVATION_CHARS,
    DEFAULT_RETRIEVAL_EXEMPT_TOOLS,
    DEFAULT_SPILL_PREVIEW_CHARS,
    DEFAULT_SUBMIT_TOOL,
    DEFAULT_SUMMARY_MAX_TOKENS,
    DEFAULT_SUMMARY_TAIL_ROUNDS,
    DEFAULT_SUMMARIZER_INPUT_CAP_CHARS,
    DEFAULT_TEST_TARGET_PATTERN,
    DEFAULT_USER_PRESERVE_BUDGET,
    DEFAULT_VERIFICATION_REQUIRE,
    DEFAULT_WINDOW_ROUNDS,
    HarnessError,
    HarnessSpec,
    ITERATION_LIMIT_NOTICE,
    LLM_RETRY_CATEGORIES,
    LLMRetryPolicy,
    MEMORY_BUDGET_UNITS,
    MEMORY_STRATEGIES,
    MemoryGenome,
    PromptGenome,
    RecoveryGenome,
    TOOL_ERROR_STRATEGIES,
    ToolGenome,
    VERIFICATION_MODES,
    VerificationGenome,
    backoff_exponential,
    backoff_fixed,
    backoff_none,
    feed_error_and_continue,
)

__all__ = [
    "BUILTIN_HARNESS", "DEFAULT_HARNESS_ID", "REPO_HARNESSES_DIR",
    "HarnessError", "HarnessSpec", "ITERATION_LIMIT_NOTICE",
    "PromptGenome", "ToolGenome", "BudgetReminder", "ControlGenome",
    "ContextGenome", "MemoryGenome", "RecoveryGenome", "VerificationGenome",
    "DEFAULT_VERIFICATION_REQUIRE", "VERIFICATION_MODES",
    "TOOL_ERROR_STRATEGIES", "LLM_RETRY_CATEGORIES", "LLMRetryPolicy",
    "BACKOFF_POLICIES", "CONTEXT_STRATEGIES", "MEMORY_STRATEGIES",
    "CONTEXT_BUDGET_UNITS", "MEMORY_BUDGET_UNITS",
    "DEFAULT_USER_PRESERVE_BUDGET", "DEFAULT_SUMMARY_MAX_TOKENS",
    "DEFAULT_SUMMARIZER_INPUT_CAP_CHARS", "DEFAULT_RETRIEVAL_EXEMPT_TOOLS",
    "DEFAULT_SUBMIT_TOOL", "DEFAULT_EDIT_TOOLS", "DEFAULT_TEST_TARGET_PATTERN",
    "default_harness", "feed_error_and_continue", "from_dict", "load_harness",
    "load_named_harness", "manifest_candidates", "resolve_harness",
]
