"""Deterministic observation compaction: LLM-free windowed folding.

Three passes, oldest first — see :func:`compact_observations` for the
full policy. Message structure is preserved so the result is always a
protocol-legal sequence.
"""

from __future__ import annotations

from typing import Any

from agent_runtime.harness import (
    DEFAULT_CONTEXT_BUDGET,
    DEFAULT_HEAD_CHARS,
    DEFAULT_KEEP_RECENT_ROUNDS,
    DEFAULT_WINDOW_ROUNDS,
)

from agent_runtime.agent.context.metrics import _count_reasoning, total_chars


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
                         exempt_tools: tuple[str, ...] = (),
                         count_reasoning: bool | None = None,
                         ) -> list[dict[str, Any]]:
    """Return a compacted copy of ``messages`` when it exceeds ``budget``.

    Three passes, oldest first:

    1. Compact tool observations in rounds older than the last
       ``window_rounds`` rounds (the comfort zone that is normally kept
       intact), and drop those rounds' assistant reasoning to a one-space
       pad the same way (stale chain-of-thought is replayed whole yet
       rarely reused; a truncated fragment would mislead, so it goes
       wholesale — the key stays for echo contracts).
       Observations of ``exempt_tools`` survive pass 1 verbatim.
    2. If the request would still exceed ``budget``, keep compacting
       tool observations round by round from the oldest round still
       intact — but never inside the most recent ``keep_recent_rounds``
       rounds.  Those are the active working context; if they alone
       exceed the budget the budget yields, not the context.
       Exempt observations still survive: retrieval anchors (grep/read
       results) outlive non-exempt ones so multi-step reasoning keeps
       its references without a re-query round (Gemini CLI exempts
       retrieval tools from its collapse layer for the same reason).
    3. If the budget is still exceeded after pass 2 exhausted the
       non-exempt rounds, exempt observations fold too, using the same
       ``head_chars``.  Exemption is a fold order, never an exemption
       from the budget.

    Reasoning chars count toward ``budget`` only when the echo policy
    will actually replay them on the wire (see the shared wire policy).
    Below budget the original list is
    returned unchanged.
    """
    count = _count_reasoning(count_reasoning)
    if total_chars(messages, count) <= budget:
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

    exempt = set(exempt_tools)

    def _compact_at(cutoff: int,
                    fold_exempt: bool = False) -> tuple[list[dict[str, Any]], bool]:
        """Compact tool messages and stale reasoning in rounds 1..cutoff."""
        result: list[dict[str, Any]] = []
        compacted_any = False
        for index, message in enumerate(messages):
            role = message.get("role")
            if role == "tool" and 0 < _round_of(index) <= cutoff:
                tool = _tool_name_for(messages, index)
                if not fold_exempt and tool in exempt:
                    result.append(message)
                    continue
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
    # Exempt observations keep surviving this pass.
    cutoff = window_cutoff
    while (total_chars(result, count) > budget
           and cutoff < never_touch_from_round):
        cutoff += 1
        result, compacted_any = _compact_at(cutoff)
        if not compacted_any:
            break

    # Pass 3: budget still exceeded — exempt observations fold too.
    # The exemption is a fold ORDER (retrieval anchors fold last), not
    # an exemption from the budget; keep_recent_rounds still yields
    # nothing (the budget yields to the working core, as before).
    if exempt and total_chars(result, count) > budget:
        result, compacted_any = _compact_at(cutoff, fold_exempt=True)
    return result


__all__ = ["compact_observations"]
