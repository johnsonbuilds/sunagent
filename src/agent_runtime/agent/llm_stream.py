"""Streaming chunk assembly: merge provider deltas, detect dead streams.

Pure stream mechanics: how the chunks a provider streams are merged into
one response (including provider-specific tool-call fields that must be
echoed back verbatim), and the ways a stream delivers nothing — an idle
gap past the timeout, a close without a finish marker, a clean but empty
stream, or a thinking turn still going when our reasoning-character
budget cuts it.  A response the *provider* cut at its own output limit is
not diagnosed here: it is a complete response, handed back for
``llm_request`` to classify.  Failures raise the stream-side taxonomy
errors (``llm_category`` on each) that ``llm_recovery`` classifies; this
module never retries.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from agent_runtime.agent.llm_recovery import ReasoningBudgetExceeded
from agent_runtime.events import EventEmitter
from agent_runtime.runtime_config import RuntimeConfig
from agent_runtime.trace import RunTrace

if TYPE_CHECKING:
    # Type-only edge back to the layer that calls this one; there is no
    # runtime import cycle.
    from agent_runtime.agent.llm_request import ChatModel

logger = logging.getLogger(__name__)


def _stream_idle_timeout(runtime: RuntimeConfig | None = None) -> float | None:
    """Idle timeout; ``None`` resolves the environment via ``RuntimeConfig``."""
    return (runtime or RuntimeConfig.from_env()).stream_idle_timeout


def _reasoning_budget_chars(runtime: RuntimeConfig | None = None) -> int | None:
    """Max accumulated reasoning chars before a thinking-only turn is cut.

    Guards against reasoning models that spiral into unbounded chain of
    thought without ever emitting content or tool calls (observed on
    ARS-class tasks). 0 or a negative value disables the guard.
    """
    return (runtime or RuntimeConfig.from_env()).max_reasoning_chars


class StreamIdleTimeout(RuntimeError):
    """No chunk arrived within the idle timeout."""

    llm_category = "stream_idle_timeout"


class StreamTruncated(RuntimeError):
    """The server closed the stream without a finish_reason marker."""

    llm_category = "stream_truncated"


class StreamEmpty(RuntimeError):
    """A clean stream that carried no content, reasoning, or tool calls."""

    llm_category = "stream_empty"


_STREAM_CALL_KEYS = frozenset({"index", "id", "function"})


def merge_stream_tool_call(calls: list[dict[str, Any]], delta: Mapping[str, Any]) -> None:
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


async def consume_stream(llm: ChatModel, messages: list[dict[str, Any]],
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
    timeout = _stream_idle_timeout(runtime)
    logger.debug("llm.stream.start iteration=%d", iteration)
    stream_iterator = llm.stream(messages, tools).__aiter__()
    while True:
        try:
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
            # Keep nested detail objects (``prompt_tokens_details``) as well
            # as the flat ints: ``response_usage`` reads the prompt-cache
            # count out of either shape, so dropping the nested one here
            # would make a streamed turn report fewer cached tokens than
            # the same turn sent without streaming.
            usage = {key: value for key, value in chunk_usage.items()
                     if isinstance(value, (int, Mapping))
                     and not isinstance(value, bool)}
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
            merge_stream_tool_call(tool_calls, delta)
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


__all__ = [
    "StreamEmpty",
    "StreamIdleTimeout",
    "StreamTruncated",
    "consume_stream",
    "merge_stream_tool_call",
]
