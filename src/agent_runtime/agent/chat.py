"""Reliable LLM communication layer: streaming, recovery retry, and budget guard.

This module owns one request end-to-end: how it is sent (streaming or
not), what a failed attempt recovers to, and the turn-level bookkeeping
that every attempt owes regardless of outcome (billed usage).  It knows
the request view — ``TurnHistory`` — not the tool loop, so all recovery
that stays inside a request lives here, and nothing that needs a *next*
turn does.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from agent_runtime.agent.runtime_message import runtime_user_message
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


def _stream_idle_timeout(runtime: RuntimeConfig | None = None) -> float | None:
    """Idle timeout, explicit config wins; env is the legacy fallback."""
    if runtime is not None:
        return runtime.stream_idle_timeout
    raw = os.getenv("AGENT_RUNTIME_STREAM_IDLE_TIMEOUT", "120")
    try:
        timeout = float(raw)
    except ValueError:
        return 120.0
    return timeout if timeout > 0 else None


def _reasoning_budget_chars(runtime: RuntimeConfig | None = None) -> int | None:
    """Max accumulated reasoning chars before a thinking-only turn is cut.

    Guards against reasoning models that spiral into unbounded chain of
    thought without ever emitting content or tool calls (observed on
    ARS-class tasks). 0 or a negative value disables the guard.
    Explicit config wins; env is the legacy fallback.
    """
    if runtime is not None:
        return runtime.max_reasoning_chars
    raw = os.getenv("AGENT_RUNTIME_MAX_REASONING_CHARS", "50000")
    try:
        budget = int(raw)
    except ValueError:
        return 50_000
    return budget if budget > 0 else None


# ---------------------------------------------------------------------
# LLM failure taxonomy: one table, one dispatcher
#
# Every failed LLM request resolves to a :class:`RecoveryPlan`, and
# :func:`chat_with_recovery` is the only place that acts on it.  Three
# strategies cover every case, including "do not retry":
#
#   ``replay``   resend the identical request.  The harness's
#                ``recovery.llm_errors.<category>`` policy owns the count
#                and the delay; ``max_retries: 0`` means "fail the turn
#                now" (the fail-fast path of ``invalid_request``).
#   ``refresh``  rewrite the request before resending (shrink the
#                view); an identical replay can only be rejected again.
#                The same policy owns the count.
#   ``nudge``    rewrite the *conversation* and continue on the next
#                turn.  No policy applies: this is not a replay of the
#                same request, and ``control.max_iterations`` is what
#                bounds it.
#
# Both rewrites are performed by the dispatcher below, through the
# ``TurnHistory`` it is handed: one object carries the request view, the
# usage ledger and the append point, so no caller passes callbacks in and
# no reference to a stale list is held across a retry.
#
# Adding a failure mode is therefore one line here plus its wording on
# the exception, never a second, hidden handling path in the caller.
REPLAY = "replay"
REFRESH = "refresh"
NUDGE = "nudge"


@dataclass(frozen=True)
class RecoveryPlan:
    """What the recovery layer should do after one failed LLM request."""

    strategy: str
    category: str | None = None
    """``recovery.llm_errors`` key; ``None`` when no policy governs it."""


# Categories an identical resend cannot fix: the payload itself has to
# change first.  Everything else is a plain replay whose retry count the
# harness owns.
_CATEGORY_STRATEGY = {"context_window": REFRESH}


class ThinkingOnlyTurn(RuntimeError):
    """A response that spent itself thinking and delivered nothing.

    No content and no tool call, so the loop cannot read it as an answer.
    The remedy is a conversation rewrite — an honest placeholder for the
    missing turn plus :meth:`guidance` telling the model what went wrong
    and that the next turn must deliver — never an identical replay:
    resending the same request reproduces the same turn.

    Subclasses supply the wording; the recovery layer only needs
    :attr:`llm_strategy` and :attr:`reasoning_chars`.
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

    Endpoints that hide their chain of thought stream no reasoning back,
    so the honest description of a 0-char cut is "before anything
    visible", not "after 0 chars of reasoning".
    """
    if reasoning_chars > 0:
        return f"after {reasoning_chars} chars of reasoning"
    return "before producing any visible content",


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

    ``finish_reason == "length"`` with no content and no tool call: the
    model did not choose to stop, the maximum token count did it for
    them, so the truncated thought is not an answer.  Left alone it looks
    like a legitimate empty final answer and the turn ends with nothing
    but a reasoning block.

    ``reasoning_chars`` may be 0 when the endpoint hides its reasoning
    (nothing was streamed back), so the wording never claims a reasoning
    length it cannot see: it says the response was cut before anything
    visible came out.
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


class StreamIdleTimeout(RuntimeError):
    """No chunk arrived within the idle timeout."""

    llm_category = "stream_idle_timeout"


class StreamTruncated(RuntimeError):
    """The server closed the stream without a finish_reason marker."""

    llm_category = "stream_truncated"


class StreamEmpty(RuntimeError):
    """A clean stream that carried no content, reasoning, or tool calls."""

    llm_category = "stream_empty"


# Statuses that say "this request can never succeed as written": the
# provider parsed it and refused the payload itself. Replaying them is
# pure waste (one incident spent 126s on six replays of a rejected
# request before the 400 reached the user).
_DETERMINISTIC_REJECTION_STATUSES = frozenset({400, 404, 422})


def _status_code(exc: BaseException) -> int | None:
    """HTTP status on a provider error, duck-typed so the optional SDK
    never enters this module (openai's ``APIStatusError`` exposes it)."""
    code = getattr(exc, "status_code", None)
    if isinstance(code, bool) or not isinstance(code, int):
        return None
    return code


def _retry_4xx_enabled(runtime: RuntimeConfig | None = None) -> bool:
    """Opt-out for gateways that report transient failures as 4xx.

    Explicit config wins; env is the legacy fallback (same pattern as
    the stream/reasoning knobs above).
    """
    if runtime is not None:
        return runtime.retry_4xx
    raw = os.getenv("AGENT_RUNTIME_RETRY_4XX", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _llm_error_category(exc: BaseException,
                        runtime: RuntimeConfig | None = None) -> str | None:
    """Map an LLM-call failure to its ``recovery.llm_errors`` category.

    Returns ``None`` when the failure has no replay policy at all (a
    thinking-only turn is rewritten here instead of replayed).  A
    deterministic rejection (malformed request, unknown endpoint) is
    ``invalid_request``; everything else is ``provider_error``, whose
    policy owns the transient cases (5xx, connection drops, rate limits).
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


def _recovery_plan(exc: BaseException,
                   runtime: RuntimeConfig | None = None) -> RecoveryPlan:
    """Resolve one LLM failure to the single place that acts on it.

    Precedence is: an error that declared its own strategy (a
    thinking-only turn) keeps it; otherwise the category decides, and
    only ``context_window`` needs its payload rewritten before the
    resend.  Everything else is a plain replay whose retry count and
    delay come from the harness — including the fail-fast case, which is
    simply a policy with ``max_retries: 0``.
    """
    strategy = getattr(exc, "llm_strategy", None)
    if isinstance(strategy, str):
        return RecoveryPlan(strategy)
    category = _llm_error_category(exc, runtime)
    return RecoveryPlan(_CATEGORY_STRATEGY.get(category, REPLAY), category)


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


_STREAM_CALL_KEYS = frozenset({"index", "id", "function"})


def _merge_stream_tool_call(calls: list[dict[str, Any]], delta: Mapping[str, Any]) -> None:
    index = delta.get("index", len(calls))
    if not isinstance(index, int):
        index = len(calls)
    while len(calls) <= index:
        calls.append({"id": "", "type": "function",
                      "function": {"name": "", "arguments": ""}})
    target = calls[index]
    if isinstance(delta.get("id"), str):
        target["id"] = delta["id"]
    function = delta.get("function")
    if isinstance(function, Mapping):
        target_function = target["function"]
        if isinstance(function.get("name"), str):
            target_function["name"] += function["name"]
        if isinstance(function.get("arguments"), str):
            target_function["arguments"] += function["arguments"]
    # Provider-specific fields stay opaque: carried verbatim onto the
    # assembled call so history can echo them back unchanged (Gemini
    # rejects the next request when a functionCall's thought_signature is
    # missing). ``index`` is a stream-position artifact, not part of the
    # call, so it is never kept.
    for key, value in delta.items():
        if key not in _STREAM_CALL_KEYS:
            target[key] = value


async def _consume_stream(llm: ChatModel, messages: list[dict[str, Any]],
                          tools: list[dict[str, Any]] | None,
                          iteration: int, *,
                          trace: RunTrace | None = None,
                          events: EventEmitter | None = None,
                          runtime: RuntimeConfig | None = None) -> dict[str, Any]:
    """Assemble one response from provider streaming chunks."""
    content: list[str] = []
    reasoning: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    ttft_ms: float | None = None
    stream_started = time.monotonic()
    reasoning_budget = _reasoning_budget_chars(runtime)
    logger.debug("llm.stream.start iteration=%d", iteration)
    stream_iterator = llm.stream(messages, tools).__aiter__()
    while True:
        try:
            timeout = _stream_idle_timeout(runtime)
            if timeout is None:
                chunk = await anext(stream_iterator)
            else:
                async with asyncio.timeout(timeout):
                    chunk = await anext(stream_iterator)
        except StopAsyncIteration:
            break
        except TimeoutError as exc:
            logger.error("llm.stream.timeout iteration=%d timeout_seconds=%s",
                         iteration, timeout)
            raise StreamIdleTimeout(
                f"LLM stream produced no chunk for {timeout:g} seconds"
            ) from exc
        if not isinstance(chunk, Mapping):
            raise TypeError("LLM stream chunks must be objects")
        # A trailing usage chunk carries no payload but may or may not
        # carry finish_reason (provider-dependent): harvest usage first so
        # the token-budget anchor is fed on streaming turns too.
        chunk_usage = chunk.get("usage")
        if isinstance(chunk_usage, Mapping) and chunk_usage:
            usage = {key: value for key, value in chunk_usage.items()
                     if isinstance(value, int) and not isinstance(value, bool)}
        reason = chunk.get("finish_reason")
        if isinstance(reason, str) and reason:
            finish_reason = reason
            continue
        text = chunk.get("content") or ""
        if text:
            content.append(str(text))
        reasoning_text = chunk.get("reasoning_content") or ""
        if reasoning_text:
            reasoning.append(str(reasoning_text))
        for delta in chunk.get("tool_calls") or []:
            if not isinstance(delta, Mapping):
                raise TypeError("LLM tool call chunks must be objects")
            _merge_stream_tool_call(tool_calls, delta)
        if reasoning_budget is not None and reasoning and not content \
                and not tool_calls:
            total_reasoning = sum(len(part) for part in reasoning)
            if total_reasoning > reasoning_budget:
                logger.error(
                    "llm.reasoning_budget iteration=%d budget=%d "
                    "reasoning_chars=%d", iteration, reasoning_budget,
                    total_reasoning)
                if trace is not None:
                    trace.emit("llm.reasoning_budget", iteration,
                               budget=reasoning_budget,
                               reasoning_chars=total_reasoning)
                raise ReasoningBudgetExceeded(total_reasoning,
                                               reasoning_budget)
        if text or reasoning_text:
            if events is not None:
                events.emit("assistant.delta", iteration,
                            content=text, reasoning=reasoning_text)
        if ttft_ms is None and (text or reasoning_text or chunk.get("tool_calls")):
            ttft_ms = round((time.monotonic() - stream_started) * 1000, 3)
        # NOTE: per-chunk logging intentionally disabled: a stuck run can
        # stream tens of thousands of whitespace-only chunks and each one
        # printed a log line. Full content stays available in trace
        # llm.chunk records and the assembled llm.end message.
        # logger.debug("llm.chunk iteration=%d content=%r reasoning=%r tool_calls=%r",
        #              iteration, text, reasoning_text, chunk.get("tool_calls") or [])
        if trace is not None and trace.trace_chunks:
            trace.emit("llm.chunk", iteration,
                       content=text, tool_call_count=len(chunk.get("tool_calls") or []),
                       reasoning_chars=len(reasoning_text))
    logger.debug("llm.stream.end iteration=%d finish_reason=%r", iteration,
                 finish_reason)
    if trace is not None:
        trace.emit("llm.stream.finish", iteration, finish_reason=finish_reason)
    response: dict[str, Any] = {"content": "".join(content), "tool_calls": tool_calls}
    if reasoning:
        response["reasoning_content"] = "".join(reasoning)
    if usage is not None:
        response["usage"] = usage
    if finish_reason is not None:
        response["finish_reason"] = finish_reason
    if ttft_ms is not None:
        response["ttft_ms"] = ttft_ms
    if finish_reason is None:
        # The server cut the stream without a finish marker (observed on
        # free-tier reasoning models after very long thinking). Treat the
        # truncated stream as a failed turn so it can be retried instead
        # of silently becoming an empty final answer.
        raise StreamTruncated(
            "LLM stream ended without a finish_reason "
            "(stream may have been truncated by the server)")
    if not content and not reasoning and not tool_calls:
        if finish_reason == "length":
            # Nothing visible was produced *because the output limit was
            # hit*, most likely while the model was still thinking on an
            # endpoint that does not stream reasoning back.  That is not
            # an empty stream (a replay would only hit the same wall):
            # hand the response on so the caller reads it as a truncated
            # thinking turn, exactly like the non-streaming path does.
            return response
        raise StreamEmpty(
            "LLM stream ended without any content, reasoning, or tool calls")
    return response


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
            response = await _consume_stream(llm, request, tools, iteration,
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
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    cached_tokens = int(usage.get("cached_tokens") or 0)
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


def _nudge_thinking_only(history: TurnHistory, exc: ThinkingOnlyTurn, *,
                         iteration: int, trace: RunTrace | None) -> None:
    """Rewrite the conversation after a turn that delivered nothing.

    An honest placeholder stands in for the answer that never came, and
    the guidance tells the model what actually happened — our budget cut
    it, or the provider's output limit did — and that the next turn must
    deliver.  Both wordings come from the exception, next to the
    diagnosis, so a new cause cannot inherit another one's story.
    """
    logger.warning("llm.nudge iteration=%d kind=%s %s",
                   iteration, exc.kind, exc)
    if trace is not None:
        trace.emit("llm.nudge", iteration, kind=exc.kind,
                   reasoning_chars=exc.reasoning_chars)
    history.append({"role": "assistant", "content": exc.placeholder()})
    history.append(runtime_user_message(exc.guidance(), kind=exc.kind))


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
    """One chat request, recovered per the plan :func:`_recovery_plan`
    resolves for the failure.

    This is the only dispatcher of LLM failures; the three strategies are
    described once, next to the taxonomy above.

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
            plan = _recovery_plan(exc, runtime)
            if plan.strategy == NUDGE:
                if not allow_nudge or not isinstance(exc, ThinkingOnlyTurn):
                    raise
                _nudge_thinking_only(history, exc, iteration=iteration,
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
