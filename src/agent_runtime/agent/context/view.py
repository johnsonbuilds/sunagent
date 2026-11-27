"""Request-view adapter: genome in, provider-ready view out.

Unpacks the harness :class:`ContextGenome` into the strategy functions, which
stay pure and parameter-driven.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from agent_runtime.harness import ContextGenome

from agent_runtime.agent.context.compact import compact_observations
from agent_runtime.agent.context.metrics import ViewBudget
from agent_runtime.agent.context.strategies import CONTEXT_STRATEGY_DEFINITIONS
from agent_runtime.agent.context.summary import extract_summary

logger = logging.getLogger(__name__)


async def apply_context_strategy(messages: list[dict[str, Any]], context: ContextGenome, *,
                                 compact_messages: list[dict[str, Any]] | None = None,
                                 trace: Any = None, iteration: int | None = None,
                                 boundary: int = 0,
                                 budget: ViewBudget | None = None,
                                 force_budget: int | None = None,
                                 summarizer: Any = None,
                                 usage_sink: Any = None,
                                 fallback_strategy: str = "compact_observations",
                                 **options: Any) -> list[dict[str, Any]]:
    """Build the request view of ``messages`` under the memory genome.

    ``messages`` is the FULL conversation (the source of truth, never mutated
    here). ``compact_messages`` is the working view carried across turns: it is
    used *only* to decide whether a re-compaction is due (its size vs the
    budget), while compaction always works on the FULL history — that keeps the
    per-turn request bounded without re-compacting on every iteration.

    When a re-compaction produces a new view, a ``context.{strategy}`` event is
    emitted on ``trace`` so the caller stays free of strategy logic.

    ``fallback_strategy`` names the registry entry used when a ``force_budget``
    hard gate produces no shrinkage (resending the view would be rejected
    again).  It defaults to the deterministic compactor; pass a registered
    custom name to override without touching this adapter.

    ``budget`` is the one valuation of this view (limit, unit, usage anchor and
    whether reasoning rides the wire).  Omitted, it is built from the genome
    with reasoning counted — direct callers that talk to a non-replaying
    endpoint must pass their own ``ViewBudget`` instead.
    """
    from agent_runtime.agent.context.strategies import ContextStrategyRequest

    strategy = context.strategy
    definition = CONTEXT_STRATEGY_DEFINITIONS.get(strategy)
    if definition is None:
        raise ValueError(
            f"unknown context strategy: {strategy!r}; "
            f"known: {sorted(CONTEXT_STRATEGY_DEFINITIONS)}")

    if budget is None:
        budget = ViewBudget.for_context(context)
    # force_budget (context-window hard gate) must push below the size the
    # provider just rejected, so it wins whenever it is the tighter bound.
    # Keep the raw target for the fallback: it may deliberately exceed the
    # genome budget (``shrink_view`` targets 70% of the *measured* rejected
    # size), and the hard gate must honour that measurement.
    gate_budget = budget
    if force_budget is not None:
        gate_budget = budget.with_limit(min(budget.limit, force_budget))
    working = compact_messages if compact_messages is not None else messages
    if definition.budget_gated and gate_budget.size(working) <= gate_budget.limit:
        return working

    # Wall time spent building the view: summarizer calls plus the deterministic
    # compactor.  ``run.metrics`` sums it from this event.
    started = time.monotonic()
    request = ContextStrategyRequest(
        messages=messages,
        working=working,
        context=context,
        budget=gate_budget,
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
            and gate_budget.size(result) >= gate_budget.size(working)):
        # Hard gate with no shrinkage: resending this view would be rejected
        # again, burning a retry.  Fall back to ``fallback_strategy``; if even
        # that cannot shrink, ``TurnHistory.shrink_view`` reports no-progress by
        # size.
        logger.warning("context.force_budget.no_shrink falling back to %s",
                       fallback_strategy)
        fallback = CONTEXT_STRATEGY_DEFINITIONS.get(fallback_strategy)
        if fallback is None:
            raise ValueError(
                f"unknown fallback context strategy: {fallback_strategy!r}; "
                f"known: {sorted(CONTEXT_STRATEGY_DEFINITIONS)}")
        # The fallback works against the raw hard-gate target, not the tighter
        # genome bound: the provider rejected a view bigger than the genome
        # budget, and shrinking only to the genome bound can still leave the
        # request above the measured rejection point.
        fallback_budget = budget.with_limit(force_budget)
        fallback_request = ContextStrategyRequest(
            messages=messages,
            working=working,
            context=context,
            budget=fallback_budget,
            boundary=boundary,
            previous_summary=extract_summary(working),
            trace=trace,
            iteration=iteration,
            prompt=options.get("prompt"),
            summarizer=summarizer,
            usage_sink=usage_sink if usage_sink is not None else options.get("usage_sink"),
        )
        # The deterministic compactor path is char-metered: ``char_limit``
        # converts the token-unit force budget through the anchor ratio.  Custom
        # fallback strategies receive the whole budget and do their own unit
        # handling.
        if fallback_strategy == "compact_observations":
            result = compact_observations(
                messages, budget=fallback_budget.char_limit,
                window_rounds=context.window_rounds,
                keep_recent_rounds=context.keep_recent_rounds,
                head_chars=context.head_chars,
                exempt_tools=context.retrieval_exempt_tools,
                echo_reasoning=budget.echo_reasoning)
        else:
            result = await fallback.handler(fallback_request)

    if trace is not None and result is not working and result is not messages:
        trace.emit(f"context.{strategy}", iteration,
                   duration_ms=round((time.monotonic() - started) * 1000, 3),
                   original_chars=budget.chars(messages),
                   request_chars=budget.chars(result),
                   original_count=len(messages),
                   request_count=len(result),
                   budget_unit=budget.unit, budget=budget.limit)
        summary = extract_summary(result)
        if summary is not None:
            # The summary text lives on the trace (per-run, queryable) instead of
            # a shared on-disk file.
            trace.emit(f"context.{strategy}.text", iteration, summary=summary)
    return result


# Backwards-compatibility alias (pre-rename import path / function name).
apply_memory_strategy = apply_context_strategy


__all__ = ["apply_context_strategy", "apply_memory_strategy"]
