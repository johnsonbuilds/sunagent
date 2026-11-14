import asyncio
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


sys.path.insert(0, str(Path(__file__).parents[2]))
sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from webui.board import BoardStore
from webui.executor import BoardExecutor, TurnOutcome
from webui.sessions import SessionStore


class BoardExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._root = TemporaryDirectory()
        self.addCleanup(self._root.cleanup)
        root = Path(self._root.name)
        self.board = BoardStore(root)
        self.board.load()
        self.sessions = SessionStore(root / "sessions")
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self.executor: BoardExecutor | None = None

    def _ensure_session(self, task_id: str) -> str:
        session_id, _ = self.board.ensure_session(task_id, self.sessions)
        return session_id

    def _task(self, title: str, *, priority: str = "normal") -> dict:
        return self.board.create(
            title,
            description=f"description for {title}",
            status="todo",
            priority=priority,
        )

    async def _default_turn(self, _session, message: str) -> TurnOutcome:
        self.calls.append(message)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
        finally:
            self.active -= 1
        return TurnOutcome("completed")

    def _start(self, run_turn=None) -> BoardExecutor:
        executor = BoardExecutor(
            self.board, self.sessions,
            ensure_session=self._ensure_session,
            run_turn=run_turn or self._default_turn,
            poll_interval=0.01,
        )
        self.executor = executor
        self.addAsyncCleanup(executor.stop)
        executor.start()
        return executor

    async def _wait_until(self, predicate, timeout: float = 2.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not predicate():
            if loop.time() > deadline:
                self.fail("timed out waiting for executor state")
            await asyncio.sleep(0.01)

    async def test_runs_priority_order_one_at_a_time(self) -> None:
        low = self._task("Low", priority="low")
        urgent = self._task("Urgent", priority="urgent")
        normal = self._task("Normal", priority="normal")
        self._start()

        await self._wait_until(
            lambda: all(self.board.get(task["id"]).status == "review"
                        for task in (low, urgent, normal)))

        self.assertEqual(self.calls, [
            "description for Urgent",
            "description for Normal",
            "description for Low",
        ])
        self.assertEqual(self.max_active, 1)
        for task in (low, urgent, normal):
            stored = self.board.get(task["id"])
            self.assertIsNone(stored.last_error)
            self.assertIsNotNone(stored.finished_at)

    async def test_failure_lands_in_review_with_error(self) -> None:
        async def fail(_session, message: str) -> TurnOutcome:
            self.calls.append(message)
            return TurnOutcome("error", error="boom")

        task = self._task("Fail")
        self._start(fail)
        await self._wait_until(
            lambda: self.board.get(task["id"]).status == "review")

        stored = self.board.get(task["id"])
        self.assertEqual(stored.status, "review")
        self.assertEqual(stored.last_error, "boom")
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.calls), 1)

    async def test_manual_doing_is_not_started(self) -> None:
        task = self.board.create(
            "Manual doing",
            description="do not run this automatically",
            status="doing",
        )
        self._start()
        await asyncio.sleep(0.05)

        self.assertEqual(self.calls, [])
        self.assertEqual(self.board.get(task["id"]).status, "doing")
        self.assertIsNone(self.board.get(task["id"]).run_id)

    async def test_pause_blocks_claims_until_resumed(self) -> None:
        task = self._task("Paused")
        executor = self._start()
        executor.set_paused(True)
        await asyncio.sleep(0.05)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.board.get(task["id"]).status, "todo")

        executor.set_paused(False)
        executor.wake()
        await self._wait_until(
            lambda: self.board.get(task["id"]).status == "review")
        self.assertEqual(len(self.calls), 1)

    async def test_user_status_change_wins(self) -> None:
        gate = asyncio.Event()
        started = asyncio.Event()

        async def blocked(_session, message: str) -> TurnOutcome:
            self.calls.append(message)
            started.set()
            await gate.wait()
            return TurnOutcome("completed")

        task = self._task("Override")
        self._start(blocked)
        await asyncio.wait_for(started.wait(), timeout=1.0)
        self.board.move(task["id"], "done")
        gate.set()
        await asyncio.sleep(0.05)

        self.assertEqual(self.board.get(task["id"]).status, "done")

    async def test_shutdown_requeues_the_running_task(self) -> None:
        gate = asyncio.Event()
        started = asyncio.Event()

        async def blocked(_session, message: str) -> TurnOutcome:
            self.calls.append(message)
            started.set()
            await gate.wait()
            return TurnOutcome("completed")

        task = self._task("Shutdown")
        executor = self._start(blocked)
        await asyncio.wait_for(started.wait(), timeout=1.0)

        await executor.stop()
        stored = self.board.get(task["id"])
        self.assertEqual(stored.status, "todo")
        self.assertIsNotNone(stored.queued_at)
        self.assertIsNone(stored.run_id)
        self.assertIn("shutdown", stored.last_error or "")

    async def test_rerun_reuses_the_same_session(self) -> None:
        sessions_seen: list[str] = []

        async def run_turn(session, message: str) -> TurnOutcome:
            sessions_seen.append(session.id)
            self.calls.append(message)
            await asyncio.sleep(0.01)
            return TurnOutcome("completed")

        task = self._task("Reuse")
        executor = self._start(run_turn)
        await self._wait_until(
            lambda: self.board.get(task["id"]).status == "review")
        session_id = self.board.get(task["id"]).session_id
        self.assertIsNotNone(session_id)

        self.board.move(task["id"], "todo")
        executor.wake()
        await self._wait_until(
            lambda: len(sessions_seen) >= 2
            and self.board.get(task["id"]).status == "review")

        self.assertEqual(set(sessions_seen), {session_id})
        self.assertEqual(self.calls,
                         ["description for Reuse", "description for Reuse"])

    async def test_priority_change_while_running_does_not_preempt(self) -> None:
        gate = asyncio.Event()
        started = asyncio.Event()

        async def blocked(_session, message: str) -> TurnOutcome:
            self.calls.append(message)
            started.set()
            await gate.wait()
            return TurnOutcome("completed")

        task = self._task("Running low", priority="low")
        self._start(blocked)
        await asyncio.wait_for(started.wait(), timeout=1.0)

        self.board.update(task["id"], {"priority": "urgent"})
        await asyncio.sleep(0.05)
        self.assertEqual(self.calls, ["description for Running low"])

        gate.set()
        await self._wait_until(
            lambda: self.board.get(task["id"]).status == "review")

    async def test_urgent_task_does_not_preempt_running_task(self) -> None:
        gates = [asyncio.Event(), asyncio.Event()]
        started = [asyncio.Event(), asyncio.Event()]

        async def blocked(_session, message: str) -> TurnOutcome:
            index = len(self.calls)
            self.calls.append(message)
            started[index].set()
            await gates[index].wait()
            return TurnOutcome("completed")

        low = self._task("Low", priority="low")
        self._start(blocked)
        await asyncio.wait_for(started[0].wait(), timeout=1.0)

        urgent = self._task("Urgent", priority="urgent")
        await asyncio.sleep(0.05)
        self.assertEqual(self.calls, ["description for Low"])

        gates[0].set()
        await asyncio.wait_for(started[1].wait(), timeout=1.0)
        self.assertEqual(self.calls,
                         ["description for Low", "description for Urgent"])

        gates[1].set()
        await self._wait_until(
            lambda: all(self.board.get(task["id"]).status == "review"
                        for task in (low, urgent)))

    async def test_pause_does_not_cancel_running_task_or_claim_next(self) -> None:
        gates = [asyncio.Event(), asyncio.Event()]
        started = [asyncio.Event(), asyncio.Event()]

        async def blocked(_session, message: str) -> TurnOutcome:
            index = len(self.calls)
            self.calls.append(message)
            started[index].set()
            await gates[index].wait()
            return TurnOutcome("completed")

        first = self._task("First")
        second = self._task("Second")
        executor = self._start(blocked)
        await asyncio.wait_for(started[0].wait(), timeout=1.0)

        executor.set_paused(True)
        gates[0].set()
        await self._wait_until(
            lambda: self.board.get(first["id"]).status == "review")
        await asyncio.sleep(0.05)

        self.assertEqual(self.board.get(second["id"]).status, "todo")
        self.assertEqual(len(self.calls), 1)

        executor.set_paused(False)
        executor.wake()
        await asyncio.wait_for(started[1].wait(), timeout=1.0)
        gates[1].set()
        await self._wait_until(
            lambda: self.board.get(second["id"]).status == "review")

    async def test_busy_session_delays_the_run(self) -> None:
        task = self._task("Busy")
        session_id = self._ensure_session(task["id"])
        session = self.sessions.get(session_id)
        session.busy = True
        executor = self._start()

        await asyncio.sleep(0.05)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.board.get(task["id"]).status, "todo")

        session.busy = False
        executor.wake()
        await self._wait_until(
            lambda: self.board.get(task["id"]).status == "review")
        self.assertEqual(self.calls, ["description for Busy"])


if __name__ == "__main__":
    unittest.main()
