import json
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.agent.loop import run_turn
from agent_runtime.agent.verification import (
    match_executed_command,
    render_submission,
)
from agent_runtime.harness import (
    HarnessSpec,
    VerificationGenome,
    from_dict,
)
from agent_runtime.tools.tools import ToolRegistry, ToolSpec
from agent_runtime.trace import RunTrace
from tests.agent.test_loop import FakeLLM
from tests.agent.test_verification import (
    EDIT_SCHEMA,
    RUN_SCHEMA,
    contract_harness,
)


GOOD_FIELDS = {
    "solution_description": "Root cause was X in auth.py, fixed with quote_plus().",
    "evidence": "pytest tests/test_login.py -x passed: 5 passed in 1.2s, "
                "output observed above.",
    "command_to_verify": "pytest tests/test_login.py -x",
}

SUBMIT_SCHEMA = {
    "type": "object",
    "properties": {
        "solution_description": {"type": "string"},
        "evidence": {"type": "string"},
        "command_to_verify": {"type": "string"},
    },
    "required": ["solution_description", "evidence", "command_to_verify"],
    "additionalProperties": False,
}


async def fake_ok_run(command: str) -> dict:
    return {"exit_code": 0, "stdout": "5 passed in 1.2s", "stderr": ""}


async def fake_edit(path: str = "") -> dict:
    return {"ok": True}


async def exploding_submit(**kwargs) -> dict:
    raise AssertionError("submit_result must be intercepted, never executed")


def submit_registry(run_handler=None) -> ToolRegistry:
    return ToolRegistry([
        ToolSpec("run_command", "", RUN_SCHEMA, run_handler or fake_ok_run),
        ToolSpec("edit_file", "", EDIT_SCHEMA, fake_edit),
        ToolSpec("submit_result", "", SUBMIT_SCHEMA, exploding_submit),
    ])


def submit_turn(fields: dict, call_id: str = "9") -> dict:
    return {"content": "", "tool_calls": [{
        "id": call_id, "function": {
            "name": "submit_result", "arguments": json.dumps(fields)}}]}


def edit_turn(call_id: str = "0") -> dict:
    return {"content": "", "tool_calls": [{
        "id": call_id, "function": {
            "name": "edit_file", "arguments": '{"path": "auth.py"}'}}]}


def run_turn_call(command: str, call_id: str = "1") -> dict:
    return {"content": "", "tool_calls": [{
        "id": call_id, "function": {
            "name": "run_command",
            "arguments": json.dumps({"command": command})}}]}


def submit_harness(rerun: bool = False) -> HarnessSpec:
    return HarnessSpec(verification=VerificationGenome(
        enabled=True, mode="task_result",
        require=("solution_description", "evidence", "command_to_verify"),
        rerun_declared_command=rerun))


class MatchExecutedCommandTests(unittest.TestCase):
    def test_dirty_declaration_resolves_to_history_command(self) -> None:
        dirty = ("pytest tests/test_login.py -x`\nthis command was just "
                 "re-run and exits 0 (output: `5 passed`).")
        self.assertEqual(
            match_executed_command(dirty, ["pytest tests/test_login.py -x"]),
            "pytest tests/test_login.py -x")

    def test_unrun_command_matches_nothing(self) -> None:
        self.assertIsNone(
            match_executed_command("pytest tests/other.py",
                                   ["pytest tests/test_login.py -x"]))


class SubmitResultLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_submit_accepted_as_rendered_answer(self) -> None:
        llm = FakeLLM([edit_turn(), run_turn_call(GOOD_FIELDS["command_to_verify"]),
                       submit_turn(GOOD_FIELDS)])
        answer = await run_turn("fix it", llm, submit_registry(),
                                harness=submit_harness())
        self.assertEqual(answer, render_submission(GOOD_FIELDS))
        self.assertEqual(len(llm.messages), 3)

    async def test_thin_evidence_nudged_then_resubmitted(self) -> None:
        thin = dict(GOOD_FIELDS, evidence="looks good")
        llm = FakeLLM([edit_turn(), run_turn_call(GOOD_FIELDS["command_to_verify"]),
                       submit_turn(thin), submit_turn(GOOD_FIELDS)])
        answer = await run_turn("fix it", llm, submit_registry(),
                                harness=submit_harness())
        self.assertEqual(answer, render_submission(GOOD_FIELDS))
        self.assertEqual(len(llm.messages), 4)
        self.assertIn("evidence", llm.messages[3][-1]["content"])

    async def test_edit_less_submit_nudged_then_fixed(self) -> None:
        llm = FakeLLM([run_turn_call(GOOD_FIELDS["command_to_verify"]),
                       submit_turn(GOOD_FIELDS),
                       edit_turn(call_id="2"),
                       submit_turn(GOOD_FIELDS, call_id="3")])
        answer = await run_turn("fix it", llm, submit_registry(),
                                harness=submit_harness())
        self.assertEqual(answer, render_submission(GOOD_FIELDS))
        self.assertEqual(len(llm.messages), 4)
        self.assertIn("solution_description", llm.messages[2][-1]["content"])

    async def test_plain_text_finish_nudged_to_submit(self) -> None:
        llm = FakeLLM([edit_turn(), run_turn_call(GOOD_FIELDS["command_to_verify"]),
                       {"content": "done", "tool_calls": []},
                       submit_turn(GOOD_FIELDS)])
        answer = await run_turn("fix it", llm, submit_registry(),
                                harness=submit_harness())
        self.assertEqual(answer, render_submission(GOOD_FIELDS))
        self.assertEqual(len(llm.messages), 4)
        self.assertIn("submit_result", llm.messages[3][-1]["content"])

    async def test_mixed_work_and_submit_in_one_turn(self) -> None:
        llm = FakeLLM([
            edit_turn(),
            {"content": "", "tool_calls": [
                {"id": "1", "function": {
                    "name": "run_command",
                    "arguments": json.dumps(
                        {"command": GOOD_FIELDS["command_to_verify"]})}},
                {"id": "9", "function": {
                    "name": "submit_result",
                    "arguments": json.dumps(GOOD_FIELDS)}}]},
        ])
        answer = await run_turn("fix it", llm, submit_registry(),
                                harness=submit_harness())
        self.assertEqual(answer, render_submission(GOOD_FIELDS))
        self.assertEqual(len(llm.messages), 2)

    async def test_missing_arg_rejected_then_fixed(self) -> None:
        bad = {key: GOOD_FIELDS[key] for key in ("solution_description",
                                                 "command_to_verify")}
        llm = FakeLLM([edit_turn(), run_turn_call(GOOD_FIELDS["command_to_verify"]),
                       submit_turn(bad), submit_turn(GOOD_FIELDS)])
        answer = await run_turn("fix it", llm, submit_registry(),
                                harness=submit_harness())
        self.assertEqual(answer, render_submission(GOOD_FIELDS))
        self.assertEqual(len(llm.messages), 4)


class SubmitRerunTests(unittest.IsolatedAsyncioTestCase):
    async def test_dirty_declaration_reruns_history_match(self) -> None:
        seen: list[str] = []

        async def recording_run(command: str) -> dict:
            seen.append(command)
            return {"exit_code": 0, "stdout": "5 passed in 1.2s",
                    "stderr": ""}

        dirty = dict(GOOD_FIELDS, command_to_verify=(
            "pytest tests/test_login.py -x`\nthis command was just re-run "
            "and exits 0 (output: `5 passed`)."))
        llm = FakeLLM([edit_turn(), run_turn_call(GOOD_FIELDS["command_to_verify"]),
                       submit_turn(dirty)])
        trace = RunTrace()
        answer = await run_turn("fix it", llm, submit_registry(recording_run),
                                harness=submit_harness(rerun=True), trace=trace)
        self.assertEqual(answer, render_submission(dirty))
        # The rerun executed the clean history command, not the dirty string.
        self.assertEqual(seen[-1], GOOD_FIELDS["command_to_verify"])
        reruns = [event for event in trace.events
                  if event.event_type == "verification.rerun"]
        self.assertEqual(len(reruns), 1)
        self.assertTrue(reruns[0].data["success"])
        self.assertEqual(reruns[0].data["reran"],
                         GOOD_FIELDS["command_to_verify"])

    async def test_genuine_rerun_failure_rejects_then_recovers(self) -> None:
        async def routing_run(command: str) -> dict:
            if "test_pass" in command:
                return {"exit_code": 0, "stdout": "5 passed in 1.2s",
                        "stderr": ""}
            return {"exit_code": 1, "stdout": "1 failed in 0.5s",
                    "stderr": ""}

        failing = {
            "solution_description": GOOD_FIELDS["solution_description"],
            "evidence": "pytest tests/test_fail.py: 1 failed in 0.5s observed.",
            "command_to_verify": "pytest tests/test_fail.py",
        }
        passing = dict(GOOD_FIELDS, command_to_verify="pytest tests/test_pass.py")
        llm = FakeLLM([
            edit_turn(),
            run_turn_call("pytest tests/test_fail.py"),
            submit_turn(failing),
            run_turn_call("pytest tests/test_pass.py", call_id="2"),
            submit_turn(passing, call_id="3"),
        ])
        answer = await run_turn("fix it", llm, submit_registry(routing_run),
                                harness=submit_harness(rerun=True))
        self.assertEqual(answer, render_submission(passing))
        self.assertEqual(len(llm.messages), 5)
        self.assertIn("command_to_verify", llm.messages[3][-1]["content"])


class SubmitModeGatingTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_not_intercepted_in_return_contract_mode(self) -> None:
        seen: list[dict] = []

        async def dummy_submit(**kwargs) -> dict:
            seen.append(kwargs)
            return {"submitted": False}

        registry = ToolRegistry([
            ToolSpec("run_command", "", RUN_SCHEMA, fake_ok_run),
            ToolSpec("edit_file", "", EDIT_SCHEMA, fake_edit),
            ToolSpec("submit_result", "", SUBMIT_SCHEMA, dummy_submit),
        ])
        good_text = render_submission(GOOD_FIELDS)
        llm = FakeLLM([edit_turn(), run_turn_call(GOOD_FIELDS["command_to_verify"]),
                       submit_turn(GOOD_FIELDS),
                       {"content": good_text, "tool_calls": []}])
        answer = await run_turn("fix it", llm, registry,
                                harness=contract_harness())
        self.assertEqual(answer, good_text)
        self.assertEqual(len(seen), 1)  # executed as a normal tool


class TaskResultGenomeTests(unittest.TestCase):
    def test_task_result_fills_default_require(self) -> None:
        spec = from_dict({"verification": {"mode": "task_result"}})
        self.assertEqual(tuple(spec.verification.require),
                         ("solution_description", "evidence", "command_to_verify"))

    def test_rerun_allowed_with_task_result(self) -> None:
        spec = from_dict({"verification": {"mode": "task_result",
                                           "rerun_declared_command": True}})
        self.assertTrue(spec.verification.rerun_declared_command)

    def test_rerun_still_rejected_when_off(self) -> None:
        from agent_runtime.harness import HarnessError
        with self.assertRaises(HarnessError):
            from_dict({"verification": {"rerun_declared_command": True}})


if __name__ == "__main__":
    unittest.main()
