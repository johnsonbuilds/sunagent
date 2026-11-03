"""LLM-backed history summarization: snapshot + verbatim tail.

Structure: ``leading + [summary] + preserved user messages + tail``.
See :func:`summarize_history` for the full policy.
"""

from __future__ import annotations

import logging
from typing import Any

from agent_runtime.harness import (
    DEFAULT_CONTEXT_BUDGET,
    DEFAULT_SUMMARIZER_INPUT_CAP_CHARS,
    DEFAULT_SUMMARY_MAX_TOKENS,
    DEFAULT_SUMMARY_TAIL_ROUNDS,
    DEFAULT_USER_PRESERVE_BUDGET,
)

from agent_runtime.agent.context.compact import _round_starts, compact_observations
from agent_runtime.agent.context.metrics import (
    _count_reasoning,
    _view_size,
    total_chars,
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
- **Environment State**: ..."""

# Appended when a previous compaction summary exists: the new summary
# must integrate it, not replace it — earlier work state and failure
# lessons survive every compaction (same anchoring rule as Gemini CLI /
# opencode update mode).
SUMMARIZER_INTEGRATION_ADDENDUM = f"""

A PREVIOUS SNAPSHOT message — one that starts with the
"{SUMMARY_PREFIX.strip()}" prefix — is provided before the
conversation history. You must merge it into your output:
- Fold the previous "Work State", "Next Move" and "Working Context &
  Anchors" into the new snapshot; keep items that are still relevant,
  mark superseded ones as completed/obsolete, never silently drop a
  failure lesson.
- The output is the single consolidated snapshot for the whole session,
  in the same section format as before."""


def _is_summary_message(message: dict[str, Any]) -> bool:
    """Whether ``message`` is a machine-generated compaction summary.

    Summaries ride the request view as ``user`` messages so every provider
    accepts them, which makes them indistinguishable from real user turns
    by role alone.  The marker prefix is the only reliable discriminator;
    keep that test in one place so no caller mistakes a snapshot for user
    speech or preserves it as an authoritative user message.
    """
    return (message.get("role") == "user"
            and str(message.get("content", "")).startswith(SUMMARY_PREFIX))


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
    # A factory (e.g. the provider builder) is called on first use; a
    # ready adapter exposes ``chat`` and is used as-is.
    if callable(llm) and not hasattr(llm, "chat"):
        return llm()
    return llm


def _strip_leading_system(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop any leading ``system`` messages (the agent's persona/system prompt).

    The summarizer must see the interaction *history*, not be re-primed with the
    agent's own system prompt, which would make it re-enter the agent/tool-calling
    role and emit a tool call instead of a summary.
    """
    index = 0
    while index < len(messages) and messages[index].get("role") == "system":
        index += 1
    return messages[index:]


async def _generate_summary(messages: list[dict[str, Any]], *,
                            previous_summary: str | None,
                            summary_max_tokens: int,
                            llm: Any = None,
                            prompt: str | None = None) -> tuple[str | None, tuple[int, int]]:
    """Ask the LLM for a structured summary of the compaction segment.

    The payload is assembled as: a fixed summarizer-identity system message, the
    raw interaction history (agent system prompt stripped), optionally the
    previous snapshot as a leading user message, and a final user
    instruction carrying the extraction schema. Ordering the instruction last
    exploits recency to dominate the model's output, and removing the agent's
    system prompt stops the model from imitating the agent's tool calls.

    ``summary_max_tokens`` caps the summarizer's output at the request
    level (pi-style bound: a snapshot is a checkpoint, not a retelling).
    Exceptions propagate to the caller, which records them on the trace
    and falls back to the deterministic compactor instead of aborting
    the turn.
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
    response = await model.chat(summarizer_messages, tools=None,
                                max_tokens=summary_max_tokens)
    return (response.get("content") or "", response_usage(response))


_REQUIRED_SECTIONS = ["## 1. Work State", "## 2. Next Move", "## 3. Working Context & Anchors"]


def _validate_summary(summary: str, *,
                      summary_max_tokens: int = DEFAULT_SUMMARY_MAX_TOKENS) -> None:
    """Raise ``ValueError`` when ``summary`` does not look like a valid
    structured summary produced by the summarizer.

    A valid summary must be non-empty, contain all three required sections,
    and stay within the output cap — a summary that grows toward the
    history it replaces defeats compaction (Gemini CLI rejects an
    inflated summary outright; we do the same at roughly 6 chars per
    token, generous to CJK text where the ratio is closer to 2-3).
    section headers.
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
                            boundary: int = 0,
                            previous_summary: str | None = None,
                            unit: str = "chars",
                            usage_anchor: tuple[int, int, int] | None = None,
                            exempt_tools: tuple[str, ...] = (),
                            prompt: str | None = None,
                            trace: Any = None,
                            iteration: int | None = None,
                            llm: Any = None,
                            count_reasoning: bool | None = None,
                            usage_sink: Any = None,
                            ) -> list[dict[str, Any]]:
    """Return a summarized request view of ``messages`` once over ``budget``.

    Structure: ``leading + [summary] + preserved user messages + tail``.

    * ``leading`` — the system prompt and the original task (round 0);
      it anchors the final answer to the task definition and never moves,
      which also keeps the request prefix stable across compactions.
    * summary — a structured snapshot injected as a ``user`` message.
    * preserved users — mid-conversation user messages kept VERBATIM
      (newest first within ``user_preserve_budget`` chars, either budget
      unit).  User messages are
      the authoritative record of intent; summarising them risks the
      next reasoning step missing a mid-session redirect (opencode /
      Codex keep them verbatim for the same reason).  Messages beyond
      the budget feed the summarizer instead of being dropped.
    * tail — the most recent ``tail_rounds`` rounds verbatim, preserving
      tool-call pairs and short-term coherence.

    Incremental input: with ``boundary`` (the messages length when the
    previous summary was produced) and ``previous_summary``, the
    summarizer reads only ``messages[boundary:]`` plus that snapshot and
    must integrate it — a second compaction costs O(new segment), not
    O(full history).  Below budget, or when nothing older can collapse,
    the input list is returned unchanged; summarizer failure or an
    invalid/oversized summary falls back to ``compact_observations``.
    Observability: the summary text is emitted on ``trace`` (per-run)
    rather than written to a shared file.
    """
    count = _count_reasoning(count_reasoning)
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

    # A snapshot must never enter the source history; filter it so an
    # imported one cannot freeze the incremental boundary via stale text.
    leading = [message for message in messages[:starts[0]]
               if not _is_summary_message(message)]
    tail_start = starts[-tail_rounds]
    tail = messages[tail_start:]

    segment_start = max(starts[0], boundary)
    segment = [message for message in messages[segment_start:tail_start]
               if not _is_summary_message(message)]

    # user_preserve_budget stays in chars under either unit: routing it
    # through the anchor ratio would add conversion noise to a coarse guard.
    preserved: list[dict[str, Any]] = []
    novel: list[dict[str, Any]] = []
    user_chars = 0
    for message in reversed(segment):
        if message.get("role") == "user":
            content = str(message.get("content", ""))
            if user_chars + len(content) <= user_preserve_budget:
                preserved.append(message)
                user_chars += len(content)
                continue
        novel.append(message)
    novel.reverse()
    preserved.reverse()  # restore chronological order
    # The snapshot must cover the verbatim-kept users so the boundary
    # can safely advance past them.

    summarizer_input = list(segment)

    if not novel:
        if previous_summary is not None:
            summary_msg = {"role": "user",
                           "content": SUMMARY_PREFIX + previous_summary}
            return leading + [summary_msg] + preserved + tail
        return messages

    summary: str | None = None
    summarizer_prompt_tokens = 0
    summarizer_completion_tokens = 0
    used_capped_input = False
    full_input_chars = total_chars(summarizer_input, count)
    for attempt in (1, 2):
        attempt_input = summarizer_input
        if attempt == 2 and full_input_chars > DEFAULT_SUMMARIZER_INPUT_CAP_CHARS:
            # Retry on a deterministically folded copy: same pairs/users,
            # old observations shrunk — attacks the likely cause.
            attempt_input = compact_observations(
                summarizer_input, budget=DEFAULT_SUMMARIZER_INPUT_CAP_CHARS,
                exempt_tools=exempt_tools, count_reasoning=count)
            used_capped_input = True
        try:
            summary, (attempt_prompt, attempt_completion) = await _generate_summary(
                attempt_input, previous_summary=previous_summary,
                summary_max_tokens=summary_max_tokens, prompt=prompt, llm=llm)
            summarizer_prompt_tokens += attempt_prompt
            summarizer_completion_tokens += attempt_completion
            if usage_sink is not None:
                try:
                    result_sink = usage_sink(attempt_prompt, attempt_completion)
                    if hasattr(result_sink, "__await__"):
                        await result_sink
                except Exception:
                    logger.debug("context.summary.usage_sink_failed",
                                 exc_info=True)
        except Exception as exc:
            # Billed call failed before producing a snapshot; fall back.
            if trace is not None:
                trace.emit("context.llm_summary.error", iteration,
                           error=str(exc)[:500], error_type=type(exc).__name__,
                           summarizer_input_chars=full_input_chars,
                           summarizer_input_chars_capped=total_chars(
                               attempt_input, count),
                           input_capped=used_capped_input,
                           summarizer_input_count=len(summarizer_input),
                           boundary=boundary, char_budget=char_budget,
                           summary_max_tokens=summary_max_tokens)
            logger.warning("context.summary.failed falling back to "
                           "compact_observations: %s", exc)
            return compact_observations(messages, budget=char_budget,
                                        exempt_tools=exempt_tools,
                                        count_reasoning=count)
        try:
            _validate_summary(summary, summary_max_tokens=summary_max_tokens)
            break
        except ValueError as exc:
            # Unusable snapshot (flaky non-compliance); retry once, then fall back.
            if trace is not None:
                trace.emit("context.llm_summary.invalid", iteration,
                           reason=str(exc)[:500], attempt=attempt,
                           summary_chars=len(summary or ""),
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
                           summary_max_tokens=summary_max_tokens)
            if attempt == 2:
                logger.warning("context.summary.invalid %s falling back to compact_observations", exc)
                return compact_observations(messages, budget=char_budget,
                                            exempt_tools=exempt_tools,
                                            count_reasoning=count)
            logger.warning("context.summary.invalid %s retrying (%d/2)",
                           exc, attempt + 1)
    summary_msg = {"role": "user", "content": SUMMARY_PREFIX + summary}
    view = leading + [summary_msg] + preserved + tail
    if trace is not None:
        trace.emit("context.llm_summary.yield", iteration,
                   summary=summary, preserved_users=len(preserved),
                   summarizer_input_count=len(summarizer_input),
                   input_capped=used_capped_input,
                   boundary=boundary, char_budget=char_budget,
                   prompt_tokens=summarizer_prompt_tokens,
                   completion_tokens=summarizer_completion_tokens,
                   total_tokens=(summarizer_prompt_tokens
                                 + summarizer_completion_tokens))
    return view


__all__ = [
    "SUMMARY_PREFIX", "SUMMARIZER_INSTRUCTION",
    "extract_summary", "summarize_history",
]
