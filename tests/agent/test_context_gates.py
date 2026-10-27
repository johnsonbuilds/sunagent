"""Context-management gate tests: token metering, user preservation,
incremental summarization, and the context-window hard gate."""

from __future__ import annotations

import unittest
from typing import Any

from agent_runtime.agent.chat import chat_with_recovery
from agent_runtime.agent.memory import (
    DEFAULT_SUMMARY_MAX_TOKENS,
    DEFAULT_USER_PRESERVE_BUDGET,
    SUMMARY_PREFIX,
    _anchor_chars_per_token,
    _view_size,
    apply_memory_strategy,
    set_summary_llm,
    summarize_history,
)
from agent_runtime.agent.turn_history import Conversation, TurnHistory
from agent_runtime.harness import HarnessSpec, MemoryGenome, from_dict
from agent_runtime.providers.llm import ContextWindowExceededError


def build_rounds(rounds: int, tool_chars: int = 100,
                 mid_users: list[str] | None = None) -> list[dict[str, Any]]:
    """system + task, then N (assistant tool_call, tool result) rounds,
    with optional verbatim user messages interleaved before each round."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do the task"},
    ]
    users = list(mid_users or [])
    for index in range(rounds):
        # Mid-users start at round 1: round 0 is leading territory (the
        # original task plus any early user text is preserved whole).
        if index >= 1 and index - 1 < len(users):
            messages.append({"role": "user", "content": users[index - 1]})
        messages.append({
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": f"call-{index}", "type": "function",
                "function": {"name": "run_command", "arguments": "{}"},
            }],
        })
        messages.append({
            "role": "tool", "_id": f"call-{index}",
            "content": "x" * tool_chars,
        })
    return messages


def valid_summary(marker: str = "SNAP") -> str:
    return (
        "# Agent Context Snapshot\n\n"
        "## 1. Work State\n"
        "### Completed\n"
        f"- {marker}\n\n"
        "## 2. Next Move\n"
        "- continue\n\n"
        "## 3. Working Context & Anchors\n"
        "- **Relevant Files**: none"
    )


class FakeSummaryLLM:
    def __init__(self, summary: str | None = None,
                 *, error: Exception | None = None) -> None:
        self.error = error
        self.call_count = 0
        self.last_messages: list[dict[str, Any]] = []
        self.last_request_kwargs: dict[str, Any] = {}
        self.summary = summary if summary is not None else valid_summary()

    async def chat(self, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None = None,
                   **request_kwargs: Any) -> dict[str, Any]:
        self.call_count += 1
        self.last_messages = list(messages)
        self.last_request_kwargs = dict(request_kwargs)
        if self.error is not None:
            raise self.error
        return {"content": self.summary, "tool_calls": []}


class ViewSizeTests(unittest.TestCase):
    def test_chars_unit_counts_characters(self) -> None:
        messages = build_rounds(2, tool_chars=50)
        from agent_runtime.agent.memory import _total_chars
        self.assertEqual(_view_size(messages, False, "chars", None),
                         _total_chars(messages, False))

    def test_no_anchor_estimates_by_ratio(self) -> None:
        messages = build_rounds(2, tool_chars=50)
        total_chars = _view_size(messages, False, "chars", None)
        estimated = _view_size(messages, False, "tokens", None)
        self.assertAlmostEqual(estimated, total_chars / 4, delta=1)

    def test_anchor_measures_below_and_estimates_above(self) -> None:
        messages = build_rounds(4, tool_chars=1_000)
        total_chars = _view_size(messages, False, "chars", None)
        anchor = (5_000, len(messages), total_chars)  # fully measured
        self.assertEqual(_view_size(messages, False, "tokens", anchor), 5_000)
        # grow the view: only the tail is estimated, at the anchor ratio
        grown = messages + build_rounds(1, tool_chars=400)[2:]
        tail_chars = _view_size(grown[len(messages):], False, "chars", None)
        expected = 5_000 + int(tail_chars * 5_000 / total_chars)
        self.assertEqual(_view_size(grown, False, "tokens", anchor), expected)

    def test_anchor_ratio_helper(self) -> None:
        self.assertEqual(_anchor_chars_per_token(None), 4.0)
        self.assertAlmostEqual(_anchor_chars_per_token((100, 5, 500)), 5.0)
        self.assertEqual(_anchor_chars_per_token((0, 5, 0)), 4.0)


class UserPreservationTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        set_summary_llm(None)

    async def test_mid_users_kept_verbatim_budget_order(self) -> None:
        messages = build_rounds(6, tool_chars=1_000,
                                mid_users=["pivot to task B", "one more thing"])
        llm = FakeSummaryLLM()
        set_summary_llm(llm)
        result = await summarize_history(messages, budget=1, tail_rounds=2)
        contents = [str(m.get("content", "")) for m in result]
        # both user messages survive verbatim, after the summary
        self.assertIn("pivot to task B", contents)
        self.assertIn("one more thing", contents)
        # leading system+task and the summary structure are intact
        self.assertEqual(result[0]["content"], "sys")
        self.assertEqual(result[1]["content"], "do the task")
        self.assertTrue(contents[2].startswith(SUMMARY_PREFIX))
        # user messages appear after the summary, before the tail
        summary_index = next(i for i, c in enumerate(contents)
                             if c.startswith(SUMMARY_PREFIX))
        pivot_index = contents.index("pivot to task B")
        self.assertGreater(pivot_index, summary_index)

    async def test_overflow_users_join_summarizer_input(self) -> None:
        messages = build_rounds(6, tool_chars=1_000,
                                mid_users=["old redirect", "recent redirect"])
        llm = FakeSummaryLLM()
        set_summary_llm(llm)
        await summarize_history(messages, budget=1, tail_rounds=2,
                                user_preserve_budget=len("recent redirect"))
        contents = [str(m.get("content", "")) for m in llm.last_messages]
        # the budget only fits the newest user message verbatim; the older
        # one is fed to the summarizer verbatim instead
        self.assertIn("old redirect", contents)
        self.assertNotIn("old redirect",
                         [str(m.get("content", ""))
                          for m in llm.last_messages
                          if m.get("role") == "user"
                          and str(m.get("content", "")).startswith("do the")])
        result_contents = None  # view check via second call
        result = await summarize_history(messages, budget=1, tail_rounds=2,
                                         user_preserve_budget=len("recent redirect"))
        view_contents = [str(m.get("content", "")) for m in result]
        self.assertIn("recent redirect", view_contents)
        self.assertNotIn("old redirect", view_contents)


class IncrementalSummaryTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        set_summary_llm(None)

    async def test_boundary_limits_summarizer_input(self) -> None:
        messages = build_rounds(6, tool_chars=1_000)
        llm = FakeSummaryLLM()
        set_summary_llm(llm)
        boundary = len(messages) - 8  # two rounds summarized, two kept as tail
        await summarize_history(messages, budget=1, tail_rounds=2,
                                boundary=boundary,
                                previous_summary=valid_summary("OLD"))
        # the summarizer sees the previous snapshot plus only the new
        # segment (the tail rounds are excluded from its input)
        fed = llm.last_messages
        self.assertTrue(any(str(m.get("content", "")).startswith(SUMMARY_PREFIX)
                            for m in fed))
        self.assertLess(len(fed), len(messages))

    async def test_previous_summary_is_integrated_not_replaced(self) -> None:
        messages = build_rounds(4, tool_chars=1_000)
        llm = FakeSummaryLLM()
        set_summary_llm(llm)
        result = await summarize_history(messages, budget=1, tail_rounds=1,
                                         boundary=len(messages) - 4,
                                         previous_summary=valid_summary("OLD"))
        contents = [str(m.get("content", "")) for m in result]
        summary = next(c for c in contents if c.startswith(SUMMARY_PREFIX))
        self.assertIn("SNAP", summary)

    async def test_max_tokens_requested(self) -> None:
        messages = build_rounds(6, tool_chars=1_000)
        llm = FakeSummaryLLM()
        set_summary_llm(llm)
        await summarize_history(messages, budget=1, tail_rounds=2,
                                summary_max_tokens=512)
        self.assertEqual(llm.last_request_kwargs.get("max_tokens"), 512)

    async def test_oversized_summary_falls_back(self) -> None:
        messages = build_rounds(6, tool_chars=1_000)
        llm = FakeSummaryLLM(summary="z" * (DEFAULT_SUMMARY_MAX_TOKENS * 6 + 1))
        set_summary_llm(llm)
        result = await summarize_history(messages, budget=1, tail_rounds=2)
        # invalid (oversized) summary -> deterministic compaction fallback:
        # no summary message in the view, old tool outputs truncated
        self.assertFalse(any(
            str(m.get("content", "")).startswith(SUMMARY_PREFIX)
            for m in result))


class HardGateRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def _spec(self) -> HarnessSpec:
        return from_dict({
            "recovery": {"llm_errors": {
                "context_window": {"max_retries": 2, "backoff": "none"},
            }},
        })

    async def test_context_window_error_triggers_refresh_and_resend(self) -> None:
        spec = self._spec()
        attempts: list[int] = []

        class FlakyLLM:
            async def chat(self, messages, tools=None, **kwargs):
                attempts.append(len(messages))
                if len(attempts) == 1:
                    raise ContextWindowExceededError("maximum context length")
                return {"content": "ok", "tool_calls": []}

        messages = build_rounds(6, tool_chars=100)

        async def refresh() -> bool:
            messages[:] = messages[:2]  # compact in place
            return True

        response = await chat_with_recovery(
            FlakyLLM(), messages, None, 1, spec, refresh=refresh)
        self.assertEqual(response["content"], "ok")
        self.assertEqual(attempts, [14, 2])  # resent the compacted view

    async def test_refresh_false_propagates(self) -> None:
        spec = self._spec()

        class OverflowLLM:
            async def chat(self, messages, tools=None, **kwargs):
                raise ContextWindowExceededError("context window")

        async def refresh() -> bool:
            return False

        with self.assertRaises(ContextWindowExceededError):
            await chat_with_recovery(
                OverflowLLM(), build_rounds(2, tool_chars=10), None, 1, spec,
                refresh=refresh)


class TurnHistoryAnchorTests(unittest.IsolatedAsyncioTestCase):
    async def test_record_usage_and_boundary(self) -> None:
        conversation = Conversation()
        conversation.messages.extend(build_rounds(3, tool_chars=50))
        history = TurnHistory(conversation.messages)
        history.record_usage(5_000)
        anchor = history.usage_anchor
        assert anchor is not None
        tokens, view_len, view_chars = anchor
        self.assertEqual((tokens, view_len), (5_000, len(history.view)))
        self.assertGreater(view_chars, 0)

        # A summary-producing refresh moves the boundary forward
        genome = MemoryGenome(strategy="llm_summary", context_budget=1,
                              summary_tail_rounds=1)
        llm = FakeSummaryLLM()
        set_summary_llm(llm)
        await history.refresh_view(genome)
        self.assertGreater(history._summary_boundary, 0)
        set_summary_llm(None)


class TB4HarnessTests(unittest.TestCase):
    def test_tb_v4_loads_new_genes(self) -> None:
        from agent_runtime.harness import load_named_harness
        spec = load_named_harness("tb-v4")
        self.assertEqual(spec.memory.budget_unit, "tokens")
        self.assertEqual(spec.memory.context_budget, 30_000)
        self.assertEqual(spec.memory.user_preserve_budget, 8_000)
        self.assertEqual(spec.memory.summary_max_tokens, 2_048)
        policy = spec.recovery.llm_errors["context_window"]
        self.assertEqual((policy.max_retries, policy.backoff), (2, "none"))

    def test_legacy_defaults_unchanged(self) -> None:
        genome = MemoryGenome()
        self.assertEqual(genome.budget_unit, "chars")
        self.assertEqual(genome.user_preserve_budget,
                         DEFAULT_USER_PRESERVE_BUDGET)
        self.assertEqual(genome.summary_max_tokens,
                         DEFAULT_SUMMARY_MAX_TOKENS)


if __name__ == "__main__":
    unittest.main()
