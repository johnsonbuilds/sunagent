"""OpenAI-compatible model adapter and runtime-neutral chat responses."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any


logger = logging.getLogger(__name__)


def _reasoning_effort() -> str | None:
    """Optional reasoning-effort hint (e.g. ``low``) for models that
    support controlling chain-of-thought depth.  Read per request so
    the env can be toggled without rebuilding the provider."""
    value = os.getenv("AGENT_RUNTIME_REASONING_EFFORT", "").strip()
    return value or None


def _reasoning_echo_mode() -> str:
    """Outbound reasoning-replay policy: ``auto``, ``always``, or ``never``.

    Read per request like :func:`_reasoning_effort`.  Unknown values fall
    back to ``auto`` so a typo never strands the run on a wrong policy.
    """
    value = os.getenv("AGENT_RUNTIME_REASONING_ECHO", "auto").strip().lower()
    return value if value in ("auto", "always", "never") else "auto"


# Substring markers for "echo" families: thinking-mode APIs that expect
# the assistant `reasoning_content` back on every replay (DeepSeek's
# contract: "the reasoning_content in the thinking mode must be passed
# back to the API").  Matched against MODEL_ID and LLM_BASE_URL.  A
# gateway that proxies one of these under a neutral host is misclassified
# — pin AGENT_RUNTIME_REASONING_ECHO=always|never for such setups.
_ECHO_FAMILY_MARKERS: tuple[str, ...] = (
    "deepseek", "glm", "zhipu", "bigmodel",
    "kimi", "moonshot", "mimo", "xiaomi", "ling",
)

# Hosts that proxy other providers.  A gateway drops the assistant
# turn's reasoning fields before the request reaches the upstream
# model, so replaying them is a no-op: measured against OpenRouter,
# 6400 chars of replayed reasoning cost 0 extra prompt tokens and the
# model never saw the text (the same 6400 chars in ``content`` cost
# +1202 tokens).  Model-name matching cannot catch this — OpenRouter
# serves ``deepseek/...`` models, so the family heuristic above would
# replay reasoning the gateway is about to drop, and the context-budget
# layer would then charge for tokens that are never sent.
_GATEWAY_HOST_MARKERS: tuple[str, ...] = ("openrouter.ai",)


def _endpoint_replays_reasoning(base_url: str | None) -> bool | None:
    """Whether the endpoint honours a replayed chain of thought.

    ``True``/``False`` for a host we know the behaviour of, ``None``
    when the host is unknown (the caller then falls back to the
    model-family heuristic).
    """
    host = (base_url or "").lower()
    if any(marker in host for marker in _GATEWAY_HOST_MARKERS):
        return False
    return None


def _should_echo_reasoning(model: str, base_url: str | None,
                           mode: str | None = None) -> bool:
    """Whether assistant ``reasoning_content`` is replayed to the provider."""
    if mode is None:
        mode = _reasoning_echo_mode()
    if mode == "always":
        return True
    if mode == "never":
        return False
    verdict = _endpoint_replays_reasoning(base_url)
    if verdict is not None:
        return verdict
    haystack = f"{model} {base_url or ''}".lower()
    return any(marker in haystack for marker in _ECHO_FAMILY_MARKERS)


_DEFAULT_MODEL = "glm-5.3"


class ContextWindowExceededError(RuntimeError):
    """The provider rejected the request for exceeding the context window.

    Carries ``llm_category = "context_window"`` so the recovery layer
    routes it to the ``recovery.llm_errors.context_window`` policy.  An
    in-place retry can never succeed: the caller must compact the request
    view before resending (agent loop's refresh hook).
    """

    llm_category = "context_window"


# Substring markers for context-window rejections.  Providers word the
# same 400 differently ("context_length_exceeded", "maximum context
# length", …), so the match is a lowercase substring scan over the
# exception text — same approach as the OpenHands error classifier.
# Specific markers match on their own; the two generic ones ("context
# window", "too many tokens") only count alongside an overflow companion
# word, so unrelated errors mentioning a context window (e.g. "invalid
# context window parameter") stay on the provider_error retry policy
# instead of burning the hard gate's compaction budget.
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


def _usage_dict(usage: Any) -> dict[str, int]:
    """Normalise a provider ``usage`` object to a plain int dict.

    ``prompt_tokens`` is the measured size of the request as the provider
    billed it — the ground truth the token-budget layer anchors its
    estimates on.  Missing fields fall back to 0 rather than raising so a
    partial usage object never breaks a successful call.
    """
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    return {"prompt_tokens": prompt if isinstance(prompt, int) else 0,
            "completion_tokens": completion if isinstance(completion, int) else 0}


def _stream_usage_enabled() -> bool:
    """Whether streaming requests ask for a trailing usage chunk.

    ``stream_options: {"include_usage": true}`` is the OpenAI-compatible
    way to get billed token counts on a stream.  Set
    ``AGENT_RUNTIME_STREAM_USAGE=0`` for providers that reject the
    parameter outright.
    """
    return os.getenv("AGENT_RUNTIME_STREAM_USAGE", "1").strip() != "0"


def reasoning_echo_enabled(model: str | None = None,
                           base_url: str | None = None) -> bool:
    """Echo decision for the configured endpoint (env-backed defaults).

    The context-budget layer asks this so accounting matches the wire:
    reasoning only counts toward the budget when it will actually be
    replayed. ``model``/``base_url`` default to the same env resolution
    as :class:`OpenAICompatibleLLM`.
    """
    return _should_echo_reasoning(
        model if model is not None else os.getenv("MODEL_ID", _DEFAULT_MODEL),
        base_url if base_url is not None else os.getenv("LLM_BASE_URL"))


def _reasoning_text(message: Any) -> str:
    """Read a provider's chain of thought off a response or a delta.

    The field name is not standardised: DeepSeek-native APIs expose
    ``reasoning_content``, OpenRouter and other OpenAI-compatible
    gateways expose ``reasoning`` (plus a structured ``reasoning_details``
    array).  All of them are normalised into the runtime's single
    ``reasoning_content`` field so nothing downstream has to know which
    provider answered.

    An empty string means the provider sent no reasoning at all — the
    caller must treat that as "not a thinking model", not as "thinking
    with nothing to say".
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


def _drop_dangling_tool_calls(
        messages: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Drop assistant tool calls that receive no tool response.

    Pure repair for the provider pairing contract (every assistant
    ``tool_calls`` entry must be followed by a ``role: tool`` message
    with the same ``tool_call_id``).  Dangling entries arise from
    history paths that record an assistant call without executing it
    (e.g. an intercepted finish request) or from context compaction
    slicing a pair apart.  Only the wire copy is repaired; the stored
    history is untouched.  Returns the repaired list and the number of
    dropped calls.
    """
    answered: set[str] = set()
    for message in messages:
        if message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                answered.add(call_id)
    repaired: list[dict[str, Any]] = []
    dropped = 0
    for message in messages:
        calls = message.get("tool_calls")
        if message.get("role") == "assistant" and calls:
            kept = [call for call in calls
                    if isinstance(call, dict) and call.get("id") in answered]
            if len(kept) != len(calls):
                dropped += len(calls) - len(kept)
                message = dict(message)
                if kept:
                    message["tool_calls"] = kept
                else:
                    message.pop("tool_calls", None)
        repaired.append(message)
    return repaired, dropped


def _project_messages_for_wire(messages: Sequence[dict[str, Any]],
                               echo: bool) -> list[dict[str, Any]]:
    """Return the request-side projection of ``messages``.

    Pure and non-mutating: conversation history and traces keep the full
    turn (including chain-of-thought), only the wire copy is shaped.

    - Dangling assistant tool calls (no later ``role: tool`` message
      with the same id) are dropped so the provider never 400s on the
      pairing contract.
    - ``metadata`` (observation spill paths and other runtime side-channel
      data) is stripped unconditionally — no provider understands it.
    - ``reasoning_content`` is kept only when ``echo`` is true.  Where it
      is stripped, a thinking-only assistant turn would collapse to an
      empty message, so it gets a placeholder instead (the protocol
      requires assistant messages to carry something).
    """
    paired, dropped = _drop_dangling_tool_calls(messages)
    if dropped:
        logger.warning("llm.wire.dangling_tool_calls_dropped count=%d", dropped)
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
                  model: str | None = None, **client_kwargs: Any) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise RuntimeError("Install the openai package to use this provider") from exc
        self.model = model or os.getenv("MODEL_ID", _DEFAULT_MODEL)
        self.base_url = base_url or os.getenv("LLM_BASE_URL")
        self.temperature = float(os.getenv("LLM_TEMPERATURE", 0.0))
        self.client = AsyncOpenAI(api_key=api_key or os.getenv("LLM_API_KEY"),
                                  base_url=self.base_url,
                                  **client_kwargs)

    async def chat(self, messages: Sequence[dict[str, Any]],
                   tools: list[dict[str, Any]] | None = None,
                   **request_kwargs: Any) -> dict[str, Any]:
        echo = _should_echo_reasoning(self.model, self.base_url)
        kwargs = {"model": self.model,
                  "messages": _project_messages_for_wire(messages, echo),
                  "temperature": self.temperature, **request_kwargs}
        effort = _reasoning_effort()
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
        message = response.choices[0].message
        calls = [call.model_dump(exclude_none=True) for call in (message.tool_calls or [])]
        result = {"content": message.content or "", "tool_calls": calls}
        reasoning = _reasoning_text(message)
        if reasoning:
            result["reasoning_content"] = reasoning
        usage = getattr(response, "usage", None)
        if usage is not None:
            result["usage"] = _usage_dict(usage)
        return result

    async def stream(self, messages: Sequence[dict[str, Any]],
                     tools: list[dict[str, Any]] | None = None,
                     **request_kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        """Yield provider-independent content and tool-call deltas.

        After the last payload delta, yields one final dict carrying the
        ``finish_reason`` (e.g. ``stop``, ``tool_calls``) when the stream
        terminated normally.  If the server cuts the stream without a
        finish marker, no such signal is emitted.
        """
        echo = _should_echo_reasoning(self.model, self.base_url)
        kwargs = {"model": self.model,
                  "messages": _project_messages_for_wire(messages, echo),
                  "stream": True, "temperature": self.temperature,
                  **request_kwargs}
        effort = _reasoning_effort()
        if effort is not None:
            kwargs.setdefault("reasoning_effort", effort)
        if tools is not None:
            kwargs["tools"] = tools
        if _stream_usage_enabled():
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
                # Some providers append a final chunk with empty choices
                # (e.g. a usage summary); it carries no delta payload.
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
                function = call.function
                calls.append({
                    "index": call.index,
                    "id": call.id,
                    "function": {
                        "name": function.name,
                        "arguments": function.arguments or "",
                    },
                })
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
    "_looks_like_context_overflow", "_usage_dict", "_stream_usage_enabled",
]

OpenAILLM = OpenAICompatibleLLM
