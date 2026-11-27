"""Import-graph regressions: trace/metrics stay independent.

The trace writer and the metrics index share record contracts, but neither may
import the other.  These checks run in a fresh interpreter because import
cycles depend on what was loaded first.
"""

import ast
import os
import subprocess
import sys
import unittest
from pathlib import Path


SRC = Path(__file__).parents[1] / "src"


def agent_runtime_imports(path: Path) -> set[str]:
    """Resolve package-internal imports declared by one module."""
    module = f"agent_runtime.{path.stem}"
    tree = ast.parse(path.read_text())
    found: set[str] = set()

    def add(name: str) -> None:
        if name == "agent_runtime" or name.startswith("agent_runtime."):
            found.add(name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package = module.split(".")[:-1]
                keep = len(package) - (node.level - 1)
                target = ".".join(package[:keep])
                if node.module:
                    target = f"{target}.{node.module}" if target else node.module
            else:
                target = node.module or ""
            add(target)
            for alias in node.names:
                if alias.name != "*":
                    add(f"{target}.{alias.name}")
    return found


def run_python(code: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC), env.get("PYTHONPATH", "")]).strip(os.pathsep)
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


class ImportGraphTests(unittest.TestCase):
    def assert_imports(self, code: str) -> None:
        completed = run_python(code)
        self.assertEqual(
            completed.returncode, 0,
            f"import failed:\n{completed.stdout}\n{completed.stderr}",
        )

    def test_storage_does_not_load_trace_or_metrics(self) -> None:
        self.assert_imports(
            "import sys\n"
            "import agent_runtime.storage\n"
            "assert 'agent_runtime.trace' not in sys.modules\n"
            "assert 'agent_runtime.metrics' not in sys.modules\n"
        )

    def test_trace_does_not_load_metrics(self) -> None:
        self.assert_imports(
            "import sys\n"
            "import agent_runtime.trace\n"
            "assert 'agent_runtime.metrics' not in sys.modules\n"
        )

    def test_metrics_does_not_load_trace(self) -> None:
        self.assert_imports(
            "import sys\n"
            "import agent_runtime.metrics as metrics\n"
            "assert 'agent_runtime.trace' not in sys.modules\n"
            "assert not hasattr(metrics, 'RunConfig')\n"
            "assert not hasattr(metrics, 'RunMetrics')\n"
            "assert not hasattr(metrics, 'RunStats')\n"
        )

    def test_run_records_is_a_leaf(self) -> None:
        imports = agent_runtime_imports(
            SRC / "agent_runtime" / "run_records.py")
        self.assertEqual(imports, set(), imports)

    def test_trace_stats_only_depends_on_run_records(self) -> None:
        imports = agent_runtime_imports(
            SRC / "agent_runtime" / "trace_stats.py")
        self.assertTrue(imports, "trace_stats should import run_records")
        allowed = {"agent_runtime.run_records"}
        unexpected = {
            name for name in imports
            if name not in allowed
            and not name.startswith("agent_runtime.run_records.")
        }
        self.assertEqual(unexpected, set(), unexpected)

    def test_root_still_exposes_trace_lazily(self) -> None:
        self.assert_imports(
            "from agent_runtime import RunEvent, RunTrace\n"
            "assert RunTrace.__name__ == 'RunTrace'\n"
            "assert RunEvent.__name__ == 'RunEvent'\n"
        )


if __name__ == "__main__":
    unittest.main()
