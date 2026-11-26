"""Harness genome: the named, versioned configuration that decides how an
agent works, expressed as data instead of code.

See :mod:`agent_runtime.harness.schema` for the genome itself and
:mod:`agent_runtime.harness.loader` for manifest parsing and resolution.
"""

from .loader import (
    REPO_HARNESSES_DIR,
    default_harness,
    from_dict,
    harnesses_dir,
    list_harnesses,
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
    DEFAULT_HARNESS_ID,
    DEFAULT_HEAD_CHARS,
    DEFAULT_KEEP_RECENT_ROUNDS,
    DEFAULT_MAX_OBSERVATION_CHARS,
    DEFAULT_RETRIEVAL_EXEMPT_TOOLS,
    DEFAULT_SPILL_PREVIEW_CHARS,
    DEFAULT_SUMMARY_MAX_TOKENS,
    DEFAULT_SUMMARY_REASONING_EFFORT,
    DEFAULT_SUMMARY_TAIL_ROUNDS,
    DEFAULT_SUMMARIZER_INPUT_CAP_CHARS,
    DEFAULT_USER_PRESERVE_BUDGET,
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
    "TOOL_ERROR_STRATEGIES", "LLM_RETRY_CATEGORIES", "LLMRetryPolicy",
    "BACKOFF_POLICIES", "CONTEXT_STRATEGIES", "MEMORY_STRATEGIES",
    "CONTEXT_BUDGET_UNITS", "MEMORY_BUDGET_UNITS",
    "DEFAULT_USER_PRESERVE_BUDGET", "DEFAULT_SUMMARY_MAX_TOKENS",
    "DEFAULT_SUMMARY_REASONING_EFFORT",
    "DEFAULT_SUMMARIZER_INPUT_CAP_CHARS", "DEFAULT_RETRIEVAL_EXEMPT_TOOLS",
    "default_harness", "feed_error_and_continue", "from_dict", "harnesses_dir",
    "list_harnesses", "load_harness", "load_named_harness",
    "manifest_candidates", "resolve_harness",
]
