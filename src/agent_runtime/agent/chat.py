"""Reliable LLM communication layer: streaming, recovery retry, and budget guard."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, Protocol

from agent_runtime.agent.tool_dispatch import tool_trace_metadata
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


class ReasoningBudgetExceeded(RuntimeError):
    """A thinking-only turn blew the reasoning budget; nudge and retry."""

    def __init__(self, reasoning_chars: int, budget: int) -> None:
        super().__init__(
            f"reasoning exceeded {budget} chars ({reasoning_chars} produced) "
            f"with no content or tool calls yet")
        self.reasoning_chars = reasoning_chars
        self.budget = budget


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

    Returns ``None`` when the failure has dedicated handling elsewhere and
    must never be retried here (the reasoning-budget nudge continues the
    loop by itself).  A deterministic rejection (malformed request,
    unknown endpoint) is ``invalid_request``; everything else is
    ``provider_error``, whose policy owns the transient cases (5xx,
    connection drops, rate limits).
    """
    category = getattr(exc, "llm_category", None)
    if isinstance(category, str):
        return category
    if isinstance(exc, ReasoningBudgetExceeded):
        return None
    if (_status_code(exc) in _DETERMINISTIC_REJECTION_STATUSES
            and not _retry_4xx_enabled(runtime)):
        return "invalid_request"
    return "provider_error"


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
        raise StreamEmpty(
            "LLM stream ended without any content, reasoning, or tool calls")
    return response


async def _chat_response(llm: ChatModel, messages: list[dict[str, Any]],
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
    """
    request = list(messages)
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
    _end(status="success",
         content=response.get("content") or "",
         reasoning_content=response.get("reasoning_content") or "",
         tool_calls=tool_calls,
         tool_count=len(tool_calls),
         tools=[tool_trace_metadata(call).get("tool") for call in tool_calls],
         final=not tool_calls,
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
    return response


async def chat_with_recovery(llm: ChatModel, messages: list[dict[str, Any]],
                             tools: list[dict[str, Any]] | None,
                             iteration: int,
                             harness: HarnessSpec, *,
                             stream: bool = False,
                             trace: RunTrace | None = None,
                             events: EventEmitter | None = None,
                             refresh: Callable[[], Awaitable[bool]] | None = None,
                             runtime: RuntimeConfig | None = None,
                             ) -> dict[str, Any]:
    """One chat request, retried per the ``recovery.llm_errors`` gene.

    Retries replay the identical request in place; they do not consume
    ``control.max_iterations``. Attempt counts are kept per category so
    one exhausted policy cannot eat another category's budget.

    The ``context_window`` category is the exception: replaying the same
    over-limit request can never succeed, so when a ``refresh`` hook is
    supplied the caller compacts the request view *in place* (mutating
    ``messages``' contents, not rebinding it) before the resend; without
    a hook — or when the hook reports nothing further to compact — the
    error propagates.  No backoff sleep on this category: the compaction
    itself is the delay.
    """
    attempted: dict[str, int] = {}
    while True:
        try:
            return await _chat_response(llm, messages, tools, iteration,
                                        stream=stream, trace=trace, events=events,
                                        runtime=runtime,
                                        attempt=sum(attempted.values()) + 1)
        except Exception as exc:
            category = _llm_error_category(exc, runtime)
            if category is None:
                raise
            policy = harness.recovery.llm_errors.get(category)
            tried = attempted.get(category, 0)
            if policy is None or tried >= policy.max_retries:
                raise
            if category == "context_window":
                if refresh is None or not await refresh():
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
    "StreamEmpty",
    "StreamIdleTimeout",
    "StreamTruncated",
    "chat_with_recovery",
    "use_streaming",
]
