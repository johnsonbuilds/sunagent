import json
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.agent.loop import run_turn
from agent_runtime.agent.verification import check_contract, missing_fields
from agent_runtime.harness import HarnessSpec, VerificationGenome, from_dict
from agent_runtime.tools.tools import ToolRegistry, ToolSpec
from tests.agent.test_loop import FakeLLM, make_registry


GOOD = """solution_description: Root cause was X in auth.py, fixed with quote_plus().
evidence: pytest tests/test_login.py -x passed: 5 passed in 1.2s, output observed above.
command_to_verify: pytest tests/test_login.py -x"""

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


def contract_harness() -> HarnessSpec:
    return HarnessSpec(verification=VerificationGenome(
        enabled=True, mode="return_contract",
        require=("solution_description", "evidence", "command_to_verify")))


class VerificationParsingTests(unittest.TestCase):
    def test_full_answer_shape_passes(self) -> None:
        self.assertEqual(missing_fields(GOOD, ("solution_description", "evidence",
                                               "command_to_verify")), [])

    def test_missing_key_reported(self) -> None:
        self.assertEqual(missing_fields("nothing here", ("evidence",)), ["evidence"])

    def test_bare_header_without_content_fails(self) -> None:
        self.assertEqual(missing_fields("evidence:", ("evidence",)), ["evidence"])

    def test_short_command_accepted_by_shape(self) -> None:
        self.assertEqual(missing_fields("command_to_verify: pytest -q",
                                        ("command_to_verify",)), [])

    def test_json_object_accepted(self) -> None:
        answer = ('{"solution_description": "root cause and fix described in detail here", '
                  '"evidence": "pytest passed with 5 tests green observed", '
                  '"command_to_verify": "pytest tests/ -x"}')
        self.assertEqual(missing_fields(answer, ("solution_description", "evidence",
                                                 "command_to_verify")), [])

    def test_json_thin_value_fails(self) -> None:
        answer = ('{"solution_description": "fix", "evidence": "ok", '
                  '"command_to_verify": "pytest tests/ -x"}')
        missing = missing_fields(answer, ("solution_description", "evidence",
                                           "command_to_verify"))
        self.assertIn("solution_description", missing)
        self.assertIn("evidence", missing)


class GroundingTests(unittest.TestCase):
    def _messages(self) -> list:
        return [assistant_run("pytest tests/test_login.py -x"),
                {"role": "tool", "tool_call_id": "1",
                 "content": "5 passed in 1.2s"}]

    def test_grounded_answer_accepted(self) -> None:
        gaps = check_contract(GOOD, ("solution_description", "evidence",
                                     "command_to_verify"), self._messages())
        self.assertEqual(gaps, {})

    def test_invented_evidence_rejected(self) -> None:
        answer = GOOD.replace("5 passed in 1.2s", "all 200 integration tests green")
        gaps = check_contract(answer, ("solution_description", "evidence",
                                       "command_to_verify"), self._messages())
        self.assertIn("evidence", gaps)

    def test_unrun_command_rejected(self) -> None:
        answer = GOOD.replace("pytest tests/test_login.py -x", "pytest tests/other.py")
        gaps = check_contract(answer, ("solution_description", "evidence",
                                       "command_to_verify"), self._messages())
        self.assertIn("command_to_verify", gaps)

    def test_no_trajectory_at_all_rejected(self) -> None:
        gaps = check_contract(GOOD, ("solution_description", "evidence",
                                     "command_to_verify"), [])
        self.assertIn("evidence", gaps)
        self.assertIn("command_to_verify", gaps)

    def test_no_messages_means_shape_only(self) -> None:
        gaps = check_contract(GOOD, ("solution_description", "evidence",
                                     "command_to_verify"), None)
        self.assertEqual(gaps, {})


class VerificationGenomeTests(unittest.TestCase):
    def test_return_contract_fills_default_require(self) -> None:
        spec = from_dict({"verification": {"mode": "return_contract"}})
        self.assertEqual(tuple(spec.verification.require),
                         ("solution_description", "evidence", "command_to_verify"))

    def test_bad_mode_rejected(self) -> None:
        from agent_runtime.harness import HarnessError
        with self.assertRaises(HarnessError):
            from_dict({"verification": {"mode": "judge"}})

    def test_rerun_requires_return_contract(self) -> None:
        from agent_runtime.harness import HarnessError
        with self.assertRaises(HarnessError):
            from_dict({"verification": {"rerun_declared_command": True}})


class ReturnContractLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_then_grounded_answer_accepted(self) -> None:
        llm = FakeLLM([
            {"content": "", "tool_calls": [{"id": "1", "function": {
                "name": "run_command",
                "arguments": '{"command": "pytest tests/test_login.py -x"}'}}]},
            {"content": GOOD, "tool_calls": []},
        ])
        answer = await run_turn("fix it", llm, run_registry(),
                                harness=contract_harness())
        self.assertEqual(answer, GOOD)
        self.assertEqual(len(llm.messages), 2)

    async def test_invented_evidence_nudged_then_fixed(self) -> None:
        invented = GOOD.replace("5 passed in 1.2s", "all 200 integration tests green")
        llm = FakeLLM([
            {"content": "", "tool_calls": [{"id": "1", "function": {
                "name": "run_command",
                "arguments": '{"command": "pytest tests/test_login.py -x"}'}}]},
            {"content": invented, "tool_calls": []},
            {"content": GOOD, "tool_calls": []},
        ])
        answer = await run_turn("fix it", llm, run_registry(),
                                harness=contract_harness())
        self.assertEqual(answer, GOOD)
        self.assertEqual(len(llm.messages), 3)
        self.assertIn("evidence", llm.messages[2][-1]["content"])

    async def test_off_mode_accepts_immediately(self) -> None:
        llm = FakeLLM([{"content": "done", "tool_calls": []}])
        answer = await run_turn("fix it", llm, make_registry(),
                                harness=HarnessSpec())
        self.assertEqual(answer, "done")
        self.assertEqual(len(llm.messages), 1)

    async def test_last_iteration_accepts_without_retry(self) -> None:
        llm = FakeLLM([{"content": "done", "tool_calls": []}])
        from agent_runtime.harness import ControlGenome
        harness = HarnessSpec(control=ControlGenome(max_iterations=1),
                              verification=VerificationGenome(
                                  enabled=True, mode="return_contract",
                                  require=("solution_description",)))
        answer = await run_turn("fix it", llm, make_registry(), harness=harness)
        self.assertEqual(answer, "done")
        self.assertEqual(len(llm.messages), 1)


if __name__ == "__main__":
    unittest.main()
