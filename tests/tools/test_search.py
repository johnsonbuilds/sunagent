import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.execution.local import LocalWorkspace
from agent_runtime.tools.tools import create_default_registry


SEARCH_TOOLS = ["write_file", "read_file", "grep_search", "find_files"]


def make_registry(directory: str):
    return create_default_registry(workspace=LocalWorkspace(directory),
                                   enabled=SEARCH_TOOLS)


class GrepSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_finds_matches_with_line_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            await registry.execute("write_file", {
                "path": "src/app.py",
                "content": "def main():\n    return serve()\n"})
            await registry.execute("write_file", {
                "path": "notes.txt", "content": "serve is called\nserve twice\n"})

            result = await registry.execute("grep_search", {
                "pattern": r"serve\(\)"})

        self.assertEqual(result["match_count"], 1)
        match = result["matches"][0]
        self.assertEqual(match, {"path": "src/app.py", "line": 2,
                                 "preview": "return serve()"})

    async def test_line_numbers_pair_with_read_file_offset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            await registry.execute("write_file", {
                "path": "a.txt", "content": "one\ntwo\nthree\nfour\n"})

            hit = await registry.execute("grep_search", {"pattern": "three"})
            page = await registry.execute("read_file", {
                "path": hit["matches"][0]["path"],
                "offset": hit["matches"][0]["line"], "limit": 1})

        self.assertEqual(page["content"], "three")

    async def test_include_filters_by_file_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            await registry.execute("write_file", {
                "path": "a.py", "content": "needle\n"})
            await registry.execute("write_file", {
                "path": "b.txt", "content": "needle\n"})

            result = await registry.execute("grep_search", {
                "pattern": "needle", "include": "*.py"})

        self.assertEqual([m["path"] for m in result["matches"]], ["a.py"])

    async def test_ignore_case(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            await registry.execute("write_file", {
                "path": "a.txt", "content": "CaSe\n"})

            result = await registry.execute("grep_search", {
                "pattern": "case", "ignore_case": True})

        self.assertEqual(result["match_count"], 1)

    async def test_skip_dirs_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            await registry.execute("write_file", {
                "path": ".git/config", "content": "needle\n"})
            await registry.execute("write_file", {
                "path": "real.txt", "content": "clean\n"})

            result = await registry.execute("grep_search", {"pattern": "needle"})

        self.assertEqual(result["match_count"], 0)
        self.assertEqual(result["files_scanned"], 1)  # real.txt only

    async def test_max_results_truncates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            await registry.execute("write_file", {
                "path": "a.txt", "content": "hit\n" * 10})

            result = await registry.execute("grep_search", {
                "pattern": "hit", "max_results": 3})

        self.assertEqual(result["match_count"], 3)
        self.assertTrue(result["truncated"])
        self.assertIn("note", result)

    async def test_invalid_regex_raises_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            with self.assertRaisesRegex(ValueError, "invalid regex"):
                await registry.execute("grep_search", {"pattern": "[unclosed"})


class FindFilesTests(unittest.IsolatedAsyncioTestCase):
    async def test_recursive_pattern_matches_nested_and_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            for path in ("app.py", "src/models/user.py", "src/util.ts"):
                await registry.execute("write_file", {"path": path,
                                                      "content": "x"})

            result = await registry.execute("find_files", {
                "pattern": "**/*.py"})

        self.assertEqual(result["matches"], ["app.py", "src/models/user.py"])
        self.assertFalse(result["truncated"])

    async def test_star_matches_across_separators(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            for path in ("a.py", "deep/nested/b.py"):
                await registry.execute("write_file", {"path": path,
                                                      "content": "x"})

            result = await registry.execute("find_files", {"pattern": "*.py"})

        self.assertEqual(result["matches"], ["a.py", "deep/nested/b.py"])

    async def test_bare_name_matches_anywhere(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            await registry.execute("write_file", {
                "path": "src/models/user.py", "content": "x"})

            result = await registry.execute("find_files", {
                "pattern": "user.py"})

        self.assertEqual(result["matches"], ["src/models/user.py"])

    async def test_max_results_truncates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            for index in range(5):
                await registry.execute("write_file", {
                    "path": f"f{index}.txt", "content": "x"})

            result = await registry.execute("find_files", {
                "pattern": "*.txt", "max_results": 2})

        self.assertEqual(result["match_count"], 2)
        self.assertEqual(result["total_matches"], 5)
        self.assertTrue(result["truncated"])


if __name__ == "__main__":
    unittest.main()
