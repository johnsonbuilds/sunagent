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


if __name__ == "__main__":
    unittest.main()
