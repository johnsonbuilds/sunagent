"""A rejected request must fail once, not be replayed until the budget burns.

Incident: session ``sess-20261120-080624`` (harness ``coding-v1``,
``provider_error`` = 6 retries / 2s exponential) sent a request the
provider refused with ``400 INVALID_ARGUMENT``. Unclassified errors fell
through to ``provider_error``, so the turn spent 2+4+8+16+32+64 = 126
seconds and six identical requests before the 400 reached the user.

Deterministic rejections now classify as ``invalid_request`` (zero retries
by default, per-harness opt-in). Transient failures keep their policy.
"""

import sys
import unittest
from os import environ
from pathlib import Path
from typing import Any
from unittest import mock


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

import agent_runtime.agent.llm_request as request_module
from agent_runtime.agent.llm_recovery import (
    NUDGE,
    REFRESH,
    REPLAY,
    ReasoningBudgetExceeded,
    ReasoningTruncated,
    _thinking_cut_clause,
    llm_error_category,
    recovery_plan,
)
from agent_runtime.agent.llm_stream import (
    StreamEmpty,
    StreamIdleTimeout,
    StreamTruncated,
)
from agent_runtime.agent.turn_factory import run_turn
from agent_runtime.harness import (
    HarnessSpec,
    LLMRetryPolicy,
    RecoveryGenome,
    default_harness,
    from_dict,
    load_named_harness,
)
from agent_runtime.providers.llm import ContextWindowExceededError
from agent_runtime.runtime_config import RuntimeConfig
from agent_runtime.tools.tools import ToolRegistry, ToolSpec
from agent_runtime.trace import RunTrace

# Same reason as tests/agent/test_loop.py: memory injection would pollute
# the system prompt without touching what these tests assert.
environ["AGENT_RUNTIME_MEMORY"] = "0"

_RUNTIME = RuntimeConfig(model="m", base_url=None)


class ProviderError(Exception):
    """Stands in for openai.APIStatusError: status plus a provider body."""

    def __init__(self, status_code: int, message: str = "rejected") -> None:
        super().__init__(f"Error code: {status_code} - {message}")
        self.status_code = status_code


class StatusLLM:
    """Chat model that serves queued errors first, then queued responses."""

    def __init__(self, responses: list[dict[str, Any]] | None = None,
                 errors: list[ProviderError] | None = None) -> None:
        self.responses = responses or []
        self.errors = errors or []
        self.calls = 0

    def _next_error(self) -> Any | None:
        self.calls += 1
        return self.errors.pop(0) if self.errors else None

    async def chat(self, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        error = self._next_error()
        if error is not None:
            raise error
        return self.responses.pop(0)

    async def stream(self, messages: list[dict[str, Any]],
                     tools: list[dict[str, Any]] | None = None):
        error = self._next_error()
        if error is not None:
            raise error
        response = self.responses.pop(0)
        yield dict(response)
        yield {"content": "", "finish_reason": "stop"}


def make_registry() -> ToolRegistry:
    async def handler(**_: Any) -> str:
        return "ok"

    return ToolRegistry([ToolSpec("weather", "", {"type": "object",
                                                 "properties": {}}, handler)])


def coding_v1_like_harness(**overrides: LLMRetryPolicy) -> HarnessSpec:
    """The retry genome that turned one 400 into 126 seconds of replay."""
    entries = {category: LLMRetryPolicy()
               for category in default_harness().recovery.llm_errors}
    entries["provider_error"] = LLMRetryPolicy(
        max_retries=6, backoff="exponential", base_delay=2.0)
    entries.update(overrides)
    return HarnessSpec(recovery=RecoveryGenome(llm_errors=entries))


class ClassificationTests(unittest.TestCase):
    def test_deterministic_statuses_are_invalid_request(self) -> None:
        for status in (400, 404, 422):
            with self.subTest(status=status):
                self.assertEqual(llm_error_category(ProviderError(status),
                                                     _RUNTIME),
                                 "invalid_request")

    def test_transient_failures_stay_provider_error(self) -> None:
        for status in (401, 403, 408, 429, 500, 502, 503):
            with self.subTest(status=status):
                self.assertEqual(llm_error_category(ProviderError(status),
                                                     _RUNTIME),
                                 "provider_error")
        self.assertEqual(llm_error_category(ValueError("dns hiccup"), _RUNTIME),
                         "provider_error")

    def test_a_non_integer_status_code_is_ignored(self) -> None:
        for bogus in (True, "400", 400.0, None):
            with self.subTest(status=bogus):
                error = ProviderError(400)
                error.status_code = bogus
                self.assertEqual(llm_error_category(error, _RUNTIME), "provider_error")

    def test_context_window_keeps_precedence_over_the_status(self) -> None:
        error = ContextWindowExceededError("400 - context_length_exceeded")
        error.status_code = 400

        self.assertEqual(llm_error_category(error, _RUNTIME), "context_window")

    def test_the_escape_hatch_restores_provider_error(self) -> None:
        config = RuntimeConfig(model="m", base_url=None, retry_4xx=True)

        self.assertEqual(llm_error_category(ProviderError(400), config),
                         "provider_error")
        self.assertEqual(
            llm_error_category(ProviderError(400),
                                RuntimeConfig(model="m", base_url=None)),
            "invalid_request")

    def test_the_env_escape_hatch_flows_through_runtime_config(self) -> None:
        previous = environ.get("AGENT_RUNTIME_RETRY_4XX")
        try:
            environ["AGENT_RUNTIME_RETRY_4XX"] = "1"
            self.assertEqual(
                llm_error_category(ProviderError(400),
                                   RuntimeConfig.from_env()),
                "provider_error")
            environ["AGENT_RUNTIME_RETRY_4XX"] = "0"
            self.assertEqual(
                llm_error_category(ProviderError(400),
                                   RuntimeConfig.from_env()),
                "invalid_request")
        finally:
            if previous is None:
                environ.pop("AGENT_RUNTIME_RETRY_4XX", None)
            else:
                environ["AGENT_RUNTIME_RETRY_4XX"] = previous

    def test_runtime_config_reads_the_env_flag(self) -> None:
        previous = environ.get("AGENT_RUNTIME_RETRY_4XX")
        try:
            environ["AGENT_RUNTIME_RETRY_4XX"] = "1"
            self.assertTrue(RuntimeConfig.from_env().retry_4xx)
            environ.pop("AGENT_RUNTIME_RETRY_4XX")
            self.assertFalse(RuntimeConfig.from_env().retry_4xx)
        finally:
            if previous is None:
                environ.pop("AGENT_RUNTIME_RETRY_4XX", None)
            else:
                environ["AGENT_RUNTIME_RETRY_4XX"] = previous


class RecoveryPlanTests(unittest.TestCase):
    """One table decides every LLM failure, so test the table itself.

    The three strategies are replay (harness owns count and delay),
    refresh (compact, then resend) and nudge (rewrite the conversation).
    A failure with no replay policy at all — a thinking-only turn — used
    to be signalled by ``category is None`` and handled in a second place;
    the plan is now the single answer to "what happens next".
    """

    def test_transient_and_rejected_failures_replay(self) -> None:
        cases = [
            (StreamIdleTimeout("no chunk"), "stream_idle_timeout"),
            (StreamTruncated("closed early"), "stream_truncated"),
            (StreamEmpty("empty"), "stream_empty"),
            (ProviderError(503), "provider_error"),
            (ValueError("dns hiccup"), "provider_error"),
            (ProviderError(400), "invalid_request"),
        ]
        for error, category in cases:
            with self.subTest(error=type(error).__name__, category=category):
                plan = recovery_plan(error, _RUNTIME)
                self.assertEqual(plan.strategy, REPLAY)
                self.assertEqual(plan.category, category)

    def test_a_rejection_is_replay_so_the_harness_can_still_fail_fast(self) -> None:
        """"Do not retry" is a policy (max_retries 0), not a strategy."""
        plan = recovery_plan(ProviderError(400), _RUNTIME)

        self.assertEqual(plan.strategy, REPLAY)
        self.assertEqual(
            coding_v1_like_harness().recovery.llm_errors[plan.category].max_retries,
            0)

    def test_a_context_overflow_refreshes_before_resending(self) -> None:
        error = ContextWindowExceededError("400 - context_length_exceeded")
        error.status_code = 400

        plan = recovery_plan(error, _RUNTIME)

        self.assertEqual(plan.strategy, REFRESH)
        self.assertEqual(plan.category, "context_window")

    def test_a_thinking_only_turn_is_nudged_with_no_policy(self) -> None:
        for error in (ReasoningBudgetExceeded(600, 500), ReasoningTruncated(600)):
            with self.subTest(error=type(error).__name__):
                plan = recovery_plan(error, _RUNTIME)
                self.assertEqual(plan.strategy, NUDGE)
                self.assertIsNone(plan.category)
                self.assertIsNone(llm_error_category(error, _RUNTIME))
                self.assertEqual(error.reasoning_chars, 600)

    def test_the_two_thinking_only_turns_state_their_own_cause(self) -> None:
        budget = ReasoningBudgetExceeded(600, 500)
        truncated = ReasoningTruncated(600)

        self.assertIn("reasoning exceeded 500 chars", str(budget))
        # A provider-side cut is not our budget being exceeded: the message
        # must not borrow that diagnosis.
        self.assertNotIn("exceeded", str(truncated))
        self.assertIn("finish_reason=length", str(truncated))
        self.assertIn("600 chars of reasoning", str(truncated))
        # Distinct guidance and placeholders, because the remedies differ in
        # what the model is told actually happened.
        self.assertNotEqual(budget.guidance(), truncated.guidance())
        self.assertNotEqual(budget.placeholder(), truncated.placeholder())
        self.assertNotEqual(budget.fallback_answer(),
                            truncated.fallback_answer())
        self.assertNotEqual(budget.kind, truncated.kind)

    def test_a_cut_with_no_visible_reasoning_does_not_claim_zero_chars(
            self) -> None:
        """Hidden chain of thought: the cut is real, the char count is not.

        Endpoints that do not stream ``reasoning_content`` back report
        ``finish_reason=length`` with nothing visible, so a wording built
        around "0 chars of reasoning" would describe a measurement that
        never happened.
        """
        hidden = ReasoningTruncated(0)

        # A substring check alone let this through once: a stray trailing
        # comma made the clause a one-tuple, whose ``repr`` contains the
        # phrase but renders "('before …',)" into the text the model reads.
        self.assertIsInstance(_thinking_cut_clause(0), str)
        self.assertIsInstance(_thinking_cut_clause(7), str)
        self.assertEqual(_thinking_cut_clause(0),
                         "before producing any visible content")
        for text in (str(hidden), hidden.placeholder(), hidden.guidance()):
            self.assertIn("before producing any visible content", text)
            self.assertNotIn("('before", text)
            self.assertNotIn("',)", text)
            self.assertNotIn("0 chars", text)
        # It is still the same failure, so the strategy does not change.
        self.assertEqual(recovery_plan(hidden, _RUNTIME).strategy, NUDGE)


class GenomeTests(unittest.TestCase):
    def test_every_harness_defaults_to_zero_invalid_request_retries(self) -> None:
        spec = from_dict({})

        self.assertEqual(spec.recovery.llm_errors["invalid_request"].max_retries, 0)

    def test_the_live_coding_harness_does_not_retry_rejections(self) -> None:
        spec = load_named_harness("coding-v1")

        # The genome that produced the incident keeps its provider retries…
        self.assertEqual(spec.recovery.llm_errors["provider_error"].max_retries, 6)
        # …but a refused request is no longer replayed.
        self.assertEqual(spec.recovery.llm_errors["invalid_request"].max_retries, 0)


class FailFastTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_rejected_request_is_sent_exactly_once(self) -> None:
        llm = StatusLLM(responses=[{"content": "unused", "tool_calls": []}],
                        errors=[ProviderError(400, "missing a thought_signature")])
        trace = RunTrace(run_id="invalid-request")

        with mock.patch.object(request_module.asyncio, "sleep",
                               new=mock.AsyncMock()) as fake_sleep:
            answer = await run_turn("go", llm, make_registry(),
                                    harness=coding_v1_like_harness(),
                                    trace=trace)

        self.assertTrue(answer.startswith("LLM error:"))
        self.assertIn("400", answer)
        self.assertEqual(llm.calls, 1)
        fake_sleep.assert_not_awaited()
        self.assertEqual([event for event in trace.events
                          if event.event_type == "llm.retry"], [])

    async def test_transient_failures_keep_the_provider_policy(self) -> None:
        llm = StatusLLM(responses=[{"content": "done", "tool_calls": []}],
                        errors=[ProviderError(503, "upstream unavailable")])

        answer = await run_turn("go", llm, make_registry(),
                                harness=coding_v1_like_harness())

        self.assertEqual(answer, "done")
        self.assertEqual(llm.calls, 2)

    async def test_a_harness_can_opt_into_retrying_rejections(self) -> None:
        llm = StatusLLM(responses=[{"content": "done", "tool_calls": []}],
                        errors=[ProviderError(400, "gateway hiccup")])
        harness = coding_v1_like_harness(
            invalid_request=LLMRetryPolicy(max_retries=1, backoff="none"))

        answer = await run_turn("go", llm, make_registry(), harness=harness)

        self.assertEqual(answer, "done")
        self.assertEqual(llm.calls, 2)

    async def test_the_streaming_path_classifies_the_same_way(self) -> None:
        llm = StatusLLM(errors=[ProviderError(400, "malformed")])

        with mock.patch.object(request_module.asyncio, "sleep",
                               new=mock.AsyncMock()) as fake_sleep:
            answer = await run_turn("go", llm, make_registry(), stream=True,
                                    harness=coding_v1_like_harness())

        self.assertTrue(answer.startswith("LLM error:"))
        self.assertIn("400", answer)
        self.assertEqual(llm.calls, 1)
        fake_sleep.assert_not_awaited()


if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path

    # A direct run skips ``conftest.py``: isolate the runtime home here too.
    _sys.path.insert(0, str(next(parent for parent in
                                 _Path(__file__).resolve().parents
                                 if (parent / "conftest.py").is_file())))
    from _direct_run import isolate_runtime_home

    isolate_runtime_home()
    unittest.main()
