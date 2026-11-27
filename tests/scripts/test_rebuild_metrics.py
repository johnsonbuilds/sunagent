import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import rebuild_metrics
from agent_runtime import metrics, run_records


def record(run_id: str, *, status: str = "completed",
           finished_at: float = 1.0) -> run_records.RunMetrics:
    return run_records.RunMetrics(
        run_id=run_id, origin="webui", status=status,
        started_at=finished_at - 1.0, finished_at=finished_at,
        tokens=run_records.TokenUsage(prompt=10, completion=5, cached=2),
    )


def write_trace(traces: Path, run_id: str, *, status: str = "completed",
                finished_at: float = 1.0, with_metrics: bool = True,
                torn_line: bool = False) -> Path:
    """Write a trace the way ``RunTrace`` does: header, events, metrics."""
    traces.mkdir(parents=True, exist_ok=True)
    path = traces / f"{run_id}.jsonl"
    lines = [json.dumps({"record_type": "harness", "run_id": run_id,
                         "harness_id": "general-v1"})]
    if torn_line:
        # A killed writer leaves a half line; it must not lose the record.
        lines.append('{"run_id": "%s", "event_type": "agent.end"' % run_id)
    lines.append(json.dumps({"run_id": run_id, "event_id": "evt-1",
                             "event_type": "agent.end", "iteration": 0}))
    if with_metrics:
        lines.append(json.dumps({
            "run_id": run_id, "event_id": "evt-2",
            "event_type": "run.metrics",
            "data": record(run_id, status=status,
                           finished_at=finished_at).to_dict(),
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class RebuildMetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.traces = self.home / "traces"
        self.index = self.home / "metrics" / "runs.jsonl"

    def run_main(self, *extra: str) -> int:
        with redirect_stdout(io.StringIO()):
            return rebuild_metrics.main(
                ["--traces", str(self.traces), "--metrics", str(self.index),
                 *extra])

    def test_one_line_per_trace_that_recorded_metrics(self) -> None:
        write_trace(self.traces, "run-a")
        write_trace(self.traces, "run-b", finished_at=2.0, torn_line=True)
        # Killed before finalize: nothing to rebuild, and it is reported.
        write_trace(self.traces, "run-c", with_metrics=False)

        result = rebuild_metrics.plan(self.traces, self.index)

        self.assertEqual([r.run_id for r in result.records], ["run-a", "run-b"])
        self.assertEqual(result.traces, 3)
        self.assertEqual(result.skipped, ["run-c.jsonl"])
        self.assertFalse(self.index.exists(), "planning must not write")

        self.assertEqual(self.run_main("--write"), 0)

        written = metrics.read_run_metrics(self.index)
        self.assertEqual([r.run_id for r in written], ["run-a", "run-b"])
        self.assertEqual(written[1].status, "completed")
        self.assertEqual(written[0].tokens.total, 15)
        self.assertEqual(written[0].tokens.cached, 2)

    def test_an_existing_line_wins_over_its_trace_and_is_not_duplicated(self) -> None:
        metrics.append_run_metrics(record("run-a", status="completed"), self.index)
        write_trace(self.traces, "run-a", status="cancelled", finished_at=9.0)
        write_trace(self.traces, "run-b", finished_at=2.0)

        self.assertEqual(self.run_main("--write", "--keep-existing"), 0)

        merged = metrics.read_run_metrics(self.index)
        self.assertEqual([r.run_id for r in merged], ["run-a", "run-b"])
        self.assertEqual([r.status for r in merged], ["completed", "completed"])

        # Without --keep-existing the traces are the only source, and the
        # lines come out in finish order.
        self.assertEqual(self.run_main("--write"), 0)

        rebuilt = metrics.read_run_metrics(self.index)
        self.assertEqual([r.run_id for r in rebuilt], ["run-b", "run-a"])
        self.assertEqual(rebuilt[1].status, "cancelled")


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
