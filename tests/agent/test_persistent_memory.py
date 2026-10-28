"""Persistent cross-session memory: render, inject, and trace behavior.

Covers the memory block contract end to end:

* env switch off (the benchmark default) leaves zero trace — no block,
  no directory, no trace event;
* on, the block lands in the system prompt once (idempotent across
  turns in one conversation) with the flat tree and its caps;
* the loop emits a ``memory.recall`` trace event carrying the tree
  snapshot metadata.
"""

import sys
import tempfile
import unittest
from os import environ
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

import agent_runtime.memory as memory_module
from agent_runtime.agent.loop import Conversation, run_turn
from agent_runtime.execution.local import LocalWorkspace
from agent_runtime.harness import HarnessSpec
from agent_runtime.memory import (
    MAX_MEMORY_CHARS,
    MAX_MEMORY_ENTRIES,
    ENV_MEMORY,
    MEMORY_CONVENTION,
    memory_enabled,
    render_memory_block,
)
from agent_runtime.tools.tools import ToolRegistry
from agent_runtime.trace import RunTrace


class FakeLLM:
    """Minimal chat model: pops canned responses, records requests."""

    def __init__(self, responses: list[dict] | None = None) -> None:
        self.responses = responses or []
        self.messages: list[list[dict]] = []

    async def chat(self, messages: list[dict], tools=None) -> dict:
        self.messages.append(messages.copy())
        return self.responses.pop(0)


def _plain_answer() -> dict:
    return {"content": "done", "tool_calls": [], "finish_reason": "stop"}


def _workspace(tmp: str) -> LocalWorkspace:
    return LocalWorkspace(root=Path(tmp))


class MemorySwitchTests(unittest.TestCase):
    def test_off_by_default(self) -> None:
        with mock.patch.dict(environ, {}, clear=False):
            environ.pop(ENV_MEMORY, None)
            self.assertFalse(memory_enabled())

    def test_truthy_values(self) -> None:
        for raw in ("1", "true", "on", "TRUE", "On"):
            with mock.patch.dict(environ, {ENV_MEMORY: raw}):
                self.assertTrue(memory_enabled(), raw)

    def test_falsy_values(self) -> None:
        for raw in ("", "0", "off", "no", "false"):
            with mock.patch.dict(environ, {ENV_MEMORY: raw}):
                self.assertFalse(memory_enabled(), raw)

    def test_off_renders_empty_and_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(environ, {}, clear=False):
                environ.pop(ENV_MEMORY, None)
                view = render_memory_block(_workspace(tmp))
            self.assertEqual(view.block, "")
            self.assertFalse((Path(tmp) / "memory").exists())

    def test_none_workspace_renders_empty(self) -> None:
        with mock.patch.dict(environ, {ENV_MEMORY: "1"}):
            view = render_memory_block(None)
        self.assertEqual(view.block, "")

    def test_workspace_without_root_renders_empty(self) -> None:
        with mock.patch.dict(environ, {ENV_MEMORY: "1"}):
            view = render_memory_block(object())
        self.assertEqual(view.block, "")

    def test_empty_string_root_renders_empty_and_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = _workspace(tmp)
            workspace.root = ""
            with mock.patch.dict(environ, {ENV_MEMORY: "1"}):
                view = render_memory_block(workspace)
            self.assertEqual(view.block, "")
            self.assertFalse((Path(tmp) / "memory").exists())

    def test_filesystem_failure_degrades_to_empty_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # A regular file where the memory directory should be: mkdir
            # must fail, and render must degrade instead of raising.
            (Path(tmp) / "memory").write_text("in the way")
            with mock.patch.dict(environ, {ENV_MEMORY: "1"}):
                view = render_memory_block(_workspace(tmp))
            self.assertEqual(view.block, "")


class MemoryRenderTests(unittest.TestCase):
    def _render(self, tmp: str):
        with mock.patch.dict(environ, {ENV_MEMORY: "1"}):
            return render_memory_block(_workspace(tmp))

    def test_empty_memory_creates_directory_and_notes_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            view = self._render(tmp)
            self.assertTrue((Path(tmp) / "memory").is_dir())
            self.assertIn(MEMORY_CONVENTION, view.block)
            self.assertIn("nothing recorded", view.block)
            self.assertEqual(view.files, 0)
            self.assertEqual(view.tree_hash, "")
            self.assertFalse(view.truncated)

    def test_flat_listing_with_paths_and_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "memory"
            (root / "people").mkdir(parents=True)
            (root / "decisions.md").write_text("x" * 100)
            (root / "people" / "johnson.md").write_text("y" * 40)
            view = self._render(tmp)
            self.assertIn("  memory/decisions.md (100 B)", view.block)
            self.assertIn("  memory/people/johnson.md (40 B)", view.block)
            self.assertEqual(view.files, 2)
            self.assertEqual(view.total_bytes, 140)
            self.assertEqual(len(view.tree_hash), 16)
            self.assertIn("Current contents of memory/:", view.block)

    def test_hidden_entries_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "memory"
            (root / ".hidden").mkdir(parents=True)
            (root / ".DS_Store").write_text("junk")
            (root / ".hidden" / "secret.md").write_text("nope")
            (root / "visible.md").write_text("yes")
            view = self._render(tmp)
            self.assertEqual(view.files, 1)
            self.assertIn("memory/visible.md", view.block)
            self.assertNotIn("hidden", view.block)

    def test_symlinks_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "memory"
            root.mkdir()
            (root / "real.md").write_text("yes")
            (Path(tmp) / "outside.md").write_text("outside")
            try:
                (root / "link.md").symlink_to(Path(tmp) / "outside.md")
                (root / "dirlink").symlink_to(Path(tmp))
            except OSError:
                self.skipTest("symlinks not supported on this platform")
            view = self._render(tmp)
            self.assertEqual(view.files, 1)
            self.assertIn("memory/real.md", view.block)
            self.assertNotIn("link.md", view.block)
            self.assertNotIn("dirlink", view.block)

    def test_listing_is_deterministically_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "memory"
            root.mkdir()
            for name in ("zeta.md", "alpha", "mid.md"):
                target = root / name
                if name == "alpha":
                    target.mkdir()
                    (target / "inner.md").write_text("i")
                else:
                    target.write_text("f")
            first = self._render(tmp)
            second = self._render(tmp)
            self.assertEqual(first.block, second.block)
            self.assertEqual(first.tree_hash, second.tree_hash)

    def test_tree_hash_tracks_content_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "memory"
            root.mkdir()
            (root / "a.md").write_text("one")
            before = self._render(tmp)
            (root / "b.md").write_text("two")
            after = self._render(tmp)
            self.assertNotEqual(before.tree_hash, after.tree_hash)

    def test_human_sizes(self) -> None:
        from agent_runtime.memory import _human_size
        self.assertEqual(_human_size(0), "0 B")
        self.assertEqual(_human_size(512), "512 B")
        self.assertEqual(_human_size(1023), "1023 B")
        self.assertEqual(_human_size(1024), "1.0 KB")
        self.assertEqual(_human_size(2048), "2.0 KB")
        self.assertEqual(_human_size(5 * 1024 ** 2), "5.0 MB")
        self.assertEqual(_human_size(2 * 1024 ** 3), "2.0 GB")
        self.assertEqual(_human_size(3 * 1024 ** 4), "3.0 TB")

    def test_entry_cap_truncates_with_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "memory"
            root.mkdir()
            for index in range(5):
                (root / f"f{index}.md").write_text("x")
            with mock.patch.object(memory_module, "MAX_MEMORY_ENTRIES", 2):
                view = self._render(tmp)
            self.assertTrue(view.truncated)
            self.assertEqual(view.files, 5)
            self.assertIn("3 more entries not shown", view.block)

    def test_char_cap_truncates_with_marker(self) -> None:
        self.assertGreater(MAX_MEMORY_ENTRIES, 0)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "memory"
            root.mkdir()
            # Enough long-named files that rendered lines exceed the
            # char cap long before the entry cap.
            count = MAX_MEMORY_CHARS // 40 + 10
            for index in range(count):
                (root / f"file-with-a-rather-long-name-{index:04d}.md").write_text("x")
            with mock.patch.object(memory_module, "MAX_MEMORY_ENTRIES", 10 ** 6):
                view = self._render(tmp)
            self.assertTrue(view.truncated)
            self.assertEqual(view.files, count)
            self.assertLessEqual(len(view.block),
                                 MAX_MEMORY_CHARS + len(MEMORY_CONVENTION) + 200)
            self.assertIn("more entries not shown", view.block)

    def test_trace_meta_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "memory"
            root.mkdir()
            (root / "a.md").write_text("hello")
            view = self._render(tmp)
        self.assertEqual(view.trace_meta(), {
            "files": 1, "bytes": 5, "truncated": False,
            "tree_hash": view.tree_hash})


class MemoryLoopIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """End to end: the block rides the system prompt; the trace records it."""

    async def test_on_injects_block_and_traces_recall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory_root = Path(tmp) / "memory"
            memory_root.mkdir()
            (memory_root / "facts.md").write_text("sunagent author: johnson")
            llm = FakeLLM([_plain_answer()])
            tools = ToolRegistry([], workspace=_workspace(tmp))
            trace = RunTrace(run_id="memory-on")
            with mock.patch.dict(environ, {ENV_MEMORY: "1"}):
                await run_turn("hi", llm, tools, harness=HarnessSpec(),
                               trace=trace)
            system = llm.messages[0][0]
            self.assertEqual(system["role"], "system")
            self.assertIn("## Persistent memory", system["content"])
            self.assertIn("memory/facts.md", system["content"])
            recalls = [event for event in trace.events
                       if event.event_type == "memory.recall"]
            self.assertEqual(len(recalls), 1)
            data = recalls[0].data
            self.assertEqual(data["files"], 1)
            self.assertEqual(data["bytes"], len("sunagent author: johnson"))
            self.assertFalse(data["truncated"])
            self.assertEqual(len(data["tree_hash"]), 16)

    async def test_off_leaves_system_prompt_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            llm = FakeLLM([_plain_answer()])
            tools = ToolRegistry([], workspace=_workspace(tmp))
            trace = RunTrace(run_id="memory-off")
            with mock.patch.dict(environ, {}, clear=False):
                environ.pop(ENV_MEMORY, None)
                await run_turn("hi", llm, tools, harness=HarnessSpec(),
                               trace=trace)
            system = llm.messages[0][0]
            self.assertNotIn("Persistent memory", system["content"])
            self.assertFalse([event for event in trace.events
                              if event.event_type == "memory.recall"])
            # A disabled run must not even bootstrap the directory.
            self.assertFalse((Path(tmp) / "memory").exists())

    async def test_second_turn_does_not_reinject(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            llm = FakeLLM([_plain_answer(), _plain_answer()])
            tools = ToolRegistry([], workspace=_workspace(tmp))
            conversation = Conversation()
            trace = RunTrace(run_id="memory-two-turns")
            with mock.patch.dict(environ, {ENV_MEMORY: "1"}):
                await run_turn("first", llm, tools, harness=HarnessSpec(),
                               conversation=conversation, trace=trace)
                await run_turn("second", llm, tools, harness=HarnessSpec(),
                               conversation=conversation, trace=trace)
            systems = [message for message in conversation.messages
                       if message.get("role") == "system"]
            self.assertEqual(len(systems), 1)
            # And the second request saw that same single system message.
            self.assertEqual(llm.messages[1][0]["role"], "system")
            self.assertIn("Persistent memory", llm.messages[1][0]["content"])
            # The no-op injection on the second turn emits no new event:
            # one recall for the conversation, not one per turn.
            recalls = [event for event in trace.events
                       if event.event_type == "memory.recall"]
            self.assertEqual(len(recalls), 1)


if __name__ == "__main__":
    unittest.main()
