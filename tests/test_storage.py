import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_runtime import storage


class RuntimeHomeTests(unittest.TestCase):
    def test_the_default_home_is_the_project_workspace(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_RUNTIME_HOME", None)
            home = storage.runtime_home()

        self.assertEqual(home.name, "workspace")
        self.assertTrue(home.is_dir() or home.parent.is_dir())

    def test_the_env_var_moves_the_whole_home(self) -> None:
        with patch.dict(os.environ, {"AGENT_RUNTIME_HOME": "/tmp/elsewhere"}):
            home = storage.runtime_home()

            self.assertEqual(home, Path("/tmp/elsewhere"))
            self.assertEqual(storage.sessions_dir(),
                             Path("/tmp/elsewhere/sessions"))
            self.assertEqual(storage.traces_dir(), Path("/tmp/elsewhere/traces"))
            self.assertEqual(storage.memory_dir(), Path("/tmp/elsewhere/memory"))

    def test_every_artifact_lands_under_the_same_home(self) -> None:
        with patch.dict(os.environ, {"AGENT_RUNTIME_HOME": "/tmp/elsewhere"}):
            paths = [storage.sessions_dir(), storage.traces_dir(),
                     storage.memory_dir(), storage.session_memory_path(),
                     storage.trace_path("run-1")]

        for path in paths:
            self.assertTrue(str(path).startswith("/tmp/elsewhere"), path)

    def test_the_trace_file_is_named_after_the_run(self) -> None:
        with patch.dict(os.environ, {"AGENT_RUNTIME_HOME": "/tmp/elsewhere"}):
            path = storage.trace_path("run-abc123")

        self.assertEqual(path.name, "run-abc123.jsonl")
        self.assertEqual(path.parent.name, "traces")

    def test_the_summary_is_a_single_file_under_memory(self) -> None:
        with patch.dict(os.environ, {"AGENT_RUNTIME_HOME": "/tmp/elsewhere"}):
            path = storage.session_memory_path()

        self.assertEqual(path.name, "session_memory.md")
        self.assertEqual(path.parent.name, "memory")


if __name__ == "__main__":
    unittest.main()
