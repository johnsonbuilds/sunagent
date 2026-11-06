"""Tool execution for one iteration of a turn."""

from __future__ import annotations

import logging
import time
from typing import Any

from agent_runtime.agent.observations import ObservationFormatter
from agent_runtime.agent.tool_dispatch import (
    ToolCallOutcome,
    ValidatedToolCall,
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
        """Run the iteration's calls; a returned string is a guard stop.

        A validated submit call is skipped here and validated after dispatch.
        """
        # Rejections are user messages, so defer them until tool messages land.
        deferred_rejections: list[dict[str, Any]] = []
        submit_tool = self.finisher.rules.submit_tool
        stop: str | None = None
        try:
            for outcome in outcomes:
                if (submit_args is not None and outcome.validated is not None
                        and outcome.validated.name == submit_tool):
                    continue
                stop = await self._dispatch_one(
                    outcome, iteration, history, deferred_rejections)
                if stop is not None:
                    break
        finally:
            # Never drop rejections when a guard stop or TurnFailure unwinds.
            for observation in deferred_rejections:
                history.append(observation)
        return stop

    async def _dispatch_one(
            self, outcome: ToolCallOutcome, iteration: int,
            history: TurnHistory,
            deferred_rejections: list[dict[str, Any]]) -> str | None:
        tool_call = outcome.tool_call
        tool_meta = tool_trace_metadata(tool_call)
        logger.debug("tool.dispatch iteration=%d tool=%r tool_call_id=%r",
                     iteration, tool_meta.get("tool"),
                     tool_meta.get("tool_call_id"))
        payload = tool_call_payload(tool_call)
        self.events.emit("tool.started", iteration,
                         call_id=payload["call_id"], tool=payload["tool"],
                         arguments=payload["arguments"])
        tool_started = time.monotonic()
        if outcome.rejection is not None:
            return self._handle_rejection(
                outcome, iteration, payload, deferred_rejections)
        validated = outcome.validated
        assert validated is not None
        try:
            result = await self._execute_tool(
                validated, iteration, tool_meta, payload, tool_started)
        except Exception as exc:
            return self._handle_execution_error(
                exc, validated, tool_call, payload, iteration, history)
        await self._record_result(result, validated, tool_call, history)
        return None

    async def _execute_tool(self, validated: ValidatedToolCall, iteration: int,
                            tool_meta: dict[str, Any], payload: dict[str, Any],
                            tool_started: float) -> Any:
        with self.trace.span("tool", iteration, **tool_meta) as span_meta:
            result = await self.tools.execute(validated.name,
                                              validated.arguments)
            span_meta.update(tool=validated.name, tool_call_id=validated.id)
        logger.debug("tool.result iteration=%d tool=%r result_chars=%d",
                     iteration, validated.name, len(str(result)))
        self.events.emit("tool.completed", iteration,
                         call_id=payload["call_id"],
                         duration=round(time.monotonic() - tool_started, 3),
                         **tool_result_summary(result))
        return result

    def _handle_rejection(
            self, outcome: ToolCallOutcome, iteration: int,
            payload: dict[str, Any],
            deferred_rejections: list[dict[str, Any]]) -> str | None:
        rejection = outcome.rejection
        assert rejection is not None
        deferred_rejections.append(
            self.guard.rejection_observation(outcome, iteration))
        if payload["tool"] == self.finisher.rules.submit_tool:
            # A malformed finish is not work: do not reset the fuse.
            self.finisher.note_finish_violation(iteration)
        return self.guard.check_tool_error(
            iteration, payload["tool"], rejection)

    def _handle_execution_error(
            self, exc: Exception, validated: ValidatedToolCall,
            tool_call: Any, payload: dict[str, Any], iteration: int,
            history: TurnHistory) -> str | None:
        logger.error("tool.error iteration=%d tool=%r error=%s",
                     iteration, validated.name, exc)
        self.events.emit("tool.failed", iteration,
                         call_id=payload["call_id"],
                         tool=payload["tool"], error=str(exc))
        observation = self.harness.tool_error_observation(
            exc, tool=payload["tool"])
        history.append(tool_observation_message(tool_call, observation))
        self.finisher.note_work_executed()
        return self.guard.check_tool_error(iteration, validated.name, exc)

    async def _record_result(self, result: Any, validated: ValidatedToolCall,
                             tool_call: Any, history: TurnHistory) -> None:
        rendered = await self.observations.render(result, validated.id)
        metadata = ({"spill_path": rendered.spill_path}
                    if rendered.spill_path else None)
        history.append(tool_observation_message(
            tool_call, rendered.text, metadata))
        self.guard.note_success()
        self.finisher.note_work_executed()


__all__ = ["ToolRunner"]
