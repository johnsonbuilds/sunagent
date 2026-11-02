import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.agent.turn_factory import run_turn
from agent_runtime.agent.observations import ObservationFormatter, format_tool_result
from agent_runtime.execution.local import LocalWorkspace
from agent_runtime.tools.files import read_output
from agent_runtime.tools.tools import ToolRegistry, ToolSpec


SHELL_SCHEMA = {"type": "object", "properties": {}}


async def _echo_handler() -> str:
    return "done"


def _registry_with(handler, workspace=None) -> ToolRegistry:
    return ToolRegistry([ToolSpec("produce", "", SHELL_SCHEMA, handler)],
                        workspace=workspace)


class _FakeLLM:
    def __init__(self, responses) -> None:
        self.responses = responses
        self.messages = []

    async def chat(self, messages, tools=None):
        self.messages.append(messages.copy())
        return self.responses.pop(0)


class FormatToolResultTests(unittest.TestCase):
    def test_string_passes_through(self) -> None:
        self.assertEqual(format_tool_result("plain"), "plain")

    def test_successful_shell_result_drops_empty_fields(self) -> None:
        rendered = format_tool_result({
            "stdout": "hello\n", "stderr": "", "exit_code": 0,
            "duration": 1.23, "script_path": ".scripts/0001.py",
        })
        self.assertEqual(rendered, "hello\n\nscript: .scripts/0001.py")

    def test_successful_shell_result_without_output(self) -> None:
        rendered = format_tool_result({
            "stdout": "", "stderr": "", "exit_code": 0, "duration": 0.1})
        self.assertEqual(rendered, "ok (no output)")

    def test_failed_shell_result_keeps_exit_code_and_stderr(self) -> None:
        rendered = format_tool_result({
            "stdout": "partial", "stderr": "boom", "exit_code": 2,
            "duration": 0.5})
        self.assertEqual(rendered, "exit_code: 2\npartial\n--- stderr ---\nboom")

    def test_shell_error_dict_renders_error_line(self) -> None:
        rendered = format_tool_result({
            "stdout": "", "stderr": "", "exit_code": None, "duration": 0.0,
            "error": {"type": "CommandBlocked", "message": "nope"}})
        self.assertEqual(rendered, "error: CommandBlocked: nope")

    def test_generic_mapping_renders_json(self) -> None:
        rendered = format_tool_result({"path": "a.txt", "bytes_written": 4})
        self.assertEqual(rendered, '{"path": "a.txt", "bytes_written": 4}')

    def test_non_string_non_mapping_uses_str(self) -> None:
        self.assertEqual(format_tool_result([1, 2]), "[1, 2]")


class ReadOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_pages_spilled_file_and_rejects_outside_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = LocalWorkspace(directory)
            await workspace.write_file(".outputs/obs-0001.txt",
                                       "\n".join(f"line{i}" for i in range(1, 101)))
            page = await read_output(".outputs/obs-0001.txt", offset=2, limit=3,
                                     workspace=workspace)
            with self.assertRaisesRegex(ValueError, "only reads files under"):
                await read_output("secret.txt", workspace=workspace)
            with self.assertRaisesRegex(ValueError, "only reads files under"):
                await read_output(".outputs/../../etc/passwd",
                                  workspace=workspace)
        self.assertEqual(page["content"], "line2\nline3\nline4")


class ObservationFormatterTests(unittest.IsolatedAsyncioTestCase):
    async def test_small_result_stays_inline_without_archive(self) -> None:
        # Observations at or below the compaction head size can never lose
        # data, so they skip the archive entirely.
        formatter = ObservationFormatter(LocalWorkspace("/tmp"), max_chars=100)
        rendered = await formatter.render("small", call_id="c1")
        self.assertEqual(rendered.text, "small")
        self.assertIsNone(rendered.spill_path)

    async def test_result_without_call_id_is_not_archived(self) -> None:
        formatter = ObservationFormatter(LocalWorkspace("/tmp"), max_chars=100)
        rendered = await formatter.render("x" * 500)
        self.assertIsNone(rendered.spill_path)
        self.assertEqual(rendered.text, "x" * 500)

    async def test_archive_threshold_follows_compaction_head_size(self) -> None:
        formatter = ObservationFormatter(LocalWorkspace("/tmp"), max_chars=100)
        at_limit = await formatter.render("a" * 200, call_id="c1")
        beyond = await formatter.render("a" * 201, call_id="c2")
        self.assertIsNone(at_limit.spill_path)
        self.assertEqual(beyond.spill_path, ".outputs/obs-c2.txt")

    async def test_oversized_result_is_windowed_with_archive_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = LocalWorkspace(directory)
            formatter = ObservationFormatter(workspace, max_chars=100)
            big = "x" * 10_000
            rendered = await formatter.render(big, call_id="c1")

            self.assertEqual(rendered.spill_path, ".outputs/obs-c1.txt")
            self.assertIn("characters omitted", rendered.text)
            self.assertIn(f"full output saved to {rendered.spill_path}", rendered.text)
            self.assertIn("read_output", rendered.text)
            self.assertLess(len(rendered.text), len(big))
            archived = await read_output(rendered.spill_path, workspace=workspace)
        self.assertEqual(archived["content"], big)

    async def test_archiving_same_call_id_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = LocalWorkspace(directory)
            formatter = ObservationFormatter(workspace, max_chars=100)
            first = await formatter.render("v1" * 200, call_id="c1")
            second = await formatter.render("v2" * 200, call_id="c1")

            self.assertEqual(first.spill_path, second.spill_path)
            archived = await read_output(second.spill_path, workspace=workspace)
        self.assertEqual(archived["content"], "v2" * 200)


class LoopObservationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_archives_oversized_tool_observation(self) -> None:
        async def huge(_location: str = "") -> str:
            return "y" * 20_000

        llm = _FakeLLM([
            {"content": "", "tool_calls": [{"id": "1", "function": {
                "name": "produce", "arguments": "{}"}}]},
            {"content": "done", "tool_calls": []},
        ])
        with tempfile.TemporaryDirectory() as directory:
            answer = await run_turn("go", llm,
                                    _registry_with(huge, LocalWorkspace(directory)),
                                    max_iterations=3)
            observation_message = llm.messages[1][-1]
            observation = observation_message["content"]

            self.assertEqual(answer, "done")
            self.assertEqual(observation_message["metadata"]["spill_path"],
                             ".outputs/obs-1.txt")
            self.assertIn(f"full output saved to .outputs/obs-1.txt", observation)
            archived = await read_output(".outputs/obs-1.txt",
                                         workspace=LocalWorkspace(directory))
        self.assertEqual(archived["content"], "y" * 20_000)

    async def test_explicit_workspace_enables_archiving(self) -> None:
        async def huge(_location: str = "") -> str:
            return "y" * 20_000

        llm = _FakeLLM([
            {"content": "", "tool_calls": [{"id": "1", "function": {
                "name": "produce", "arguments": "{}"}}]},
            {"content": "done", "tool_calls": []},
        ])
        with tempfile.TemporaryDirectory() as directory:
            workspace = LocalWorkspace(directory)
            await run_turn("go", llm, _registry_with(huge),
                           workspace=workspace, max_iterations=3)
            message = llm.messages[1][-1]

            self.assertEqual(message["metadata"]["spill_path"],
                             ".outputs/obs-1.txt")
            archived = await read_output(".outputs/obs-1.txt",
                                         workspace=workspace)
        self.assertEqual(archived["content"], "y" * 20_000)

    async def test_loop_archives_small_observation_verbatim(self) -> None:
        async def small(_location: str = "") -> str:
            return "Singapore"

        llm = _FakeLLM([
            {"content": "", "tool_calls": [{"id": "1", "function": {
                "name": "produce", "arguments": "{}"}}]},
            {"content": "ok", "tool_calls": []},
        ])
        answer = await run_turn("go", llm, _registry_with(small), max_iterations=3)
        self.assertEqual(answer, "ok")
        message = llm.messages[1][-1]
        self.assertEqual(message["content"], "Singapore")
        self.assertNotIn("metadata", message)

    async def test_loop_archives_midsize_observation(self) -> None:
        # Mid-size observations (200-16k) are exactly the ones compaction
        # used to destroy; they must carry an archive from birth.
        async def midsize(_location: str = "") -> str:
            return "z" * 2_000

        llm = _FakeLLM([
            {"content": "", "tool_calls": [{"id": "call-9", "function": {
                "name": "produce", "arguments": "{}"}}]},
            {"content": "ok", "tool_calls": []},
        ])
        with tempfile.TemporaryDirectory() as directory:
            await run_turn("go", llm,
                           _registry_with(midsize, LocalWorkspace(directory)),
                           max_iterations=3)
            message = llm.messages[1][-1]

            self.assertEqual(message["content"], "z" * 2_000)
            self.assertEqual(message["metadata"]["spill_path"],
                             ".outputs/obs-call-9.txt")
            archived = await read_output(".outputs/obs-call-9.txt",
                                         workspace=LocalWorkspace(directory))
        self.assertEqual(archived["content"], "z" * 2_000)


if __name__ == "__main__":
    unittest.main()
