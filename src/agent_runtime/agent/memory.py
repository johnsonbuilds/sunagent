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

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from agent_runtime.harness import (
    DEFAULT_CONTEXT_BUDGET,
    DEFAULT_HEAD_CHARS,
    DEFAULT_KEEP_RECENT_ROUNDS,
    DEFAULT_SUMMARY_TAIL_ROUNDS,
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
    budget = memory.context_budget
    working = compact_messages if compact_messages is not None else messages
    if _total_chars(working, count) <= budget:
        return working

    if strategy == STRATEGY_COMPACT_OBSERVATIONS:
        result = compact_observations(messages, budget=budget,
                                      window_rounds=memory.window_rounds,
                                      keep_recent_rounds=memory.keep_recent_rounds,
                                      head_chars=memory.head_chars)
    else:  # STRATEGY_LLM_SUMMARY
        result = await summarize_history(
            messages, budget=budget,
            tail_rounds=memory.summary_tail_rounds,
            prompt=options.get("prompt"),
            session_memory_path=options.get("session_memory_path"))

    if trace is not None and result is not working and result is not messages:
        trace.emit(f"memory.{strategy}", iteration,
                   original_chars=_total_chars(messages, count),
                   request_chars=_total_chars(result, count),
                   original_count=len(messages),
                   request_count=len(result))
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


def _session_memory_path() -> Path | None:
    """Default on-disk location for the latest session summary.

    ``AGENT_RUNTIME_WORKSPACE`` selects the root; the summary is written as
    ``<workspace>/memory/session_memory.md`` (one file per task, overwritten).
    """
    workspace = os.getenv("AGENT_RUNTIME_WORKSPACE") or "."
    return Path(workspace) / "memory" / "session_memory.md"


def _write_session_memory(content: str, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


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


async def _generate_summary(messages: list[dict[str, Any]], llm: Any,
                            prompt: str | None) -> str:
    """Ask the LLM for a structured summary of the full conversation.

    The payload is assembled as: a fixed summarizer-identity system message, the
    raw interaction history (agent system prompt stripped), and a final user
    instruction carrying the extraction schema. Ordering the instruction last
    exploits recency to dominate the model's output, and removing the agent's
    system prompt stops the model from imitating the agent's tool calls.
    """
    history = _strip_leading_system(messages)
    instruction = prompt or SUMMARIZER_INSTRUCTION
    summarizer_messages: list[dict[str, Any]] = [
        {"role": "system", "content": SUMMARIZER_SYSTEM},
        *history,
        {"role": "user", "content": instruction},
    ]
    response = await llm.chat(summarizer_messages, tools=None)
    return response.get("content") or ""


_REQUIRED_SECTIONS = ["## 1. Work State", "## 2. Next Move", "## 3. Working Context & Anchors"]


def _validate_summary(summary: str) -> None:
    """Raise ``ValueError`` when ``summary`` does not look like a valid
    structured summary produced by the summarizer.

    A valid summary must be non-empty and contain all three required
    section headers.
    """
    stripped = summary.strip()
    if not stripped:
        raise ValueError("summary is empty")
    for section in _REQUIRED_SECTIONS:
        if section not in stripped:
            raise ValueError(f"summary missing required section {section!r}")


async def summarize_history(messages: list[dict[str, Any]], *,
                            budget: int | None = None,
                            tail_rounds: int | None = None,
                            prompt: str | None = None,
                            session_memory_path: Path | None = None
                            ) -> list[dict[str, Any]]:
    """Return a summarized copy of ``messages`` once over ``budget``.

    The summarizer reads the FULL history and produces a structured summary
    (see ``SUMMARIZER_INSTRUCTION``). The request view keeps the system
    prompt and original task, injects the summary as a ``user`` message, and
    appends the most recent ``tail_rounds`` rounds verbatim (preserving
    tool-call pairs and short-term coherence). The summary is also written to
    ``session_memory.md`` (overwriting any prior summary for this task).

    Below budget, or when there are no older rounds to collapse, the original
    list is returned unchanged. If the summarizer LLM call fails *or* produces
    an invalid summary (missing sections), the strategy falls back to the
    deterministic ``compact_observations`` so the turn still proceeds.
    Reasoning chars are budgeted only when the echo policy will replay
    them on the wire (see :func:`_reasoning_replayed`).
    """
    if budget is None:
        budget = DEFAULT_CONTEXT_BUDGET
    if tail_rounds is None:
        tail_rounds = DEFAULT_SUMMARY_TAIL_ROUNDS
    count = _reasoning_replayed()
    llm = _get_summary_llm()
    if _total_chars(messages, count) <= budget:
        return messages
    starts = _round_starts(messages)
    total_rounds = len(starts)
    if total_rounds <= tail_rounds:
        return messages
    logger.debug("memory.summary.start rounds=%d tail=%d", total_rounds, tail_rounds)
    try:
        summary = await _generate_summary(messages, llm, prompt)
    except Exception:
        logger.warning("memory.summary.failed falling back to compact_observations")
        return compact_observations(messages, budget=budget)
    try:
        _validate_summary(summary)
    except ValueError as exc:
        logger.warning("memory.summary.invalid %s falling back to compact_observations", exc)
        return compact_observations(messages, budget=budget)
    leading = messages[:starts[0]]
    tail_start = starts[-tail_rounds]
    tail = messages[tail_start:]
    summary_msg = {"role": "user", "content": SUMMARY_PREFIX + summary}
    view = leading + [summary_msg] + tail
    path = session_memory_path or _session_memory_path()
    if path is not None:
        await asyncio.to_thread(_write_session_memory, summary, path)
    return view


__all__ = [
    "DEFAULT_CONTEXT_BUDGET", "DEFAULT_HEAD_CHARS", "DEFAULT_SUMMARY_TAIL_ROUNDS",
    "DEFAULT_KEEP_RECENT_ROUNDS", "DEFAULT_WINDOW_ROUNDS",
    "STRATEGY_COMPACT_OBSERVATIONS", "STRATEGY_FULL_HISTORY", "STRATEGY_LLM_SUMMARY",
    "apply_memory_strategy", "compact_observations", "set_summary_llm",
    "summarize_history",
]
