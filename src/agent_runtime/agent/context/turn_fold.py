"""Turn-fold view: completed turns collapse to user + final answer.

Same conversation, view-level folding only. The full ``messages`` history
is never mutated here; this module returns the *request view* the LLM
sees:

* leading ``system`` messages: verbatim;
* each COMPLETED run turn: the task ``user`` message verbatim plus the
  turn's last assistant message with substantive content (tool calls,
  reasoning and metadata stripped);
* the CURRENT turn (from the last task user message onward): verbatim,
  including all intermediate assistant / tool messages.

A "task user" is a real user turn boundary. Loop-internal ``user``-role
nudges (iteration-limit notice, submit/contract nudges, reasoning-budget
and budget-reminder nudges, compaction snapshots) carry a runtime
provenance marker and never open a turn.
"""

from __future__ import annotations

from typing import Any

from agent_runtime.agent.runtime_message import is_runtime_user_message


def _is_task_user(message: dict[str, Any]) -> bool:
    """Whether ``message`` opens a new run turn."""
    if message.get("role") != "user":
        return False
    if is_runtime_user_message(message):
        return False
    return bool(str(message.get("content") or "").strip())


def _last_final_content(segment: list[dict[str, Any]]) -> str | None:
    """Last assistant message with substantive content in ``segment``."""
    for message in reversed(segment):
        if message.get("role") != "assistant":
            continue
        content = str(message.get("content") or "")
        if content.strip():
            return content
    return None


def turn_fold_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold completed turns to user + final, keep the current turn whole."""
    task_starts = [i for i, m in enumerate(messages) if _is_task_user(m)]
    if len(task_starts) < 2:
        return messages
    view: list[dict[str, Any]] = []
    # Everything before the first task user (system prompt etc.).
    view.extend(messages[:task_starts[0]])
    # Completed turns: user verbatim + stripped final.
    for pos, start in enumerate(task_starts[:-1]):
        end = task_starts[pos + 1]
        view.append(messages[start])
        final = _last_final_content(messages[start:end])
        if final is not None:
            view.append({"role": "assistant", "content": final})
    # Current turn: verbatim from its task user onward.
    view.extend(messages[task_starts[-1]:])
    return view


__all__ = ["turn_fold_messages"]
