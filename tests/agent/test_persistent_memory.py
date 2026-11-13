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
from types import SimpleNamespace
from unittest import mock


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

import agent_runtime.memory as memory_module
from agent_runtime.agent.loop import Conversation
from agent_runtime.agent.turn_factory import run_turn
from agent_runtime.execution.local import LocalWorkspace
from agent_runtime.harness import HarnessSpec
from agent_runtime.memory import (
    MAX_MEMORY_CHARS,
    MAX_MEMORY_ENTRIES,
    ENV_MEMORY,
    ENV_MEMORY_DIR,
    memory_enabled,
    memory_root,
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


HOME_ENV = "AGENT_RUNTIME_HOME"


def _memory_env(tmp: str, **extra: str) -> dict[str, str]:
    """Env that pins this test's memory directory to ``<tmp>/memory``.

    Memory is anchored on the runtime home (``AGENT_RUNTIME_HOME``), not
    on the sandbox, so every enabled test has to say where the home is --
    otherwise the render would create ``workspace/memory/`` inside the
    checkout.  ``tmp`` doubles as sandbox root, so the listing is spelled
    ``memory/...``: memory sits directly under the root here.
    """
    env = {ENV_MEMORY: "1", HOME_ENV: tmp}
    env.update(extra)
    return env


def _convention(location: str = "memory/") -> str:
    """The convention paragraph as rendered for ``location``."""
    return memory_module._convention(location)


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
            home = Path(tmp) / "home"
            with mock.patch.dict(environ, {HOME_ENV: str(home)}):
                environ.pop(ENV_MEMORY, None)
                view = render_memory_block(_workspace(tmp))
            self.assertEqual(view.block, "")
            self.assertFalse((Path(tmp) / "memory").exists())
            # A disabled run must not bootstrap the runtime home either.
            self.assertFalse(home.exists())

    def test_none_workspace_is_a_wiring_error(self) -> None:
        with mock.patch.dict(environ, {}, clear=False):
            environ.pop(ENV_MEMORY, None)
            with self.assertRaisesRegex(ValueError, "requires a workspace"):
                render_memory_block(None)
        with mock.patch.dict(environ, {ENV_MEMORY: "1"}):
            with self.assertRaisesRegex(ValueError, "requires a workspace"):
                render_memory_block(None)

    def test_workspace_without_root_skips_memory_with_warning(self) -> None:
        with mock.patch.dict(environ, {ENV_MEMORY: "1"}):
            with self.assertLogs("agent_runtime.memory", level="WARNING"):
                view = render_memory_block(SimpleNamespace(root=None))
        self.assertEqual(view.block, "")

    def test_workspace_without_root_is_ignored_when_memory_is_off(self) -> None:
        with mock.patch.dict(environ, {}, clear=False):
            environ.pop(ENV_MEMORY, None)
            view = render_memory_block(SimpleNamespace(root=None))
        self.assertEqual(view.block, "")

    def test_empty_string_root_skips_memory_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = _workspace(tmp)
            workspace.root = ""
            with mock.patch.dict(environ, _memory_env(tmp)):
                with self.assertLogs("agent_runtime.memory", level="WARNING"):
                    view = render_memory_block(workspace)
            self.assertEqual(view.block, "")
            self.assertFalse((Path(tmp) / "memory").exists())

    def test_filesystem_failure_degrades_to_empty_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # A regular file where the memory directory should be: mkdir
            # must fail, and render must degrade instead of raising.
            (Path(tmp) / "memory").write_text("in the way")
            with mock.patch.dict(environ, _memory_env(tmp)):
                view = render_memory_block(_workspace(tmp))
            self.assertEqual(view.block, "")

    def test_memory_outside_sandbox_is_skipped_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp) / "sandbox"
            sandbox.mkdir()
            home = Path(tmp) / "home"
            with mock.patch.dict(environ, {ENV_MEMORY: "1",
                                           HOME_ENV: str(home)}):
                with self.assertLogs("agent_runtime.memory", level="WARNING"):
                    view = render_memory_block(_workspace(str(sandbox)))
            # The file tools would reject the paths, so rendering them
            # would only teach the agent paths it cannot open.
            self.assertEqual(view.block, "")
            self.assertFalse(home.exists())


class MemoryRenderTests(unittest.TestCase):
    def _render(self, tmp: str):
        with mock.patch.dict(environ, _memory_env(tmp)):
            return render_memory_block(_workspace(tmp))

    def test_empty_memory_creates_directory_and_notes_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            view = self._render(tmp)
            self.assertTrue((Path(tmp) / "memory").is_dir())
            self.assertIn(_convention(), view.block)
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
                                 MAX_MEMORY_CHARS + len(_convention()) + 200)
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


class MemoryLocationTests(unittest.IsolatedAsyncioTestCase):
    """Memory is runtime state; the paths it shows are sandbox-relative."""

    def test_runtime_home_anchors_memory_not_the_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            home = repo / "workspace"
            (home / "memory").mkdir(parents=True)
            (home / "memory" / "facts.md").write_text("x" * 12)
            with mock.patch.dict(environ, {ENV_MEMORY: "1",
                                           HOME_ENV: str(home)}):
                self.assertEqual(memory_root(), home / "memory")
                view = render_memory_block(_workspace(str(repo)))
            # Memory is under the home, not next to it under the sandbox...
            self.assertTrue((home / "memory" / "facts.md").is_file())
            self.assertFalse((repo / "memory").exists())
            # ...and the listing says so, spelled from the sandbox root.
            self.assertIn("`workspace/memory/`", view.block)
            self.assertIn("Current contents of workspace/memory/:", view.block)
            self.assertIn("  workspace/memory/facts.md (12 B)", view.block)

    async def test_listed_paths_resolve_through_the_file_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            home = repo / "workspace"
            (home / "memory").mkdir(parents=True)
            (home / "memory" / "facts.md").write_text("sunagent author: johnson")
            workspace = _workspace(str(repo))
            with mock.patch.dict(environ, {ENV_MEMORY: "1",
                                           HOME_ENV: str(home)}):
                view = render_memory_block(workspace)
                listed = [line.split(" (")[0].strip() for line in
                          view.block.splitlines()
                          if line.startswith("  workspace/")]
                self.assertEqual(listed, ["workspace/memory/facts.md"])
                self.assertIn("johnson",
                              (await workspace.read_file(listed[0]))["content"])
                await workspace.write_file("workspace/memory/new.md", "hi")
            self.assertEqual((home / "memory" / "new.md").read_text(), "hi")

    def test_memory_dir_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            notes = repo / "notes"
            with mock.patch.dict(environ, _memory_env(tmp,
                                                      **{ENV_MEMORY_DIR: str(notes)})):
                self.assertEqual(memory_root(), notes)
                view = render_memory_block(_workspace(str(repo)))
            self.assertTrue(notes.is_dir())
            self.assertFalse((notes.parent / "memory").exists())
            self.assertIn("`notes/`", view.block)

    def test_default_layout_under_the_project_checkout(self) -> None:
        # The shipped default: sandbox = project root, home = <root>/workspace.
        with mock.patch.dict(environ, {ENV_MEMORY: "1"}, clear=False):
            environ.pop(HOME_ENV, None)
            environ.pop(ENV_MEMORY_DIR, None)
            self.assertEqual(memory_root(),
                             Path(__file__).parents[2] / "workspace" / "memory")


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
            with mock.patch.dict(environ, _memory_env(tmp)):
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
            with mock.patch.dict(environ, _memory_env(tmp)):
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
