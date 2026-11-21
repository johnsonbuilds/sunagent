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

    def test_every_artifact_lands_under_the_same_home(self) -> None:
        with patch.dict(os.environ, {"AGENT_RUNTIME_HOME": "/tmp/elsewhere"}):
            paths = [storage.sessions_dir(), storage.traces_dir(),
                     storage.trace_path("run-1")]

        for path in paths:
            self.assertTrue(str(path).startswith("/tmp/elsewhere"), path)

    def test_the_trace_file_is_named_after_the_run(self) -> None:
        with patch.dict(os.environ, {"AGENT_RUNTIME_HOME": "/tmp/elsewhere"}):
            path = storage.trace_path("run-abc123")

        self.assertEqual(path.name, "run-abc123.jsonl")
        self.assertEqual(path.parent.name, "traces")

    def test_metrics_index_lives_under_the_same_home(self) -> None:
        with patch.dict(os.environ, {"AGENT_RUNTIME_HOME": "/tmp/elsewhere"}):
            path = storage.metrics_path()
            directory = storage.metrics_dir()

        self.assertEqual(path, Path("/tmp/elsewhere/metrics/runs.jsonl"))
        self.assertEqual(directory, Path("/tmp/elsewhere/metrics"))


class IsolationTests(unittest.TestCase):
    def test_the_suite_never_writes_into_the_checkout(self) -> None:
        # ``tests/conftest.py`` points the home at a temporary directory:
        # a test run must not append to ``workspace/metrics/runs.jsonl``.
        checkout_home = Path(__file__).parents[1] / "workspace"

        self.assertNotEqual(storage.runtime_home(), checkout_home)
        self.assertFalse(str(storage.metrics_path()).startswith(
            str(checkout_home)))


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
