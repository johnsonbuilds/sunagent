import json
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.agent.loop import run_turn
from agent_runtime.agent.verification import (
    SUBMIT_FIELDS,
    check_submission,
    executed_commands,
)
from agent_runtime.harness import HarnessSpec, VerificationGenome, from_dict
from agent_runtime.tools.tools import ToolRegistry, ToolSpec
from tests.agent.test_loop import FakeLLM, make_registry


FIELDS = {
    "solution_description": "Root cause was X in auth.py, fixed with quote_plus().",
    "evidence": "pytest -q passed: 5 passed in 1.2s, output observed above.",
    "command_to_verify": "pytest tests/test_x.py -q",
}

REQUIRE = ("solution_description", "evidence", "command_to_verify")

RUN_SCHEMA = {
    "type": "object",
    "properties": {"command": {"type": "string"}},
    "required": ["command"],
}


async def fake_run(command: str) -> dict:
    return {"exit_code": 0,
            "stdout": "5 passed in 1.2s",
            "stderr": ""}


def run_registry() -> ToolRegistry:
    return ToolRegistry([ToolSpec("run_command", "", RUN_SCHEMA, fake_run)])


def assistant_run(command: str, call_id: str = "1") -> dict:
    return {"role": "assistant",
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": "run_command",
                                         "arguments": json.dumps({"command": command})}}]}


def assistant_edit(tool: str = "edit_file", call_id: str = "0") -> dict:
    return {"role": "assistant",
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": tool,
                                         "arguments": "{}"}}]}


EDIT_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
}


class ShapeTests(unittest.TestCase):
    def test_full_fields_pass_shape(self) -> None:
        self.assertEqual(check_submission(FIELDS, REQUIRE, None), {})

    def test_thin_values_flagged(self) -> None:
        gaps = check_submission({"solution_description": "fix", "evidence": "ok",
                                 "command_to_verify": "pytest tests/test_x.py -q"},
                                REQUIRE, None)
        self.assertIn("solution_description", gaps)
        self.assertIn("evidence", gaps)
        self.assertNotIn("command_to_verify", gaps)

    def test_require_subset_checked(self) -> None:
        self.assertEqual(check_submission({"solution_description": "x" * 25},
                                          ("solution_description",), None), {})


class GroundingTests(unittest.TestCase):
    def _messages(self) -> list:
        return [assistant_edit(),
                assistant_run("pytest tests/test_x.py -q"),
                {"role": "tool", "tool_call_id": "1",
                 "content": "5 passed in 1.2s"}]

    def test_grounded_submission_accepted(self) -> None:
        self.assertEqual(check_submission(FIELDS, REQUIRE, self._messages()), {})

    def test_edit_less_submission_rejected_as_solution_description(self) -> None:
        messages = [assistant_run("pytest tests/test_x.py -q"),
                    {"role": "tool", "tool_call_id": "1",
                     "content": "5 passed in 1.2s"}]
        gaps = check_submission(FIELDS, REQUIRE, messages)
        self.assertIn("solution_description", gaps)

    def test_each_edit_tool_counts(self) -> None:
        from agent_runtime.agent.verification import has_source_edit
        for tool in ("edit_file", "apply_patch", "write_file"):
            self.assertTrue(has_source_edit([assistant_edit(tool)]))
        self.assertFalse(has_source_edit([assistant_run("pytest tests/test_x.py -q")]))
        self.assertFalse(has_source_edit([]))

    def test_invented_evidence_rejected(self) -> None:
        fields = dict(FIELDS, evidence="all 200 integration tests green here")
        gaps = check_submission(fields, REQUIRE, self._messages())
        self.assertIn("evidence", gaps)

    def test_unrun_command_rejected(self) -> None:
        fields = dict(FIELDS, command_to_verify="npm test")
        gaps = check_submission(fields, REQUIRE, self._messages())
        self.assertIn("command_to_verify", gaps)

    def test_no_trajectory_at_all_rejected(self) -> None:
        gaps = check_submission(FIELDS, REQUIRE, [])
        self.assertIn("solution_description", gaps)
        self.assertIn("evidence", gaps)
        self.assertIn("command_to_verify", gaps)


class AuthenticityTests(unittest.TestCase):
    def test_any_run_command_is_authentic(self) -> None:
        # File-level commands pass here when executed and quoted.
        # Narrow/bare commands are rejected by the file-level gate.
        for command, evidence in (
                ("pytest tests/test_x.py -q", "pytest tests/test_x.py -q: 5 passed in 1.2s."),
                ("pytest tests/test_x.py", "pytest tests/test_x.py: 5 passed."),
                ("python tests/test_fix.py", "tests/test_fix.py: OK printed.")):
            messages = [assistant_edit(),
                        assistant_run(command),
                        {"role": "tool", "tool_call_id": "1",
                         "content": evidence}]
            fields = {"solution_description": FIELDS["solution_description"],
                      "evidence": evidence,
                      "command_to_verify": command}
            gaps = check_submission(fields, REQUIRE, messages)
            self.assertNotIn("command_to_verify", gaps, command)

    def test_history_commands_listed(self) -> None:
        messages = [{"role": "assistant", "tool_calls": [{
            "id": "1", "type": "function", "function": {
                "name": "run_command",
                "arguments": json.dumps({"command": "pytest tests/test_x.py -q",
                                         "cwd": "/testbed"})}}]}]
        self.assertEqual(executed_commands(messages), ["pytest tests/test_x.py -q"])

    def test_program_only_similarity_rejected(self) -> None:
        # No substring relation, only a shared program: not grounded.
        # (The old program+tokens rule misfired on shell-prefixed commands.)
        messages = [assistant_edit(),
                    assistant_run("pytest tests/other/ -q -x"),
                    {"role": "tool", "tool_call_id": "1",
                     "content": "5 passed in 1.2s"}]
        gaps = check_submission(FIELDS, REQUIRE, messages)
        self.assertIn("command_to_verify", gaps)


class ExitCodeMaskingTests(unittest.TestCase):
    def _piped(self, command: str) -> dict[str, str]:
        evidence = f"{command}: 5 passed in 1.2s observed."
        messages = [assistant_edit(),
                    assistant_run(command),
                    {"role": "tool", "tool_call_id": "1",
                     "content": "5 passed in 1.2s"}]
        fields = {"solution_description": FIELDS["solution_description"],
                  "evidence": evidence,
                  "command_to_verify": command}
        return check_submission(fields, REQUIRE, messages)

    def test_piped_declaration_rejected(self) -> None:
        gaps = self._piped("pytest -q 2>&1 | tail -20")
        self.assertIn("command_to_verify", gaps)
        self.assertIn("mask the test exit code",
                      gaps["command_to_verify"])

    def test_semicolon_chain_rejected(self) -> None:
        gaps = self._piped("pytest -q; echo done")
        self.assertIn("command_to_verify", gaps)

    def test_and_chain_and_redirect_allowed(self) -> None:
        for command in ("cd /testbed && pytest tests/test_x.py -q",
                        "pytest tests/test_x.py -q > /tmp/run.log 2>&1",
                        "source env.sh && conda activate t && pytest tests/test_x.py -q"):
            gaps = self._piped(command)
            self.assertNotIn("command_to_verify", gaps, command)

    def test_quoted_pipe_is_not_a_pipe(self) -> None:
        from agent_runtime.agent.verification import _masks_exit_code
        self.assertFalse(_masks_exit_code("pytest -q --tb=no 2>&1"))
        self.assertFalse(_masks_exit_code("grep -E '^(FAILED|ERROR)' /tmp/x"))
        self.assertTrue(_masks_exit_code("pytest -q | grep -E 'a|b'"))
        self.assertTrue(_masks_exit_code("pytest -q || echo failed"))


class FileLevelTests(unittest.TestCase):
    def _file_level(self, command: str) -> dict[str, str]:
        evidence = f"{command}: 5 passed in 1.2s observed."
        messages = [assistant_edit(),
                    assistant_run(command),
                    {"role": "tool", "tool_call_id": "1",
                     "content": "5 passed in 1.2s"}]
        fields = {"solution_description": FIELDS["solution_description"],
                  "evidence": evidence,
                  "command_to_verify": command}
        return check_submission(fields, REQUIRE, messages)

    def test_narrow_k_rejected(self) -> None:
        gaps = self._file_level('pytest tests/test_x.py -q -k "foo"')
        self.assertIn("command_to_verify", gaps)

    def test_bare_pytest_rejected(self) -> None:
        gaps = self._file_level("cd /testbed && pytest -q")
        self.assertIn("command_to_verify", gaps)

    def test_file_level_accepted(self) -> None:
        gaps = self._file_level("cd /testbed && pytest tests/test_auth.py -q --tb=short")
        self.assertNotIn("command_to_verify", gaps)

    def test_prefix_chain_accepted(self) -> None:
        gaps = self._file_level(
            "source env.sh && conda activate t && pytest tests/test_x.py -q")
        self.assertNotIn("command_to_verify", gaps)


class BudgetReminderGenomeTests(unittest.TestCase):
    def test_defaults_off(self) -> None:
        spec = from_dict({})
        self.assertFalse(spec.control.budget_reminder.enabled)

    def test_enabled_parsed(self) -> None:
        spec = from_dict({"control": {"budget_reminder": {
            "enabled": True, "at_fractions": [0.6, 0.85]}}})
        self.assertTrue(spec.control.budget_reminder.enabled)
        self.assertEqual(tuple(spec.control.budget_reminder.at_fractions),
                         (0.6, 0.85))

    def test_bad_fraction_rejected(self) -> None:
        from agent_runtime.harness import HarnessError
        with self.assertRaises(HarnessError):
            from_dict({"control": {"budget_reminder": {
                "enabled": True, "at_fractions": [1.5]}}})


class VerificationGenomeTests(unittest.TestCase):
    def test_return_contract_fills_default_require(self) -> None:
        spec = from_dict({"verification": {"mode": "return_contract"}})
        self.assertEqual(tuple(spec.verification.require), SUBMIT_FIELDS)

    def test_task_result_fills_default_require(self) -> None:
        spec = from_dict({"verification": {"mode": "task_result"}})
        self.assertEqual(tuple(spec.verification.require), SUBMIT_FIELDS)

    def test_bad_mode_rejected(self) -> None:
        from agent_runtime.harness import HarnessError
        with self.assertRaises(HarnessError):
            from_dict({"verification": {"mode": "judge"}})

    def test_legacy_rerun_key_tolerated(self) -> None:
        spec = from_dict({"verification": {"rerun_declared_command": True}})
        self.assertFalse(hasattr(spec.verification, "rerun_declared_command"))


class OffModeLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_off_mode_accepts_text_immediately(self) -> None:
        llm = FakeLLM([{"content": "done", "tool_calls": []}])
        answer = await run_turn("fix it", llm, make_registry(),
                                harness=HarnessSpec())
        self.assertEqual(answer, "done")
        self.assertEqual(len(llm.messages), 1)

    async def test_last_iteration_text_accepted_as_is(self) -> None:
        llm = FakeLLM([{"content": "done", "tool_calls": []}])
        from agent_runtime.harness import ControlGenome
        harness = HarnessSpec(control=ControlGenome(max_iterations=1),
                              verification=VerificationGenome(
                                  enabled=True, mode="task_result",
                                  require=("solution_description",)))
        answer = await run_turn("fix it", llm, make_registry(), harness=harness)
        self.assertEqual(answer, "done")
        self.assertEqual(len(llm.messages), 1)


if __name__ == "__main__":
    unittest.main()
