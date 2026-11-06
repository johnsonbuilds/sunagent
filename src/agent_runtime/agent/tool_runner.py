"""Tool execution for one iteration of a turn.

The loop decides *which* tool calls to trust; this module owns what
happens after that decision: executing validated calls in order,
observing rejections, rendering results, and asking the guard whether a
repeated failure should end the turn.

Keeping execution here leaves the loop as a coordinator (request → decide
→ finish) and gives the tool path a single place to change.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from agent_runtime.agent.observations import ObservationFormatter
from agent_runtime.agent.tool_dispatch import (
    ToolCallOutcome,
    tool_call_payload,
    tool_observation_message,
    tool_result_summary,
    tool_trace_metadata,
)
from agent_runtime.agent.turn_finisher import TurnFinisher
from agent_runtime.agent.turn_guard import TurnGuard
from agent_runtime.agent.turn_history import TurnHistory
from agent_runtime.events import EventEmitter
from agent_runtime.harness import HarnessSpec
from agent_runtime.tools import ToolExecutor
from agent_runtime.trace import RunTrace

logger = logging.getLogger(__name__)


class ToolRunner:
    """Execute the validated tool calls of one iteration, in order."""

    def __init__(self, *, tools: ToolExecutor, harness: HarnessSpec,
                 observations: ObservationFormatter, finisher: TurnFinisher,
                 guard: TurnGuard, trace: RunTrace,
                 events: EventEmitter) -> None:
        self.tools = tools
        self.harness = harness
        self.observations = observations
        self.finisher = finisher
        self.guard = guard
        self.trace = trace
        self.events = events

    async def dispatch(self, outcomes: list[ToolCallOutcome],
                       submit_args: dict[str, Any] | None, iteration: int,
                       history: TurnHistory) -> str | None:
        """Execute this iteration's tool calls in order; ``None`` lets it on.

        A validated submit call is skipped here — it is a finish
        attempt, validated against the updated history afterwards. A
        returned string is a guard stop (same-tool error budget) for the
        caller to return as the turn's answer.
        """
        events, trace = self.events, self.trace
        tools, harness = self.tools, self.harness
        submit_tool = self.finisher.rules.submit_tool
        # Rejection observations are user messages. Append them only after
        # every executed call's tool message has landed: an assistant
        # tool_calls block must be followed directly by its role:tool
        # responses, never by a runtime user message in between.
        deferred_rejections: list[dict[str, Any]] = []
        stop: str | None = None
        try:
            for outcome in outcomes:
                if (submit_args is not None and outcome.validated is not None
                        and outcome.validated.name == submit_tool):
                    continue  # finish attempt, validated below
                tool_call = outcome.tool_call
                tool_meta = tool_trace_metadata(tool_call)
                logger.debug("tool.dispatch iteration=%d tool=%r tool_call_id=%r",
                             iteration, tool_meta.get("tool"),
                             tool_meta.get("tool_call_id"))
                payload = tool_call_payload(tool_call)
                events.emit("tool.started", iteration, call_id=payload["call_id"],
                            tool=payload["tool"], arguments=payload["arguments"])
                tool_started = time.monotonic()
                if outcome.rejection is not None:
                    observation = self.guard.rejection_observation(
                        outcome, iteration)
                    deferred_rejections.append(observation)
                    if payload["tool"] == submit_tool:
                        # Malformed finish attempt, not work: no reset.
                        self.finisher.note_finish_violation(iteration)
                    stop = self.guard.check_tool_error(
                        iteration, payload["tool"], outcome.rejection)
                    if stop is not None:
                        break
                    continue
                validated = outcome.validated
                assert validated is not None
                try:
                    with trace.span("tool", iteration, **tool_meta) as span_meta:
                        result = await tools.execute(validated.name,
                                                     validated.arguments)
                        span_meta.update(tool=validated.name,
                                         tool_call_id=validated.id)
                    logger.debug(
                        "tool.result iteration=%d tool=%r result_chars=%d",
                        iteration, validated.name, len(str(result)))
                    events.emit("tool.completed", iteration,
                                call_id=payload["call_id"],
                                duration=round(time.monotonic() - tool_started, 3),
                                **tool_result_summary(result))
                except Exception as exc:
                    logger.error("tool.error iteration=%d tool=%r error=%s",
                                 iteration, validated.name, exc)
                    events.emit("tool.failed", iteration,
                                call_id=payload["call_id"],
                                tool=payload["tool"], error=str(exc))
                    observation = harness.tool_error_observation(
                        exc, tool=payload["tool"])
                    history.append(tool_observation_message(tool_call, observation))
                    self.finisher.note_work_executed()  # executed: new information
                    stop = self.guard.check_tool_error(
                        iteration, validated.name, exc)
                    if stop is not None:
                        break
                    continue
                rendered = await self.observations.render(result, validated.id)
                metadata = ({"spill_path": rendered.spill_path}
                            if rendered.spill_path else None)
                history.append(tool_observation_message(
                    tool_call, rendered.text, metadata))
                self.guard.note_success()
                self.finisher.note_work_executed()  # executed work resets the fuse
        finally:
            # A guard stop or TurnFailure must not drop an already-produced
            # rejection observation; appending it here keeps history usable
            # for a later turn while preserving the pairing contract.
            for observation in deferred_rejections:
                history.append(observation)
        return stop


__all__ = ["ToolRunner"]
