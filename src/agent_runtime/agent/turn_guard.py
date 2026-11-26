"""Turn guard: the policies that can stop or reshape an in-flight turn.

The loop decides *when* to call the model and which tools to run; this
module owns the cross-cutting policy around those decisions:

* the same-tool / same-error fuse, including its stop answer,
* the budget reminder injected at configured iteration fractions, and
* the observation returned to the model when a tool call is rejected
  before execution.

Keeping these here lets the loop stay a coordinator: it asks the guard
whether to stop and otherwise continues with the next request.

The context-window hard gate (shrink after a provider rejects a request)
is NOT here: it only touches the request view, so it belongs to
``TurnHistory.shrink_view``.
"""

from __future__ import annotations

import logging
from typing import Any

from agent_runtime.agent.runtime_message import runtime_user_message
from agent_runtime.agent.tool_dispatch import (
    ToolCallOutcome,
    rejected_observation_content,
    rejected_observation_message,
    tool_call_payload,
    tool_trace_metadata,
    trace_tool_rejection,
)
from agent_runtime.agent.tool_error_budget import (
    SameToolErrorBudget,
    resolve_max_retries,
)
from agent_runtime.agent.turn_history import TurnHistory
from agent_runtime.events import EventEmitter
from agent_runtime.harness import HarnessSpec
from agent_runtime.runtime_config import RuntimeConfig
from agent_runtime.trace import RunTrace

logger = logging.getLogger(__name__)


class TurnGuard:
    """Per-turn policy state; one instance is created per turn.

    The error budget is turn-local (consecutive identical failures), so a
    guard must never be shared across turns.
    """

    def __init__(self, *, harness: HarnessSpec,
                 trace: RunTrace, events: EventEmitter,
                 max_iterations: int,
                 runtime: RuntimeConfig | None = None) -> None:
        self.harness = harness
        self.trace = trace
        self.events = events
        self.max_iterations = max_iterations
        # ``runtime=None`` keeps the historical shape (env resolved once
        # inside ``resolve_max_retries``); the composition root passes a
        # config, and then the environment is never consulted again.
        self.error_budget = SameToolErrorBudget(
            resolve_max_retries(harness.recovery.tool_error_max_retries,
                                runtime=runtime))

    def maybe_remind(self, history: TurnHistory, iteration: int) -> None:
        """Nudge at configured budget fractions (control gene).

        Fires once per threshold: reports the remaining iteration budget,
        explains what an iteration is, and points the model back at the
        required result. Early reminders ask for an exploration cutoff;
        later ones ask for production and verification. The reminder is
        informational, not a restriction.
        """
        reminder = self.harness.control.budget_reminder
        if not reminder.enabled:
            return
        if iteration < 2:
            return
        triggered = any(
            int(f * self.max_iterations) == iteration
            for f in reminder.at_fractions
        )
        if not triggered:
            return
        remaining = self.max_iterations - iteration
        if remaining > self.max_iterations // 2:
            guidance = (
                "You may still be exploring; set a concrete cutoff for "
                "exploration and reserve enough iterations for implementation "
                "and verification."
            )
        else:
            guidance = (
                "You are closer to the limit; stop exploring and spend the "
                "remaining iterations producing and verifying the required "
                "result."
            )
        history.append(runtime_user_message(
            f"Budget {iteration}/{self.max_iterations} "
            f"({remaining} iterations left). One iteration is one model turn; "
            "a turn may contain multiple tool calls. This is informational, "
            f"not a restriction. {guidance} The required result must exist and "
            "be verified before the budget runs out.",
            kind="budget_reminder",
        ))
        self.trace.emit("budget.reminder", iteration, remaining=remaining)

    def check_tool_error(self, iteration: int, tool: Any,
                         error: Exception) -> str | None:
        """Stop the turn when one tool repeats one failure too often.

        Returns the manual-handling message when the budget trips,
        else ``None`` to let the loop continue.
        """
        count, tripped = self.error_budget.note_failure(
            tool if isinstance(tool, str) else None, error)
        if not tripped:
            return None
        detail = str(error).strip().split("\n")[0][:500] or "unknown error"
        answer = (
            f"STOPPED_SAME_TOOL_ERROR: tool {tool!r} failed {count} "
            f"times in a row with the same error: {detail} "
            f"Stopped early to save iterations; "
            f"please handle it manually and resume."
        )
        logger.error("loop.guard iteration=%d tool=%r consecutive=%d error=%s",
                     iteration, tool, count, detail)
        self.trace.emit("loop.guard", iteration, tool=tool,
                        error=str(error)[:500], consecutive=count)
        self.events.emit("agent.completed", iteration,
                         iterations=iteration, answer=answer)
        return answer

    def note_success(self) -> None:
        """An executed tool call produced new information: reset the fuse."""
        self.error_budget.note_success()

    def rejection_observation(self, outcome: ToolCallOutcome,
                              iteration: int) -> dict[str, Any]:
        """Observe a tool call rejected by validation; it never executed.

        Side-effects (logging / trace events) happen exactly once here; the
        returned observation message is the caller's responsibility to append
        via :meth:`TurnHistory.append`.
        """
        tool_call = outcome.tool_call
        rejection = outcome.rejection
        assert rejection is not None
        payload = tool_call_payload(tool_call)
        logger.error("tool.error iteration=%d tool=%r error=%s",
                     iteration, payload["tool"], rejection)
        trace_tool_rejection(self.trace, iteration,
                             tool_trace_metadata(tool_call), rejection)
        self.events.emit("tool.failed", iteration, call_id=payload["call_id"],
                         tool=payload["tool"], error=str(rejection))
        observation = rejected_observation_content(
            tool_call,
            self.harness.tool_error_observation(rejection, tool=payload["tool"]))
        return rejected_observation_message(tool_call, observation)


__all__ = ["TurnGuard"]
