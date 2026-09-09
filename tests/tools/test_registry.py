import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.execution.local import LocalWorkspace
from agent_runtime.tools.tools import create_default_registry


BUILTIN_TOOLS = ["run_command", "write_file", "read_file", "read_output",
                 "list_dir", "edit_file", "apply_patch", "grep_search",
                 "glob_files", "find_symbol", "find_references",
                 "execute_code"]


class DefaultRegistryTests(unittest.TestCase):
    def test_default_registry_exposes_all_builtins(self) -> None:
        registry = create_default_registry()
        self.assertEqual([schema["function"]["name"] for schema in registry.schemas],
                         BUILTIN_TOOLS)

    def test_enabled_filter_selects_named_tools_in_order(self) -> None:
        registry = create_default_registry(enabled=["read_file", "run_command"])
        self.assertEqual([schema["function"]["name"] for schema in registry.schemas],
                         ["read_file", "run_command", "read_output"])

    def test_baseline_tool_set_can_be_selected(self) -> None:
        registry = create_default_registry(enabled=["run_command"])
        self.assertEqual([schema["function"]["name"] for schema in registry.schemas],
                         ["run_command", "read_output"])

    def test_read_output_is_always_available_infrastructure(self) -> None:
        # The observation spiller references .outputs/ files; the paging
        # tool must exist even when the harness gene disables everything.
        registry = create_default_registry(enabled=[])
        self.assertEqual([schema["function"]["name"] for schema in registry.schemas],
                         ["read_output"])

    def test_unknown_enabled_tool_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown tools in harness"):
            create_default_registry(enabled=["definitely_not_a_tool"])


class ToolRoutingGuidanceTests(unittest.TestCase):
    """Each file tool states its own fit without naming siblings.

    Functional sentences (what the tool does) stay untouched; exactly one
    suitability sentence says what it is best suited for. Tools stay
    independent: no description references another tool by name.
    """

    def descriptions(self) -> dict[str, str]:
        registry = create_default_registry()
        return {schema["function"]["name"]: schema["function"]["description"]
                for schema in registry.schemas}

    def test_each_file_tool_names_no_sibling(self) -> None:
        descs = self.descriptions()
        tools = ["write_file", "edit_file", "apply_patch"]
        for name in tools:
            others = [t for t in tools if t != name]
            for other in others:
                self.assertNotIn(other, descs[name])

    def test_write_file_suits_rewrites_new_and_broken_states(self) -> None:
        desc = self.descriptions()["write_file"]
        self.assertIn("Create or overwrite", desc)
        self.assertIn("Best suited", desc)

    def test_edit_file_suits_single_point_edits(self) -> None:
        desc = self.descriptions()["edit_file"]
        self.assertIn("exactly one occurrence", desc)
        self.assertIn("Best suited", desc)

    def test_apply_patch_suits_batched_multi_file_edits(self) -> None:
        desc = self.descriptions()["apply_patch"]
        self.assertIn("SEARCH/REPLACE blocks", desc)
        self.assertIn("Best suited", desc)


class WorkspaceInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_tools_use_injected_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = create_default_registry(
                workspace=LocalWorkspace(directory), enabled=["write_file"])

            written = await registry.execute("write_file", {
                "path": "injected.txt", "content": "here"})
            listing = sorted(path.name for path in Path(directory).iterdir())

        self.assertEqual(written["bytes_written"], 4)
        self.assertEqual(listing, ["injected.txt"])


if __name__ == "__main__":
    unittest.main()
