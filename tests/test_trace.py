"""RunTrace persistence: asynchronous writes and idempotent flush."""

import asyncio
import json
import sys
import tempfile
import threading
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_runtime.harness import HarnessSpec
from agent_runtime.trace import RunTrace


def read_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


class AsyncTraceWriterTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_reaches_disk_before_flush(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live.jsonl"
            trace = RunTrace(run_id="live", output_path=path)

            trace.emit("agent.start")

            # The writer runs concurrently with the turn: give it a few
            # scheduling turns instead of waiting for flush.
            for _ in range(100):
                if path.exists() and path.read_text().strip():
                    break
                await asyncio.sleep(0.01)

            self.assertTrue(path.exists())
            self.assertEqual([record["event_type"] for record in read_records(path)],
                             ["agent.start"])

            await trace.flush()

    async def test_flush_twice_writes_each_event_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            spec = HarnessSpec(id="header-once")
            trace = RunTrace(run_id="dup", output_path=path, harness=spec)

            trace.emit("agent.start")
            trace.emit("agent.end")
            await trace.flush()
            # Callers such as AgentTurn and the Harbor adapter both flush.
            await trace.flush()

            records = read_records(path)
            self.assertEqual([record["event_type"] for record in records[1:]],
                             ["agent.start", "agent.end"])
            self.assertEqual(sum(1 for record in records
                                 if record.get("record_type") == "harness"),
                             1)

    async def test_emit_after_flush_starts_a_new_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            trace = RunTrace(run_id="late", output_path=path)

            trace.emit("agent.start")
            await trace.flush()
            trace.emit("agent.completed")
            await trace.flush()
            await trace.flush()

            self.assertEqual([record["event_type"] for record in read_records(path)],
                             ["agent.start", "agent.completed"])

    def test_emit_without_loop_then_flush(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sync.jsonl"
            trace = RunTrace(run_id="sync", output_path=path)

            trace.emit("agent.start")
            trace.emit("agent.end")

            asyncio.run(trace.flush())
            asyncio.run(trace.flush())

            self.assertEqual([record["event_type"] for record in read_records(path)],
                             ["agent.start", "agent.end"])

    async def test_second_cancel_during_flush_preserves_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cancel.jsonl"
            trace = RunTrace(run_id="cancel", output_path=path)

            entered = threading.Event()
            release = threading.Event()
            original_write = trace._write_events

            def slow_write(events: list) -> None:
                entered.set()
                release.wait(5)
                original_write(events)

            trace._write_events = slow_write  # type: ignore[method-assign]
            trace.emit("agent.start")

            async def turn() -> None:
                try:
                    await asyncio.Event().wait()  # pretend to be working
                finally:
                    await trace.flush()

            task = asyncio.create_task(turn())
            deadline = asyncio.get_running_loop().time() + 5
            while not entered.is_set():
                self.assertLess(asyncio.get_running_loop().time(), deadline)
                await asyncio.sleep(0.005)

            # Queued behind the in-flight batch while the writer is blocked.
            trace.emit("tool.start")
            writer_task = trace._writer
            self.assertIsNotNone(writer_task)

            with mock.patch.object(trace, "_drain_cancelled",
                                   wraps=trace._drain_cancelled) as drain:
                task.cancel()            # first cancel: enters finally -> flush
                await asyncio.sleep(0)   # let flush await queue.join()
                task.cancel()            # second cancel: interrupts flush awaits
                release.set()            # unblock the in-flight write

                with self.assertRaises(asyncio.CancelledError):
                    await task
                drain.assert_called_once()

            # The writer must exit instead of leaking on queue.get().
            self.assertIsNone(trace._writer)
            assert writer_task is not None
            await asyncio.wait_for(writer_task, timeout=5)

            # Every event is persisted exactly once. Ordering between the
            # in-flight batch and the synchronous tail is best effort.
            event_types = Counter(record["event_type"]
                                  for record in read_records(path))
            self.assertEqual(event_types, Counter({
                "agent.start": 1,
                "tool.start": 1,
            }))




class RunRecordPolicyTests(unittest.IsolatedAsyncioTestCase):
    """Phase 1: retention policy, run.config/run.metrics, drop accounting."""

    def _trace(self, path, **kwargs):
        spec = HarnessSpec()
        return RunTrace(run_id="run-1", output_path=path, harness=spec, **kwargs)

    async def test_control_retention_keeps_only_control_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            trace = self._trace(path, retain_events="control")
            trace.emit("agent.start")
            trace.emit("llm.start", 1, messages=[{"role": "user", "content": "hi"}])
            trace.emit("tool.end", 1, status="success")
            trace.emit("context.llm_summary.text", 1, summary="s")
            await trace.flush()

            self.assertEqual([event.event_type for event in trace.events],
                             ["agent.start"])
            # The file still holds everything (authoritative replay log).
            written = [json.loads(line).get("event_type")
                       for line in path.read_text().splitlines()]
            self.assertIn("llm.start", written)
            self.assertIn("tool.end", written)
            self.assertEqual(trace.event_count, 4)

    async def test_none_retention_keeps_nothing(self) -> None:
        trace = self._trace(None, retain_events="none")
        trace.emit("agent.start")
        self.assertEqual(trace.events, [])
        self.assertEqual(trace.event_count, 1)

    async def test_run_config_and_metrics_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            trace = self._trace(path, retain_events="control")
            config = trace.record_config(model_params={"model": "m"},
                                         tools=[{"name": "read_file"}])
            self.assertEqual(config.run_id, "run-1")
            self.assertEqual(config.harness_id, trace.harness.id)
            self.assertNotIn("api_key", json.dumps(config.to_dict()))

            from agent_runtime.metrics import finalize_run
            from agent_runtime.run_records import RunMetrics
            record = finalize_run(trace, status="completed", answer="done")
            self.assertEqual(record.run_id, "run-1")
            await trace.flush()

            kinds = [event.event_type for event in trace.events]
            self.assertEqual(kinds, ["run.config", "run.metrics"])
            written = [json.loads(line).get("event_type")
                       for line in path.read_text().splitlines()]
            self.assertIn("run.metrics", written)
            self.assertIsInstance(record, RunMetrics)

    async def test_run_config_records_the_resolved_reasoning_echo(self) -> None:
        # Post-mortems of a cross-tool "amnesia" loop need the resolved bit,
        # not only the knob: see docs/session-thinking-loop-analysis-1126.md.
        from agent_runtime.runtime_config import RuntimeConfig

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            trace = self._trace(path, retain_events="control")
            runtime = RuntimeConfig(model="deepseek/deepseek-v4.1-flash",
                                    base_url="https://openrouter.ai/api/v1")
            trace.record_config(model_params={"model": runtime.model},
                                tools=[], runtime=runtime)
            await trace.flush()

            config = next(event.data for event in trace.events
                          if event.event_type == "run.config")
            self.assertEqual(config["runtime"]["reasoning_echo_mode"], "auto")
            self.assertIs(config["runtime"]["reasoning_echo"], True)

    async def test_streamed_chunks_are_dropped_and_counted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            trace = RunTrace(run_id="run-1",
                             output_path=Path(directory) / "run.jsonl",
                             queue_size=1)
            trace._ensure_writer(asyncio.get_running_loop())
            for _ in range(5):
                trace.emit("llm.chunk", 1, content="x")
            self.assertGreater(trace.dropped, 0)
            self.assertEqual(trace.stats.dropped_events, trace.dropped)
            # Control events are never dropped, even with a full queue.
            trace.emit("agent.error", 1, error="boom")
            self.assertIsNotNone(trace.agent_error())
            await trace.flush()
            written = [json.loads(line).get("event_type")
                       for line in (Path(directory) / "run.jsonl")
                       .read_text().splitlines()]
            self.assertIn("agent.error", written)


class AgentErrorTests(unittest.IsolatedAsyncioTestCase):
    """``agent_error`` must not depend on what the trace retained."""

    def test_none_retention_still_reports_the_failure(self) -> None:
        trace = RunTrace(run_id="run-1", retain_events="none")
        trace.emit("agent.error", 1, error="boom", stage="llm")

        self.assertEqual(trace.events, [])
        error = trace.agent_error()
        self.assertIsNotNone(error)
        self.assertEqual(error.data["stage"], "llm")

    def test_a_successful_run_has_no_error(self) -> None:
        trace = RunTrace(retain_events="none")
        trace.emit("agent.end", 1, status="success")

        self.assertIsNone(trace.agent_error())


class HarnessHeaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_header_carries_the_join_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            trace = RunTrace(run_id="run-1", output_path=path,
                             harness=HarnessSpec(), origin="webui",
                             session_id="sess-1", task_id="task-1",
                             run_index=3, model="m")
            trace.emit("agent.start")
            await trace.flush()

            header = read_records(path)[0]

        self.assertEqual(header["record_type"], "harness")
        self.assertEqual(header["origin"], "webui")
        self.assertEqual(header["session_id"], "sess-1")
        self.assertEqual(header["task_id"], "task-1")
        self.assertEqual(header["run_index"], 3)
        self.assertEqual(header["model"], "m")


class GitStateCacheTests(unittest.IsolatedAsyncioTestCase):
    """A non-repository must not be re-probed for every run."""

    def test_an_empty_result_is_cached_too(self) -> None:
        from agent_runtime import trace as trace_module

        with mock.patch.object(trace_module, "_git_cache", (0.0, {})), \
                mock.patch.object(trace_module.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=1, stdout="")
            first = trace_module.git_state()
            second = trace_module.git_state()

        self.assertEqual(first, {})
        self.assertEqual(second, {})
        self.assertEqual(run.call_count, 3)


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
