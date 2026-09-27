import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.execution.local import LocalWorkspace
from agent_runtime.tools.tools import create_default_registry


FILE_TOOLS = ["write_file", "read_file", "list_dir", "edit_file"]


def make_registry(directory: str):
    return create_default_registry(workspace=LocalWorkspace(directory),
                                   enabled=FILE_TOOLS)


class FileToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_then_read_through_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)

            written = await registry.execute("write_file", {
                "path": "a.py", "content": "x = 1\n"})
            read = await registry.execute("read_file", {"path": "a.py"})

        self.assertEqual(written["bytes_written"], 6)
        self.assertEqual(read["content"], "x = 1\n")

    async def test_write_in_missing_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            with self.assertRaisesRegex(ValueError, "parent directory"):
                await registry.execute("write_file", {
                    "path": "nope/sub/new.py", "content": "x = 1\n"})

        self.assertFalse((Path(directory) / "nope").exists())

    async def test_write_in_existing_directory_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            Path(directory, "sub").mkdir()
            await registry.execute("write_file", {
                "path": "sub/keep.py", "content": "x = 1\n"})
            written = await registry.execute("write_file", {
                "path": "sub/new.py", "content": "y = 2\n"})

        self.assertEqual(written["bytes_written"], 6)

    async def test_read_pages_with_explicit_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            content = "".join(f"line {index:04d}\n" for index in range(1, 2501))
            await registry.execute("write_file", {"path": "big.txt",
                                                  "content": content})

            first = await registry.execute("read_file", {
                "path": "big.txt", "limit": 10})
            last = await registry.execute("read_file", {
                "path": "big.txt", "offset": 2495, "limit": 100})

        self.assertEqual(first["start_line"], 1)
        self.assertEqual(first["end_line"], 10)
        self.assertTrue(first["truncated"])
        self.assertEqual(first["total_lines"], 2500)
        self.assertEqual(last["content"].splitlines()[-1], "line 2500")
        self.assertFalse(last["truncated"])

    async def test_edit_file_replaces_unique_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            await registry.execute("write_file", {
                "path": "calc.py", "content": "def add(a, b):\n    return a + b\n"})

            edited = await registry.execute("edit_file", {
                "path": "calc.py", "old_str": "return a + b",
                "new_str": "return a - b"})
            read = await registry.execute("read_file", {"path": "calc.py"})

        self.assertEqual(edited["occurrences_replaced"], 1)
        self.assertEqual(read["content"], "def add(a, b):\n    return a - b\n")

    async def test_edit_file_rejects_missing_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            await registry.execute("write_file", {"path": "a.txt",
                                                  "content": "hello\n"})

            with self.assertRaisesRegex(ValueError, "not found"):
                await registry.execute("edit_file", {
                    "path": "a.txt", "old_str": "goodbye", "new_str": "x"})

    async def test_edit_file_rejects_ambiguous_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            await registry.execute("write_file", {
                "path": "a.txt", "content": "x = 1\nx = 1\n"})

            with self.assertRaisesRegex(ValueError, "appears 2 times"):
                await registry.execute("edit_file", {
                    "path": "a.txt", "old_str": "x = 1", "new_str": "x = 2"})

    async def test_edit_file_rejects_empty_old_str(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)

            with self.assertRaisesRegex(ValueError, "must not be empty"):
                await registry.execute("edit_file", {
                    "path": "a.txt", "old_str": "", "new_str": "x"})

    async def test_edit_file_reports_missing_file_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)

            edited = await registry.execute("edit_file", {
                "path": "missing.txt", "old_str": "a", "new_str": "b"})

        self.assertEqual(edited["error"]["type"], "FileNotFoundError")

    async def test_path_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)

            with self.assertRaisesRegex(ValueError, "escapes"):
                await registry.execute("write_file", {
                    "path": "../outside.txt", "content": "x"})

    async def test_list_dir_through_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            Path(directory, "sub").mkdir()
            await registry.execute("write_file", {"path": "sub/a.txt",
                                                  "content": "x"})

            listing = await registry.execute("list_dir", {"path": "."})

        self.assertEqual([entry["name"] for entry in listing["entries"]], ["sub"])


if __name__ == "__main__":
    unittest.main()
