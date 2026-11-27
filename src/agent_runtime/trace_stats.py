"""Live counters folded from a run's trace events.

``RunStats`` is owned by the tracing layer, not by metrics persistence: it
accumulates directly from every ``RunTrace.emit`` so the record survives the
``retain_events`` policy.  With ``control`` retention the payload events never
stay in ``trace.events``, and a reader scanning that list would report zeros.

This module is a leaf: it depends only on the pure record contracts in
:mod:`agent_runtime.run_records`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from agent_runtime.run_records import (
    LatencyBreakdown,
    TokenUsage,
    parse_float,
    parse_int,
    parse_optional_float,
    parse_text,
    parse_text_list,
)


@dataclass
class RunStats:
    """Live counters a :class:`~agent_runtime.trace.RunTrace` feeds per event.

    Accumulated here rather than derived from ``trace.events`` so the record
    survives the ``retain_events`` policy: with ``control`` the payload events
    never stay in the list, and a reader scanning it would report zeros.
    """

    started_at: float = 0.0
    iterations: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    error_kinds: dict[str, int] = field(default_factory=dict)
    edited_files: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    summarizer_prompt_tokens: int = 0
    summarizer_completion_tokens: int = 0
    llm_ms: float = 0.0
    tool_ms: float = 0.0
    summarizer_ms: float = 0.0
    ttft_ms: float | None = None
    retries: int = 0
    summary_attempts: int = 0
    summary_invalid: int = 0
    compactions: int = 0
    policy_blocks: int = 0
    has_answer: bool = False
    dropped_events: int = 0

    def observe(self, event_type: str, iteration: int,
                data: Mapping[str, Any]) -> None:
        """Fold one emitted trace event into the counters."""
        if isinstance(iteration, int) and not isinstance(iteration, bool):
            self.iterations = max(self.iterations, iteration)
        if event_type == "llm.end":
            self.llm_ms += parse_float(data.get("duration_ms"))
            if self.ttft_ms is None:
                self.ttft_ms = parse_optional_float(data.get("ttft_ms"))
        elif event_type == "llm.retry":
            self.retries += 1
        elif event_type == "tool.end":
            self._observe_tool_end(data)
        elif event_type == "agent.completed":
            answer = data.get("answer")
            self.has_answer = bool(str(answer).strip()) if answer else False
        elif event_type == "agent.end":
            self._observe_agent_end(data)
        elif event_type.startswith("context.llm_summary."):
            if not event_type.endswith(".text"):
                self.summary_attempts += 1
            if event_type == "context.llm_summary.invalid":
                self.summary_invalid += 1
        elif event_type.startswith("context.") and not event_type.endswith(".text"):
            if data.get("request_chars") is not None:
                self.compactions += 1
                self.summarizer_ms += parse_float(data.get("duration_ms"))
        elif event_type in {"guard.block", "policy.block"}:
            self.policy_blocks += 1

    def _observe_tool_end(self, data: Mapping[str, Any]) -> None:
        self.tool_calls += 1
        self.tool_ms += parse_float(data.get("exec_ms"))
        if data.get("status") == "error":
            self.tool_errors += 1
            kind = parse_text(data.get("error_kind")) or "unknown"
            self.error_kinds[kind] = self.error_kinds.get(kind, 0) + 1
            if kind == "command_blocked":
                # A guard or policy block surfaces as a failed tool call, not as
                # a ``policy.block`` event (nothing emits one), so this is where
                # the VCR's "no block" condition is counted.
                self.policy_blocks += 1
        for path in parse_text_list(data.get("edited_files")):
            if path not in self.edited_files:
                self.edited_files.append(path)

    def _observe_agent_end(self, data: Mapping[str, Any]) -> None:
        # The turn ledger already sums main + summarizer billing; split it back
        # apart so ``tokens`` keeps its documented invariants.
        prompt = parse_int(data.get("prompt_tokens"))
        completion = parse_int(data.get("completion_tokens"))
        summarizer_prompt = parse_int(data.get("summarizer_prompt_tokens"))
        summarizer_completion = parse_int(data.get("summarizer_completion_tokens"))
        self.prompt_tokens = max(0, prompt - summarizer_prompt)
        self.completion_tokens = max(0, completion - summarizer_completion)
        self.cached_tokens = parse_int(data.get("cached_tokens"))
        self.summarizer_prompt_tokens = summarizer_prompt
        self.summarizer_completion_tokens = summarizer_completion

    def token_usage(self) -> TokenUsage:
        return TokenUsage(prompt=self.prompt_tokens,
                          completion=self.completion_tokens,
                          cached=self.cached_tokens,
                          summarizer=(self.summarizer_prompt_tokens
                                      + self.summarizer_completion_tokens))

    def latency(self, duration_ms: float) -> LatencyBreakdown:
        accounted = self.llm_ms + self.tool_ms + self.summarizer_ms
        return LatencyBreakdown(llm=self.llm_ms, tool=self.tool_ms,
                                summarizer=self.summarizer_ms,
                                overhead=max(0.0, duration_ms - accounted))


__all__ = ["RunStats"]
