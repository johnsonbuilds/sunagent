import sys
import unittest
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.execution.harbor import HarborShellExecutor
from agent_runtime.execution.local import LocalShellExecutor


class FakeExecResult:
    def __init__(self, stdout: str | None, stderr: str | None,
                 return_code: int) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.return_code = return_code


class FakeHarborEnvironment:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, int | None]] = []

    async def exec(self, command: str, cwd: str | None = None,
                   timeout_sec: int | None = None) -> Any:
        self.calls.append((command, cwd, timeout_sec))
        return FakeExecResult("remote output", "remote error", 4)


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_executor_runs_commands(self) -> None:
        result = await LocalShellExecutor().execute("printf local")

        self.assertEqual(result["stdout"], "local")
        self.assertEqual(result["exit_code"], 0)
        self.assertNotIn("error", result)

    async def test_harbor_executor_maps_exec_result(self) -> None:
        environment = FakeHarborEnvironment()
        executor = HarborShellExecutor(environment)

        result = await executor.execute("printf remote", cwd="/workspace", timeout=1.2)

        self.assertEqual(result["stdout"], "remote output")
        self.assertEqual(result["stderr"], "remote error")
        self.assertEqual(result["exit_code"], 4)
        self.assertEqual(environment.calls, [("printf remote", "/workspace", 2)])

    async def test_harbor_executor_works_inside_running_event_loop(self) -> None:
        environment = FakeHarborEnvironment()
        executor = HarborShellExecutor(environment)

        result = await executor.execute("printf remote")

        self.assertEqual(result["exit_code"], 4)


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
