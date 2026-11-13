import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parents[2]))
sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.harness import list_harnesses

MANIFEST = """\
id: {harness_id}
prompt:
  system: hello
tools:
  enabled: []
"""


class ListHarnessesTests(unittest.TestCase):
    def test_scans_only_flat_yaml_files(self) -> None:
        with TemporaryDirectory() as root:
            directory = Path(root)
            (directory / "flat-v1.yaml").write_text(
                MANIFEST.format(harness_id="flat-v1"), encoding="utf-8")
            archived = directory / "archived"
            archived.mkdir()
            (archived / "old-v0.yaml").write_text(
                MANIFEST.format(harness_id="old-v0"), encoding="utf-8")

            with patch("agent_runtime.harness.loader.harnesses_dir",
                       return_value=directory):
                specs = list_harnesses()

        self.assertEqual([spec.id for spec in specs], ["flat-v1"])

    def test_invalid_manifest_is_skipped(self) -> None:
        with TemporaryDirectory() as root:
            directory = Path(root)
            (directory / "broken.yaml").write_text(
                "control:\n  max_iterations: 0\n", encoding="utf-8")
            (directory / "good.yaml").write_text(
                MANIFEST.format(harness_id="good"), encoding="utf-8")

            with patch("agent_runtime.harness.loader.harnesses_dir",
                       return_value=directory):
                specs = list_harnesses()

        self.assertEqual([spec.id for spec in specs], ["good"])

    def test_missing_directory_is_empty(self) -> None:
        with TemporaryDirectory() as root:
            with patch("agent_runtime.harness.loader.harnesses_dir",
                       return_value=Path(root) / "absent"):
                self.assertEqual(list_harnesses(), [])


if __name__ == "__main__":
    unittest.main()
