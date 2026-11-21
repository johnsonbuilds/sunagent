import asyncio
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime import metrics


class RunConfigTests(unittest.TestCase):
    def test_round_trips_every_field(self) -> None:
        config = metrics.RunConfig(
            run_id="run-1", origin="webui", harness_id="coding-v1",
            genes_hash="g8c1", model="deepseek-v4.1-flash",
            session_id="sess-1", task_id="task-1", run_index=2,
            model_params={"temperature": 0.2, "stream": True},
            tools=[{"name": "read_file", "parameters": {"type": "object"}}],
            runtime={"context_budget_tokens": 60000},
            env={"AGENT_RUNTIME_HARNESS": "coding-v1"},
            git={"commit": "1626ef1", "dirty": True, "status_count": 3},
        )

        restored = metrics.RunConfig.from_dict(
            json.loads(json.dumps(config.to_dict())))

        self.assertEqual(restored, config)

    def test_missing_and_unknown_keys_are_tolerated(self) -> None:
        restored = metrics.RunConfig.from_dict(
            {"run_id": "run-1", "future_key": 1, "tools": "not-a-list"})

        self.assertEqual(restored.run_id, "run-1")
        self.assertEqual(restored.origin, metrics.DEFAULT_ORIGIN)
        self.assertEqual(restored.tools, [])
        self.assertIsNone(restored.task_id)
        self.assertIsNone(restored.run_index)


class RunMetricsTests(unittest.TestCase):
    def _record(self) -> metrics.RunMetrics:
        return metrics.RunMetrics(
            run_id="run-1", origin="webui", harness_id="coding-v1",
            genes_hash="g8c1", model="deepseek-v4.1-flash",
            session_id="sess-1", task_id="task-1", run_index=2,
            status="completed", started_at=1763700000.1,
            finished_at=1763700182.4, duration_ms=182300.0, iterations=17,
            tool_calls=24, tool_errors=2,
            error_kinds={"schema": 1, "not_found": 1},
            edited_files=["src/app/main.py"],
            tokens=metrics.TokenUsage(prompt=412000, completion=18000,
                                      cached=300000),
            latency_ms=metrics.LatencyBreakdown(llm=150000.0, tool=21000.0,
                                                overhead=11300.0),
            ttft_ms=820.0, retries=1, compactions=1, trace_bytes=2400000,
            has_answer=True, summary_invalid=1, summary_attempts=3,
        )

    def test_round_trips_every_field(self) -> None:
        record = self._record()

        restored = metrics.RunMetrics.from_dict(
            json.loads(json.dumps(record.to_dict())))

        self.assertEqual(restored, record)
        self.assertEqual(restored.tokens.total, 430000)
        self.assertTrue(restored.artifact_evidence)
        self.assertTrue(restored.has_answer)
        self.assertEqual(restored.summary_attempts, 3)

    def test_a_record_without_artifact_evidence(self) -> None:
        record = metrics.RunMetrics.from_dict(
            {"run_id": "run-2", "status": "error",
             "error": {"stage": "llm", "code": "timeout"}})

        self.assertFalse(record.artifact_evidence)
        self.assertEqual(record.error, {"stage": "llm", "code": "timeout"})
        self.assertEqual(record.edited_files, [])

    def test_an_unknown_status_does_not_claim_success(self) -> None:
        record = metrics.RunMetrics.from_dict(
            {"run_id": "run-3", "status": "mystery"})

        self.assertEqual(record.status, "error")

    def test_an_incomplete_error_object_becomes_null(self) -> None:
        record = metrics.RunMetrics.from_dict(
            {"run_id": "run-4", "error": {"stage": ""}})

        self.assertIsNone(record.error)

    def test_counters_never_treat_a_boolean_as_one(self) -> None:
        record = metrics.RunMetrics.from_dict(
            {"run_id": "run-5", "iterations": True, "tool_calls": 3})

        self.assertEqual(record.iterations, 0)
        self.assertEqual(record.tool_calls, 3)

    def test_a_boolean_flag_only_accepts_a_real_boolean(self) -> None:
        self.assertFalse(metrics.RunMetrics.from_dict(
            {"has_answer": "false"}).has_answer)
        self.assertTrue(metrics.RunMetrics.from_dict(
            {"has_answer": True}).has_answer)

    def test_text_fields_are_trimmed_before_they_are_validated(self) -> None:
        record = metrics.RunMetrics.from_dict(
            {"run_id": " run-6 ", "status": " completed ",
             "edited_files": ["", "  ", " src/app/main.py "]})

        self.assertEqual(record.run_id, "run-6")
        self.assertEqual(record.status, "completed")
        # A blank path must not count as artifact evidence.
        self.assertEqual(record.edited_files, ["src/app/main.py"])

    def test_non_finite_timings_are_clamped(self) -> None:
        record = metrics.RunMetrics.from_dict(
            {"run_id": "run-7", "duration_ms": float("inf"),
             "ttft_ms": float("nan"),
             "latency_ms": {"llm": float("nan")}})

        self.assertEqual(record.duration_ms, 0.0)
        self.assertIsNone(record.ttft_ms)
        self.assertEqual(record.latency_ms.llm, 0.0)

    def test_a_hand_built_record_cannot_hold_a_non_finite_float(self) -> None:
        record = metrics.RunMetrics(run_id="run-8", duration_ms=float("nan"),
                                    ttft_ms=float("inf"))

        self.assertEqual(record.duration_ms, 0.0)
        # An unusable first-token latency is "unknown", not "instant".
        self.assertIsNone(record.ttft_ms)


class RunsIndexTests(unittest.TestCase):
    def test_append_creates_the_index_and_appends_one_line_per_run(self) -> None:
        with TemporaryDirectory() as root:
            path = Path(root) / "metrics" / "runs.jsonl"
            first = metrics.RunMetrics(run_id="run-1", status="completed")
            second = metrics.RunMetrics(run_id="run-2", status="error")

            metrics.append_run_metrics(first, path=path)
            metrics.append_run_metrics(second.to_dict(), path=path)

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual([json.loads(line)["run_id"] for line in lines],
                             ["run-1", "run-2"])

    def test_read_skips_lines_that_are_not_records(self) -> None:
        with TemporaryDirectory() as root:
            path = Path(root) / "runs.jsonl"
            path.write_text('{"run_id":"run-1"}\n\nnot json\n', encoding="utf-8")

            records = metrics.read_run_metrics(path)

            self.assertEqual([record.run_id for record in records], ["run-1"])

    def test_read_skips_json_that_is_not_an_object(self) -> None:
        with TemporaryDirectory() as root:
            path = Path(root) / "runs.jsonl"
            path.write_text('null\n123\n[]\n"foo"\n{"run_id":"run-1"}\n',
                            encoding="utf-8")

            records = metrics.read_run_metrics(path)

            # Otherwise each one would default into a ghost ``completed`` run.
            self.assertEqual([record.run_id for record in records], ["run-1"])

    def test_the_index_never_holds_nan_or_infinity(self) -> None:
        with TemporaryDirectory() as root:
            path = Path(root) / "runs.jsonl"
            metrics.append_run_metrics(metrics.RunMetrics(
                run_id="run-1", duration_ms=float("nan"),
                ttft_ms=float("inf")), path=path)

            line = path.read_text(encoding="utf-8")
            self.assertNotIn("NaN", line)
            self.assertNotIn("Infinity", line)
            self.assertEqual(json.loads(line)["duration_ms"], 0.0)
            self.assertIsNone(json.loads(line)["ttft_ms"])

    def test_a_record_that_no_longer_serializes_is_swallowed(self) -> None:
        with TemporaryDirectory() as root:
            path = Path(root) / "runs.jsonl"
            record = metrics.RunMetrics(run_id="run-1")
            # A field mutated after construction escapes the normalizing
            # ``__post_init__``; the ``finally`` path must still not raise.
            record.duration_ms = float("nan")

            with self.assertLogs("agent_runtime.metrics", level="WARNING"):
                metrics.append_run_metrics(record, path=path)

            self.assertFalse(path.exists())

    def test_read_of_a_missing_index_is_empty(self) -> None:
        with TemporaryDirectory() as root:
            self.assertEqual(
                metrics.read_run_metrics(Path(root) / "runs.jsonl"), [])

    def test_the_index_is_compact_json(self) -> None:
        with TemporaryDirectory() as root:
            path = Path(root) / "runs.jsonl"
            metrics.append_run_metrics(
                metrics.RunMetrics(run_id="run-1", model="模型"), path=path)

            line = path.read_text(encoding="utf-8").strip()
            self.assertNotIn(", ", line)
            self.assertIn("模型", line)


if __name__ == "__main__":
    unittest.main()


class _FlushGate:
    """Trace stand-in whose ``blocking_call``-th flush waits forever.

    Lets a test cancel the finalizer *while it is flushing*, which is the
    only way to exercise the cancel path deterministically.
    """

    output_path = None
    origin = "cli"
    model = ""
    session_id = None
    task_id = None
    run_index = None
    harness = None

    def __init__(self, run_id: str, *, blocking_call: int) -> None:
        self.run_id = run_id
        self.stats = metrics.RunStats()
        self.blocking_call = blocking_call
        self.calls = 0
        self.entered = asyncio.Event()

    def record_metrics(self, record):
        return record

    async def flush(self) -> None:
        self.calls += 1
        if self.calls == self.blocking_call:
            self.entered.set()
            await asyncio.Event().wait()


class FinalizeRunTests(unittest.IsolatedAsyncioTestCase):
    """Phase 1: counters are folded live, so retention cannot zero them."""

    async def test_finalize_aggregates_the_run_from_trace_events(self) -> None:
        from agent_runtime.trace import RunTrace

        with TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            trace = RunTrace(run_id="run-7", retain_events="control",
                             origin="webui", session_id="sess-1",
                             task_id="task-1", run_index=2)
            trace.emit("llm.start", 1, messages=[{"role": "user", "content": "x"}])
            trace.emit("llm.end", 1, duration_ms=100.0, ttft_ms=80.0,
                       status="success")
            trace.emit("tool.end", 1, status="success", exec_ms=10.0,
                       tool="write_file", edited_files=["src/a.py"])
            trace.emit("tool.end", 2, status="error", exec_ms=5.0,
                       tool="run_command", error_kind="exit_code")
            trace.emit("llm.retry", 1, category="provider_error")
            trace.emit("context.llm_summary.invalid", 1, reason="short")
            trace.emit("context.llm_summary.yield", 1, summary="s")
            trace.emit("llm.end", 2, duration_ms=50.0, ttft_ms=70.0,
                       status="success")
            trace.emit("agent.end", 2, prompt_tokens=1000,
                       completion_tokens=200, cached_tokens=400,
                       summarizer_prompt_tokens=300,
                       summarizer_completion_tokens=50)
            trace.emit("agent.completed", 2, iterations=2, answer="done")

            record = metrics.finalize_run(trace, status="completed",
                                          answer="done", path=path)

            self.assertEqual(record.origin, "webui")
            self.assertEqual(record.task_id, "task-1")
            self.assertEqual(record.run_index, 2)
            self.assertEqual(record.iterations, 2)
            self.assertTrue(record.has_answer)
            self.assertEqual(record.tool_calls, 2)
            self.assertEqual(record.tool_errors, 1)
            self.assertEqual(record.error_kinds, {"exit_code": 1})
            self.assertEqual(record.edited_files, ["src/a.py"])
            # cached is a subset of prompt; summarizer is billed on top.
            self.assertEqual(record.tokens.prompt, 700)
            self.assertEqual(record.tokens.completion, 150)
            self.assertEqual(record.tokens.cached, 400)
            self.assertEqual(record.tokens.summarizer, 350)
            self.assertEqual(record.tokens.total, 700 + 150 + 350)
            self.assertEqual(record.ttft_ms, 80.0)
            self.assertEqual(record.latency_ms.llm, 150.0)
            self.assertEqual(record.latency_ms.tool, 15.0)
            self.assertEqual(record.retries, 1)
            self.assertEqual(record.summary_attempts, 2)
            self.assertEqual(record.summary_invalid, 1)

            stored = metrics.read_run_metrics(path)
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0].run_id, "run-7")
            # The same record is on the trace as run.metrics, and the
            # control-only window still holds the events that explain it.
            kinds = [event.event_type for event in trace.events]
            self.assertEqual(kinds, ["llm.retry", "context.llm_summary.invalid",
                                     "context.llm_summary.yield", "agent.end",
                                     "agent.completed", "run.metrics"])

    def test_a_blocked_command_counts_as_a_policy_block(self) -> None:
        # Nothing emits ``policy.block``; a guard block surfaces as a
        # failed tool call, and VCR's "no block" condition reads it here.
        stats = metrics.RunStats()
        stats.observe("tool.end", 1,
                      {"status": "error", "error_kind": "command_blocked",
                       "exec_ms": 1.0})
        stats.observe("tool.end", 1,
                      {"status": "error", "error_kind": "exit_code"})

        self.assertEqual(stats.policy_blocks, 1)
        self.assertEqual(stats.tool_calls, 2)
        self.assertEqual(stats.tool_errors, 2)

    async def test_the_record_carries_policy_blocks(self) -> None:
        from agent_runtime.trace import RunTrace

        with TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            trace = RunTrace(run_id="run-8")
            trace.emit("tool.end", 1, status="error",
                       error_kind="command_blocked", exec_ms=2.0)

            record = metrics.finalize_run(trace, status="completed",
                                          answer="blocked", path=path)

            self.assertEqual(record.policy_blocks, 1)
            self.assertEqual(
                metrics.read_run_metrics(path)[0].policy_blocks, 1)

    async def test_afinalize_run_persists_metrics_and_stops_the_writer(self) -> None:
        from agent_runtime.trace import RunTrace

        with TemporaryDirectory() as directory:
            trace_path = Path(directory) / "trace.jsonl"
            index_path = Path(directory) / "runs.jsonl"
            trace = RunTrace(run_id="run-9", output_path=trace_path,
                             retain_events="control")
            trace.emit("agent.start", 1, message="hi")
            # The turn's own ``finally`` already flushed; ``run.metrics``
            # emitted afterwards restarts a writer that nobody would stop.
            await trace.flush()
            self.assertIsNone(trace._writer)

            await metrics.afinalize_run(trace, status="completed",
                                        answer="ok", path=index_path)

            self.assertIsNone(trace._writer)
            self.assertIsNone(trace._queue)
            # Only this test's own task is left: no writer parks on the
            # queue holding the trace alive.
            self.assertEqual(len(asyncio.all_tasks()), 1)
            written = [json.loads(line) for line in
                       trace_path.read_text().splitlines()]
            self.assertEqual(written[-1]["event_type"], "run.metrics")
            self.assertGreater(written[-1]["data"]["trace_bytes"], 0)
            self.assertEqual(len(metrics.read_run_metrics(index_path)), 1)

    async def test_afinalize_run_survives_a_broken_trace(self) -> None:
        class FailingTrace:
            run_id = "run-broken"
            output_path = None
            origin = "cli"
            model = ""
            session_id = None
            task_id = None
            run_index = None
            harness = None
            stats = metrics.RunStats()

            def record_metrics(self, record):
                return record

            async def flush(self) -> None:
                raise OSError("disk gone")
        with TemporaryDirectory() as directory:
            with self.assertLogs("agent_runtime.metrics", level="WARNING"):
                record = await metrics.afinalize_run(
                    FailingTrace(), status="error",
                    path=Path(directory) / "runs.jsonl")

        self.assertEqual(record.run_id, "run-broken")
        self.assertEqual(record.status, "error")

    async def test_a_cancel_during_the_first_flush_still_writes_the_record(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            trace = _FlushGate("run-cancel-pre", blocking_call=1)

            task = asyncio.create_task(
                metrics.afinalize_run(trace, status="cancelled", path=path))
            await trace.entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            # The cancellation is propagated, not swallowed ...
            self.assertEqual(trace.calls, 2)
            # ... yet the record still landed exactly once.
            records = metrics.read_run_metrics(path)
            self.assertEqual([record.run_id for record in records],
                             ["run-cancel-pre"])
            self.assertEqual(records[0].status, "cancelled")

    async def test_a_cancel_during_the_second_flush_still_writes_the_record(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            trace = _FlushGate("run-cancel-post", blocking_call=2)

            task = asyncio.create_task(
                metrics.afinalize_run(trace, status="error", path=path))
            await trace.entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            self.assertEqual(trace.calls, 2)
            records = metrics.read_run_metrics(path)
            self.assertEqual([record.run_id for record in records],
                             ["run-cancel-post"])
            self.assertEqual(records[0].status, "error")
