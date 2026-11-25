"""Reliable LLM communication layer: one request, attempted and retried.

This module owns one request end-to-end: how it is sent (streaming or
not), what a failed attempt recovers to, and the turn-level bookkeeping
that every attempt owes regardless of outcome (billed usage).  It knows
the request view — ``TurnHistory`` — not the tool loop, so it is the one
place an LLM failure is dispatched.  The two facets it composes live
beside it: ``llm_stream`` (chunk merging, idle-timeout and
empty/truncated-stream detection) and ``llm_recovery`` (failure taxonomy,
:class:`RecoveryPlan`, nudge wording).

``chat_with_recovery`` is the dispatcher for the three recovery
strategies (see ``llm_recovery``): ``replay`` / ``refresh`` resend the
request inside this call, so they never consume the iteration budget;
``nudge`` rewrites the conversation and returns ``None`` so the caller
simply reads the next view on its next turn.  The loop only says where
the history is and whether a next turn exists at all.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
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


def use_streaming(runtime: RuntimeConfig | None = None) -> bool:
    """Whether the runtime should consume LLM streaming (AGENT_RUNTIME_STREAM)."""
    if runtime is not None:
        return runtime.streaming
    value = os.getenv("AGENT_RUNTIME_STREAM", "1").lower()
    return value not in {"0", "false", "no", "off"}


class ChatModel(Protocol):
    @property
    def echo_reasoning(self) -> bool:
        """Whether this endpoint replays ``reasoning_content`` on the wire.

        Optional for Duck-typed fakes: callers must use
        ``runtime_config.resolve_echo_reasoning`` with a runtime fallback
        instead of accessing it directly.
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
                         runtime: RuntimeConfig | None = None,
                         attempt: int = 1) -> dict[str, Any]:
    """Consume either runtime streaming or a single runtime response.

    This is one real provider attempt, so it owns the ``llm.start`` /
    ``llm.end`` pair: the request payload is written once on start, the
    assembled response plus usage/timing once on end.  A retried attempt
    (same iteration, ``attempt > 1``) therefore gets its own pair instead
    of inflating one span's duration.

    It also owns the turn's usage ledger: every attempt that came back —
    including one this module then rejects as a truncated thinking turn —
    was billed, and a rejected turn's tokens are often the largest single
    charge of the run.  Recording here means no recovery path can drop
    it, whichever strategy handles the failure.
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
                   # conversation minus the newest message is what a
                   # provider prompt cache can reuse across iterations.
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
    # provider that reports a non-numeric field and reads the cached count
    # from any of the shapes a direct endpoint returns
    # (``prompt_tokens_details.cached_tokens``, ``prompt_cache_hit_tokens``),
    # so the ledger does not silently lose cache hits or crash a turn on a
    # weird usage payload.
    prompt_tokens, completion_tokens, cached_tokens = response_usage(response)
    tool_calls = response.get("tool_calls") or []
    # Bill the attempt before deciding whether to accept it: this is the
    # one place that sees every response, accepted or not.
    history.record_usage(prompt_tokens, completion_tokens, cached_tokens)
    # A provider that cuts the response at ``max_tokens`` while the model
    # is still thinking yields neither content nor a tool call.  Left
    # alone, the loop reads that as a legitimate final answer and the turn
    # ends with an empty answer (the UI keeps only the thinking block).
    # ``reasoning_content`` may be empty too (endpoints that hide it), so
    # the cut itself is the signal, not the reasoning length.
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
        # Trace is fully recorded above (reasoning chars, finish_reason,
        # final=False) and the attempt is already billed; raise so the
        # dispatcher nudges the model to deliver instead of the caller
        # accepting the empty response as the answer.
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
                             runtime: RuntimeConfig | None = None,
                             ) -> dict[str, Any] | None:
    """One chat request, recovered per the plan :func:`recovery_plan`
    resolves for the failure.

    This is the only dispatcher of LLM failures; the three strategies are
    described once, in the taxonomy table of ``llm_recovery``.

    It takes the ``history`` rather than a message list because two of the
    three strategies rewrite it: ``refresh`` shrinks the view
    (``TurnHistory.shrink_view``) and ``nudge`` appends the placeholder
    and guidance.  Reading ``history.view`` fresh on every attempt is what
    makes a second retry see the first retry's compaction — a rebinding
    compaction can never be applied to a list reference held from before
    it ran.

    ``replay`` retries replay the identical request in place and do not
    consume ``control.max_iterations``. Attempt counts are kept per
    category so one exhausted policy cannot eat another category's
    budget.

    ``refresh`` (``context_window``) can never succeed as a replay, so it
    shrinks before the resend; when the view cannot shrink further the
    error propagates instead of burning retries.  No backoff sleep on
    this strategy: the compaction itself is the delay.

    ``nudge`` (a thinking-only turn) rewrites the conversation and
    returns ``None`` so the caller continues with its next turn instead
    of reading the empty response as an answer.  ``allow_nudge=False``
    (the forced summary turn, which has no next turn and no tools)
    re-raises instead, leaving the caller's own fallback to answer.
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
                # A strategy with no policy key: there is nothing to look
                # a retry count up in, so it is not recoverable here.
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
# types (``llm_recovery``) and stream errors (``llm_stream``) re-exported
# from the two facet modules — so the loop's and the entry points'
# dependency surface never changes.
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
