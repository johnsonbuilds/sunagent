import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.execution.local import LocalWorkspace
from agent_runtime.tools.tools import create_default_registry


def make_registry(directory: str):
    return create_default_registry(workspace=LocalWorkspace(directory),
                                    enabled=["write_file", "read_file",
                                             "apply_patch"])


def edits(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"old_text": old, "new_text": new} for old, new in pairs]


ORIGINAL = "def add(a, b):\n    return a + b\n"


class ApplyPatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_edit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            await registry.execute("write_file", {"path": "calc.py",
                                                  "content": ORIGINAL})
            result = await registry.execute("apply_patch", {
                "path": "calc.py",
                "edits": edits(("return a + b", "return a - b"))})
            read = await registry.execute("read_file", {"path": "calc.py"})

        self.assertEqual(result["edits_applied"], 1)
        self.assertEqual(result["path"], "calc.py")
        self.assertIn("return a - b", read["content"])

    async def test_multiple_edits_on_one_file_apply_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            await registry.execute("write_file", {
                "path": "app.py",
                "content": "TITLE = 'old'\nNAME = 'old'\n"})
            result = await registry.execute("apply_patch", {
                "path": "app.py",
                "edits": edits(("TITLE = 'old'", "TITLE = 'new'"),
                               ("NAME = 'old'", "NAME = 'new'"))})
            read = await registry.execute("read_file", {"path": "app.py"})

        self.assertEqual(result["edits_applied"], 2)
        self.assertEqual(read["content"], "TITLE = 'new'\nNAME = 'new'\n")

    async def test_later_edit_may_depend_on_earlier_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            await registry.execute("write_file", {
                "path": "app.py", "content": "x = 1\n"})
            result = await registry.execute("apply_patch", {
                "path": "app.py",
                "edits": edits(("x = 1", "x = 2"),
                               ("x = 2", "x = 3"))})
            read = await registry.execute("read_file", {"path": "app.py"})

        self.assertEqual(result["edits_applied"], 2)
        self.assertEqual(read["content"], "x = 3\n")

    async def test_failed_edit_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            await registry.execute("write_file", {"path": "a.py",
                                                  "content": "keep me\n"})
            with self.assertRaisesRegex(ValueError, "not found"):
                await registry.execute("apply_patch", {
                    "path": "a.py",
                    "edits": edits(("keep me", "changed"),
                                   ("not present", "boom"))})
            read = await registry.execute("read_file", {"path": "a.py"})

        self.assertEqual(read["content"], "keep me\n")

    async def test_ambiguous_edit_text_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            await registry.execute("write_file", {
                "path": "dup.py", "content": "same\nsame\n"})
            with self.assertRaisesRegex(ValueError, "2 times"):
                await registry.execute("apply_patch", {
                    "path": "dup.py",
                    "edits": edits(("same", "other"))})

    async def test_empty_old_text_is_rejected(self) -> None:
        # creation is write_file's job; apply_patch edits existing files only
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            with self.assertRaisesRegex(ValueError, "non-empty"):
                await registry.execute("apply_patch", {
                    "path": "new.py", "edits": edits(("", "print('hi')\n"))})

    async def test_empty_new_text_deletes_the_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            await registry.execute("write_file", {
                "path": "a.py", "content": "keep\nremove\n"})
            result = await registry.execute("apply_patch", {
                "path": "a.py", "edits": edits(("remove\n", ""))})
            read = await registry.execute("read_file", {"path": "a.py"})

        self.assertEqual(result["edits_applied"], 1)
        self.assertEqual(read["content"], "keep\n")

    async def test_empty_edits_array_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            await registry.execute("write_file", {"path": "a.py",
                                                  "content": "x\n"})
            with self.assertRaisesRegex(ValueError, "non-empty"):
                await registry.execute("apply_patch", {
                    "path": "a.py", "edits": []})

    async def test_unexpected_edit_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            await registry.execute("write_file", {"path": "a.py",
                                                  "content": "x = 1\n"})
            with self.assertRaisesRegex(ValueError, "unexpected keys"):
                await registry.execute("apply_patch", {
                    "path": "a.py",
                    "edits": [{"old_text": "x = 1",
                               "new_string": "x = 2"}]})

    async def test_edit_missing_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            with self.assertRaisesRegex(ValueError, "cannot read file"):
                await registry.execute("apply_patch", {
                    "path": "ghost.py",
                    "edits": edits(("old text", "new text"))})

    async def test_absolute_path_outside_root_rejected_by_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            with self.assertRaisesRegex(ValueError, "escapes the workspace"):
                await registry.execute("apply_patch", {
                    "path": "/etc/calc.py",
                    "edits": edits(("x", "y"))})

    async def test_escaping_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            with self.assertRaisesRegex(ValueError, "escapes the workspace"):
                await registry.execute("apply_patch", {
                    "path": "../../evil.py",
                    "edits": edits(("x", "y"))})


if __name__ == "__main__":
    unittest.main()
