"""Turn guard: the policies that can stop or reshape an in-flight turn.

The loop decides *when* to call the model and which tools to run; this
module owns the cross-cutting policy around those decisions:

* the same-tool / same-error fuse, including its stop answer,
* the budget reminder injected at configured iteration fractions,
* the context-window hard-gate hook that shrinks and rebuilds the view
  after the provider rejects a request, and
* the observation returned to the model when a tool call is rejected
  before execution.

Keeping these here lets the loop stay a coordinator: it asks the guard
whether to stop and otherwise continues with the next request.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from agent_runtime.agent.context import request_view_size
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
from agent_runtime.agent.verification import SubmissionRules, has_source_edit
from agent_runtime.events import EventEmitter
from agent_runtime.harness import HarnessSpec
from agent_runtime.trace import RunTrace

logger = logging.getLogger(__name__)


class TurnGuard:
    """Per-turn policy state; one instance is created per turn.

    The error budget is turn-local (consecutive identical failures), so a
    guard must never be shared across turns.
    """

    def __init__(self, *, harness: HarnessSpec, rules: SubmissionRules,
                 trace: RunTrace, events: EventEmitter,
                 max_iterations: int) -> None:
        self.harness = harness
        self.rules = rules
        self.trace = trace
        self.events = events
        self.max_iterations = max_iterations
        self.error_budget = SameToolErrorBudget(
            resolve_max_retries(harness.recovery.tool_error_max_retries))

    def context_window_refresh(self, history: TurnHistory,
                               iteration: int) -> Callable[[], Awaitable[bool]]:
        """Build the hard-gate refresh hook handed to chat recovery.

        On a context-window rejection the hook shrinks the working
        budget to 70% of the rejected request's estimated size, rebuilds
        the view, and copies the new content into the list reference the
        recovery loop already holds (mutating in place, not rebinding).
        Returns False when the strategy cannot shrink further (identity
        or no size reduction — resending would be rejected again), which
        lets the recovery loop re-raise instead of burning retries.
        """
        async def _refresh() -> bool:
            current = request_view_size(history.view, self.harness.context,
                                        history.usage_anchor)
            anchor_before = history.usage_anchor
            target = max(1, int(current * 0.7))
            view_ref = history.view
            try:
                await history.refresh_view(self.harness.context,
                                           trace=self.trace,
                                           iteration=iteration,
                                           force_budget=target)
            except Exception as exc:
                logger.warning(
                    "llm.context_window.refresh_failed iteration=%d %s",
                    iteration, exc)
                return False
            if history.view is view_ref:
                return False
            # Compare with the pre-refresh anchor: refresh clears it
            # (stale mix), and measuring old vs new with different rulers
            # would misjudge shrinkage.
            if request_view_size(history.view, self.harness.context,
                                 anchor_before) >= current:
                logger.warning(
                    "llm.context_window.no_shrink iteration=%d", iteration)
                return False
            view_ref[:] = history.view
            return True
        return _refresh

    def maybe_remind(self, history: TurnHistory, iteration: int) -> None:
        """Nudge at configured budget fractions (control gene).

        Fires once per threshold: reports the remaining iteration budget
        and whether a source edit exists. Iterations are often mistaken
        for tokens, so the message names the unit explicitly.
        Detection reuses verification helpers — no new state tracked.
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
        edited = has_source_edit(history.messages, self.rules)
        remaining = self.max_iterations - iteration
        history.append(runtime_user_message(
            f"Budget {iteration}/{self.max_iterations} "
            f"({remaining} left). Source edit so far: {edited}. "
            "This counts iterations, not tokens: roughly "
            f"{remaining} more tool calls remain.",
            kind="budget_reminder",
        ))
        self.trace.emit("budget.reminder", iteration,
                        remaining=remaining, edited=edited)

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
