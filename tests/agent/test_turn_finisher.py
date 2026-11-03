import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.agent.runtime_message import is_runtime_user_message
from agent_runtime.agent.tool_dispatch import (
    ToolCallOutcome,
    ValidatedToolCall,
)
from agent_runtime.agent.turn_factory import create_agent_turn
from agent_runtime.agent.turn_failure import TurnFailure
from agent_runtime.agent.turn_finisher import (
    TurnFinisher,
    _rerun_declaration_broken,
    _rerun_failure_detail,
    _rerun_outcome,
)
from agent_runtime.agent.turn_history import TurnHistory
from agent_runtime.agent.verification import SubmissionRules, render_submission
from agent_runtime.events import EventEmitter
from agent_runtime.harness import HarnessSpec, VerificationGenome
from agent_runtime.tools.tools import ToolRegistry, ToolSpec
from agent_runtime.trace import RunTrace
from tests.agent.test_loop import FakeLLM


REQUIRE = ("solution_description", "evidence", "command_to_verify")

GOOD_FIELDS = {
    "solution_description": "Root cause was X in auth.py, fixed with quote_plus().",
    "evidence": "pytest tests/test_x.py -q passed: 5 passed in 1.2s, output observed above.",
    "command_to_verify": "pytest tests/test_x.py -q",
}

ENABLED = VerificationGenome(enabled=True, mode="task_result", require=REQUIRE)
OFF = VerificationGenome()
ENABLED_RULES = SubmissionRules.from_genome(ENABLED)
OFF_RULES = SubmissionRules.from_genome(OFF)


def submit_outcome(fields: dict | None = None, call_id: str = "1",
                   submit_tool: str = ENABLED_RULES.submit_tool
                   ) -> ToolCallOutcome:
    validated = ValidatedToolCall(call_id, submit_tool,
                                  dict(fields or GOOD_FIELDS))
    return ToolCallOutcome(tool_call={"id": call_id}, validated=validated,
                           rejection=None)


def rerun_tools(handler) -> ToolRegistry:
    return ToolRegistry([
        ToolSpec("run_command", "", {"type": "object", "properties": {}},
                 handler),
    ])


class RerunHelperTests(unittest.TestCase):
    def test_excerpt_keeps_head_and_tail(self) -> None:
        stdout = "START\n" + "." * 3000 + "\n1 failed, 5 passed in 0.5s\n"
        _, _, excerpt = _rerun_outcome(
            {"exit_code": 1, "stdout": stdout, "stderr": ""})
        self.assertIn("START", excerpt)
        self.assertIn("1 failed, 5 passed", excerpt)
        self.assertIn("truncated", excerpt)

    def test_ran_detection_covers_runners_not_error_lists(self) -> None:
        # Genuine failures across runners: tests ran, not broken.
        self.assertFalse(_rerun_declaration_broken(
            1, "stdout: 1 failed, 5 passed in 0.5s stderr:"))
        self.assertFalse(_rerun_declaration_broken(
            1, "stdout: --- FAIL: TestParse (0.00s) FAIL stderr:"))
        self.assertFalse(_rerun_declaration_broken(
            1, "stdout: 3 passing, 2 failing stderr:"))
        # Environment failures without runner output: broken declaration,
        # with no hardcoded error list behind the decision.
        self.assertTrue(_rerun_declaration_broken(
            126, "stdout:  stderr: bash: ./run_tests.sh: Permission denied"))
        self.assertTrue(_rerun_declaration_broken(
            1, "stdout:  stderr: /opt/miniconda3/bin/python: "
               "No module named pytest"))

    def test_missing_exit_code_is_broken(self) -> None:
        self.assertTrue(_rerun_declaration_broken(None, "1 failed"))

    def test_failure_detail_names_counts_and_failing_ids(self) -> None:
        detail = _rerun_failure_detail(
            "1 failed, 5 passed in 0.5s\nFAILED tests/test_x.py::test_a\n")
        self.assertIn("1 failed, 5 passed", detail)
        self.assertIn("tests/test_x.py::test_a", detail)


class TurnFinisherTests(unittest.IsolatedAsyncioTestCase):
    def make_finisher(self, *, rules: SubmissionRules = ENABLED_RULES,
                      handler=None, max_iterations: int = 10,
                      finish_violation_limit: int = 3):
        async def passing(command: str, timeout: float | None = None) -> dict:
            return {"exit_code": 0, "stdout": "5 passed in 1.2s", "stderr": ""}

        tools = rerun_tools(handler or passing)
        trace = RunTrace()
        events = EventEmitter(run_id=trace.run_id)
        finisher = TurnFinisher(
            rules=rules,
            tools=tools,
            trace=trace,
            events=events,
            max_iterations=max_iterations,
            finish_violation_limit=finish_violation_limit,
        )
        return finisher, tools, trace, events

    # -- submit interception ------------------------------------------------

    def test_off_mode_does_not_intercept_submit(self) -> None:
        finisher, *_ = self.make_finisher(rules=OFF_RULES)
        self.assertIsNone(finisher.take_submit_arguments([submit_outcome()]))

    def test_first_validated_submit_arguments_are_copied(self) -> None:
        finisher, *_ = self.make_finisher()
        first = submit_outcome({"solution_description": "first"}, "1")
        second = submit_outcome(GOOD_FIELDS, "2")
        arguments = finisher.take_submit_arguments([first, second])
        self.assertEqual(arguments, {"solution_description": "first"})
        self.assertIsNot(arguments, first.validated.arguments)

    def test_non_submit_calls_are_not_intercepted(self) -> None:
        finisher, *_ = self.make_finisher()
        outcome = ToolCallOutcome(
            tool_call={"id": "1"},
            validated=ValidatedToolCall("1", "run_command", {"command": "ls"}),
            rejection=None)
        self.assertIsNone(finisher.take_submit_arguments([outcome]))

    # -- plain-text answers ------------------------------------------------

    def test_plain_answer_finishes_when_verification_is_off(self) -> None:
        finisher, _, _, events = self.make_finisher(rules=OFF_RULES)
        answer = finisher.answer_without_tools(
            {"content": "done"}, 1, TurnHistory([]))
        self.assertEqual(answer, "done")
        self.assertEqual(events.events[-1].event_type, "agent.completed")

    def test_plain_answer_is_nudged_when_verification_is_on(self) -> None:
        finisher, _, trace, _ = self.make_finisher()
        history = TurnHistory([])
        answer = finisher.answer_without_tools({"content": "done"}, 1, history)
        self.assertIsNone(answer)
        self.assertIn("To finish", history.messages[-1]["content"])
        self.assertTrue(is_runtime_user_message(history.messages[-1]))
        self.assertEqual(history.messages[-1]["metadata"]["kind"],
                         "submit_nudge")
        self.assertEqual(finisher._finish_violations, 1)
        self.assertTrue(any(event.event_type == "verification.failed"
                            for event in trace.events))

    def test_plain_answer_at_budget_end_is_accepted_as_miss(self) -> None:
        finisher, _, trace, _ = self.make_finisher(max_iterations=3)
        history = TurnHistory([])
        answer = finisher.answer_without_tools({"content": "partial"}, 3,
                                               history)
        self.assertEqual(answer, "partial")
        self.assertEqual(history.messages, [])
        self.assertTrue(any(event.event_type == "verification.failed"
                            for event in trace.events))

    # -- submission validation ---------------------------------------------

    async def test_shape_gap_returns_contract_nudge_and_counts_violation(
            self) -> None:
        finisher, *_ = self.make_finisher()
        nudge = await finisher.validate_submission(
            {"solution_description": "x", "evidence": "y",
             "command_to_verify": ""}, 1)
        self.assertIn("return contract", nudge)
        self.assertEqual(finisher._finish_violations, 1)

    async def test_finish_renders_the_submission(self) -> None:
        finisher, _, _, events = self.make_finisher()
        history = TurnHistory([
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "1", "function": {
                    "name": "edit_file",
                    "arguments": '{"path": "auth.py"}'}},
                {"id": "2", "function": {
                    "name": "run_command",
                    "arguments": '{"command": "pytest tests/test_x.py -q"}'}},
            ]},
            {"role": "tool",
             "content": "pytest tests/test_x.py -q passed: 5 passed in 1.2s"},
        ])
        answer = await finisher.finish_or_nudge(GOOD_FIELDS, 1, history)
        self.assertEqual(answer, render_submission(GOOD_FIELDS))
        self.assertEqual(events.events[-1].event_type, "agent.completed")

    async def test_rejected_finish_appends_nudge_and_continues(self) -> None:
        finisher, *_ = self.make_finisher()
        history = TurnHistory([])
        answer = await finisher.finish_or_nudge(
            {"solution_description": "x", "evidence": "y",
             "command_to_verify": ""}, 1, history)
        self.assertIsNone(answer)
        self.assertIn("return contract", history.messages[-1]["content"])
        self.assertTrue(is_runtime_user_message(history.messages[-1]))
        self.assertEqual(history.messages[-1]["metadata"]["kind"],
                         "contract_nudge")

    # -- rerun -------------------------------------------------------------

    async def test_passing_rerun_accepts(self) -> None:
        finisher, _, trace, _ = self.make_finisher()
        answer = await finisher.rerun_declared_command("pytest -q", 2)
        self.assertIsNone(answer)
        self.assertTrue(any(event.event_type == "verification.passed"
                            for event in trace.events))

    async def test_broken_declaration_is_named_as_such(self) -> None:
        async def broken(command: str, timeout: float | None = None) -> dict:
            return {"exit_code": 1, "stdout": "",
                    "stderr": "bash: ./run_tests.sh: Permission denied"}

        finisher, *_ = self.make_finisher(handler=broken)
        nudge = await finisher.rerun_declared_command("./run_tests.sh", 2)
        self.assertIn("never reached the tests", nudge)
        self.assertNotIn("full suite", nudge)

    async def test_test_failure_names_exit_code_and_detail(self) -> None:
        async def failing(command: str, timeout: float | None = None) -> dict:
            return {"exit_code": 1, "stdout": "1 failed, 5 passed in 0.5s",
                    "stderr": ""}

        finisher, *_ = self.make_finisher(handler=failing)
        nudge = await finisher.rerun_declared_command("pytest -q", 2)
        self.assertIn("exited 1", nudge)
        self.assertIn("1 failed, 5 passed", nudge)

    async def test_rerun_exception_is_reported_as_not_verified(self) -> None:
        async def exploding(command: str, timeout: float | None = None) -> dict:
            raise RuntimeError("no shell")

        finisher, *_ = self.make_finisher(handler=exploding)
        nudge = await finisher.rerun_declared_command("pytest -q", 2)
        self.assertIn("failed to execute", nudge)

    # -- fuse ---------------------------------------------------------------

    def test_fuse_aborts_at_the_limit(self) -> None:
        finisher, _, trace, _ = self.make_finisher(finish_violation_limit=2)
        finisher.note_finish_violation(1)
        with self.assertRaises(TurnFailure):
            finisher.note_finish_violation(2)
        self.assertTrue(any(event.event_type == "verification.aborted"
                            for event in trace.events))

    def test_executed_work_resets_the_fuse(self) -> None:
        finisher, *_ = self.make_finisher(finish_violation_limit=2)
        finisher.note_finish_violation(1)
        finisher.note_work_executed()
        finisher.note_finish_violation(2)
        self.assertEqual(finisher._finish_violations, 1)

    def test_zero_limit_disables_the_fuse(self) -> None:
        finisher, *_ = self.make_finisher(finish_violation_limit=0)
        for iteration in range(1, 6):
            finisher.note_finish_violation(iteration)
        self.assertEqual(finisher._finish_violations, 0)

    # -- contract outcome ----------------------------------------------------

    def test_contract_outcome_is_silent_when_verification_is_off(self) -> None:
        finisher, _, trace, _ = self.make_finisher(rules=OFF_RULES)
        finisher.emit_contract_outcome(11)
        self.assertEqual(trace.events, [])

    def test_contract_outcome_records_a_miss(self) -> None:
        finisher, _, trace, _ = self.make_finisher()
        finisher.emit_contract_outcome(11)
        self.assertEqual(trace.events[-1].event_type, "verification.failed")
        self.assertEqual(trace.events[-1].data["missing"],
                         [ENABLED_RULES.submit_tool])


class TurnFinisherWiringTests(unittest.TestCase):
    def test_each_turn_gets_its_own_finisher(self) -> None:
        harness = HarnessSpec(verification=ENABLED)
        trace = RunTrace(harness=harness)
        events = EventEmitter(run_id=trace.run_id)
        registry = ToolRegistry([])

        first = create_agent_turn("one", FakeLLM([]), registry,
                                  harness=harness, trace=trace, events=events)
        second = create_agent_turn("two", FakeLLM([]), registry,
                                   harness=harness, trace=trace, events=events)

        self.assertIsNot(first.finisher, second.finisher)
        self.assertIs(first.finisher.trace, trace)
        self.assertIs(first.finisher.events, events)
        self.assertIs(first.finisher.tools, registry)

    def test_finisher_reads_resolved_budget_and_limit(self) -> None:
        harness = HarnessSpec(verification=ENABLED)
        turn = create_agent_turn("one", FakeLLM([]), ToolRegistry([]), 4,
                                 harness=harness)
        self.assertEqual(turn.finisher.max_iterations, 4)
        self.assertEqual(turn.finisher.finish_violation_limit,
                         harness.control.finish_violation_limit)
        self.assertEqual(turn.finisher.rules,
                         SubmissionRules.from_genome(harness.verification))
