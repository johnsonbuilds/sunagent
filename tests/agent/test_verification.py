import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.agent.loop import run_turn
from agent_runtime.agent.verification import missing_fields
from agent_runtime.harness import HarnessSpec, VerificationGenome, from_dict
from agent_runtime.tools.tools import ToolRegistry, ToolSpec
from tests.agent.test_loop import FakeLLM, make_registry


GOOD = """solution_description: Root cause was X in auth.py, fixed with quote_plus().
evidence: pytest tests/test_login.py -x passed: 5 passed in 1.2s, output observed above.
command_to_verify: pytest tests/test_login.py -x"""


def contract_harness() -> HarnessSpec:
    return HarnessSpec(verification=VerificationGenome(
        enabled=True, mode="return_contract",
        require=("solution_description", "evidence", "command_to_verify")))


class VerificationParsingTests(unittest.TestCase):
    def test_full_answer_passes(self) -> None:
        self.assertEqual(missing_fields(GOOD, ("solution_description", "evidence",
                                               "command_to_verify")), [])

    def test_missing_key_reported(self) -> None:
        self.assertEqual(missing_fields("nothing here", ("evidence",)), ["evidence"])

    def test_bare_header_without_content_fails(self) -> None:
        self.assertEqual(missing_fields("evidence:", ("evidence",)), ["evidence"])

    def test_json_object_accepted(self) -> None:
        answer = ('{"solution_description": "root cause and fix described in detail here", '
                  '"evidence": "pytest passed with 5 tests green observed", '
                  '"command_to_verify": "pytest tests/test_login.py -x"}')
        self.assertEqual(missing_fields(answer, ("solution_description", "evidence",
                                                 "command_to_verify")), [])

    def test_json_thin_value_fails(self) -> None:
        answer = ('{"solution_description": "fix", "evidence": "ok", '
                  '"command_to_verify": "pytest tests/ -x"}')
        missing = missing_fields(answer, ("solution_description", "evidence",
                                           "command_to_verify"))
        self.assertIn("solution_description", missing)
        self.assertIn("evidence", missing)


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
    async def test_missing_contract_retries_then_accepts(self) -> None:
        llm = FakeLLM([
            {"content": "done, fixed it", "tool_calls": []},
            {"content": GOOD, "tool_calls": []},
        ])
        answer = await run_turn("fix it", llm, make_registry(),
                                harness=contract_harness())
        self.assertEqual(answer, GOOD)
        self.assertEqual(len(llm.messages), 2)
        self.assertIn("command_to_verify", llm.messages[1][-1]["content"])

    async def test_off_mode_accepts_immediately(self) -> None:
        llm = FakeLLM([{"content": "done", "tool_calls": []}])
        answer = await run_turn("fix it", llm, make_registry(),
                                harness=HarnessSpec())
        self.assertEqual(answer, "done")
        self.assertEqual(len(llm.messages), 1)

    async def test_last_iteration_accepts_without_retry(self) -> None:
        llm = FakeLLM([{"content": "done", "tool_calls": []}])
        harness = HarnessSpec(
            verification=VerificationGenome(
                enabled=True, mode="return_contract",
                require=("solution_description",)),
        )
        from agent_runtime.harness import ControlGenome
        harness = HarnessSpec(control=ControlGenome(max_iterations=1),
                              verification=harness.verification)
        answer = await run_turn("fix it", llm, make_registry(), harness=harness)
        self.assertEqual(answer, "done")
        self.assertEqual(len(llm.messages), 1)


if __name__ == "__main__":
    unittest.main()
