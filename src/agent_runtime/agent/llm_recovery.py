"""LLM failure taxonomy, recovery planning, and the nudge rewrite.

Every failed LLM request resolves to a :class:`RecoveryPlan`, and
``chat_with_recovery`` is the only place that acts on it:

* ``replay`` / ``refresh`` resend the request inside ``llm_request``, so they
  never consume the iteration budget;
* ``nudge`` rewrites the conversation and returns ``None``, so the caller
  simply reads the next view on its next turn.

The taxonomy, the retry counts and the wording handed back to the model all
stay here; the loop only says where the history is and whether a next turn
exists at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from agent_runtime.agent.runtime_message import runtime_user_message
from agent_runtime.agent.turn_history import TurnHistory
from agent_runtime.runtime_config import RuntimeConfig
from agent_runtime.trace import RunTrace

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# LLM failure taxonomy: one table, one dispatcher
#
# ``replay`` resends the identical request (the harness's
# ``recovery.llm_errors.<category>`` policy owns count and delay; ``max_retries:
# 0`` is the fail-fast path).  ``refresh`` rewrites the request first, since an
# identical replay can only be rejected again.  ``nudge`` rewrites the
# conversation and continues on the next turn, bounded by
# ``control.max_iterations`` rather than by a retry policy.
#
# Adding a failure mode is one line here plus its wording on the exception,
# never a second, hidden handling path in the caller.
REPLAY = "replay"
REFRESH = "refresh"
NUDGE = "nudge"


@dataclass(frozen=True)
class RecoveryPlan:
    """What the recovery layer should do after one failed LLM request."""

    strategy: str
    category: str | None = None
    """``recovery.llm_errors`` key; ``None`` when no policy governs it."""


# Categories an identical resend cannot fix: the payload must change first.
_CATEGORY_STRATEGY = {"context_window": REFRESH}


class ThinkingOnlyTurn(RuntimeError):
    """A response that spent itself thinking and delivered nothing.

    No content and no tool call, so the loop cannot read it as an answer.  The
    remedy is a conversation rewrite — an honest placeholder plus
    :meth:`guidance` — never an identical replay: resending the same request
    reproduces the same turn.
    """

    llm_strategy = NUDGE
    kind = "thinking_only"

    def __init__(self, message: str, reasoning_chars: int) -> None:
        super().__init__(message)
        self.reasoning_chars = reasoning_chars

    def placeholder(self) -> str:
        """Assistant turn recorded in place of the answer that never came."""
        raise NotImplementedError

    def guidance(self) -> str:
        """User-role nudge handed back with the next request."""
        raise NotImplementedError

    def fallback_answer(self) -> str:
        """Answer used when there is no turn left to continue."""
        raise NotImplementedError


def _thinking_cut_clause(reasoning_chars: int) -> str:
    """Describe where a truncated turn was cut, for both wordings above.

    Endpoints that hide their chain of thought stream no reasoning back, so a
    0-char cut is "before anything visible", not "after 0 chars".
    """
    if reasoning_chars > 0:
        return f"after {reasoning_chars} chars of reasoning"
    return "before producing any visible content"


class ReasoningBudgetExceeded(ThinkingOnlyTurn):
    """Our own character budget cut a turn that was still thinking."""

    kind = "reasoning_budget"

    def __init__(self, reasoning_chars: int, budget: int) -> None:
        super().__init__(
            f"reasoning exceeded {budget} chars ({reasoning_chars} produced) "
            f"with no content or tool calls yet", reasoning_chars)
        self.budget = budget

    def placeholder(self) -> str:
        return "(extended reasoning omitted)"

    def guidance(self) -> str:
        return (
            f"Your last turn spent {self.reasoning_chars} characters of "
            f"reasoning and never emitted content or a tool call, so it was "
            f"cut off. Stop extending your reasoning. Produce the smallest "
            f"deliverable that makes progress now: write the minimal "
            f"artifact, or call the tool that creates it. Do not keep "
            f"planning, and do not give a final answer unless the task's "
            f"required result already exists.")

    def fallback_answer(self) -> str:
        return ("(reasoning budget exceeded while summarising; the work "
                "above stands as delivered)")


class ReasoningTruncated(ThinkingOnlyTurn):
    """The provider cut a thinking turn at its own output limit.

    ``finish_reason == "length"`` with no content and no tool call: the model
    did not choose to stop, so the truncated thought is not an answer.  Left
    alone it looks like a legitimate empty final answer and the turn ends with
    nothing but a reasoning block.
    """

    kind = "response_truncated"

    def __init__(self, reasoning_chars: int) -> None:
        super().__init__(
            "response hit the output limit (finish_reason=length) "
            f"{_thinking_cut_clause(reasoning_chars)}, with no content or "
            f"tool calls", reasoning_chars)

    def placeholder(self) -> str:
        return ("(response truncated at the output limit "
                f"{_thinking_cut_clause(self.reasoning_chars)}; nothing "
                f"was delivered)")

    def guidance(self) -> str:
        return (
            "Your previous response was cut off by the model's output "
            f"limit {_thinking_cut_clause(self.reasoning_chars)} and "
            "delivered nothing: that turn was truncated, not finished. "
            "Plan less and write sooner. Emit the next deliverable now — "
            "the minimal artifact, or the tool call that creates it — and "
            "keep the thinking short enough to fit in one response. Do not "
            "give a final answer unless the task's required result already "
            "exists.")

    def fallback_answer(self) -> str:
        return ("(the summarising turn was cut off at the output limit "
                "while still thinking; the work above stands as delivered)")


# Statuses that say "this request can never succeed as written": the provider
# parsed it and refused the payload.  Replaying them is pure waste (one
# incident spent 126s on six replays of a rejected request).
_DETERMINISTIC_REJECTION_STATUSES = frozenset({400, 404, 422})


def _status_code(exc: BaseException) -> int | None:
    """HTTP status on a provider error, duck-typed so the optional SDK never
    enters this module (openai's ``APIStatusError`` exposes it)."""
    code = getattr(exc, "status_code", None)
    if isinstance(code, bool) or not isinstance(code, int):
        return None
    return code


def _retry_4xx_enabled(runtime: RuntimeConfig) -> bool:
    """Opt-out for gateways that report transient failures as 4xx."""
    return runtime.retry_4xx


def llm_error_category(exc: BaseException,
                       runtime: RuntimeConfig) -> str | None:
    """Map an LLM-call failure to its ``recovery.llm_errors`` category.

    Returns ``None`` when the failure has no replay policy at all (a
    thinking-only turn is rewritten instead of replayed).
    """
    category = getattr(exc, "llm_category", None)
    if isinstance(category, str):
        return category
    if isinstance(exc, ThinkingOnlyTurn):
        return None
    if (_status_code(exc) in _DETERMINISTIC_REJECTION_STATUSES
            and not _retry_4xx_enabled(runtime)):
        return "invalid_request"
    return "provider_error"


def recovery_plan(exc: BaseException,
                  runtime: RuntimeConfig) -> RecoveryPlan:
    """Resolve one LLM failure to the single place that acts on it.

    An error that declared its own strategy keeps it; otherwise the category
    decides, and only ``context_window`` needs its payload rewritten.
    """
    strategy = getattr(exc, "llm_strategy", None)
    if isinstance(strategy, str):
        return RecoveryPlan(strategy)
    category = llm_error_category(exc, runtime)
    return RecoveryPlan(_CATEGORY_STRATEGY.get(category, REPLAY), category)


def nudge_thinking_only(history: TurnHistory, exc: ThinkingOnlyTurn, *,
                         iteration: int, trace: RunTrace | None) -> None:
    """Rewrite the conversation after a turn that delivered nothing.

    An honest placeholder stands in for the answer that never came, and the
    guidance says what actually happened and that the next turn must deliver.
    Both wordings come from the exception, next to the diagnosis.
    """
    logger.warning("llm.nudge iteration=%d kind=%s %s",
                   iteration, exc.kind, exc)
    if trace is not None:
        trace.emit("llm.nudge", iteration, kind=exc.kind,
                   reasoning_chars=exc.reasoning_chars)
    history.append({"role": "assistant", "content": exc.placeholder()})
    history.append(runtime_user_message(exc.guidance(), kind=exc.kind))


__all__ = [
    "NUDGE",
    "REFRESH",
    "REPLAY",
    "ReasoningBudgetExceeded",
    "ReasoningTruncated",
    "RecoveryPlan",
    "ThinkingOnlyTurn",
    "llm_error_category",
    "nudge_thinking_only",
    "recovery_plan",
]
