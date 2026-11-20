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

import agent_runtime.agent.chat as chat_module
from agent_runtime.agent.chat import _llm_error_category
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
                self.assertEqual(_llm_error_category(ProviderError(status)),
                                 "invalid_request")

    def test_transient_failures_stay_provider_error(self) -> None:
        for status in (401, 403, 408, 429, 500, 502, 503):
            with self.subTest(status=status):
                self.assertEqual(_llm_error_category(ProviderError(status)),
                                 "provider_error")
        self.assertEqual(_llm_error_category(ValueError("dns hiccup")),
                         "provider_error")

    def test_a_non_integer_status_code_is_ignored(self) -> None:
        for bogus in (True, "400", 400.0, None):
            with self.subTest(status=bogus):
                error = ProviderError(400)
                error.status_code = bogus
                self.assertEqual(_llm_error_category(error), "provider_error")

    def test_context_window_keeps_precedence_over_the_status(self) -> None:
        error = ContextWindowExceededError("400 - context_length_exceeded")
        error.status_code = 400

        self.assertEqual(_llm_error_category(error), "context_window")

    def test_the_escape_hatch_restores_provider_error(self) -> None:
        config = RuntimeConfig(model="m", base_url=None, retry_4xx=True)

        self.assertEqual(_llm_error_category(ProviderError(400), config),
                         "provider_error")
        self.assertEqual(
            _llm_error_category(ProviderError(400),
                                RuntimeConfig(model="m", base_url=None)),
            "invalid_request")

    def test_the_env_escape_hatch_applies_without_a_runtime_config(self) -> None:
        previous = environ.get("AGENT_RUNTIME_RETRY_4XX")
        try:
            environ["AGENT_RUNTIME_RETRY_4XX"] = "1"
            self.assertEqual(_llm_error_category(ProviderError(400)),
                             "provider_error")
            environ["AGENT_RUNTIME_RETRY_4XX"] = "0"
            self.assertEqual(_llm_error_category(ProviderError(400)),
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

        with mock.patch.object(chat_module.asyncio, "sleep",
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

        with mock.patch.object(chat_module.asyncio, "sleep",
                               new=mock.AsyncMock()) as fake_sleep:
            answer = await run_turn("go", llm, make_registry(), stream=True,
                                    harness=coding_v1_like_harness())

        self.assertTrue(answer.startswith("LLM error:"))
        self.assertIn("400", answer)
        self.assertEqual(llm.calls, 1)
        fake_sleep.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
