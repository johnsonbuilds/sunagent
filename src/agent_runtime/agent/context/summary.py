"""LLM-backed history summarization: snapshot + verbatim tail.

Structure: ``leading + [summary] + preserved user messages + tail``.
See :func:`summarize_history` for the full policy.
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple

from agent_runtime.harness import (
    DEFAULT_CONTEXT_BUDGET,
    DEFAULT_SUMMARIZER_INPUT_CAP_CHARS,
    DEFAULT_SUMMARY_MAX_TOKENS,
    DEFAULT_SUMMARY_REASONING_EFFORT,
    DEFAULT_SUMMARY_TAIL_ROUNDS,
    DEFAULT_USER_PRESERVE_BUDGET,
)

from agent_runtime.agent.context.compact import _round_starts, compact_observations
from agent_runtime.agent.context.metrics import _view_size, total_chars
from agent_runtime.agent.runtime_message import (
    is_runtime_user_message,
    runtime_user_message,
)

logger = logging.getLogger(__name__)

SUMMARY_PREFIX = "[SYSTEM CONTEXT COMPACTION SUMMARY]\n"

SUMMARIZER_SYSTEM = (
    "You are a professional context-compaction summarizer for an agent."
)

SUMMARIZER_INSTRUCTION = """\
Analyze the conversation history above. Produce a dense, structured summary of the CURRENT \
execution state that a fresh reasoning step can rely on.

Rules:
- Output ONLY the markdown below, with no preamble and no code fences.
- Focus strictly on tracking progress, active attempts, error lessons, and working context.
- For every failed or blocked attempt, record the exact error and root-cause lesson to prevent repeated failures (avoid Sisyphean loops).
- Be extremely specific with key entities, file paths, parameters, tool invocations, exit codes, and intermediate values.
- Keep it concise, precise, and information-dense.
- Keep the whole snapshot within 4,096 output tokens. Use only as much
  space as the state actually needs; do not pad, repeat, or restate the
  history.
- Do not paste raw arrays, full tool output, or long tables; cite the
  script or file that holds them instead.
- In "## 4. Open Questions", list every question the user asked that is
  still unanswered — one bullet per question, in the user's own words.
  Do not merge, reword, or summarise them; write "- (none)" when nothing
  is open.
- Always finish all four sections. If space runs short, compress
  historical detail in "## 1. Work State" (cite scripts/files instead of
  pasting their output) while keeping failure lessons; never drop
  "## 2", "## 3" or "## 4".

Use exactly these sections:

# Agent Context Snapshot

## 1. Work State
### Completed
- ...
### Active (In-Progress)
- ...
### Blocked / Failure Lessons
- ...

## 2. Next Move
- ...

## 3. Working Context & Anchors
- **Relevant Files / Artifacts**: ...
- **Environment State**: ...

## 4. Open Questions
- ..."""

# Appended when a previous snapshot exists: the new summary must integrate
# it, so earlier work state and failure lessons survive every compaction.
SUMMARIZER_INTEGRATION_ADDENDUM = f"""

A PREVIOUS SNAPSHOT message — one that starts with the
"{SUMMARY_PREFIX.strip()}" prefix — is provided before the
conversation history. You must merge it into your output:
- Fold the previous "Work State", "Next Move", "Working Context &
  Anchors" and "Open Questions" into the new snapshot; keep items that
  are still relevant, mark superseded ones as completed/obsolete, never
  silently drop a failure lesson.
- In "Open Questions", carry over every question the user asked that is
  still unanswered; drop only the ones the conversation has since
  answered.
- The output is the single consolidated snapshot for the whole session,
  in the same section format as before."""


def _is_summary_message(message: dict[str, Any]) -> bool:
    """Whether ``message`` is a machine-generated compaction summary."""
    metadata = message.get("metadata")
    return (
        is_runtime_user_message(message)
        and isinstance(metadata, dict)
        and metadata.get("kind") == "summary"
        and str(message.get("content", "")).startswith(SUMMARY_PREFIX)
    )


def extract_summary(view: list[dict[str, Any]] | None) -> str | None:
    """The previous compaction summary text carried in a request view."""
    if not view:
        return None
    for message in view:
        if _is_summary_message(message):
            return str(message["content"])[len(SUMMARY_PREFIX):]
    return None


def _resolve_summarizer(llm: Any) -> Any:
    """Resolve an injected summarizer object or lazy factory."""
    if llm is None:
        return None
    if callable(llm) and not hasattr(llm, "chat"):
        return llm()
    return llm


def _strip_leading_system(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop leading ``system`` messages.

    Re-priming the summarizer with the agent's own system prompt makes it
    re-enter the tool-calling role and emit a tool call instead of a summary.
    """
    index = 0
    while index < len(messages) and messages[index].get("role") == "system":
        index += 1
    return messages[index:]


class _SummaryCall(NamedTuple):
    """One summarizer response plus the metadata needed to diagnose it."""

    content: str | None
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str | None = None
    reasoning_chars: int = 0


async def _generate_summary(messages: list[dict[str, Any]], *,
                            previous_summary: str | None,
                            summary_max_tokens: int,
                            summary_reasoning_effort: str | None = (
                                DEFAULT_SUMMARY_REASONING_EFFORT),
                            llm: Any = None,
                            prompt: str | None = None) -> _SummaryCall:
    """Ask the LLM for a structured summary of the compaction segment.

    The instruction goes last so recency dominates the output.  Reasoning
    defaults to ``none``: extraction is mechanical, so thinking only eats the
    output budget.  Exceptions propagate; the caller falls back to the
    deterministic compactor instead of aborting the turn.
    """
    from agent_runtime.agent.tokens import response_usage

    history = _strip_leading_system(messages)
    instruction = prompt or SUMMARIZER_INSTRUCTION
    if previous_summary is not None:
        instruction = instruction + SUMMARIZER_INTEGRATION_ADDENDUM
    prefix: list[dict[str, Any]] = []
    if previous_summary is not None:
        prefix = [{"role": "user",
                   "content": SUMMARY_PREFIX + previous_summary}]
    summarizer_messages: list[dict[str, Any]] = [
        {"role": "system", "content": SUMMARIZER_SYSTEM},
        *prefix,
        *history,
        {"role": "user", "content": instruction},
    ]
    model = _resolve_summarizer(llm)
    if model is None:
        raise RuntimeError("no summarizer LLM is configured")
    request_kwargs: dict[str, Any] = {"max_tokens": summary_max_tokens}
    if summary_reasoning_effort is not None:
        request_kwargs["reasoning_effort"] = summary_reasoning_effort
    response = await model.chat(summarizer_messages, tools=None,
                                **request_kwargs)
    prompt_tokens, completion_tokens, _ = response_usage(response)
    reasoning = response.get("reasoning_content") or ""
    return _SummaryCall(
        content=response.get("content") or "",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        finish_reason=response.get("finish_reason"),
        reasoning_chars=len(reasoning) if isinstance(reasoning, str) else 0,
    )


_REQUIRED_SECTIONS = [
    "## 1. Work State",
    "## 2. Next Move",
    "## 3. Working Context & Anchors",
    "## 4. Open Questions",
]


def _validate_summary(summary: str, *,
                      summary_max_tokens: int = DEFAULT_SUMMARY_MAX_TOKENS) -> None:
    """Raise ``ValueError`` when ``summary`` is not a usable snapshot.

    The cap is ~6 chars per token, generous to CJK text (closer to 2-3): a
    summary that grows toward the history it replaces defeats compaction.
    """
    stripped = summary.strip()
    if not stripped:
        raise ValueError("summary is empty")
    for section in _REQUIRED_SECTIONS:
        if section not in stripped:
            raise ValueError(f"summary missing required section {section!r}")
    if len(stripped) > summary_max_tokens * 6:
        raise ValueError(
            f"summary exceeds output cap ({len(stripped)} chars > "
            f"{summary_max_tokens} tokens x 6 chars)")


async def summarize_history(messages: list[dict[str, Any]], *,
                            budget: int | None = None,
                            tail_rounds: int | None = None,
                            user_preserve_budget: int = DEFAULT_USER_PRESERVE_BUDGET,
                            summary_max_tokens: int = DEFAULT_SUMMARY_MAX_TOKENS,
                            summary_reasoning_effort: str | None = (
                                DEFAULT_SUMMARY_REASONING_EFFORT),
                            boundary: int = 0,
                            previous_summary: str | None = None,
                            unit: str = "chars",
                            usage_anchor: tuple[int, int, int] | None = None,
                            exempt_tools: tuple[str, ...] = (),
                            prompt: str | None = None,
                            trace: Any = None,
                            iteration: int | None = None,
                            llm: Any = None,
                            echo_reasoning: bool = True,
                            usage_sink: Any = None,
                            ) -> list[dict[str, Any]]:
    """Return a summarized request view of ``messages`` once over ``budget``.

    Structure: ``leading + [summary] + preserved user messages + tail``.

    ``leading`` (system prompt + round 0) never moves, which anchors the task
    and keeps the request prefix stable across compactions.  User messages are
    kept verbatim because they are the authoritative record of intent, and the
    budget spans all history so one kept by an earlier compaction survives the
    boundary advancing past it.  The tail keeps the last ``tail_rounds`` rounds
    verbatim so tool-call pairs stay intact.

    With ``boundary`` and ``previous_summary`` the summarizer reads only the new
    segment and must integrate the snapshot, so a second compaction costs
    O(new segment).  Below budget the input list is returned unchanged;
    summarizer failure or an invalid summary falls back to
    ``compact_observations``.
    """
    count = echo_reasoning
    if unit == "tokens":
        from agent_runtime.agent.context.metrics import _anchor_chars_per_token
        chars_per_token = _anchor_chars_per_token(usage_anchor)
        char_budget = int((budget or DEFAULT_CONTEXT_BUDGET) * chars_per_token)
    else:
        char_budget = budget or DEFAULT_CONTEXT_BUDGET
    if tail_rounds is None:
        tail_rounds = DEFAULT_SUMMARY_TAIL_ROUNDS

    starts = _round_starts(messages)
    if len(starts) < 2:
        return messages
    if _view_size(messages, count, unit, usage_anchor) <= (budget or DEFAULT_CONTEXT_BUDGET):
        return messages

    # A snapshot must never enter the source history: stale text would freeze
    # the incremental boundary.
    leading = [message for message in messages[:starts[0]]
               if not _is_summary_message(message)]
    tail_start = starts[-tail_rounds]
    tail = messages[tail_start:]

    # ``segment`` is the incremental summarizer input; ``preserve_pool`` spans
    # all history so a user message kept by an earlier compaction survives the
    # boundary advancing past it.
    segment = [message for message in messages[max(starts[0], boundary):tail_start]
               if not _is_summary_message(message)]
    preserve_pool = [message for message in messages[starts[0]:tail_start]
                     if not _is_summary_message(message)]

    # Stays in chars under either unit: converting through the anchor ratio
    # would add noise to a coarse guard.
    preserved: list[dict[str, Any]] = []
    user_chars = 0
    for message in reversed(preserve_pool):
        if (message.get("role") == "user"
                and not is_runtime_user_message(message)):
            content = str(message.get("content", ""))
            if user_chars + len(content) <= user_preserve_budget:
                preserved.append(message)
                user_chars += len(content)
    preserved.reverse()  # restore chronological order

    # Judged on the segment alone so a boundary already covered by the previous
    # snapshot keeps the zero-LLM-call fast path.
    kept = {id(message) for message in preserved}
    novel = [message for message in segment if id(message) not in kept]

    summarizer_input = list(segment)

    if not novel:
        if previous_summary is not None:
            summary_msg = runtime_user_message(
                SUMMARY_PREFIX + previous_summary, kind="summary")
            return leading + [summary_msg] + preserved + tail
        return messages

    summary: str | None = None
    summarizer_prompt_tokens = 0
    summarizer_completion_tokens = 0
    last_finish_reason: str | None = None
    last_reasoning_chars = 0
    last_prompt_tokens = 0
    last_completion_tokens = 0
    used_capped_input = False
    full_input_chars = total_chars(summarizer_input, count)
    for attempt in (1, 2):
        attempt_input = summarizer_input
        if attempt == 2 and full_input_chars > DEFAULT_SUMMARIZER_INPUT_CAP_CHARS:
            # Retry on a folded copy: same pairs/users, old observations shrunk.
            attempt_input = compact_observations(
                summarizer_input, budget=DEFAULT_SUMMARIZER_INPUT_CAP_CHARS,
                exempt_tools=exempt_tools, echo_reasoning=count)
            used_capped_input = True
        try:
            call = await _generate_summary(
                attempt_input, previous_summary=previous_summary,
                summary_max_tokens=summary_max_tokens,
                summary_reasoning_effort=summary_reasoning_effort,
                prompt=prompt, llm=llm)
            summary = call.content
            last_finish_reason = call.finish_reason
            last_reasoning_chars = call.reasoning_chars
            last_prompt_tokens = call.prompt_tokens
            last_completion_tokens = call.completion_tokens
            summarizer_prompt_tokens += call.prompt_tokens
            summarizer_completion_tokens += call.completion_tokens
            if usage_sink is not None:
                try:
                    result_sink = usage_sink(call.prompt_tokens,
                                             call.completion_tokens)
                    if hasattr(result_sink, "__await__"):
                        await result_sink
                except Exception:
                    logger.debug("context.summary.usage_sink_failed",
                                 exc_info=True)
        except Exception as exc:
            # Billed call failed before producing a snapshot.
            if trace is not None:
                trace.emit("context.llm_summary.error", iteration,
                           error=str(exc)[:500], error_type=type(exc).__name__,
                           summarizer_input_chars=full_input_chars,
                           summarizer_input_chars_capped=total_chars(
                               attempt_input, count),
                           input_capped=used_capped_input,
                           summarizer_input_count=len(summarizer_input),
                           boundary=boundary, char_budget=char_budget,
                           summary_max_tokens=summary_max_tokens,
                           summary_reasoning_effort=summary_reasoning_effort)
            logger.warning("context.summary.failed falling back to "
                           "compact_observations: %s", exc)
            return compact_observations(messages, budget=char_budget,
                                        exempt_tools=exempt_tools,
                                        echo_reasoning=count)
        try:
            _validate_summary(summary, summary_max_tokens=summary_max_tokens)
            break
        except ValueError as exc:
            # Flaky non-compliance: retry once, then fall back.
            if trace is not None:
                trace.emit("context.llm_summary.invalid", iteration,
                           reason=str(exc)[:500], attempt=attempt,
                           summary_chars=len(summary or ""),
                           summary_tail=(summary or "")[-200:],
                           finish_reason=last_finish_reason,
                           reasoning_chars=last_reasoning_chars,
                           attempt_prompt_tokens=last_prompt_tokens,
                           attempt_completion_tokens=last_completion_tokens,
                           prompt_tokens=summarizer_prompt_tokens,
                           completion_tokens=summarizer_completion_tokens,
                           total_tokens=(summarizer_prompt_tokens
                                         + summarizer_completion_tokens),
                           summarizer_input_chars=full_input_chars,
                           summarizer_input_chars_capped=total_chars(
                               attempt_input, count),
                           input_capped=used_capped_input,
                           summarizer_input_count=len(summarizer_input),
                           boundary=boundary, char_budget=char_budget,
                           summary_max_tokens=summary_max_tokens,
                           summary_reasoning_effort=summary_reasoning_effort)
            if attempt == 2:
                logger.warning("context.summary.invalid %s falling back to compact_observations", exc)
                return compact_observations(messages, budget=char_budget,
                                            exempt_tools=exempt_tools,
                                            echo_reasoning=count)
            logger.warning("context.summary.invalid %s retrying (%d/2)",
                           exc, attempt + 1)
    summary_msg = runtime_user_message(SUMMARY_PREFIX + summary, kind="summary")
    view = leading + [summary_msg] + preserved + tail
    if trace is not None:
        trace.emit("context.llm_summary.yield", iteration,
                   summary=summary, preserved_users=len(preserved),
                   summarizer_input_count=len(summarizer_input),
                   input_capped=used_capped_input,
                   boundary=boundary, char_budget=char_budget,
                   finish_reason=last_finish_reason,
                   reasoning_chars=last_reasoning_chars,
                   summary_reasoning_effort=summary_reasoning_effort,
                   attempt_prompt_tokens=last_prompt_tokens,
                   attempt_completion_tokens=last_completion_tokens,
                   prompt_tokens=summarizer_prompt_tokens,
                   completion_tokens=summarizer_completion_tokens,
                   total_tokens=(summarizer_prompt_tokens
                                 + summarizer_completion_tokens))
    return view


__all__ = [
    "SUMMARY_PREFIX", "SUMMARIZER_INSTRUCTION",
    "extract_summary", "summarize_history",
]
