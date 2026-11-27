"""Reliable LLM communication layer: one request, attempted and retried.

This module owns one request end-to-end: how it is sent (streaming or not),
what a failed attempt recovers to, and the turn-level bookkeeping every
attempt owes regardless of outcome (billed usage).  It knows the request view
— ``TurnHistory`` — not the tool loop, so it is the one place an LLM failure
is dispatched.  Its two facets live beside it: ``llm_stream`` (chunk merging,
idle-timeout, empty/truncated-stream detection) and ``llm_recovery`` (failure
taxonomy, :class:`RecoveryPlan`, nudge wording).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol

from agent_runtime.agent.llm_recovery import (
    NUDGE,
    REFRESH,
    RecoveryPlan,
    ReasoningBudgetExceeded,
    ReasoningTruncated,
    ThinkingOnlyTurn,
    nudge_thinking_only,
    recovery_plan,
)
from agent_runtime.agent.llm_stream import (
    StreamEmpty,
    StreamIdleTimeout,
    StreamTruncated,
    consume_stream,
)
from agent_runtime.agent.tokens import response_usage
from agent_runtime.agent.tool_dispatch import tool_trace_metadata
from agent_runtime.agent.turn_history import TurnHistory
from agent_runtime.events import EventEmitter
from agent_runtime.harness import HarnessSpec
from agent_runtime.runtime_config import RuntimeConfig
from agent_runtime.trace import RunTrace

logger = logging.getLogger(__name__)


def _fingerprint(value: Any) -> str:
    """Stable short hash of a request payload, for prefix comparison."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    except (TypeError, ValueError):
        encoded = repr(value)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _request_chars(messages: list[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        for call in message.get("tool_calls") or ():
            function = call.get("function") if isinstance(call, Mapping) else None
            if isinstance(function, Mapping):
                total += len(str(function.get("arguments") or ""))
    return total


def use_streaming(runtime: RuntimeConfig) -> bool:
    """Whether this runtime consumes LLM streaming (``runtime.streaming``)."""
    return runtime.streaming


class ChatModel(Protocol):
    @property
    def echo_reasoning(self) -> bool:
        """Whether this endpoint replays ``reasoning_content`` on the wire.

        Optional for duck-typed fakes: callers must use
        ``runtime_config.resolve_echo_reasoning`` with a runtime fallback.
        """
        ...

    async def chat(self, messages: list[dict[str, Any]],
                    tools: list[dict[str, Any]] | None = None) -> dict[str, Any]: ...

    def stream(self, messages: list[dict[str, Any]],
               tools: list[dict[str, Any]] | None = None) -> AsyncIterator[dict[str, Any]]: ...


async def _chat_response(llm: ChatModel, history: TurnHistory,
                         tools: list[dict[str, Any]] | None,
                         iteration: int, *,
                         stream: bool = False,
                         trace: RunTrace | None = None,
                         events: EventEmitter | None = None,
                         runtime: RuntimeConfig,
                         attempt: int = 1) -> dict[str, Any]:
    """Consume either runtime streaming or a single runtime response.

    This is one real provider attempt, so it owns the ``llm.start`` /
    ``llm.end`` pair: a retried attempt gets its own pair instead of inflating
    one span's duration.  It also owns the turn's usage ledger, because every
    attempt that came back was billed — including one this module then rejects
    as a truncated thinking turn, often the largest single charge of the run.
    """
    request = list(history.view)
    started = time.monotonic()
    if trace is not None:
        trace.emit("llm.start", iteration, attempt=attempt,
                   message_count=len(request),
                   tool_count=len(tools or ()),
                   request_chars=_request_chars(request),
                   request_hash=_fingerprint(request),
                   # The parent chain, not the leaf: a stable hash of the
                   # conversation minus the newest message is what a provider
                   # prompt cache can reuse across iterations.
                   prefix_hash=_fingerprint(request[:-1]),
                   messages=request)
    if events is not None:
        events.emit("assistant.started", iteration)

    def _end(**data: Any) -> None:
        if trace is None:
            return
        duration_ms = round((time.monotonic() - started) * 1000, 3)
        trace.emit("llm.end", iteration, attempt=attempt,
                   duration_ms=duration_ms, **data)

    try:
        if not stream:
            response = await llm.chat(request, tools)
            content = response.get("content") or ""
            reasoning = response.get("reasoning_content") or ""
            if content or reasoning:
                if events is not None:
                    events.emit("assistant.delta", iteration,
                                content=content, reasoning=reasoning)
            ttft_ms = None
        else:
            response = await consume_stream(llm, request, tools, iteration,
                                             trace=trace, events=events,
                                             runtime=runtime)
            ttft_ms = response.pop("ttft_ms", None)
    except asyncio.CancelledError:
        # The cancellation must not erase the request from the trace: a
        # cancelled run is exactly when its last attempt matters.
        _end(status="cancelled", tool_count=0, final=False,
             content_chars=0, reasoning_chars=0, tool_calls=[])
        raise
    except Exception as exc:
        if trace is not None:
            trace.emit("llm.error", iteration, attempt=attempt,
                       error=str(exc)[:500],
                       error_type=type(exc).__name__)
        _end(status="error", error=str(exc)[:500],
             error_type=type(exc).__name__, tool_count=0, final=False,
             content_chars=0, reasoning_chars=0, tool_calls=[])
        raise

    usage = response.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    # One parser for every billed response (``tokens.py``): it tolerates a
    # non-numeric field and reads the cached count from any shape a direct
    # endpoint returns, so the ledger never silently loses cache hits.
    prompt_tokens, completion_tokens, cached_tokens = response_usage(response)
    tool_calls = response.get("tool_calls") or []
    # Bill before deciding whether to accept: this is the one place that sees
    # every response, accepted or not.
    history.record_usage(prompt_tokens, completion_tokens, cached_tokens)
    # A provider that cuts the response at ``max_tokens`` while the model is
    # still thinking yields neither content nor a tool call, which the loop
    # would read as a legitimate final answer.  ``reasoning_content`` may be
    # empty too, so the cut itself is the signal, not the reasoning length.
    truncated_thinking = (response.get("finish_reason") == "length"
                          and not (response.get("content") or "").strip()
                          and not tool_calls)
    _end(status="success",
         content=response.get("content") or "",
         reasoning_content=response.get("reasoning_content") or "",
         tool_calls=tool_calls,
         tool_count=len(tool_calls),
         tools=[tool_trace_metadata(call).get("tool") for call in tool_calls],
         final=not tool_calls and not truncated_thinking,
         finish_reason=response.get("finish_reason"),
         usage=dict(usage),
         prompt_tokens=prompt_tokens,
         completion_tokens=completion_tokens,
         cached_tokens=cached_tokens,
         total_tokens=prompt_tokens + completion_tokens,
         content_chars=len(response.get("content") or ""),
         reasoning_chars=len(response.get("reasoning_content") or ""),
         ttft_ms=ttft_ms)
    if events is not None:
        events.emit("assistant.completed", iteration,
                    tool_calls=[tool_trace_metadata(call).get("tool")
                                for call in tool_calls])
    if truncated_thinking:
        # Already traced and billed above; raise so the dispatcher nudges the
        # model to deliver instead of the caller accepting an empty answer.
        raise ReasoningTruncated(
            len(response.get("reasoning_content") or ""))
    return response


async def chat_with_recovery(llm: ChatModel, history: TurnHistory,
                             tools: list[dict[str, Any]] | None,
                             iteration: int,
                             harness: HarnessSpec, *,
                             stream: bool = False,
                             trace: RunTrace | None = None,
                             events: EventEmitter | None = None,
                             allow_nudge: bool = True,
                             runtime: RuntimeConfig,
                             ) -> dict[str, Any] | None:
    """One chat request, recovered per the plan :func:`recovery_plan` resolves.

    This is the only dispatcher of LLM failures; the three strategies are
    described once, in the taxonomy table of ``llm_recovery``.

    It takes ``history`` rather than a message list because two strategies
    rewrite it: ``refresh`` shrinks the view and ``nudge`` appends the
    placeholder and guidance.  Reading ``history.view`` fresh on every attempt
    is what lets a second retry see the first retry's compaction.

    ``replay`` does not consume ``control.max_iterations``, and attempt counts
    are kept per category so one exhausted policy cannot eat another's budget.
    ``refresh`` shrinks before the resend (an identical replay can only be
    rejected again) and propagates when the view cannot shrink further; no
    backoff sleep, the compaction itself is the delay.  ``nudge`` returns
    ``None`` so the caller continues with its next turn; ``allow_nudge=False``
    (the forced summary turn, which has no next turn) re-raises instead.
    """
    attempted: dict[str, int] = {}
    while True:
        try:
            return await _chat_response(llm, history, tools, iteration,
                                        stream=stream, trace=trace,
                                        events=events, runtime=runtime,
                                        attempt=sum(attempted.values()) + 1)
        except Exception as exc:
            plan = recovery_plan(exc, runtime)
            if plan.strategy == NUDGE:
                if not allow_nudge or not isinstance(exc, ThinkingOnlyTurn):
                    raise
                nudge_thinking_only(history, exc, iteration=iteration,
                                     trace=trace)
                return None
            category = plan.category
            if category is None:
                # No policy key to look a retry count up in: not recoverable.
                raise
            policy = harness.recovery.llm_errors.get(category)
            tried = attempted.get(category, 0)
            if policy is None or tried >= policy.max_retries:
                raise
            if plan.strategy == REFRESH and not await history.shrink_view(
                    harness.context, trace=trace, iteration=iteration):
                raise
            delay = policy.delay(tried + 1)
            logger.warning(
                "llm.retry iteration=%d category=%s attempt=%d/%d "
                "delay=%.3fs error=%s", iteration, category,
                tried + 1, policy.max_retries, delay, exc)
            if trace is not None:
                trace.emit("llm.retry", iteration, category=category,
                           attempt=tried + 1,
                           max_retries=policy.max_retries,
                           delay=round(delay, 3), error=str(exc))
            attempted[category] = tried + 1
            if delay > 0:
                await asyncio.sleep(delay)


# The public surface of the LLM layer stays here — including the taxonomy
# types and stream errors re-exported from the two facet modules — so the
# loop's and the entry points' dependency surface never changes.
__all__ = [
    "ChatModel",
    "ReasoningBudgetExceeded",
    "ReasoningTruncated",
    "RecoveryPlan",
    "StreamEmpty",
    "StreamIdleTimeout",
    "StreamTruncated",
    "ThinkingOnlyTurn",
    "chat_with_recovery",
    "use_streaming",
]
