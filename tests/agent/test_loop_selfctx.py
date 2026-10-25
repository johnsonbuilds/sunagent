"""Loop-level tests for strategy self_managed_file (spec §10)."""
import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.agent.loop import AgentTurn, Conversation
from agent_runtime.agent.turn_history import TurnHistory
from agent_runtime.execution.local import LocalWorkspace
from agent_runtime.harness import HarnessSpec, MemoryGenome, PromptGenome
from agent_runtime.tools.tools import ToolRegistry, ToolSpec
from agent_runtime.trace import RunTrace


async def echo_handler(command: str) -> dict[str, Any]:
    return {"stdout": f"ran: {command}", "stderr": "", "exit_code": 0}


def make_harness(**memory_kw: Any) -> HarnessSpec:
    defaults = {"context_budget": 2000, "reserve_chars": 400,
                "max_rollback_retries": 2}
    defaults.update(memory_kw)
    memory = MemoryGenome(strategy="self_managed_file", **defaults)
    return HarnessSpec(id="selfctx-test", memory=memory,
                       prompt=PromptGenome(system="sys"))


def make_registry(workspace: Any) -> ToolRegistry:
    async def run_command(command: str) -> dict[str, Any]:
        return await echo_handler(command)
    spec = ToolSpec("run_command", "run", {"type": "object",
                                           "properties": {"command":
                                                          {"type": "string"}}},
                    run_command)
    return ToolRegistry([spec], workspace=workspace)


class FakeLLM:
    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = script

    async def chat(self, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return self.script.pop(0)


class ReplaceViewTests(unittest.TestCase):
    def test_replace_view_guards_prefix(self):
        conv = Conversation()
        conv.append({"role": "system", "content": "s"})
        conv.append({"role": "user", "content": "t"})
        history = TurnHistory(conv.messages)
        history.append({"role": "assistant", "content": "a"})
        with self.assertRaises(ValueError):
            history.replace_view([{"role": "system", "content": "HACKED"},
                                  {"role": "user", "content": "t"}],
                                 protect=2)
        ok = [{"role": "system", "content": "s"},
              {"role": "user", "content": "t"},
              {"role": "user", "content": "note"}]
        history.replace_view(ok, protect=2)
        self.assertEqual(history.view, ok)
        # messages untouched.
        self.assertEqual(conv.messages[-1],
                         {"role": "assistant", "content": "a"})


class MirrorSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_mirror_no_sync(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            workspace = LocalWorkspace(root=tmp)
            harness = make_harness()
            registry = make_registry(workspace)
            llm = FakeLLM([
                {"content": "", "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "run_command",
                                 "arguments": '{"command": "ls"}'}}]},
                {"content": "done", "tool_calls": []},
            ])
            turn = AgentTurn("hi", llm, registry, max_iterations=3,
                             harness=harness)
            answer = await turn.run()
            self.assertEqual(answer, "done")
            # messages never rewritten by the mirror path.
            roles = [m.get("role") for m in turn.conversation.messages]
            self.assertIn("tool", roles)
            kinds = [e["event_type"] for e in turn.trace.events
                     if isinstance(e, dict)]
            self.assertNotIn("context.mirror_sync", kinds)

    async def test_stale_mirror_after_tool_is_noop(self):
        # End-to-end stale-file guard: render at turn start, append the
        # tool's own observation, then read back the pre-tool file. The
        # view must survive untouched and no sync may fire.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            workspace = LocalWorkspace(root=tmp)
            harness = make_harness(context_budget=10_000)
            registry = make_registry(workspace)
            turn = AgentTurn.__new__(AgentTurn)
            turn.harness = harness
            turn.tools = registry
            turn.trace = RunTrace(run_id="stale")
            from agent_runtime.agent.context_mirror import render_mirror
            from agent_runtime.agent.turn_history import TurnHistory as TH
            history = TH([{"role": "system", "content": "s"},
                          {"role": "user", "content": "t"}])
            history.append({"role": "assistant", "content": "",
                            "tool_calls": [
                                {"id": "c1", "type": "function",
                                 "function": {"name": "run_command",
                                              "arguments": "{}"}}]})
            rendered = await turn._render_mirror(history)
            self.assertIsNotNone(rendered)
            # Simulate the tool result landing AFTER the render.
            from agent_runtime.agent.tool_dispatch import (
                _tool_observation_message)
            history.append(_tool_observation_message(
                {"id": "c1"}, "tool output here"))
            before_view = list(history.view)
            receipt = await turn._maybe_sync_mirror(history, 1)
            self.assertIsNone(receipt)
            self.assertEqual(history.view, before_view)
            kinds = [e.event_type for e in turn.trace.events]
            self.assertNotIn("context.mirror_sync", kinds)

    async def test_compressed_mirror_syncs_view(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            workspace = LocalWorkspace(root=tmp)
            harness = make_harness(context_budget=10_000)
            history = TurnHistory([{"role": "system", "content": "s"},
                                   {"role": "user", "content": "t"}])
            from agent_runtime.agent.loop import AgentTurn as T
            turn = T.__new__(T)
            from agent_runtime.harness import default_harness as _  # noqa
            turn.harness = harness
            turn.tools = make_registry(workspace)
            turn.trace = RunTrace(run_id="m")
            from agent_runtime.agent.context_mirror import render_mirror
            from agent_runtime.agent import loop as loop_mod
            loop_mod.logger.disabled = True
            history.append({"role": "assistant", "content": "work " * 200})
            rendered = await turn._render_mirror(history)
            self.assertIsNotNone(rendered)
            # Simulate the model compressing the mirror via edit.
            from agent_runtime.agent.context_mirror import apply_mirror
            compressed = ("## turn 1 [assistant]\nsummary: did work\n")
            res = apply_mirror(compressed, messages=history.messages,
                               view=history.view, protect=2,
                               limit_chars=9600)
            self.assertEqual(res.status, "applied")
            history.replace_view(res.new_view, protect=2)
            self.assertIn("summary", history.view[-1]["content"])
            self.assertEqual(history.messages[-1]["content"], "work " * 200)


if __name__ == "__main__":
    unittest.main()
