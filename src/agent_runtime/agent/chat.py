"""Reliable LLM communication layer: streaming, recovery retry, and budget guard."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol

from agent_runtime.agent.tool_dispatch import _tool_trace_metadata
from agent_runtime.events import EventEmitter
from agent_runtime.harness import HarnessSpec
from agent_runtime.trace import RunTrace

logger = logging.getLogger(__name__)


def _stream_idle_timeout() -> float | None:
    raw = os.getenv("AGENT_RUNTIME_STREAM_IDLE_TIMEOUT", "120")
    try:
        timeout = float(raw)
    except ValueError:
        return 120.0
    return timeout if timeout > 0 else None


def _reasoning_budget_chars() -> int | None:
    """Max accumulated reasoning chars before a thinking-only turn is cut.

    Guards against reasoning models that spiral into unbounded chain of
    thought without ever emitting content or tool calls (observed on
    ARS-class tasks). 0 or a negative value disables the guard.
    """
    raw = os.getenv("AGENT_RUNTIME_MAX_REASONING_CHARS", "50000")
    try:
        budget = int(raw)
    except ValueError:
        return 50_000
    return budget if budget > 0 else None


class _ReasoningBudgetExceeded(RuntimeError):
    """A thinking-only turn blew the reasoning budget; nudge and retry."""

    def __init__(self, reasoning_chars: int, budget: int) -> None:
        super().__init__(
            f"reasoning exceeded {budget} chars ({reasoning_chars} produced) "
            f"with no content or tool calls yet")
        self.reasoning_chars = reasoning_chars
        self.budget = budget


class _StreamIdleTimeout(RuntimeError):
    """No chunk arrived within the idle timeout."""

    llm_category = "stream_idle_timeout"


class _StreamTruncated(RuntimeError):
    """The server closed the stream without a finish_reason marker."""

    llm_category = "stream_truncated"


class _StreamEmpty(RuntimeError):
    """A clean stream that carried no content, reasoning, or tool calls."""

    llm_category = "stream_empty"


def _llm_error_category(exc: BaseException) -> str | None:
    """Map an LLM-call failure to its ``recovery.llm_errors`` category.

    Returns ``None`` when the failure has dedicated handling elsewhere and
    must never be retried here (the reasoning-budget nudge continues the
    loop by itself); everything unrecognized counts as ``provider_error``.
    """
    category = getattr(exc, "llm_category", None)
    if isinstance(category, str):
        return category
    if isinstance(exc, _ReasoningBudgetExceeded):
        return None
    return "provider_error"


def use_streaming() -> bool:
    """Whether the runtime should consume LLM streaming (AGENT_RUNTIME_STREAM)."""
    value = os.getenv("AGENT_RUNTIME_STREAM", "1").lower()
    return value not in {"0", "false", "no", "off"}


class ChatModel(Protocol):
    async def chat(self, messages: list[dict[str, Any]],
                    tools: list[dict[str, Any]] | None = None) -> dict[str, Any]: ...

    def stream(self, messages: list[dict[str, Any]],
               tools: list[dict[str, Any]] | None = None) -> AsyncIterator[dict[str, Any]]: ...


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


async def _consume_stream(llm: ChatModel, messages: list[dict[str, Any]],
                          tools: list[dict[str, Any]] | None,
                          iteration: int, *,
                          trace: RunTrace | None = None,
                          events: EventEmitter | None = None) -> dict[str, Any]:
    """Assemble one response from provider streaming chunks."""
    content: list[str] = []
    reasoning: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    finish_reason: str | None = None
    reasoning_budget = _reasoning_budget_chars()
    logger.debug("llm.stream.start iteration=%d", iteration)
    stream_iterator = llm.stream(messages, tools).__aiter__()
    while True:
        try:
            timeout = _stream_idle_timeout()
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
            raise _StreamIdleTimeout(
                f"LLM stream produced no chunk for {timeout:g} seconds"
            ) from exc
        if not isinstance(chunk, Mapping):
            raise TypeError("LLM stream chunks must be objects")
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
                raise _ReasoningBudgetExceeded(total_reasoning,
                                               reasoning_budget)
        if text or reasoning_text:
            if events is not None:
                events.emit("assistant.delta", iteration,
                            content=text, reasoning=reasoning_text)
        # NOTE: per-chunk logger line intentionally disabled: a long
        # run can stream tens of thousands of chunks (often blank filler)
        # and each one printed a log line. Full content stays available
        # in trace llm.chunk records and the assembled llm.end message.
        # logger.debug("llm.chunk iteration=%d content=%r reasoning=%r tool_calls=%r",
        #              iteration, text, reasoning_text, chunk.get("tool_calls") or [])
        if trace is not None:
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
    if finish_reason is None:
        # The server cut the stream without a finish marker (observed on
        # free-tier reasoning models after very long thinking). Treat the
        # truncated stream as a failed turn so it can be retried instead
        # of silently becoming an empty final answer.
        raise _StreamTruncated(
            "LLM stream ended without a finish_reason "
            "(stream may have been truncated by the server)")
    if not content and not reasoning and not tool_calls:
        raise _StreamEmpty(
            "LLM stream ended without any content, reasoning, or tool calls")
    return response


async def _chat_response(llm: ChatModel, messages: list[dict[str, Any]],
                         tools: list[dict[str, Any]] | None,
                         iteration: int, *,
                         stream: bool = False,
                         trace: RunTrace | None = None,
                         events: EventEmitter | None = None) -> dict[str, Any]:
    """Consume either runtime streaming or a single runtime response."""
    if events is not None:
        events.emit("assistant.started", iteration)
    if not stream:
        chat = llm.chat
        response = await chat(messages, tools)
        content = response.get("content") or ""
        reasoning = response.get("reasoning_content") or ""
        if content or reasoning:
            if events is not None:
                events.emit("assistant.delta", iteration,
                            content=content, reasoning=reasoning)
    else:
        response = await _consume_stream(llm, messages, tools, iteration,
                                         trace=trace, events=events)
    if events is not None:
        events.emit("assistant.completed", iteration,
                    tool_calls=[_tool_trace_metadata(call).get("tool")
                                for call in response.get("tool_calls") or []])
    return response


async def chat_with_recovery(llm: ChatModel, messages: list[dict[str, Any]],
                             tools: list[dict[str, Any]] | None,
                             iteration: int,
                             harness: HarnessSpec, *,
                             stream: bool = False,
                             trace: RunTrace | None = None,
                             events: EventEmitter | None = None) -> dict[str, Any]:
    """One chat request, retried per the ``recovery.llm_errors`` gene.

    Retries replay the identical request in place; they do not consume
    ``control.max_iterations``. Attempt counts are kept per category so
    one exhausted policy cannot eat another category's budget.
    """
    attempted: dict[str, int] = {}
    while True:
        try:
            return await _chat_response(llm, messages, tools, iteration,
                                        stream=stream, trace=trace, events=events)
        except Exception as exc:
            category = _llm_error_category(exc)
            if category is None:
                raise
            policy = harness.recovery.llm_errors.get(category)
            tried = attempted.get(category, 0)
            if policy is None or tried >= policy.max_retries:
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
    "_ReasoningBudgetExceeded",
    "_StreamEmpty",
    "_StreamIdleTimeout",
    "_StreamTruncated",
    "chat_with_recovery",
    "use_streaming",
]
