import json
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.agent.loop import run_turn
from agent_runtime.agent.verification import render_submission
from agent_runtime.harness import (
    ControlGenome,
    HarnessSpec,
    VerificationGenome,
    from_dict,
)
from agent_runtime.tools.tools import ToolRegistry, ToolSpec
from agent_runtime.trace import RunTrace
from tests.agent.test_loop import FakeLLM
from tests.agent.test_verification import EDIT_SCHEMA, RUN_SCHEMA


GOOD_FIELDS = {
    "solution_description": "Root cause was X in auth.py, fixed with quote_plus().",
    "evidence": "pytest -q passed: 5 passed in 1.2s, output observed above.",
    "command_to_verify": "pytest -q",
}

REQUIRE = ("solution_description", "evidence", "command_to_verify")

# Mirrors the real submit_result spec: command_to_verify is optional at
# the schema level so non-code harnesses can omit it; code harnesses
# enforce it via verification.require.
SUBMIT_SCHEMA = {
    "type": "object",
    "properties": {
        "solution_description": {"type": "string"},
        "evidence": {"type": "string"},
        "command_to_verify": {"type": "string"},
    },
    "required": ["solution_description", "evidence"],
    "additionalProperties": False,
}


async def fake_ok_run(command: str, **kwargs) -> dict:
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


def run_turn_call(command: str, call_id: str = "1",
                  cwd: str | None = None) -> dict:
    arguments: dict = {"command": command}
    if cwd is not None:
        arguments["cwd"] = cwd
    return {"content": "", "tool_calls": [{
        "id": call_id, "function": {
            "name": "run_command", "arguments": json.dumps(arguments)}}]}


def submit_harness(require=("solution_description", "evidence",
                            "command_to_verify")) -> HarnessSpec:
    return HarnessSpec(verification=VerificationGenome(
        enabled=True, mode="task_result", require=require))


class MatchCommandTests(unittest.TestCase):
    def test_substring_declaration_accepted(self) -> None:
        from agent_runtime.agent.verification import _command_grounded
        self.assertTrue(_command_grounded(
            "pytest -q", ["cd /testbed && pytest -q 2>&1 | tail -5"]))

    def test_program_only_similarity_rejected(self) -> None:
        from agent_runtime.agent.verification import _command_grounded
        self.assertFalse(_command_grounded(
            "pytest -q", ["pytest tests/other/ -q -x"]))
        self.assertFalse(_command_grounded("npm test", ["pytest -q"]))


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
        bad = {"solution_description": GOOD_FIELDS["solution_description"]}
        llm = FakeLLM([edit_turn(), run_turn_call(GOOD_FIELDS["command_to_verify"]),
                       submit_turn(bad), submit_turn(GOOD_FIELDS)])
        answer = await run_turn("fix it", llm, submit_registry(),
                                harness=submit_harness())
        self.assertEqual(answer, render_submission(GOOD_FIELDS))
        self.assertEqual(len(llm.messages), 4)

    async def test_narrow_command_accepted_when_run_and_quoted(self) -> None:
        # Fullness is the prompt contract per repository; the gate only
        # verifies authenticity (run, passing on rerun, quoted).
        scoped = dict(GOOD_FIELDS,
                      evidence="pytest tests/test_x.py: 5 passed in 1.2s.",
                      command_to_verify="pytest tests/test_x.py")
        llm = FakeLLM([edit_turn(),
                       run_turn_call("pytest tests/test_x.py"),
                       submit_turn(scoped)])
        answer = await run_turn("fix it", llm, submit_registry(),
                                harness=submit_harness())
        self.assertEqual(answer, render_submission(scoped))
        self.assertEqual(len(llm.messages), 3)

    async def test_bespoke_script_accepted_when_run_and_quoted(self) -> None:
        script = dict(GOOD_FIELDS,
                      evidence="python /tmp/verify_fix.py: 5 passed in 1.2s.",
                      command_to_verify="python /tmp/verify_fix.py")
        llm = FakeLLM([edit_turn(),
                       run_turn_call("python /tmp/verify_fix.py"),
                       submit_turn(script)])
        answer = await run_turn("fix it", llm, submit_registry(),
                                harness=submit_harness())
        self.assertEqual(answer, render_submission(script))
        self.assertEqual(len(llm.messages), 3)


class NonCodeHarnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_optional_without_rerun(self) -> None:
        fields = {"solution_description": GOOD_FIELDS["solution_description"],
                  "evidence": GOOD_FIELDS["evidence"]}
        llm = FakeLLM([edit_turn(), run_turn_call("pytest -q"),
                       submit_turn(fields)])
        harness = submit_harness(
            require=("solution_description", "evidence"))
        trace = RunTrace()
        answer = await run_turn("fix it", llm, submit_registry(),
                                harness=harness, trace=trace)
        self.assertEqual(answer, render_submission(fields))
        self.assertEqual(len(llm.messages), 3)
        self.assertFalse(any(event.event_type == "verification.rerun"
                             for event in trace.events))


class SubmitRerunTests(unittest.IsolatedAsyncioTestCase):
    async def test_dirty_declaration_runs_verbatim_and_fails(self) -> None:
        seen: list[str] = []

        async def recording_run(command: str, **kwargs) -> dict:
            seen.append(command)
            if "`" in command or "\n" in command:
                return {"exit_code": 2, "stdout": "",
                        "stderr": "bash: unexpected EOF"}
            return {"exit_code": 0, "stdout": "5 passed in 1.2s",
                    "stderr": ""}

        dirty = dict(GOOD_FIELDS, command_to_verify=(
            "pytest -q`\nthis command was just re-run "
            "and exits 0 (output: `5 passed`)."))
        llm = FakeLLM([edit_turn(), run_turn_call(GOOD_FIELDS["command_to_verify"]),
                       submit_turn(dirty),
                       submit_turn(GOOD_FIELDS, call_id="4")])
        trace = RunTrace()
        answer = await run_turn("fix it", llm, submit_registry(recording_run),
                                harness=submit_harness(), trace=trace)
        # Grounding still matches by substring, but the rerun executes the
        # dirty string verbatim — declare exactly what was run instead.
        self.assertEqual(answer, render_submission(GOOD_FIELDS))
        self.assertEqual(len(llm.messages), 4)
        self.assertIn("command_to_verify", llm.messages[3][-1]["content"])
        reruns = [event for event in trace.events
                  if event.event_type == "verification.rerun"]
        self.assertEqual(len(reruns), 2)
        self.assertFalse(reruns[0].data["success"])
        self.assertEqual(reruns[0].data["command"], dirty["command_to_verify"])
        self.assertTrue(reruns[1].data["success"])

    async def test_rerun_verbatim_with_long_timeout(self) -> None:
        seen: list[dict] = []

        async def recording_run(command: str, cwd=None,
                                timeout=None) -> dict:
            seen.append({"command": command, "cwd": cwd, "timeout": timeout})
            return {"exit_code": 0, "stdout": "5 passed in 1.2s",
                    "stderr": ""}

        llm = FakeLLM([edit_turn(),
                       run_turn_call("pytest -q", cwd="/testbed"),
                       submit_turn(GOOD_FIELDS)])
        trace = RunTrace()
        answer = await run_turn("fix it", llm, submit_registry(recording_run),
                                harness=submit_harness(), trace=trace)
        self.assertEqual(answer, render_submission(GOOD_FIELDS))
        # History cwd is NOT replayed: the declared string runs verbatim
        # with the long suite timeout instead of the 30s tool default.
        self.assertEqual(seen[-1], {"command": "pytest -q", "cwd": None,
                                    "timeout": 600.0})
        reruns = [event for event in trace.events
                  if event.event_type == "verification.rerun"]
        self.assertEqual(len(reruns), 1)
        self.assertEqual(reruns[0].data["command"], "pytest -q")

    async def test_genuine_rerun_failure_rejects_then_recovers(self) -> None:
        async def routing_run(command: str, **kwargs) -> dict:
            if " -x" in command:
                return {"exit_code": 1, "stdout": "1 failed in 0.5s",
                        "stderr": ""}
            return {"exit_code": 0, "stdout": "5 passed in 1.2s",
                    "stderr": ""}

        failing = {
            "solution_description": GOOD_FIELDS["solution_description"],
            "evidence": "pytest -q -x: 1 failed in 0.5s observed.",
            "command_to_verify": "pytest -q -x",
        }
        llm = FakeLLM([
            edit_turn(),
            run_turn_call("pytest -q -x"),
            submit_turn(failing),
            run_turn_call("pytest -q", call_id="2"),
            submit_turn(GOOD_FIELDS, call_id="3"),
        ])
        answer = await run_turn("fix it", llm, submit_registry(routing_run),
                                harness=submit_harness())
        self.assertEqual(answer, render_submission(GOOD_FIELDS))
        self.assertEqual(len(llm.messages), 5)
        self.assertIn("command_to_verify", llm.messages[3][-1]["content"])

    async def test_genuine_failure_nudge_names_what_failed(self) -> None:
        async def routing_run(command: str, **kwargs) -> dict:
            if " -x" in command:
                return {"exit_code": 1,
                        "stdout": ("....F..\n1 failed, 5 passed in 0.5s\n"
                                   "FAILED test_x.py::test_y - "
                                   "AssertionError: bad value\n"),
                        "stderr": ""}
            return {"exit_code": 0, "stdout": "5 passed in 1.2s",
                    "stderr": ""}

        failing = {
            "solution_description": GOOD_FIELDS["solution_description"],
            "evidence": "pytest -q -x: 1 failed, 5 passed in 0.5s observed.",
            "command_to_verify": "pytest -q -x",
        }
        llm = FakeLLM([
            edit_turn(),
            run_turn_call("pytest -q -x"),
            submit_turn(failing),
            run_turn_call("pytest -q", call_id="2"),
            submit_turn(GOOD_FIELDS, call_id="3"),
        ])
        answer = await run_turn("fix it", llm, submit_registry(routing_run),
                                harness=submit_harness())
        self.assertEqual(answer, render_submission(GOOD_FIELDS))
        # The nudge states the result + the acceptance criterion only:
        # no fix target (tests vs. source belongs to the task
        # instructions), no prescribed action, no blanket "full suite".
        nudge = llm.messages[3][-1]["content"]
        self.assertIn("test_x.py::test_y", nudge)
        self.assertIn("1 failed, 5 passed", nudge)
        self.assertIn("only accepted when that command exits 0", nudge)
        self.assertNotIn("full suite", nudge)
        self.assertNotIn("fix the failing tests", nudge)
        self.assertNotIn("Re-run", nudge)
        self.assertNotIn("until it exits 0", nudge)

    async def test_piped_declaration_rejected_then_fixed(self) -> None:
        piped = dict(
            GOOD_FIELDS,
            evidence="pytest -q | tail: 5 passed in 1.2s observed.",
            command_to_verify="pytest -q 2>&1 | tail -5",
        )
        llm = FakeLLM([
            edit_turn(),
            run_turn_call("pytest -q 2>&1 | tail -5"),
            submit_turn(piped),
            run_turn_call("pytest -q", call_id="2"),
            submit_turn(GOOD_FIELDS, call_id="3"),
        ])
        answer = await run_turn("fix it", llm, submit_registry(),
                                harness=submit_harness())
        self.assertEqual(answer, render_submission(GOOD_FIELDS))
        # Grounded (it was run) but rejected: the pipe masks exit code.
        nudge = llm.messages[3][-1]["content"]
        self.assertIn("mask the test exit code", nudge)
        self.assertNotIn("full suite", nudge)

    async def test_redirect_declaration_accepted(self) -> None:
        logged = dict(
            GOOD_FIELDS,
            evidence="pytest -q > log: 5 passed in 1.2s observed.",
            command_to_verify="cd /testbed && pytest -q > /tmp/run.log 2>&1",
        )
        llm = FakeLLM([
            edit_turn(),
            run_turn_call("cd /testbed && pytest -q > /tmp/run.log 2>&1"),
            submit_turn(logged),
        ])
        answer = await run_turn("fix it", llm, submit_registry(),
                                harness=submit_harness())
        self.assertEqual(answer, render_submission(logged))

    async def test_unlisted_env_error_still_declaration_fix(self) -> None:
        # No hardcoded error list: any rerun without test-runner output
        # (here "Permission denied", never in any list) takes the
        # declaration-fix branch.
        async def routing_run(command: str, **kwargs) -> dict:
            if command == "./run_tests.sh":
                return {"exit_code": 126, "stdout": "",
                        "stderr": "bash: ./run_tests.sh: Permission denied"}
            return {"exit_code": 0, "stdout": "5 passed in 1.2s",
                    "stderr": ""}

        broken = dict(
            GOOD_FIELDS,
            evidence="./run_tests.sh: Permission denied observed.",
            command_to_verify="./run_tests.sh",
        )
        llm = FakeLLM([
            edit_turn(),
            run_turn_call("./run_tests.sh"),
            submit_turn(broken),
            run_turn_call("pytest -q", call_id="2"),
            submit_turn(GOOD_FIELDS, call_id="3"),
        ])
        answer = await run_turn("fix it", llm, submit_registry(routing_run),
                                harness=submit_harness())
        self.assertEqual(answer, render_submission(GOOD_FIELDS))
        nudge = llm.messages[3][-1]["content"]
        self.assertIn("never reached the tests", nudge)
        self.assertNotIn("full suite", nudge)
        self.assertNotIn("yourself", nudge)

    async def test_rerun_excerpt_keeps_head_and_tail(self) -> None:
        from agent_runtime.agent.loop import _rerun_outcome
        stdout = "START\n" + "." * 3000 + "\n1 failed, 5 passed in 0.5s\n"
        _, _, excerpt = _rerun_outcome(
            {"exit_code": 1, "stdout": stdout, "stderr": ""})
        self.assertIn("START", excerpt)
        self.assertIn("1 failed, 5 passed", excerpt)
        self.assertIn("truncated", excerpt)

    async def test_contract_nudge_states_reasons_only(self) -> None:
        from agent_runtime.agent.verification import contract_nudge
        nudge = contract_nudge({"solution_description": "no source edits yet",
                                "command_to_verify": "was never run"})
        self.assertIn("no source edits yet", nudge)
        self.assertIn("was never run", nudge)
        self.assertIn("submit_result requires", nudge)
        self.assertNotIn("Next step", nudge)
        self.assertNotIn("full suite", nudge)
        self.assertNotIn("until it exits 0", nudge)

    async def test_ran_detection_covers_runners_not_error_lists(self) -> None:
        from agent_runtime.agent.loop import _rerun_declaration_broken
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

    async def test_env_broken_rerun_nudges_declaration_fix(self) -> None:
        async def routing_run(command: str, **kwargs) -> dict:
            if command == "python -m pytest -q":
                return {"exit_code": 1, "stdout": "",
                        "stderr": ("/opt/miniconda3/bin/python: "
                                   "No module named pytest")}
            return {"exit_code": 0, "stdout": "5 passed in 1.2s",
                    "stderr": ""}

        broken = dict(
            GOOD_FIELDS,
            evidence=("python -m pytest -q: No module named pytest observed."),
            command_to_verify="python -m pytest -q",
        )
        fixed = dict(
            GOOD_FIELDS,
            evidence="env python -m pytest -q: 5 passed in 1.2s observed.",
            command_to_verify="/opt/miniconda3/envs/testbed/bin/python -m pytest -q",
        )
        llm = FakeLLM([
            edit_turn(),
            run_turn_call("python -m pytest -q"),
            submit_turn(broken),
            run_turn_call(fixed["command_to_verify"], call_id="2"),
            submit_turn(fixed, call_id="3"),
        ])
        trace = RunTrace()
        answer = await run_turn("fix it", llm, submit_registry(routing_run),
                                harness=submit_harness(), trace=trace)
        self.assertEqual(answer, render_submission(fixed))
        reruns = [event for event in trace.events
                  if event.event_type == "verification.rerun"]
        self.assertEqual(len(reruns), 2)
        self.assertFalse(reruns[0].data["success"])
        # Result + reason only: no prescribed action, no full-suite ask
        # (the tests were never reached).
        nudge = llm.messages[3][-1]["content"]
        self.assertIn("never reached the tests", nudge)
        self.assertNotIn("full suite", nudge)
        self.assertNotIn("yourself", nudge)


class FinishFuseTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_text_finishes_abort_the_turn(self) -> None:
        llm = FakeLLM([{"content": "done", "tool_calls": []}] * 3)
        trace = RunTrace()
        answer = await run_turn("fix it", llm, submit_registry(),
                                harness=submit_harness(), trace=trace)
        self.assertIn("ABORTED_REPEATED_FINISH_VIOLATIONS", answer)
        self.assertEqual(len(llm.messages), 3)
        self.assertTrue(any(event.event_type == "verification.aborted"
                            for event in trace.events))

    async def test_interleaving_work_resets_the_fuse(self) -> None:
        llm = FakeLLM([{"content": "done", "tool_calls": []},
                       {"content": "done", "tool_calls": []},
                       edit_turn(),
                       {"content": "done", "tool_calls": []},
                       {"content": "done", "tool_calls": []},
                       edit_turn(call_id="2"),
                       run_turn_call("pytest -q", call_id="3"),
                       submit_turn(GOOD_FIELDS, call_id="4")])
        answer = await run_turn("fix it", llm, submit_registry(),
                                harness=submit_harness())
        self.assertEqual(answer, render_submission(GOOD_FIELDS))

    async def test_repeated_invalid_submits_abort(self) -> None:
        thin = dict(GOOD_FIELDS, evidence="looks good")
        llm = FakeLLM([submit_turn(thin, call_id=str(n)) for n in range(3)])
        answer = await run_turn("fix it", llm, submit_registry(),
                                harness=submit_harness())
        self.assertIn("ABORTED_REPEATED_FINISH_VIOLATIONS", answer)
        self.assertEqual(len(llm.messages), 3)

    async def test_fuse_disabled_with_zero_limit(self) -> None:
        harness = HarnessSpec(
            control=ControlGenome(max_iterations=4, finish_violation_limit=0),
            verification=VerificationGenome(
                enabled=True, mode="task_result", require=REQUIRE))
        llm = FakeLLM([{"content": "done", "tool_calls": []}] * 4)
        answer = await run_turn("fix it", llm, submit_registry(),
                                harness=harness)
        self.assertNotIn("ABORTED", answer)
        self.assertEqual(len(llm.messages), 4)


class SubmitModeGatingTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_mode_unifies_on_submit_behavior(self) -> None:
        seen: list[dict] = []

        async def dummy_submit(**kwargs) -> dict:
            seen.append(kwargs)
            return {"submitted": False}

        registry = ToolRegistry([
            ToolSpec("run_command", "", RUN_SCHEMA, fake_ok_run),
            ToolSpec("edit_file", "", EDIT_SCHEMA, fake_edit),
            ToolSpec("submit_result", "", SUBMIT_SCHEMA, dummy_submit),
        ])
        legacy = HarnessSpec(verification=VerificationGenome(
            enabled=True, mode="return_contract", require=REQUIRE))
        llm = FakeLLM([edit_turn(), run_turn_call("pytest -q"),
                       {"content": "still prose", "tool_calls": []},
                       submit_turn(GOOD_FIELDS, call_id="4")])
        answer = await run_turn("fix it", llm, registry, harness=legacy)
        self.assertEqual(answer, render_submission(GOOD_FIELDS))
        # Intercepted like task_result (never executed); prose still nudged.
        self.assertEqual(seen, [])
        self.assertEqual(len(llm.messages), 4)
        self.assertIn("submit_result", llm.messages[3][-1]["content"])


class TaskResultGenomeTests(unittest.TestCase):
    def test_task_result_fills_default_require(self) -> None:
        spec = from_dict({"verification": {"mode": "task_result"}})
        self.assertEqual(tuple(spec.verification.require),
                         ("solution_description", "evidence", "command_to_verify"))

    def test_legacy_rerun_key_ignored(self) -> None:
        # code-v10..v12 manifests carry the retired flag; they must still
        # load for lineage, and the rerun is unconditional now.
        spec = from_dict({"verification": {"mode": "task_result",
                                           "rerun_declared_command": True}})
        self.assertFalse(hasattr(spec.verification, "rerun_declared_command"))
        legacy = from_dict({"verification": {"mode": "return_contract",
                                             "rerun_declared_command": False}})
        self.assertEqual(legacy.verification.mode, "return_contract")

    def test_finish_violation_limit_defaults_and_validates(self) -> None:
        spec = from_dict({})
        self.assertEqual(spec.control.finish_violation_limit, 3)
        from agent_runtime.harness import HarnessError
        with self.assertRaises(HarnessError):
            from_dict({"control": {"finish_violation_limit": -1}})


if __name__ == "__main__":
    unittest.main()
