"""Context management strategies: the request view of conversation history.

The conversation itself is the source of truth and is never mutated by
these strategies.  A strategy only produces the *view* of the history
that gets sent to the LLM on each request, which keeps the on-disk
trace, future phases (summarisation), and post-hoc analysis intact.

``compact_observations`` is a deterministic, LLM-free window that
replaces stale tool observations with one-line summaries once the request
would exceed the context budget.  Message structure is preserved — a
compacted tool message stays a tool message with the same
``tool_call_id`` — so the result is always a protocol-legal sequence.

All knobs (budgets, windows, tail sizes) come from the harness's
``MemoryGenome`` (see ``agent_runtime.harness``), threaded in by the
caller; this module holds no policy constants of its own.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from agent_runtime.harness import (
    DEFAULT_CONTEXT_BUDGET,
    DEFAULT_HEAD_CHARS,
    DEFAULT_KEEP_RECENT_ROUNDS,
    DEFAULT_SUMMARY_MAX_TOKENS,
    DEFAULT_SUMMARY_TAIL_ROUNDS,
    DEFAULT_USER_PRESERVE_BUDGET,
    DEFAULT_WINDOW_ROUNDS,
    MemoryGenome,
)

logger = logging.getLogger(__name__)

STRATEGY_FULL_HISTORY = "full_history"
STRATEGY_COMPACT_OBSERVATIONS = "compact_observations"
STRATEGY_LLM_SUMMARY = "llm_summary"

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
SUMMARIZER_INTEGRATION_ADDENDUM = """

A PREVIOUS SNAPSHOT follows the conversation history (or is interleaved
with it). You must merge it into your output:
- Fold the previous "Work State", "Next Move" and "Working Context &
  Anchors" into the new snapshot; keep items that are still relevant,
  mark superseded ones as completed/obsolete, never silently drop a
  failure lesson.
- The output is the single consolidated snapshot for the whole session,
  in the same section format as before."""


def _extract_summary(view: list[dict[str, Any]] | None) -> str | None:
    """The previous compaction summary text carried in a request view."""
    if not view:
        return None
    for message in view:
        if (message.get("role") == "user"
                and str(message.get("content", "")).startswith(SUMMARY_PREFIX)):
            return str(message["content"])[len(SUMMARY_PREFIX):]
    return None


def _reasoning_replayed() -> bool:
    """Whether reasoning_content rides the wire for the configured endpoint.

    Budget accounting must match the wire (providers/llm.py projection):
    reasoning only counts toward the context budget when the echo policy
    will actually replay it.  Lazy import matches ``_get_summary_llm``.
    """
    from agent_runtime.providers.llm import reasoning_echo_enabled
    return reasoning_echo_enabled()


def _message_chars(message: dict[str, Any],
                   count_reasoning: bool = True) -> int:
    # reasoning_content rides the wire on echo-family providers, so it
    # must count toward the context budget there — otherwise
    # long-thinking models outrun compaction without ever tripping it.
    # When the echo policy strips it, counting it would overestimate the
    # request and compact observations too early.
    total = len(message.get("content") or "")
    if count_reasoning:
        total += len(message.get("reasoning_content") or "")
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        total += len(function.get("arguments") or "")
    return total


def _total_chars(messages: list[dict[str, Any]],
                 count_reasoning: bool = True) -> int:
    return sum(_message_chars(message, count_reasoning)
               for message in messages)


def _estimate_tokens(chars: int, chars_per_token: float = 4.0) -> int:
    """Char-based token estimate; code-heavy mixed text runs ~3-5 c/t."""
    return max(1, int(chars / chars_per_token)) if chars else 0


def _view_size(messages: list[dict[str, Any]], count_reasoning: bool,
               unit: str,
               anchor: tuple[int, int, int] | None = None) -> int:
    """Request-view size in the genome's budget unit.

    ``chars`` (legacy) counts characters.  ``tokens`` extrapolates from
    the usage anchor ``(measured prompt tokens, view length, view chars)``
    produced by the last billed response: below the anchor's view length
    the provider's own number is the truth, above it the tail is
    estimated with the char ratio the anchor itself calibrates — no
    tokenizer dependency, and the ratio converges as measurements
    accumulate.
    """
    if unit != "tokens":
        return _total_chars(messages, count_reasoning)
    if anchor is None:
        return _estimate_tokens(_total_chars(messages, count_reasoning))
    tokens, anchor_len, anchor_chars = anchor
    ratio = tokens / anchor_chars if anchor_chars > 0 else 0.25
    if len(messages) <= anchor_len:
        # View only shrank since the measurement: scale the measured
        # number down by the calibrated ratio.
        return max(1, int(_total_chars(messages, count_reasoning) * ratio))
    tail_chars = _total_chars(messages[anchor_len:], count_reasoning)
    return tokens + int(tail_chars * ratio)


def _anchor_chars_per_token(anchor: tuple[int, int, int] | None) -> float:
    """Char-per-token ratio implied by the usage anchor (default 4.0)."""
    if anchor is None:
        return 4.0
    tokens, _, anchor_chars = anchor
    if tokens <= 0 or anchor_chars <= 0:
        return 4.0
    return anchor_chars / tokens


def request_view_size(view: list[dict[str, Any]], memory: MemoryGenome,
                      usage_anchor: tuple[int, int, int] | None = None) -> int:
    """Request-view size in the genome's unit, for gate decisions."""
    return _view_size(view, _reasoning_replayed(), memory.budget_unit,
                      usage_anchor)


def _round_starts(messages: list[dict[str, Any]]) -> list[int]:
    """Indices where each agent round begins.

    A round is anchored by its first assistant message; the initial
    user task belongs to round 0 (everything before the first assistant
    message), so it can never fall inside a compaction cutoff.
    """
    starts: list[int] = []
    previous_role = None
    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "system":
            continue
        if role == "assistant" and previous_role in (None, "user", "tool"):
            starts.append(index)
        previous_role = role
    return starts


def _drop_stale_reasoning(message: dict[str, Any]) -> dict[str, Any]:
    """Erase one assistant message's chain-of-thought for replay.

    Old-round thinking is replayed whole to echo-family providers yet has
    little reuse value for the model's next step — the visible content and
    tool trail carry the decisions.  A truncated fragment has no
    independent semantics and can mislead (a half-finished hypothesis
    reads like a settled conclusion), so the text is dropped wholesale.
    The key survives as a one-space pad: echo contracts like DeepSeek's
    require ``reasoning_content`` present on assistant messages, and an
    empty string 400s on some endpoints.  Idempotent — an already-padded
    message returns unchanged so pass-2 progress stays honest.
    """
    reasoning = message.get("reasoning_content") or ""
    if reasoning == " ":
        return message
    compacted = dict(message)
    compacted["reasoning_content"] = " "
    return compacted


def _compact_tool_message(message: dict[str, Any], tool: str,
                          head_chars: int) -> dict[str, Any]:
    content = message.get("content") or ""
    if len(content) <= head_chars:
        return message
    spill_path = (message.get("metadata") or {}).get("spill_path")
    # Spilled observations can be paged back losslessly; unspilled ones
    # only exist in the (model-invisible) full history, so re-running is
    # then the only recovery — say so honestly instead of forbidding it.
    spill_note = (f" Full output file: {spill_path} (page it with "
                  f"read_output; do NOT re-run the tool)."
                  if spill_path
                  else " The omitted middle is not recoverable without "
                       "re-running the tool — do that only if you truly "
                       "need it.")
    summary = (f"[observation compacted: {tool} returned {len(content)} chars; "
               f"first {head_chars} chars below.{spill_note}]\n")
    compacted = dict(message)
    compacted["content"] = summary + content[:head_chars]
    return compacted


def _tool_name_for(messages: list[dict[str, Any]],
                   index: int) -> str:
    """Best-effort tool name for the tool message at ``index``."""
    tool_call_id = messages[index].get("tool_call_id")
    if tool_call_id:
        for message in messages[:index]:
            for call in message.get("tool_calls") or []:
                if call.get("id") == tool_call_id:
                    return (call.get("function") or {}).get("name") or "tool"
    return "tool"


def compact_observations(messages: list[dict[str, Any]], *,
                         budget: int = DEFAULT_CONTEXT_BUDGET,
                         window_rounds: int = DEFAULT_WINDOW_ROUNDS,
                         keep_recent_rounds: int = DEFAULT_KEEP_RECENT_ROUNDS,
                         head_chars: int = DEFAULT_HEAD_CHARS,
                         ) -> list[dict[str, Any]]:
    """Return a compacted copy of ``messages`` when it exceeds ``budget``.

    Two passes, oldest first:

    1. Compact tool observations in rounds older than the last
       ``window_rounds`` rounds (the comfort zone that is normally kept
       intact), and drop those rounds' assistant reasoning to a one-space
       pad the same way (stale chain-of-thought is replayed whole yet
       rarely reused; a truncated fragment would mislead, so it goes
       wholesale — the key stays for echo contracts).
    2. If the request would still exceed ``budget``, keep compacting
       tool observations round by round from the oldest round still
       intact — but never inside the most recent ``keep_recent_rounds``
       rounds.  Those are the active working context; if they alone
       exceed the budget the budget yields, not the context.

    Reasoning chars count toward ``budget`` only when the echo policy
    will actually replay them on the wire (see :func:`_reasoning_replayed`
    — the single source of truth).  Below budget the original list is
    returned unchanged.
    """
    count = _reasoning_replayed()
    if _total_chars(messages, count) <= budget:
        return messages
    starts = _round_starts(messages)
    if not starts:
        return messages
    total_rounds = len(starts)
    never_touch_from_round = max(0, total_rounds - keep_recent_rounds)

    def _round_of(index: int) -> int:
        position = 0
        for start in starts:
            if start > index:
                break
            position += 1
        return position

    def _compact_at(cutoff: int) -> tuple[list[dict[str, Any]], bool]:
        """Compact tool messages and stale reasoning in rounds 1..cutoff."""
        result: list[dict[str, Any]] = []
        compacted_any = False
        for index, message in enumerate(messages):
            role = message.get("role")
            if role == "tool" and 0 < _round_of(index) <= cutoff:
                tool = _tool_name_for(messages, index)
                compacted = _compact_tool_message(message, tool, head_chars)
                if compacted is not message:
                    compacted_any = True
                result.append(compacted)
            elif (role == "assistant" and 0 < _round_of(index) <= cutoff
                    and message.get("reasoning_content")):
                compacted = _drop_stale_reasoning(message)
                if compacted is not message:
                    compacted_any = True
                result.append(compacted)
            else:
                result.append(message)
        return result, compacted_any

    # Pass 1: everything outside the comfort window.
    window_cutoff = max(0, total_rounds - window_rounds)
    result, _ = _compact_at(window_cutoff)

    # Pass 2: under budget pressure, shrink the window round by round
    # until the budget is met or only the keep-recent core remains.
    cutoff = window_cutoff
    while (_total_chars(result, count) > budget
           and cutoff < never_touch_from_round):
        cutoff += 1
        result, compacted_any = _compact_at(cutoff)
        if not compacted_any:
            break
    return result


async def apply_memory_strategy(messages: list[dict[str, Any]], memory: MemoryGenome, *,
                                compact_messages: list[dict[str, Any]] | None = None,
                                trace: Any = None, iteration: int | None = None,
                                boundary: int = 0,
                                usage_anchor: tuple[int, int, int] | None = None,
                                force_budget: int | None = None,
                                **options: Any) -> list[dict[str, Any]]:
    """Build the request view of ``messages`` under the memory genome.

    ``memory`` is the harness's :class:`MemoryGenome` — strategy plus all
    view-shaping knobs (budget, windows, tail sizes); this adapter unpacks
    it into the strategy functions, which stay pure and parameter-driven.

    ``messages`` is the FULL conversation (the source of truth, never mutated
    here). ``compact_messages`` is the working/compacted view carried across
    turns: it is used *only* to decide whether a re-compaction is due (its size
    vs the context budget), while the actual compaction always summarizes or
    compacts the FULL history. This keeps the per-turn request bounded and
    avoids re-compacting on every iteration.

    When a re-compaction actually produces a new view, a ``memory.{strategy}``
    event is emitted on ``trace`` (if provided) so the caller stays free of
    strategy logic.

    Reasoning chars are budgeted only when the echo policy will replay
    them on the wire (see :func:`_reasoning_replayed`).
    """
    strategy = memory.strategy
    if strategy == STRATEGY_FULL_HISTORY:
        return messages
    if strategy not in (STRATEGY_COMPACT_OBSERVATIONS, STRATEGY_LLM_SUMMARY):
        raise ValueError(f"unknown memory strategy: {strategy!r}")

    count = _reasoning_replayed()
    unit = memory.budget_unit
    # force_budget (context-window hard gate) must push below the size the
    # provider just rejected, so it wins whenever it is the tighter bound.
    budget = memory.context_budget
    if force_budget is not None:
        budget = min(budget, force_budget)
    working = compact_messages if compact_messages is not None else messages
    if _view_size(working, count, unit, usage_anchor) <= budget:
        return working

    if strategy == STRATEGY_COMPACT_OBSERVATIONS:
        # compact_observations is char-metered by construction; under the
        # tokens unit the budget is converted with the anchor-calibrated
        # ratio (a threshold decision, so conversion noise is acceptable).
        char_budget = budget
        if unit == "tokens":
            char_budget = max(1, int(budget * _anchor_chars_per_token(usage_anchor)))
        result = compact_observations(messages, budget=char_budget,
                                      window_rounds=memory.window_rounds,
                                      keep_recent_rounds=memory.keep_recent_rounds,
                                      head_chars=memory.head_chars)
    else:  # STRATEGY_LLM_SUMMARY
        previous_summary = _extract_summary(working)
        result = await summarize_history(
            messages, budget=budget,
            tail_rounds=memory.summary_tail_rounds,
            user_preserve_budget=memory.user_preserve_budget,
            summary_max_tokens=memory.summary_max_tokens,
            boundary=boundary,
            previous_summary=previous_summary,
            unit=unit, usage_anchor=usage_anchor,
            prompt=options.get("prompt"), trace=trace,
            iteration=iteration)

    if trace is not None and result is not working and result is not messages:
        trace.emit(f"memory.{strategy}", iteration,
                   original_chars=_total_chars(messages, count),
                   request_chars=_total_chars(result, count),
                   original_count=len(messages),
                   request_count=len(result),
                   budget_unit=unit, budget=budget)
        summary = _extract_summary(result)
        if summary is not None:
            # The summary text lives on the trace (per-run, queryable)
            # instead of a shared on-disk file.
            trace.emit(f"memory.{strategy}.text", iteration, summary=summary)
    return result


_summary_llm: Any = None


def set_summary_llm(llm: Any) -> None:
    """Override the summarizer LLM (used by tests and custom wiring)."""
    global _summary_llm
    _summary_llm = llm


def _get_summary_llm() -> Any:
    """Lazily build the summarizer LLM used by ``llm_summary``.

    A dedicated model can be selected via ``AGENT_RUNTIME_SUMMARY_MODEL`` so
    summarization can run on a different (e.g. cheaper/faster) model than the
    agent's main LLM; it otherwise falls back to the default model.
    """
    global _summary_llm
    if _summary_llm is None:
        from agent_runtime.providers.llm import OpenAICompatibleLLM
        _summary_llm = OpenAICompatibleLLM(
            model=os.getenv("AGENT_RUNTIME_SUMMARY_MODEL") or None)
    return _summary_llm


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
                            prompt: str | None = None) -> str | None:
    """Ask the LLM for a structured summary of the compaction segment.

    The payload is assembled as: a fixed summarizer-identity system message, the
    raw interaction history (agent system prompt stripped), optionally the
    previous snapshot as a leading user message, and a final user
    instruction carrying the extraction schema. Ordering the instruction last
    exploits recency to dominate the model's output, and removing the agent's
    system prompt stops the model from imitating the agent's tool calls.

    ``summary_max_tokens`` caps the summarizer's output at the request
    level (pi-style bound: a snapshot is a checkpoint, not a retelling).
    An exception from the summarizer LLM is returned as ``None`` so the
    caller can fall back to the deterministic compactor instead of
    aborting the turn.
    """
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
    model = llm if llm is not None else await _get_summary_llm_async()
    try:
        response = await model.chat(summarizer_messages, tools=None,
                                    max_tokens=summary_max_tokens)
    except Exception as exc:
        logger.warning("memory.summary.llm_error %s", exc)
        return None
    return response.get("content") or ""


async def _get_summary_llm_async() -> Any:
    """Lazily build the summarizer LLM (async variant for tests)."""
    if _summary_llm is None:
        set_summary_llm(_build_summary_llm())
    return _summary_llm


def _build_summary_llm() -> Any:
    """Construct the default summarizer provider."""
    from agent_runtime.providers.llm import OpenAICompatibleLLM
    return OpenAICompatibleLLM(
        model=os.getenv("AGENT_RUNTIME_SUMMARY_MODEL") or None)


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
                            prompt: str | None = None,
                            trace: Any = None,
                            iteration: int | None = None,
                            ) -> list[dict[str, Any]]:
    """Return a summarized request view of ``messages`` once over ``budget``.

    Structure: ``leading + [summary] + preserved user messages + tail``.

    * ``leading`` — the system prompt and the original task (round 0);
      it anchors the final answer to the task definition and never moves,
      which also keeps the request prefix stable across compactions.
    * summary — a structured snapshot injected as a ``user`` message.
    * preserved users — mid-conversation user messages kept VERBATIM
      (newest first within ``user_preserve_budget``).  User messages are
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
    count = _reasoning_replayed()
    if unit == "tokens":
        chars_per_token = _anchor_chars_per_token(usage_anchor)
        char_budget = int((budget or DEFAULT_CONTEXT_BUDGET) * chars_per_token)
    else:
        char_budget = budget or DEFAULT_CONTEXT_BUDGET
    if tail_rounds is None:
        tail_rounds = DEFAULT_SUMMARY_TAIL_ROUNDS

    starts = _round_starts(messages)
    if len(starts) < 2:
        return messages
    # Below budget: the view is already small enough — identity.
    if _view_size(messages, count, unit, usage_anchor) <= (budget or DEFAULT_CONTEXT_BUDGET):
        return messages

    leading = messages[:starts[0]]
    tail_start = starts[-tail_rounds]
    tail = messages[tail_start:]

    # Mid-segment: the part between the leading task and the verbatim
    # tail, restricted to what the previous summary has NOT already
    # absorbed (incremental boundary).
    segment_start = max(starts[0], boundary)
    segment = messages[segment_start:tail_start]

    # Preserve user messages verbatim, newest first within the budget;
    # overflow users (oldest, past the budget) join the summarizer input.
    preserved: list[dict[str, Any]] = []
    summarizer_input: list[dict[str, Any]] = []
    user_chars = 0
    for message in reversed(segment):
        if message.get("role") == "user":
            content = str(message.get("content", ""))
            if user_chars + len(content) <= user_preserve_budget:
                preserved.append(message)
                user_chars += len(content)
                continue
        summarizer_input.append(message)
    summarizer_input.reverse()
    preserved.reverse()  # restore chronological order

    if not summarizer_input:
        # Nothing new to summarise: keep the previous snapshot in place.
        if previous_summary is not None:
            summary_msg = {"role": "user",
                           "content": SUMMARY_PREFIX + previous_summary}
            return leading + [summary_msg] + preserved + tail
        return messages

    summary = await _generate_summary(
        summarizer_input, previous_summary=previous_summary,
        summary_max_tokens=summary_max_tokens)
    if summary is None:
        logger.warning("memory.summary.failed falling back to compact_observations")
        return compact_observations(messages, budget=char_budget)
    try:
        _validate_summary(summary, summary_max_tokens=summary_max_tokens)
    except ValueError as exc:
        logger.warning("memory.summary.invalid %s falling back to compact_observations", exc)
        return compact_observations(messages, budget=char_budget)
    summary_msg = {"role": "user", "content": SUMMARY_PREFIX + summary}
    view = leading + [summary_msg] + preserved + tail
    if trace is not None:
        trace.emit("memory.llm_summary.yield", iteration,
                   summary=summary, preserved_users=len(preserved),
                   summarizer_input_count=len(summarizer_input),
                   boundary=boundary, char_budget=char_budget)
    return view


__all__ = [
    "DEFAULT_CONTEXT_BUDGET", "DEFAULT_HEAD_CHARS", "DEFAULT_SUMMARY_TAIL_ROUNDS",
    "DEFAULT_KEEP_RECENT_ROUNDS", "DEFAULT_WINDOW_ROUNDS",
    "DEFAULT_USER_PRESERVE_BUDGET", "DEFAULT_SUMMARY_MAX_TOKENS",
    "STRATEGY_COMPACT_OBSERVATIONS", "STRATEGY_FULL_HISTORY", "STRATEGY_LLM_SUMMARY",
    "SUMMARY_PREFIX", "SUMMARIZER_INSTRUCTION", "request_view_size",
    "apply_memory_strategy", "compact_observations", "set_summary_llm",
    "summarize_history",
]
