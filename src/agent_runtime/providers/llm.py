"""OpenAI-compatible model adapter and runtime-neutral chat responses."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agent_runtime.runtime_config import RuntimeConfig


logger = logging.getLogger(__name__)


class ContextWindowExceededError(RuntimeError):
    """The provider rejected the request for exceeding the context window.

    Carries ``llm_category = "context_window"`` so the recovery layer routes it
    to the ``recovery.llm_errors.context_window`` policy.  An in-place retry can
    never succeed: the caller must compact the view before resending.
    """

    llm_category = "context_window"


# Substring markers for context-window rejections: providers word the same 400
# differently, so the match is a lowercase substring scan over the exception
# text.  The generic markers only count alongside an overflow companion word,
# so an unrelated "invalid context window parameter" stays on the
# provider_error retry policy instead of burning the hard gate's budget.
_CONTEXT_OVERFLOW_SPECIFIC: tuple[str, ...] = (
    "context_length_exceeded",
    "maximum context length",
    "reduce the length",
    "input is too long",
    "prompt is too long",
    "exceeds the maximum number of tokens",
    "context length of",
)
_CONTEXT_OVERFLOW_GENERIC: tuple[str, ...] = (
    "context window",
    "too many tokens",
)
_CONTEXT_OVERFLOW_COMPANIONS: tuple[str, ...] = (
    "exceed",
    "too long",
    "too large",
    "maximum",
    "limit",
    "reduce",
    "length",
)


def _looks_like_context_overflow(exc: BaseException) -> bool:
    """Whether a provider error text matches a context-window rejection."""
    haystack = str(exc).lower()
    if any(marker in haystack for marker in _CONTEXT_OVERFLOW_SPECIFIC):
        return True
    if any(marker in haystack for marker in _CONTEXT_OVERFLOW_GENERIC):
        return any(word in haystack for word in _CONTEXT_OVERFLOW_COMPANIONS)
    return False


def _usage_field(usage: Any, name: str) -> Any:
    """Read one ``usage`` field from either an object or a plain dict."""
    if isinstance(usage, Mapping):
        return usage.get(name)
    return getattr(usage, name, None)


def _cached_tokens(usage: Any) -> int:
    """The prompt-cache hit count, however the provider spells it.

    OpenAI nests it under ``prompt_tokens_details.cached_tokens``, some gateways
    flatten it to ``cached_tokens``, DeepSeek-native APIs call it
    ``prompt_cache_hit_tokens``.  It is a *subset* of the prompt count wherever
    it appears, so it is reported, never added.
    """
    details = _usage_field(usage, "prompt_tokens_details")
    for value in (_usage_field(usage, "cached_tokens"),
                  _usage_field(details, "cached_tokens"),
                  _usage_field(usage, "prompt_cache_hit_tokens")):
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, value)
    return 0


def _usage_dict(usage: Any) -> dict[str, int]:
    """Normalise a provider ``usage`` object to a plain int dict.

    ``prompt_tokens`` is the ground truth the token-budget layer anchors its
    estimates on.  Missing fields fall back to 0 so a partial usage object never
    breaks a successful call.
    """
    prompt = _usage_field(usage, "prompt_tokens")
    completion = _usage_field(usage, "completion_tokens")
    return {"prompt_tokens": prompt if isinstance(prompt, int) else 0,
            "completion_tokens": completion if isinstance(completion, int) else 0,
            "cached_tokens": _cached_tokens(usage)}


def _reasoning_text(message: Any) -> str:
    """Read a provider's chain of thought off a response or a delta.

    The field name is not standardised: DeepSeek-native APIs expose
    ``reasoning_content``, OpenRouter-style gateways expose ``reasoning`` (plus
    a structured ``reasoning_details`` array).  All are normalised into the
    runtime's single ``reasoning_content`` field.

    An empty string means the provider sent no reasoning at all — the caller
    must treat that as "not a thinking model", not as "thinking with nothing to
    say".
    """
    for field in ("reasoning_content", "reasoning"):
        value = getattr(message, field, None)
        if isinstance(value, str) and value:
            return value
    details = getattr(message, "reasoning_details", None)
    if isinstance(details, (list, tuple)):
        parts = [entry.get("text") for entry in details
                 if isinstance(entry, dict) and entry.get("text")]
        if parts:
            return "".join(parts)
    return ""


@dataclass(frozen=True)
class WireRepair:
    """Counts for one wire-copy repair pass (observability only)."""

    moved: int = 0
    dropped_calls: int = 0
    dropped_orphan_tools: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.moved or self.dropped_calls
                    or self.dropped_orphan_tools)


def _repair_tool_call_pairing(
        messages: Sequence[dict[str, Any]]
) -> tuple[list[dict[str, Any]], WireRepair]:
    """Enforce the provider pairing contract on the wire copy.

    Each assistant ``tool_calls`` block must be followed *immediately* by the
    matching ``role: tool`` messages, with no user/assistant/system message in
    between.  Repairs: a response separated from its call is moved directly
    after the assistant; a call with no response is dropped; an orphan tool
    response is dropped.  Only the wire copy is repaired.
    """
    positions = {id(message): index for index, message in enumerate(messages)}

    responses: dict[str, dict[str, Any]] = {}
    for message in messages:
        if message.get("role") != "tool":
            continue
        call_id = message.get("tool_call_id")
        if isinstance(call_id, str) and call_id and call_id not in responses:
            responses[call_id] = message

    referenced: set[str] = set()
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, Mapping):
                continue
            call_id = call.get("id")
            if isinstance(call_id, str) and call_id:
                referenced.add(call_id)

    # Prefix count of non-tool messages makes the "was this response separated
    # from its assistant?" check O(1) per call instead of O(n^2) per batch.
    nontool_prefix = [0] * (len(messages) + 1)
    for index, message in enumerate(messages):
        nontool_prefix[index + 1] = (
            nontool_prefix[index]
            + (0 if message.get("role") == "tool" else 1))

    repaired: list[dict[str, Any]] = []
    emitted: set[str] = set()
    moved = dropped_calls = dropped_orphan_tools = 0

    for message in messages:
        if message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in referenced:
                dropped_orphan_tools += 1
            # Referenced responses are emitted right after their assistant.
            continue

        calls = message.get("tool_calls")
        if message.get("role") != "assistant" or not calls:
            repaired.append(message)
            continue

        kept: list[Any] = []
        for call in calls:
            call_id = call.get("id") if isinstance(call, Mapping) else None
            if (not isinstance(call_id, str)
                    or call_id not in responses
                    or call_id in emitted):
                dropped_calls += 1
                continue
            kept.append(call)

        out = message
        if len(kept) != len(calls):
            out = dict(message)
            if kept:
                out["tool_calls"] = kept
            else:
                out.pop("tool_calls", None)
        repaired.append(out)

        for call in kept:
            call_id = call["id"]
            response = responses[call_id]
            emitted.add(call_id)
            repaired.append(response)
            start = positions[id(message)]
            end = positions[id(response)]
            if (end <= start
                    or nontool_prefix[end] - nontool_prefix[start + 1]):
                moved += 1

    return repaired, WireRepair(moved=moved,
                                dropped_calls=dropped_calls,
                                dropped_orphan_tools=dropped_orphan_tools)


def _project_messages_for_wire(messages: Sequence[dict[str, Any]],
                               echo: bool) -> list[dict[str, Any]]:
    """Return the request-side projection of ``messages``.

    Pure and non-mutating: history and traces keep the full turn (including
    chain-of-thought), only the wire copy is shaped.  Tool-call pairing is
    repaired, ``metadata`` is stripped (no provider understands it), and
    ``reasoning_content`` is kept only when ``echo`` is true — where it is
    stripped, a thinking-only turn gets a placeholder because the protocol
    requires assistant messages to carry something.
    """
    paired, repair = _repair_tool_call_pairing(messages)
    if repair.dropped_calls:
        # Keep the old metric name for existing dashboards.
        logger.warning("llm.wire.dangling_tool_calls_dropped count=%d",
                       repair.dropped_calls)
    if repair.changed:
        logger.warning(
            "llm.wire.contract_repaired moved=%d dropped_calls=%d "
            "dropped_orphan_tools=%d",
            repair.moved, repair.dropped_calls, repair.dropped_orphan_tools)
    projected: list[dict[str, Any]] = []
    for message in paired:
        out = dict(message)
        out.pop("metadata", None)
        if not echo and out.get("role") == "assistant":
            out.pop("reasoning_content", None)
            if not out.get("content") and not out.get("tool_calls"):
                out["content"] = "(reasoning omitted)"
        projected.append(out)
    return projected


class OpenAICompatibleLLM:
    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                  model: str | None = None, config: RuntimeConfig | None = None,
                  temperature: float | None = None,
                  echo_reasoning: bool | None = None,
                  **client_kwargs: Any) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise RuntimeError("Install the openai package to use this provider") from exc
        if config is not None:
            from dataclasses import replace as _replace
            fields: dict[str, Any] = {}
            if model is not None:
                fields["model"] = model
            if base_url is not None:
                fields["base_url"] = base_url
            if api_key is not None:
                fields["api_key"] = api_key
            if temperature is not None:
                fields["temperature"] = float(temperature)
            self._config = _replace(config, **fields) if fields else config
        else:
            self._config = RuntimeConfig.from_env(model=model, base_url=base_url, api_key=api_key, temperature=temperature)
        self.model = self._config.model
        self.base_url = self._config.base_url
        self.temperature = self._config.temperature
        # Catalog override; ``None`` falls back to the family default.
        self._echo_reasoning = echo_reasoning
        self.client = AsyncOpenAI(api_key=self._config.api_key, base_url=self.base_url, **client_kwargs)

    @property
    def runtime_config(self) -> RuntimeConfig:
        return self._config

    @property
    def echo_reasoning(self) -> bool:
        """Whether assistant ``reasoning_content`` is replayed on the wire.

        A catalog entry pins it (``ModelEntry.echo_reasoning``); without a
        pin this is the pure model-family default.  Never env-backed: the
        catalog entry is the single source of truth, so the budget layer and
        the wire cannot disagree.
        """
        if self._echo_reasoning is not None:
            return self._echo_reasoning
        from agent_runtime.reasoning_echo import derives_echo_reasoning

        return derives_echo_reasoning(self.model, self.base_url)

    async def chat(self, messages: Sequence[dict[str, Any]],
                   tools: list[dict[str, Any]] | None = None,
                   **request_kwargs: Any) -> dict[str, Any]:
        echo = self.echo_reasoning
        kwargs = {"model": self.model,
                  "messages": _project_messages_for_wire(messages, echo),
                  "temperature": self.temperature, **request_kwargs}
        effort = self._config.reasoning_effort
        if effort is not None:
            kwargs.setdefault("reasoning_effort", effort)
        if tools is not None:
            kwargs["tools"] = tools
        try:
            response = await self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            if _looks_like_context_overflow(exc):
                raise ContextWindowExceededError(str(exc)) from exc
            raise
        choice = response.choices[0]
        message = choice.message
        calls = [call.model_dump(exclude_none=True) for call in (message.tool_calls or [])]
        result = {"content": message.content or "", "tool_calls": calls}
        reasoning = _reasoning_text(message)
        if reasoning:
            result["reasoning_content"] = reasoning
        # Keep the provider's stop reason so callers can tell a natural
        # stop from a max_tokens cut (e.g. a truncated summarizer output).
        finish_reason = getattr(choice, "finish_reason", None)
        if isinstance(finish_reason, str) and finish_reason:
            result["finish_reason"] = finish_reason
        usage = getattr(response, "usage", None)
        if usage is not None:
            result["usage"] = _usage_dict(usage)
        return result

    async def stream(self, messages: Sequence[dict[str, Any]],
                     tools: list[dict[str, Any]] | None = None,
                     **request_kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        """Yield provider-independent content and tool-call deltas.

        After the last payload delta, yields one final dict carrying the
        ``finish_reason`` when the stream terminated normally.  If the server
        cuts the stream without a finish marker, no such signal is emitted.
        """
        echo = self.echo_reasoning
        kwargs = {"model": self.model,
                  "messages": _project_messages_for_wire(messages, echo),
                  "stream": True, "temperature": self.temperature,
                  **request_kwargs}
        effort = self._config.reasoning_effort
        if effort is not None:
            kwargs.setdefault("reasoning_effort", effort)
        if tools is not None:
            kwargs["tools"] = tools
        if self._config.stream_usage:
            kwargs.setdefault("stream_options", {"include_usage": True})
        try:
            response = await self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            if _looks_like_context_overflow(exc):
                raise ContextWindowExceededError(str(exc)) from exc
            raise
        finish_reason: str | None = None
        usage: dict[str, int] | None = None
        async for chunk in response:
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = _usage_dict(chunk_usage)
            if not chunk.choices:
                # Some providers append a final chunk with empty choices (e.g. a
                # usage summary); it carries no delta payload.
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            reasoning = _reasoning_text(delta)
            data: dict[str, Any] = {"content": delta.content or "",
                                    "reasoning_content": reasoning}
            calls = []
            for call in (delta.tool_calls or []):
                # Serialize the whole SDK object instead of rebuilding a
                # known-fields dict: some providers attach per-call extras
                # (Gemini's ``extra_content.google.thought_signature``) that must
                # be echoed back on the next request or the call is rejected.
                calls.append(call.model_dump(exclude_none=True))
            if calls:
                data["tool_calls"] = calls
            if data["content"] or calls or reasoning:
                yield data
        if finish_reason:
            final: dict[str, Any] = {"content": "", "reasoning_content": "",
                                     "finish_reason": finish_reason}
            if usage is not None:
                final["usage"] = usage
            yield final
        elif usage is not None:
            yield {"content": "", "reasoning_content": "", "usage": usage}

__all__ = [
    "OpenAICompatibleLLM", "OpenAILLM", "ContextWindowExceededError",
    "_looks_like_context_overflow", "_usage_dict",
]

OpenAILLM = OpenAICompatibleLLM
