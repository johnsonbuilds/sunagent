import json
import sys
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.harness import HarnessError, from_dict
from agent_runtime.evaluation import (
    annotate_record,
    build_report,
    extract_record,
    find_record,
    format_report,
    latest_accuracy_by_harness,
    load_records,
    write_record,
)


def make_job(root: Path, name: str, *, passed: bool, harness_id: str = "baseline-v0",
             genes_hash: str = "abc123", error: bool = False) -> Path:
    """Create a minimal Harbor jobs directory with one trial."""
    job_dir = root / "jobs" / name
    trial_dir = job_dir / "fix-git__TRIAL01"
    (trial_dir / "agent").mkdir(parents=True)
    (trial_dir / "verifier").mkdir(parents=True)

    trace_header = {
        "record_type": "harness", "run_id": "run-x",
        "harness_id": harness_id, "genes_hash": genes_hash,
        "harness": {},
    }
    (trial_dir / "agent" / "agent-runtime.jsonl").write_text(
        json.dumps(trace_header) + "\n" + json.dumps({"event_type": "agent.start"}) + "\n")

    trial_result = {
        "trial_name": "fix-git__TRIAL01",
        "task_name": "terminal-bench/fix-git",
        "exception_info": {"type": "RuntimeError", "message": "boom"} if error else None,
        "agent_execution": {"started_at": "2026-08-18T10:00:00Z",
                            "finished_at": "2026-08-18T10:01:00Z"},
        "agent_info": {"name": "sunagent", "model_info": None},
    }
    (trial_dir / "result.json").write_text(json.dumps(trial_result))

    reward = 1.0 if passed else 0.0
    top_result = {
        "stats": {"evals": {"sunagent__terminal-bench/terminal-bench-2": {
            "n_trials": 1, "n_errors": 1 if error else 0,
            # The verifier can reward a trial even when the agent raised
            # afterwards (e.g. killed by the timeout after the solution
            # was already on disk), so the reward stays in the stats.
            "reward_stats": {"reward": {str(reward): ["fix-git__TRIAL01"]}},
        }}},
    }
    (job_dir / "result.json").write_text(json.dumps(top_result))
    (job_dir / "config.json").write_text(json.dumps({
        "datasets": [{"name": "terminal-bench/terminal-bench-2",
                      "task_names": ["terminal-bench/fix-git"]}],
    }))
    return job_dir


class ExtractRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_passed_run_extraction(self) -> None:
        job_dir = make_job(self.root, "2026-08-18__10-00-00", passed=True)
        record = extract_record(job_dir, model="qwen3-coder")
        self.assertEqual(record["harness_id"], "baseline-v0")
        self.assertEqual(record["genes_hash"], "abc123")
        self.assertEqual(record["model"], "qwen3-coder")
        self.assertEqual(record["benchmark"], "terminal-bench/terminal-bench-2")
        self.assertEqual(record["score"]["rewards"], 1)
        self.assertEqual(record["score"]["failed"], 0)
        self.assertEqual(record["score"]["total"], 1)
        self.assertEqual(record["score"]["accuracy"], 1.0)
        task = record["tasks"][0]
        self.assertEqual(task["task"], "terminal-bench/fix-git")
        self.assertEqual(task["status"], "passed")
        self.assertIsNone(task["failure_mode"])
        self.assertIn("record_type", json.loads(
            Path(task["trace"]).read_text().splitlines()[0]))

    def test_failed_run_extraction(self) -> None:
        job_dir = make_job(self.root, "2026-08-18__11-00-00", passed=False)
        record = extract_record(job_dir)
        self.assertEqual(record["tasks"][0]["status"], "failed")
        self.assertEqual(record["score"]["accuracy"], 0.0)

    def test_errored_run_extraction(self) -> None:
        job_dir = make_job(self.root, "2026-08-18__12-00-00", passed=False, error=True)
        record = extract_record(job_dir)
        self.assertEqual(record["tasks"][0]["status"], "error")
        # Harbor counts a trial that never reached the verifier as 0 in
        # the dataset mean, so accuracy is 0.0 rather than undefined.
        self.assertEqual(record["score"]["accuracy"], 0.0)
        self.assertEqual(record["score"]["errored"], 1)
        self.assertEqual(record["score"]["rewards"], 0)

    def test_reward_survives_an_agent_error(self) -> None:
        """A trial that earned a reward before raising still counts."""
        job_dir = make_job(self.root, "2026-08-18__12-30-00", passed=True,
                           error=True)
        record = extract_record(job_dir)
        self.assertEqual(record["tasks"][0]["status"], "error")
        self.assertEqual(record["score"]["rewards"], 1)
        self.assertEqual(record["score"]["accuracy"], 1.0)

    def test_record_id_uses_short_benchmark_slug(self) -> None:
        job_dir = make_job(self.root, "2026-08-18__13-00-00", passed=True)
        record = extract_record(job_dir)
        self.assertEqual(
            record["record_id"],
            "2026-08-18__13-00-00__baseline-v0__terminal-bench-2")

    def test_mixed_harnesses_in_one_job_are_rejected(self) -> None:
        job_dir = make_job(self.root, "2026-08-18__14-00-00", passed=True,
                           harness_id="baseline-v0")
        # second trial with a different harness
        other = job_dir / "hello__TRIAL02"
        (other / "agent").mkdir(parents=True)
        (other / "agent" / "agent-runtime.jsonl").write_text(json.dumps({
            "record_type": "harness", "harness_id": "baseline-v1",
            "genes_hash": "zzz"}) + "\n")
        (other / "result.json").write_text(json.dumps({
            "trial_name": "hello__TRIAL02", "task_name": "terminal-bench/hello",
            "agent_execution": {}, "agent_info": {},
        }))
        with self.assertRaisesRegex(HarnessError, "mixes harnesses"):
            extract_record(job_dir)

    def test_missing_job_directory_is_rejected(self) -> None:
        with self.assertRaisesRegex(HarnessError, "job directory not found"):
            extract_record(self.root / "jobs" / "nope")


class RecordFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.records_dir = self.root / "records"
        self.job_dir = make_job(self.root, "2026-08-18__10-00-00", passed=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_write_and_load_roundtrip(self) -> None:
        record = extract_record(self.job_dir)
        path = write_record(record, self.records_dir)
        self.assertTrue(path.exists())
        loaded = load_records(self.records_dir)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["record_id"], record["record_id"])

    def test_records_are_immutable(self) -> None:
        record = extract_record(self.job_dir)
        write_record(record, self.records_dir)
        with self.assertRaisesRegex(HarnessError, "immutable"):
            write_record(record, self.records_dir)

    def test_find_record_by_id_prefix(self) -> None:
        record = extract_record(self.job_dir)
        write_record(record, self.records_dir)
        self.assertTrue(find_record(record["record_id"][:16], self.records_dir).exists())

    def test_ambiguous_reference_is_rejected(self) -> None:
        for name in ("2026-08-18__20-00-00", "2026-08-18__21-00-00"):
            write_record(extract_record(make_job(self.root, name, passed=True)),
                         self.records_dir)
        with self.assertRaisesRegex(HarnessError, "ambiguous"):
            find_record("2026-08-18", self.records_dir)

    def test_annotate_sets_failure_mode(self) -> None:
        path = write_record(extract_record(self.job_dir), self.records_dir)
        annotate_record(str(path), "fix-git", "false-success", self.records_dir)
        updated = json.loads(path.read_text())
        self.assertEqual(updated["tasks"][0]["failure_mode"], "false-success")

    def test_annotate_unknown_task_is_rejected(self) -> None:
        path = write_record(extract_record(self.job_dir), self.records_dir)
        with self.assertRaisesRegex(HarnessError, "not in record"):
            annotate_record(str(path), "no-such-task", "x", self.records_dir)


class ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.records_dir = self.root / "records"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _record_for(self, harness_id: str, accuracy: float | None) -> dict:
        job = make_job(self.root, f"job-{harness_id}-{len(list(self.records_dir.glob('*.json')))}",
                       passed=bool(accuracy), harness_id=harness_id,
                       genes_hash=harness_id[::-1])
        record = extract_record(job)
        if accuracy is not None:
            # Synthetic multi-task score: binary fixtures cannot express
            # intermediate accuracies, so override the aggregate directly.
            record["score"] = {"rewards": accuracy * 10,
                               "failed": (1 - accuracy) * 10,
                               "total": 10, "accuracy": accuracy}
            record["tasks"] = [
                {"task": f"task-{i}", "trial": f"task-{i}", "status": "passed",
                 "reward": 1.0 if i < accuracy * 10 else 0.0,
                 "trace": "", "failure_mode": None}
                for i in range(10)
            ]
        return record

    def test_latest_accuracy_wins(self) -> None:
        write_record(self._record_for("baseline-v0", 0.4), self.records_dir)
        write_record(self._record_for("baseline-v0", 0.9), self.records_dir)
        accuracies = latest_accuracy_by_harness(load_records(self.records_dir))
        self.assertEqual(accuracies["baseline-v0"], 0.9)

    def test_accuracy_is_recomputed_for_legacy_records(self) -> None:
        """Pre-rename records keep their old score block but still report
        the reward-aligned accuracy."""
        record = self._record_for("baseline-v0", 0.4)
        record["score"] = {"passed": 4, "failed": 0, "errored": 6,
                           "total": 10, "rate": 1.0}
        accuracies = latest_accuracy_by_harness([record])
        self.assertEqual(accuracies["baseline-v0"], 0.4)

    def test_report_rows_and_delta(self) -> None:
        specs = {"baseline-v0": from_dict({}),
                 "baseline-v1": from_dict({"id": "baseline-v1", "parent": "baseline-v0"})}
        records = [self._record_for("baseline-v0", 0.4),
                   self._record_for("baseline-v1", 0.9)]
        rows = build_report(specs, records)
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["baseline-v0"]["accuracy"], 0.4)
        self.assertIsNone(by_id["baseline-v0"]["delta"])
        self.assertEqual(by_id["baseline-v1"]["delta"], 0.5)

    def test_report_orders_parents_before_children(self) -> None:
        specs = {
            "baseline-v0": from_dict({}),
            "baseline-v2": from_dict({"id": "baseline-v2", "parent": "baseline-v1"}),
            "baseline-v1": from_dict({"id": "baseline-v1", "parent": "baseline-v0"}),
        }
        rows = build_report(specs, [])
        self.assertEqual([row["id"] for row in rows],
                         ["baseline-v0", "baseline-v1", "baseline-v2"])

    def test_record_without_manifest_is_listed(self) -> None:
        rows = build_report({}, [self._record_for("ghost-v9", 1.0)])
        self.assertEqual(rows[0]["mutation"], "(manifest missing)")

    def test_format_report_renders_table(self) -> None:
        rows = [
            {"id": "baseline-v0", "parent": None, "mutation": None,
             "accuracy": 0.4, "delta": None},
            {"id": "baseline-v1", "parent": "baseline-v0",
             "mutation": "enable verification", "accuracy": 0.9, "delta": 0.5},
        ]
        text = format_report(rows)
        self.assertIn("baseline-v0", text)
        self.assertIn("+50%", text)
        self.assertIn("enable verification", text)


if __name__ == "__main__":
    unittest.main()
