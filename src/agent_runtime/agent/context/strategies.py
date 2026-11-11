"""Named context strategies: registry plus per-strategy handlers.

Adding a strategy means writing a handler, registering it here, and
adding the name to the harness genome's strategy list; the shared
adapter never needs another branch.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from agent_runtime.harness import ContextGenome

from agent_runtime.agent.context.compact import compact_observations
from agent_runtime.agent.context.metrics import (
    _anchor_chars_per_token,
    _count_reasoning,
)
from agent_runtime.agent.context.summary import extract_summary, summarize_history
from agent_runtime.agent.context.turn_fold import turn_fold_messages

STRATEGY_FULL_HISTORY = "full_history"
STRATEGY_COMPACT_OBSERVATIONS = "compact_observations"
STRATEGY_LLM_SUMMARY = "llm_summary"
STRATEGY_TURN_FOLD = "turn_fold"


@dataclass(frozen=True)
class ContextStrategyRequest:
    """Everything a context strategy may need to build the request view."""

    messages: list[dict[str, Any]]
    working: list[dict[str, Any]]
    context: ContextGenome
    budget: int
    unit: str
    count_reasoning: bool
    usage_anchor: tuple[int, int, int] | None
    boundary: int
    previous_summary: str | None
    trace: Any
    iteration: int | None
    prompt: str | None
    summarizer: Any = None
    usage_sink: Any = None


ContextStrategyHandler = Callable[
    [ContextStrategyRequest], Awaitable[list[dict[str, Any]]]]


@dataclass(frozen=True)
class ContextStrategyDefinition:
    """A named strategy handler and whether the budget gate applies first."""

    handler: ContextStrategyHandler
    budget_gated: bool = True


async def _full_history_strategy(
        request: ContextStrategyRequest) -> list[dict[str, Any]]:
    return request.messages


async def _compact_observations_strategy(
        request: ContextStrategyRequest) -> list[dict[str, Any]]:
    # compact_observations is char-metered by construction; under the
    # tokens unit the budget is converted with the anchor-calibrated
    # ratio (a threshold decision, so conversion noise is acceptable).
    char_budget = request.budget
    if request.unit == "tokens":
        char_budget = max(
            1, int(request.budget
                   * _anchor_chars_per_token(request.usage_anchor)))
    return compact_observations(
        request.messages, budget=char_budget,
        window_rounds=request.context.window_rounds,
        keep_recent_rounds=request.context.keep_recent_rounds,
        head_chars=request.context.head_chars,
        exempt_tools=request.context.retrieval_exempt_tools,
        count_reasoning=request.count_reasoning)


async def _llm_summary_strategy(
        request: ContextStrategyRequest) -> list[dict[str, Any]]:
    return await summarize_history(
        request.messages, budget=request.budget,
        tail_rounds=request.context.summary_tail_rounds,
        user_preserve_budget=request.context.user_preserve_budget,
        summary_max_tokens=request.context.summary_max_tokens,
        summary_reasoning_effort=request.context.summary_reasoning_effort,
        boundary=request.boundary,
        previous_summary=request.previous_summary,
        unit=request.unit, usage_anchor=request.usage_anchor,
        exempt_tools=request.context.retrieval_exempt_tools,
        prompt=request.prompt, trace=request.trace,
        iteration=request.iteration, llm=request.summarizer,
        count_reasoning=request.count_reasoning,
        usage_sink=request.usage_sink)


async def _turn_fold_strategy(
        request: ContextStrategyRequest) -> list[dict[str, Any]]:
    return turn_fold_messages(request.messages)


CONTEXT_STRATEGY_DEFINITIONS: dict[str, ContextStrategyDefinition] = {
    STRATEGY_FULL_HISTORY: ContextStrategyDefinition(
        _full_history_strategy, budget_gated=False),
    STRATEGY_COMPACT_OBSERVATIONS: ContextStrategyDefinition(
        _compact_observations_strategy),
    STRATEGY_LLM_SUMMARY: ContextStrategyDefinition(
        _llm_summary_strategy),
    STRATEGY_TURN_FOLD: ContextStrategyDefinition(
        _turn_fold_strategy, budget_gated=False),
}


def register_context_strategy(
        name: str, handler: ContextStrategyHandler, *,
        budget_gated: bool = True) -> None:
    """Register one context strategy under ``name``."""
    CONTEXT_STRATEGY_DEFINITIONS[name] = ContextStrategyDefinition(
        handler, budget_gated=budget_gated)


__all__ = [
    "STRATEGY_FULL_HISTORY", "STRATEGY_COMPACT_OBSERVATIONS",
    "STRATEGY_LLM_SUMMARY", "STRATEGY_TURN_FOLD",
    "CONTEXT_STRATEGY_DEFINITIONS", "ContextStrategyDefinition",
    "ContextStrategyRequest", "register_context_strategy",
]
