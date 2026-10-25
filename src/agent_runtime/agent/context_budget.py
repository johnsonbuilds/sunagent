"""Usage-based budget state machine for the self-managed context strategy.

Char-based (same ruler as ``agent.memory`` via ``context_mirror``): the
request view is measured, tiered nudges fire as it fills, and overflow
triggers rollback-retry or a final turn — never silent truncation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from agent_runtime.agent.context_mirror import total_chars


@dataclass
class BudgetDecision:
    action: str  # ok|nudge_tier|nudge_urgent|rollback|final_turn|stop
    used_chars: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    log: dict[str, Any] | None = None


def _content_chars(message: dict[str, Any]) -> int:
    return total_chars([message])


class ContextBudget:
    """Stateful budget gate; one instance per agent turn."""

    def __init__(self, limit_chars: int, reserve_chars: int = 0,
                 nudge_ratios: tuple[float, ...] = (0.25, 0.5, 0.75),
                 urgent_mult: float = 2.0,
                 retry_margin_chars: int = 0,
                 max_rollback_retries: int = 8,
                 protect: int = 2) -> None:
        self.limit = int(limit_chars)
        reserve = int(reserve_chars)
        if reserve <= 0 and self.limit > 0:
            reserve = int(self.limit * 0.2)
        self.reserve = reserve
        self.strict = max(0, self.limit - self.reserve)
        self.nudge_ratios = sorted(f for f in nudge_ratios if 0.0 < f < 1.0)
        self.urgent_mult = float(urgent_mult)
        margin = int(retry_margin_chars)
        if margin <= 0 and self.strict > 0:
            margin = int(self.strict * 0.1)
        self.retry_margin = margin
        self.max_retries = max(0, int(max_rollback_retries))
        self.protect = int(protect)
        self.n_retries = 0
        self.nudged: set[float] = set()
        self.finalized = False

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def used(self, view: list[dict[str, Any]]) -> int:
        return total_chars(view)

    def evaluate(self, view: list[dict[str, Any]]) -> BudgetDecision:
        if not self.enabled:
            return BudgetDecision("ok")
        used = self.used(view)
        # Re-arm tiers / final grant compacted back below.
        self.nudged = {f for f in self.nudged if used >= int(self.limit * f)}
        if used > self.strict:
            if (self.n_retries < self.max_retries and self.strict > 0
                    and len(view) > self.protect):
                self.n_retries += 1
                return BudgetDecision("rollback", used,
                                      log={"rollback_retry": self.n_retries,
                                           "used_chars": used})
            if self.finalized:
                return BudgetDecision("stop", used,
                                      log={"budget_stop": True,
                                           "used_chars": used})
            self.finalized = True
            return BudgetDecision(
                "final_turn", used,
                messages=[{"role": "user", "content": self.final_message(used)}],
                log={"budget_final_turn": True, "used_chars": used})
        self.finalized = False
        need, recent = self._urgent_need(view)
        if self.strict - used < need:
            return BudgetDecision(
                "nudge_urgent", used,
                messages=[{"role": "user",
                           "content": self.urgent_message(used, need)}],
                log={"budget_nudge": "urgent", "used_chars": used,
                     "urgent_need": need, "recent_obs": recent})
        crossed = [f for f in self.nudge_ratios
                   if used >= int(self.limit * f)]
        fresh = [f for f in crossed if f not in self.nudged]
        if fresh:
            tier = max(fresh)
            self.nudged |= set(crossed)
            return BudgetDecision(
                "nudge_tier", used,
                messages=[{"role": "user",
                           "content": self.tier_message(tier, used)}],
                log={"budget_nudge": tier, "used_chars": used})
        return BudgetDecision("ok", used)

    def _urgent_need(self, view: list[dict[str, Any]]) -> tuple[int, int]:
        tools = [m for m in view[self.protect:] if m.get("role") == "tool"]
        recent = max((_content_chars(m) for m in tools[-3:]), default=0)
        by_ratio = int(0.10 * self.strict)
        by_obs = int(self.urgent_mult * recent)
        cap = int(0.50 * self.strict)
        if by_obs <= by_ratio:
            return by_ratio, recent
        if by_obs > cap:
            return cap, recent
        return by_obs, recent

    # ------------------------------------------------------- rollback
    def rollback_to_margin(
            self, view: list[dict[str, Any]],
            protect: int | None = None) -> tuple[list[dict[str, Any]], int]:
        """Drop newest turns in place until margin room is free.

        Never touches the protected prefix; an assistant ``tool_calls``
        turn is dropped together with its tool result.  Returns the
        dropped messages and the char count after.
        """
        keep = self.protect if protect is None else int(protect)
        target = max(self.strict - self.retry_margin, 0)
        dropped: list[dict[str, Any]] = []
        while len(view) > keep and self.used(view) > target:
            dropped.append(view.pop())
            while (len(view) > keep
                   and view[-1].get("role") == "assistant"
                   and view[-1].get("tool_calls")):
                dropped.append(view.pop())
        return list(reversed(dropped)), self.used(view)

    def ledger_message(self, dropped: list[dict[str, Any]],
                       after: int) -> dict[str, Any]:
        cmds = _describe_dropped(dropped)
        lines = [
            f"[CONTEXT LIMIT — rollback {self.n_retries}/{self.max_retries}] "
            f"Your {len(dropped)} newest turn(s) were rolled back "
            f"(now ~{after}/{self.strict} chars). Condense the context file "
            "THIS turn: replace stale regions with shorter notes.",
        ]
        if cmds:
            lines.append("Rolled-back commands (do not re-run verbatim; "
                         "use head/grep/paging if you need them):")
            lines.extend(f"  - {c}" for c in cmds)
        return {"role": "user", "content": "\n".join(lines)}

    # ------------------------------------------------------- wording
    def final_message(self, used: int) -> str:
        return (f"[SYSTEM NOTICE] Context budget reached ({used}/{self.limit} "
                "chars). This is your FINAL turn: make sure the deliverable "
                "is complete, then finish — the session ends after this turn.")

    def urgent_message(self, used: int, need: int) -> str:
        return (f"[CONTEXT NUDGE — URGENT] {used}/{self.strict} chars used; "
                f"only {max(self.strict - used, 0)} chars of headroom left "
                f"(need ~{need} for the next observation). Compact the context "
                "file THIS turn and do nothing else.")

    def tier_message(self, frac: float, used: int) -> str:
        pct = int(round(frac * 100))
        if frac <= 0.25:
            return (f"[CONTEXT NUDGE] context at ~{pct}% of budget "
                    f"({used} chars). No action needed; keep notes current.")
        if frac <= 0.50:
            return (f"[CONTEXT NUDGE] context at ~{pct}% of budget "
                    f"({used} chars). Finish the unit in flight, then tidy "
                    "once: collapse settled tool outputs into short notes.")
        return (f"[CONTEXT NUDGE] context at ~{pct}% of budget ({used} chars) "
                "— close to the limit. Compact settled spans now; if you cross "
                "the limit you get one final turn and then the session ends.")


def _describe_dropped(dropped: list[dict[str, Any]]) -> list[str]:
    cmds: list[str] = []
    for message in dropped:
        for call in message.get("tool_calls") or []:
            desc = _describe_call(call)
            if desc and desc not in cmds:
                cmds.append(desc)
    return cmds[:5]


def _describe_call(call: dict[str, Any]) -> str:
    function = (call or {}).get("function") or {}
    name = function.get("name") or "tool"
    raw = function.get("arguments") or "{}"
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        args = {}
    hint = ""
    if isinstance(args, dict):
        for key in ("command", "code", "pattern", "path", "patch"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                hint = value
                break
    text = f"{name} {hint}".strip() if hint else name
    return " ".join(str(text).split())[:70]


__all__ = ["BudgetDecision", "ContextBudget"]
