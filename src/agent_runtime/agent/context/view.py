"""Request-view adapter: genome in, provider-ready view out.

Unpacks the harness :class:`ContextGenome` (strategy plus all
view-shaping knobs) into the strategy functions, which stay pure and
parameter-driven.
"""

from __future__ import annotations

import logging
from typing import Any

from agent_runtime.harness import ContextGenome

from agent_runtime.agent.context.compact import compact_observations
from agent_runtime.agent.context.metrics import (
    _anchor_chars_per_token,
    _count_reasoning,
    _view_size,
    total_chars,
)
from agent_runtime.agent.context.strategies import CONTEXT_STRATEGY_DEFINITIONS
from agent_runtime.agent.context.summary import extract_summary

logger = logging.getLogger(__name__)


async def apply_context_strategy(messages: list[dict[str, Any]], context: ContextGenome, *,
                                 compact_messages: list[dict[str, Any]] | None = None,
                                 trace: Any = None, iteration: int | None = None,
                                 boundary: int = 0,
                                 usage_anchor: tuple[int, int, int] | None = None,
                                 force_budget: int | None = None,
                                 summarizer: Any = None,
                                 count_reasoning: bool | None = None,
                                 usage_sink: Any = None,
                                 fallback_strategy: str = "compact_observations",
                                 **options: Any) -> list[dict[str, Any]]:
    """Build the request view of ``messages`` under the memory genome.

    ``messages`` is the FULL conversation (the source of truth, never mutated
    here). ``compact_messages`` is the working/compacted view carried across
    turns: it is used *only* to decide whether a re-compaction is due (its size
    vs the context budget), while the actual compaction always summarizes or
    compacts the FULL history. This keeps the per-turn request bounded and
    avoids re-compacting on every iteration.

    When a re-compaction actually produces a new view, a ``context.{strategy}``
    event is emitted on ``trace`` (if provided) so the caller stays free of
    strategy logic.

    ``fallback_strategy`` names the registry entry used when a
    ``force_budget`` hard gate produces no shrinkage (resending the view
    would be rejected again). It defaults to the deterministic compactor;
    pass a registered custom name to override without touching this
    adapter (OCP: new strategies plug in via ``register_context_strategy``).

    Reasoning chars are budgeted only when the echo policy will replay
    them on the wire (see the shared wire policy).
    """
    from agent_runtime.agent.context.strategies import ContextStrategyRequest

    strategy = context.strategy
    definition = CONTEXT_STRATEGY_DEFINITIONS.get(strategy)
    if definition is None:
        raise ValueError(
            f"unknown context strategy: {strategy!r}; "
            f"known: {sorted(CONTEXT_STRATEGY_DEFINITIONS)}")

    count = _count_reasoning(count_reasoning)
    unit = context.budget_unit
    # force_budget (context-window hard gate) must push below the size the
    # provider just rejected, so it wins whenever it is the tighter bound.
    budget = context.context_budget
    if force_budget is not None:
        budget = min(budget, force_budget)
    working = compact_messages if compact_messages is not None else messages
    if (definition.budget_gated
            and _view_size(working, count, unit, usage_anchor) <= budget):
        return working

    request = ContextStrategyRequest(
        messages=messages,
        working=working,
        context=context,
        budget=budget,
        unit=unit,
        count_reasoning=count,
        usage_anchor=usage_anchor,
        boundary=boundary,
        previous_summary=extract_summary(working),
        trace=trace,
        iteration=iteration,
        prompt=options.get("prompt"),
        summarizer=summarizer,
        usage_sink=usage_sink if usage_sink is not None else options.get("usage_sink"),
    )
    result = await definition.handler(request)

    if (force_budget is not None and result is not working
            and result is not messages
            and _view_size(result, count, unit, usage_anchor)
            >= _view_size(working, count, unit, usage_anchor)):
        # Hard gate with no shrinkage: resending this view would be
        # rejected again, burning a retry. Fall back to the registered
        # ``fallback_strategy`` (deterministic compactor by default) —
        # if even that cannot shrink, the refresh hook reports
        # no-progress by size.
        logger.warning("context.force_budget.no_shrink falling back to %s",
                       fallback_strategy)
        fallback = CONTEXT_STRATEGY_DEFINITIONS.get(fallback_strategy)
        if fallback is None:
            raise ValueError(
                f"unknown fallback context strategy: {fallback_strategy!r}; "
                f"known: {sorted(CONTEXT_STRATEGY_DEFINITIONS)}")
        fallback_request = ContextStrategyRequest(
            messages=messages,
            working=working,
            context=context,
            budget=force_budget,
            unit=unit,
            count_reasoning=count,
            usage_anchor=usage_anchor,
            boundary=boundary,
            previous_summary=extract_summary(working),
            trace=trace,
            iteration=iteration,
            prompt=options.get("prompt"),
            summarizer=summarizer,
            usage_sink=usage_sink if usage_sink is not None else options.get("usage_sink"),
        )
        # The deterministic compactor path is char-metered: convert an
        # explicit token-unit force budget through the anchor ratio.
        # Custom fallback strategies receive the raw force budget and do
        # their own unit handling.
        if fallback_strategy == "compact_observations":
            if unit == "tokens":
                force_chars = max(1, int(force_budget
                                         * _anchor_chars_per_token(usage_anchor)))
            else:
                force_chars = force_budget
            result = compact_observations(
                messages, budget=force_chars,
                window_rounds=context.window_rounds,
                keep_recent_rounds=context.keep_recent_rounds,
                head_chars=context.head_chars,
                exempt_tools=context.retrieval_exempt_tools)
        else:
            result = await fallback.handler(fallback_request)

    if trace is not None and result is not working and result is not messages:
        trace.emit(f"context.{strategy}", iteration,
                   original_chars=total_chars(messages, count),
                   request_chars=total_chars(result, count),
                   original_count=len(messages),
                   request_count=len(result),
                   budget_unit=unit, budget=budget)
        summary = extract_summary(result)
        if summary is not None:
            # The summary text lives on the trace (per-run, queryable)
            # instead of a shared on-disk file.
            trace.emit(f"context.{strategy}.text", iteration, summary=summary)
    return result


# Backwards-compatibility alias (pre-rename import path / function name).
apply_memory_strategy = apply_context_strategy


__all__ = ["apply_context_strategy", "apply_memory_strategy"]
