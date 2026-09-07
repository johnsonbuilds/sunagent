import sys
import unittest
from os import environ
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.agent.loop import run_turn
from agent_runtime.agent.tool_error_budget import (
    SameToolErrorBudget,
    error_signature,
    resolve_max_retries,
)
from agent_runtime.harness import (
    HarnessError,
    HarnessSpec,
    RecoveryGenome,
    from_dict,
    load_harness,
)
from agent_runtime.lineage import load_all_harnesses, validate_lineage
from agent_runtime.tools.tools import ToolRegistry, ToolSpec
from agent_runtime.trace import RunTrace


WEATHER_SCHEMA = {
    "type": "object",
    "properties": {"location": {"type": "string"}},
    "required": ["location"],
}


class FakeLLM:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.messages: list[list[dict[str, Any]]] = []

    async def chat(self, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        self.messages.append(messages.copy())
        return self.responses.pop(0)


def tool_call_response(call_id: str = "1") -> dict[str, Any]:
    return {"content": "", "tool_calls": [{"id": call_id, "function": {
        "name": "weather", "arguments": '{"location":"Paris"}'}}]}


def make_registry(handler: Any) -> ToolRegistry:
    return ToolRegistry([ToolSpec("weather", "", WEATHER_SCHEMA, handler)])


def make_harness(max_retries: int) -> HarnessSpec:
    return HarnessSpec(
        recovery=RecoveryGenome(tool_error_max_retries=max_retries))


class ErrorSignatureTests(unittest.TestCase):
    def test_line_numbers_are_ignored(self) -> None:
        self.assertEqual(
            error_signature("apply_patch: line 6: unterminated block (x)"),
            error_signature("apply_patch: line 99: unterminated block (x)"))

    def test_different_reasons_stay_different(self) -> None:
        self.assertNotEqual(
            error_signature("apply_patch: line 6: unterminated block"),
            error_signature("unexpected argument 'foo'"))

    def test_case_and_whitespace_are_ignored(self) -> None:
        self.assertEqual(error_signature("  Service UNAVAILABLE\ntail"),
                         "service unavailable")


class SameToolErrorBudgetTests(unittest.TestCase):
    def test_trips_on_third_consecutive_identical_failure(self) -> None:
        budget = SameToolErrorBudget(3)
        self.assertEqual(budget.note_failure("t", ValueError("boom")),
                         (1, False))
        self.assertEqual(budget.note_failure("t", ValueError("boom")),
                         (2, False))
        self.assertEqual(budget.note_failure("t", ValueError("boom")),
                         (3, True))

    def test_success_resets_the_count(self) -> None:
        budget = SameToolErrorBudget(2)
        budget.note_failure("t", ValueError("boom"))
        budget.note_success()
        self.assertEqual(budget.note_failure("t", ValueError("boom")),
                         (1, False))

    def test_different_error_resets_the_count(self) -> None:
        budget = SameToolErrorBudget(2)
        budget.note_failure("t", ValueError("boom-a"))
        self.assertEqual(budget.note_failure("t", ValueError("boom-b")),
                         (1, False))

    def test_different_tool_resets_the_count(self) -> None:
        budget = SameToolErrorBudget(2)
        budget.note_failure("tool-a", ValueError("boom"))
        self.assertEqual(budget.note_failure("tool-b", ValueError("boom")),
                         (1, False))

    def test_zero_disables_the_guard(self) -> None:
        budget = SameToolErrorBudget(0)
        for _ in range(5):
            self.assertEqual(budget.note_failure("t", ValueError("boom"))[1],
                             False)


class ResolveMaxRetriesTests(unittest.TestCase):
    def test_env_override_wins_over_harness_gene(self) -> None:
        previous = environ.get("AGENT_RUNTIME_TOOL_ERROR_MAX_RETRIES")
        environ["AGENT_RUNTIME_TOOL_ERROR_MAX_RETRIES"] = "1"
        try:
            self.assertEqual(resolve_max_retries(3), 1)
        finally:
            if previous is None:
                environ.pop("AGENT_RUNTIME_TOOL_ERROR_MAX_RETRIES", None)
            else:
                environ["AGENT_RUNTIME_TOOL_ERROR_MAX_RETRIES"] = previous

    def test_invalid_env_falls_back_to_harness_gene(self) -> None:
        previous = environ.get("AGENT_RUNTIME_TOOL_ERROR_MAX_RETRIES")
        environ["AGENT_RUNTIME_TOOL_ERROR_MAX_RETRIES"] = "many"
        try:
            self.assertEqual(resolve_max_retries(3), 3)
        finally:
            if previous is None:
                environ.pop("AGENT_RUNTIME_TOOL_ERROR_MAX_RETRIES", None)
            else:
                environ["AGENT_RUNTIME_TOOL_ERROR_MAX_RETRIES"] = previous


class LoopGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_execution_error_three_times_stops_early(self) -> None:
        async def failing_tool(location: str) -> str:
            raise RuntimeError("service unavailable")

        llm = FakeLLM([tool_call_response("1"), tool_call_response("2"),
                       tool_call_response("3"), tool_call_response("4")])
        trace = RunTrace(run_id="guard")

        answer = await run_turn("weather", llm, make_registry(failing_tool),
                                harness=make_harness(3), trace=trace)

        self.assertTrue(answer.startswith("STOPPED_SAME_TOOL_ERROR:"))
        self.assertIn("weather", answer)
        self.assertIn("3 times in a row", answer)
        # Stopped before the 4th LLM request instead of burning iterations.
        self.assertEqual(len(llm.messages), 3)
        guards = [e for e in trace.events if e.event_type == "loop.guard"]
        self.assertEqual(len(guards), 1)
        self.assertEqual(guards[0].data["consecutive"], 3)

    async def test_same_validation_rejection_three_times_stops_early(self) -> None:
        async def ok_tool(location: str) -> str:
            return location

        bad = {"content": "", "tool_calls": [{"id": "1", "function": {
            "name": "weather", "arguments": "{}"}}]}
        llm = FakeLLM([bad, bad, bad])

        answer = await run_turn("weather", llm, make_registry(ok_tool),
                                harness=make_harness(3))

        self.assertTrue(answer.startswith("STOPPED_SAME_TOOL_ERROR:"))
        self.assertEqual(len(llm.messages), 3)

    async def test_success_in_between_prevents_the_stop(self) -> None:
        calls = {"n": 0}

        async def flaky_tool(location: str) -> str:
            calls["n"] += 1
            if calls["n"] in (1, 2, 4, 5):
                raise RuntimeError("service unavailable")
            return "sunny"

        llm = FakeLLM([tool_call_response("1"), tool_call_response("2"),
                       tool_call_response("3"), tool_call_response("4"),
                       tool_call_response("5"),
                       {"content": "done", "tool_calls": []}])

        answer = await run_turn("weather", llm, make_registry(flaky_tool),
                                harness=make_harness(3))

        self.assertEqual(answer, "done")

    async def test_zero_disables_the_guard(self) -> None:
        async def failing_tool(location: str) -> str:
            raise RuntimeError("service unavailable")

        llm = FakeLLM([tool_call_response("1"), tool_call_response("2"),
                       {"content": "gave up", "tool_calls": []}])
        trace = RunTrace(run_id="guard-off")

        answer = await run_turn("weather", llm, make_registry(failing_tool),
                                harness=make_harness(0), trace=trace)

        self.assertEqual(answer, "gave up")
        self.assertEqual(
            [e for e in trace.events if e.event_type == "loop.guard"], [])


class RecoveryGeneParsingTests(unittest.TestCase):
    def test_default_is_zero_and_disables_the_guard(self) -> None:
        self.assertEqual(from_dict({}).recovery.tool_error_max_retries, 0)

    def test_manifest_value_is_accepted(self) -> None:
        spec = from_dict({"recovery": {"tool_error_max_retries": 3}})
        self.assertEqual(spec.recovery.tool_error_max_retries, 3)

    def test_negative_and_non_integer_are_rejected(self) -> None:
        with self.assertRaisesRegex(HarnessError, "tool_error_max_retries"):
            from_dict({"recovery": {"tool_error_max_retries": -1}})
        with self.assertRaisesRegex(HarnessError, "tool_error_max_retries"):
            from_dict({"recovery": {"tool_error_max_retries": "many"}})

    def test_code_v2_derives_from_code_v1_with_only_the_new_gene(self) -> None:
        repo_root = Path(__file__).parents[2]
        specs = load_all_harnesses(repo_root / "harnesses")
        child = specs["code-v2"]
        self.assertEqual(child.parent, "code-v1")
        self.assertEqual(child.recovery.tool_error_max_retries, 3)
        self.assertEqual(specs["code-v1"].recovery.tool_error_max_retries, 0)
        code_v2_problems = [p for p in validate_lineage(specs)
                            if "code-v2" in p]
        self.assertEqual(code_v2_problems, [])

    def test_code_v2_manifest_loads_standalone(self) -> None:
        repo_root = Path(__file__).parents[2]
        spec = load_harness(repo_root / "harnesses" / "code-v2.yaml")
        self.assertEqual(spec.id, "code-v2")
        self.assertEqual(spec.recovery.tool_error_max_retries, 3)


if __name__ == "__main__":
    unittest.main()
