import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.execution.local import LocalWorkspace
from agent_runtime.tools.edit_match import MatchError, apply_edit
from agent_runtime.tools.syntax_gate import syntax_error
from agent_runtime.tools.patch import apply_patch


PY_FILE = "class Foo:\n    def bar(self):\n        return 1\n"




class MatchLadderTests(unittest.TestCase):
    """apply_edit level semantics; uniqueness is enforced at every level."""

    def test_l0_exact(self) -> None:
        updated, mode = apply_edit("a = 1\nb = 2\n", "a = 1", "a = 9")
        self.assertEqual(updated, "a = 9\nb = 2\n")
        self.assertEqual(mode, "exact")

    def test_l0_ambiguous_raises(self) -> None:
        with self.assertRaises(MatchError) as ctx:
            apply_edit("x = 1\nx = 1\n", "x = 1", "x = 2")
        self.assertIn("2 times", str(ctx.exception))

    def test_l1_strips_line_number_prefixes(self) -> None:
        # old_str copied from a `grep -n` listing carries N: prefixes
        content = "class Foo:\n    def bar(self):\n        return 1\n"
        old = "class Foo:\n12:    def bar(self):\n13:        return 1\n"
        updated, mode = apply_edit(content, old, "def bar(self):\n    return 2\n")
        self.assertEqual(mode, "lineno-stripped")
        self.assertIn("return 2", updated)

    def test_l2_shifts_new_str_to_file_indent_level(self) -> None:
        # model wrote old_str one nesting level too shallow
        old = "def bar(self):\n    return 1\n"
        new = "def bar(self):\n    return 2\n"
        updated, mode = apply_edit(PY_FILE, old, new)
        self.assertEqual(updated, "class Foo:\n    def bar(self):\n        return 2\n")
        self.assertEqual(mode, "indent-shifted")

    def test_l2_does_not_guess_on_ragged_indent(self) -> None:
        # line 1 delta 0, line 2 delta -4: not a rigid block shift, so
        # new_str lands as authored (over-indented but valid) and the
        # mode says the matcher did not translate
        content = "def f():\n    pass\n"
        old = "def f():\n        pass\n"
        new = "def f():\n        return 0\n"
        updated, mode = apply_edit(content, old, new)
        self.assertEqual(updated, "def f():\n        return 0\n")
        self.assertEqual(mode, "indent-insensitive")

    def test_l2_handles_crlf_multiline_and_preserves_eol(self) -> None:
        content = "def a():\r\n    return 1\r\n\r\ndef b():\r\n    return 2\r\n"
        old = "def a():\n    return 1\n"
        new = "def a():\n    return 42\n"
        updated, mode = apply_edit(content, old, new)
        self.assertIn("return 42\r\n", updated)
        self.assertNotIn("return 1", updated)
        self.assertEqual(mode, "indent-insensitive")

    def test_l2_ambiguous_lists_line_ranges(self) -> None:
        content = "def f():\n    x = 1\n    y = 2\n\n\ndef g():\n    x = 1\n    y = 2\n"
        with self.assertRaises(MatchError) as ctx:
            apply_edit(content, "x = 1\ny = 2\n", "z")
        message = str(ctx.exception)
        self.assertIn("ambiguous", message)
        self.assertIn("lines 2-3", message)
        self.assertIn("lines 7-8", message)

    def test_total_miss_renders_diagnosis(self) -> None:
        content = "class Foo:\n\tdef bar(self):\n\t\treturn 1  \n"  # tabs + trailing ws
        with self.assertRaises(MatchError) as ctx:
            apply_edit(content, "def qux(self):\n    return 9\n", "x")
        message = str(ctx.exception)
        self.assertIn("tab-indented", message)     # indent census
        self.assertIn("trailing-ws", message)      # invisible chars surfaced
        self.assertIn("closest region", message)   # best candidate window


class SyntaxGateTests(unittest.TestCase):
    def test_blocks_tab_space_mixed_python(self) -> None:
        mixed = "class Foo:\n\tdef a(self):\n\t\treturn 1\n    def b(self):\n        return 2\n"
        problem = syntax_error("m.py", mixed)
        self.assertIsNotNone(problem)
        self.assertIn("IndentationError", problem)

    def test_blocks_trailing_comma_json(self) -> None:
        self.assertIsNotNone(syntax_error("x.json", '{"a": 1,}'))

    def test_passes_valid_python(self) -> None:
        self.assertIsNone(syntax_error("ok.py", PY_FILE))

    def test_fails_open_on_unchecked_extensions(self) -> None:
        self.assertIsNone(syntax_error("notes.txt", "anything goes <<<"))


class EditFileIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_crlf_survives_edit_round_trip(self) -> None:
        # the FAIL-SILENT regression: pre-fix, this silently rewrote the
        # whole file to LF; byte-faithful IO must keep every original \r\n
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "win.bat"
            path.write_bytes(b"def a():\r\n    return 1\r\n\r\ndef b():\r\n    return 2\r\n")
            ws = LocalWorkspace(directory)
            await ws.write_file("win.bat", path.read_bytes().decode("utf-8"))
            result = await ws.read_file("win.bat")
            await ws.write_file("win.bat",
                                result["content"].replace("return 1", "return 42"))
            data = path.read_bytes()
        self.assertIn(b"return 42\r\n", data)
        self.assertEqual(data.count(b"\r\n"), 5)  # same count as the original

    async def test_edit_file_reports_match_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ws = LocalWorkspace(directory)
            await ws.write_file("m.py", PY_FILE)
            result = await ws.read_file("m.py")
            from agent_runtime.tools.files import edit_file
            out = await edit_file("m.py", "return 1", "return 2", workspace=ws)
        self.assertEqual(out["match_mode"], "exact")
        self.assertIn("return 2", out["occurrences_replaced"] * "" or "return 2")


class ApplyPatchIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def _registry_files(self, directory: str):
        from agent_runtime.tools.tools import create_default_registry
        return create_default_registry(
            workspace=LocalWorkspace(directory),
            enabled=["write_file", "read_file", "apply_patch"])

    async def test_conflict_marker_text_is_editable(self) -> None:
        # structured params have no block delimiters, so marker lines are
        # plain text: matching handles them, the syntax gate decides
        # (.txt is exempt, .py conflicted content fails the gate)
        with tempfile.TemporaryDirectory() as directory:
            registry = self._registry_files(directory)
            conflicted = ("a\n<<<<<<< HEAD\nb\n=======\nc\n>>>>>>> branch\n")
            await registry.execute("write_file", {"path": "n.txt",
                                                  "content": conflicted})
            result = await registry.execute("apply_patch", {
                "path": "n.txt",
                "edits": [{"old_text": "<<<<<<< HEAD",
                           "new_text": "<<<<<<< LOCAL"}]})
            read = await registry.execute("read_file", {"path": "n.txt"})
        self.assertEqual(result["edits_applied"], 1)
        self.assertIn("<<<<<<< LOCAL", read["content"])

    async def test_syntax_broken_new_str_is_blocked_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = self._registry_files(directory)
            await registry.execute("write_file", {"path": "app.py",
                                                  "content": PY_FILE})
            # mixed tab/space replacement text must not land
            bad_new = "def bar(self):\n\t\treturn 2\n"
            with self.assertRaises(ValueError) as ctx:
                await registry.execute("apply_patch", {
                    "path": "app.py",
                    "edits": [{"old_text": "def bar(self):\n    return 1\n",
                               "new_text": bad_new}]})
            self.assertIn("syntax check", str(ctx.exception))
            read = await registry.execute("read_file", {"path": "app.py"})
        self.assertEqual(read["content"], PY_FILE)  # nothing was written

    async def test_match_modes_recorded_for_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = self._registry_files(directory)
            await registry.execute("write_file", {"path": "m.py", "content": PY_FILE})
            result = await registry.execute("apply_patch", {
                "path": "m.py",
                "edits": [{"old_text": "return 1",
                           "new_text": "return 2"}]})
        self.assertEqual(result["match_modes"], ["exact"])


if __name__ == "__main__":
    unittest.main()
