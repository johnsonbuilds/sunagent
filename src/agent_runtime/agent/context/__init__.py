"""Context management: the request view of conversation history.

The conversation itself is the source of truth and is never mutated by
these strategies.  A strategy only produces the *view* of the history
that gets sent to the LLM on each request, which keeps the on-disk
trace, future phases (summarisation), and post-hoc analysis intact.

Split layout (Step 2):

* :mod:`metrics` — char/token sizing, budget-unit conversion.
* :mod:`compact` — deterministic LLM-free observation folding.
* :mod:`summary` — LLM-backed snapshot summarization.
* :mod:`strategies` — named strategy registry + handlers.
* :mod:`view` — genome-to-view adapter (``apply_context_strategy``).

This package root re-exports the public surface so existing
``from agent_runtime.agent.context import ...`` imports keep working.
"""

from __future__ import annotations

from agent_runtime.harness import (
    DEFAULT_CONTEXT_BUDGET,
    DEFAULT_HEAD_CHARS,
    DEFAULT_KEEP_RECENT_ROUNDS,
    DEFAULT_SUMMARIZER_INPUT_CAP_CHARS,
    DEFAULT_SUMMARY_MAX_TOKENS,
    DEFAULT_SUMMARY_TAIL_ROUNDS,
    DEFAULT_USER_PRESERVE_BUDGET,
    DEFAULT_WINDOW_ROUNDS,
)

from agent_runtime.agent.context.compact import compact_observations
from agent_runtime.agent.context.metrics import (
    _anchor_chars_per_token,
    _view_size,
    request_view_size,
    total_chars,
)
from agent_runtime.agent.context.strategies import (
    CONTEXT_STRATEGY_DEFINITIONS,
    STRATEGY_COMPACT_OBSERVATIONS,
    STRATEGY_FULL_HISTORY,
    STRATEGY_LLM_SUMMARY,
    ContextStrategyDefinition,
    ContextStrategyRequest,
    register_context_strategy,
)
from agent_runtime.agent.context.summary import (
    SUMMARY_PREFIX,
    SUMMARIZER_INSTRUCTION,
    extract_summary,
    summarize_history,
)
from agent_runtime.agent.context.view import (
    apply_context_strategy,
    apply_memory_strategy,
)

__all__ = [
    "DEFAULT_CONTEXT_BUDGET", "DEFAULT_HEAD_CHARS", "DEFAULT_SUMMARY_TAIL_ROUNDS",
    "DEFAULT_KEEP_RECENT_ROUNDS", "DEFAULT_WINDOW_ROUNDS",
    "DEFAULT_USER_PRESERVE_BUDGET", "DEFAULT_SUMMARY_MAX_TOKENS",
    "DEFAULT_SUMMARIZER_INPUT_CAP_CHARS",
    "STRATEGY_COMPACT_OBSERVATIONS", "STRATEGY_FULL_HISTORY", "STRATEGY_LLM_SUMMARY",
    "CONTEXT_STRATEGY_DEFINITIONS", "ContextStrategyDefinition",
    "ContextStrategyRequest", "SUMMARY_PREFIX", "SUMMARIZER_INSTRUCTION",
    "request_view_size", "apply_context_strategy", "apply_memory_strategy",
    "compact_observations", "register_context_strategy",
    "summarize_history", "extract_summary", "total_chars",
]
