import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.execution.local import LocalWorkspace
from agent_runtime.tools.tools import create_default_registry


SYMBOL_TOOLS = ["write_file", "find_symbol", "find_references"]


def make_registry(directory: str):
    return create_default_registry(workspace=LocalWorkspace(directory),
                                   enabled=SYMBOL_TOOLS)


SOURCE = (
    "class Agent:\n"
    "    def run(self):\n"
    "        return plan()\n"
    "\n"
    "def plan():\n"
    "    agent = Agent()\n"
    "    return agent.run()\n"
)

BROKEN_SOURCE = (
    "def first():\n"
    "    return 1\n"
    "\n"
    "def second(:\n"   # syntax error from here on
    "    pass\n"
)


class FindSymbolTests(unittest.IsolatedAsyncioTestCase):
    async def test_finds_function_class_and_method(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            await registry.execute("write_file", {"path": "agent.py",
                                                  "content": SOURCE})

            fn = await registry.execute("find_symbol", {"name": "plan"})
            cls = await registry.execute("find_symbol", {"name": "Agent"})
            method = await registry.execute("find_symbol",
                                            {"name": "Agent.run"})

        self.assertEqual(fn["matches"], [
            {"path": "agent.py", "name": "plan", "kind": "function",
             "line": 5, "end_line": 7}])
        self.assertEqual(cls["matches"][0]["kind"], "class")
        self.assertEqual(method["matches"][0]["kind"], "method")
        self.assertEqual(method["matches"][0]["name"], "Agent.run")

    async def test_short_name_matches_qualified_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            await registry.execute("write_file", {"path": "agent.py",
                                                  "content": SOURCE})

            result = await registry.execute("find_symbol", {"name": "run"})

        self.assertEqual(result["match_count"], 1)
        self.assertEqual(result["matches"][0]["name"], "Agent.run")

    async def test_kind_filter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            await registry.execute("write_file", {"path": "agent.py",
                                                  "content": SOURCE})

            result = await registry.execute("find_symbol",
                                            {"name": "Agent", "kind": "function"})

        self.assertEqual(result["match_count"], 0)

    async def test_invalid_kind_raises(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            with self.assertRaisesRegex(ValueError, "kind"):
                await registry.execute("find_symbol",
                                       {"name": "x", "kind": "variable"})

    async def test_tolerates_syntax_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            await registry.execute("write_file", {"path": "broken.py",
                                                  "content": BROKEN_SOURCE})

            result = await registry.execute("find_symbol", {"name": "first"})

        self.assertEqual(result["match_count"], 1)
        self.assertEqual(result["matches"][0]["line"], 1)


class FindReferencesTests(unittest.IsolatedAsyncioTestCase):
    async def test_finds_usages_excluding_definitions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            await registry.execute("write_file", {"path": "agent.py",
                                                  "content": SOURCE})

            result = await registry.execute("find_references",
                                            {"name": "plan"})

        references = [(ref["path"], ref["line"]) for ref in result["references"]]
        self.assertEqual(references, [("agent.py", 3)])  # line 5 is the def

    async def test_reports_definition_lines_of_methods_via_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            await registry.execute("write_file", {"path": "agent.py",
                                                  "content": SOURCE})

            result = await registry.execute("find_references", {"name": "run"})

        lines = [ref["line"] for ref in result["references"]]
        self.assertEqual(lines, [7])  # agent.run(); line 2 is the def name

    async def test_no_references_reports_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            await registry.execute("write_file", {"path": "agent.py",
                                                  "content": SOURCE})

            result = await registry.execute("find_references",
                                            {"name": "nonexistent"})

        self.assertEqual(result["match_count"], 0)
        self.assertEqual(result["references"], [])


if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path

    # A direct run skips ``conftest.py``: isolate the runtime home here too.
    _sys.path.insert(0, str(next(parent for parent in
                                 _Path(__file__).resolve().parents
                                 if (parent / "conftest.py").is_file())))
    from _direct_run import isolate_runtime_home

    isolate_runtime_home()
    unittest.main()
