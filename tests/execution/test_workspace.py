import base64
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.execution.harbor import HarborWorkspace
from agent_runtime.execution.local import LocalWorkspace


class FakeExecResult:
    def __init__(self, stdout: str = "", stderr: str = "",
                 return_code: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.return_code = return_code


class ScriptedEnvironment:
    """Replays canned exec results and records every command."""

    def __init__(self, results: list[FakeExecResult] | None = None) -> None:
        self.commands: list[str] = []
        self.results = list(results or [])

    async def exec(self, command: str, cwd: str | None = None,
                   timeout_sec: int | None = None) -> Any:
        self.commands.append(command)
        return self.results.pop(0)


class LocalWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_and_read_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = LocalWorkspace(directory)

            written = await workspace.write_file("notes/a.txt", "one\ntwo\n")
            read = await workspace.read_file("notes/a.txt")

        self.assertEqual(written, {"path": "notes/a.txt", "bytes_written": 8})
        self.assertEqual(read["content"], "one\ntwo\n")
        self.assertEqual(read["start_line"], 1)
        self.assertEqual(read["end_line"], 2)
        self.assertEqual(read["total_lines"], 2)
        self.assertFalse(read["truncated"])

    async def test_read_pages_by_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = LocalWorkspace(directory)
            await workspace.write_file("rows.txt", "a\nb\nc\nd\ne\n")

            page = await workspace.read_file("rows.txt", offset=2, limit=2)

        self.assertEqual(page["content"], "b\nc")
        self.assertEqual(page["start_line"], 2)
        self.assertEqual(page["end_line"], 3)
        self.assertEqual(page["total_lines"], 5)
        self.assertTrue(page["truncated"])

    async def test_read_offset_beyond_eof_returns_empty_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = LocalWorkspace(directory)
            await workspace.write_file("rows.txt", "a\nb\n")

            page = await workspace.read_file("rows.txt", offset=9)

        self.assertEqual(page["content"], "")
        self.assertFalse(page["truncated"])

    async def test_read_missing_file_is_structured_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = LocalWorkspace(directory)

            read = await workspace.read_file("missing.txt")

        self.assertEqual(read["error"]["type"], "FileNotFoundError")
        self.assertEqual(read["path"], "missing.txt")

    async def test_read_directory_is_structured_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = LocalWorkspace(directory)
            await workspace.write_file("nested/file.txt", "x")

            read = await workspace.read_file("nested")

        self.assertEqual(read["error"]["type"], "IsADirectoryError")

    async def test_read_binary_file_is_structured_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = LocalWorkspace(directory)
            (Path(directory) / "blob.bin").write_bytes(b"\xff\xfe\x00")

            read = await workspace.read_file("blob.bin")

        self.assertEqual(read["error"]["type"], "UnicodeDecodeError")

    async def test_invalid_pagination_arguments_raise(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = LocalWorkspace(directory)
            await workspace.write_file("rows.txt", "a\n")

            with self.assertRaisesRegex(ValueError, "offset"):
                await workspace.read_file("rows.txt", offset=0)
            with self.assertRaisesRegex(ValueError, "limit"):
                await workspace.read_file("rows.txt", limit=0)

    async def test_path_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = LocalWorkspace(directory)

            with self.assertRaisesRegex(ValueError, "escapes"):
                await workspace.write_file("../outside.txt", "x")
            with self.assertRaisesRegex(ValueError, "escapes"):
                await workspace.write_file("/etc/passwd", "x")

    async def test_absolute_path_inside_root_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = LocalWorkspace(directory)

            written = await workspace.write_file(f"{directory}/inside.txt", "x")

        self.assertNotIn("error", written)

    async def test_list_dir_orders_directories_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = LocalWorkspace(directory)
            await workspace.write_file("z.txt", "x")
            await workspace.write_file("sub/a.txt", "x")
            await workspace.write_file("b.txt", "x")

            listing = await workspace.list_dir(".")

        self.assertEqual([entry["name"] for entry in listing["entries"]],
                         ["sub", "b.txt", "z.txt"])
        self.assertEqual(listing["entries"][0]["type"], "dir")
        self.assertEqual(listing["entries"][1]["type"], "file")

    async def test_list_dir_missing_is_structured_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = LocalWorkspace(directory)

            listing = await workspace.list_dir("missing")

        self.assertEqual(listing["error"]["type"], "FileNotFoundError")


class HarborWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_and_read_roundtrip_via_base64(self) -> None:
        content = "hello\nworld\n"
        encoded = base64.b64encode(content.encode()).decode()
        environment = ScriptedEnvironment([
            FakeExecResult(),
            FakeExecResult(stdout=encoded),
        ])
        workspace = HarborWorkspace(environment, root="/work")

        written = await workspace.write_file("a.txt", content)
        read = await workspace.read_file("a.txt")

        self.assertEqual(written, {"path": "a.txt", "bytes_written": 12})
        self.assertEqual(read["content"], content)
        self.assertEqual(read["total_lines"], 2)
        self.assertIn("base64 -d > /work/a.txt", environment.commands[0])
        self.assertIn("base64 < /work/a.txt", environment.commands[1])

    async def test_path_escape_is_rejected_when_root_is_set(self) -> None:
        workspace = HarborWorkspace(ScriptedEnvironment(), root="/work")

        with self.assertRaisesRegex(ValueError, "escapes"):
            await workspace.write_file("../outside.txt", "x")
        with self.assertRaisesRegex(ValueError, "escapes"):
            await workspace.write_file("/etc/passwd", "x")

    async def test_paths_pass_through_when_root_is_unset(self) -> None:
        environment = ScriptedEnvironment([FakeExecResult()])
        workspace = HarborWorkspace(environment)

        await workspace.write_file("/etc/hostname", "x")

        self.assertIn("base64 -d > /etc/hostname", environment.commands[0])

    async def test_list_dir_parses_find_output(self) -> None:
        environment = ScriptedEnvironment([
            FakeExecResult(stdout="f\t12\tapp.py\nd\t4096\tsub\n"),
        ])
        workspace = HarborWorkspace(environment, root="/work")

        listing = await workspace.list_dir(".")

        self.assertEqual(listing["entries"], [
            {"name": "sub", "type": "dir", "size": 4096},
            {"name": "app.py", "type": "file", "size": 12},
        ])
        self.assertIn("find /work", environment.commands[0])

    async def test_nonzero_exit_is_structured_error(self) -> None:
        environment = ScriptedEnvironment([
            FakeExecResult(stderr="base64: /work/missing.txt: No such file\n",
                           return_code=1),
        ])
        workspace = HarborWorkspace(environment, root="/work")

        read = await workspace.read_file("missing.txt")

        self.assertEqual(read["error"]["type"], "CommandError")
        self.assertIn("No such file", read["error"]["message"])

    async def test_search_contents_single_exec_vimgrep(self) -> None:
        environment = ScriptedEnvironment([
            FakeExecResult(stdout="src/app.py:2:7:return serve()\n"),
        ])
        workspace = HarborWorkspace(environment)

        result = await workspace.search_contents(r"serve\(\)")

        self.assertEqual(len(environment.commands), 1)
        self.assertIn("rg", environment.commands[0])
        self.assertEqual(result["match_count"], 1)
        self.assertEqual(result["matches"][0],
                         {"path": "src/app.py", "line": 2,
                          "preview": "return serve()"})

    async def test_search_contents_no_match_is_empty_not_error(self) -> None:
        environment = ScriptedEnvironment([FakeExecResult(stdout="",
                                                          return_code=1)])
        workspace = HarborWorkspace(environment)

        result = await workspace.search_contents("nothing-matches")

        self.assertEqual(result["match_count"], 0)
        self.assertFalse(result["truncated"])

    async def test_find_paths_single_exec(self) -> None:
        environment = ScriptedEnvironment([
            FakeExecResult(stdout="src/models/user.py\nsrc/util.ts\n"),
        ])
        workspace = HarborWorkspace(environment)

        result = await workspace.find_paths("**/*.py")

        self.assertEqual(len(environment.commands), 1)
        self.assertIn("find", environment.commands[0])
        self.assertEqual(result["matches"], ["src/models/user.py"])


if __name__ == "__main__":
    unittest.main()
