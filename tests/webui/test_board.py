import asyncio
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parents[2]))
sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from starlette.testclient import TestClient

from webui import server
from webui.board import (
    BoardNotFoundError,
    BoardStorageError,
    BoardStore,
    BoardValidationError,
)
from webui.sessions import SessionStore


class BoardStoreTests(unittest.TestCase):
    def _board(self, root: str) -> BoardStore:
        store = BoardStore(Path(root))
        store.load()
        return store

    def _sessions(self, root: str) -> SessionStore:
        return SessionStore(Path(root) / "sessions")

    def _create(self, board: BoardStore, title: str, **kwargs):
        kwargs.setdefault("description", f"{title} description")
        return BoardStore.create(board, title, **kwargs)

    def test_create_round_trips_through_disk(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            created = self._create(
                board, "  Write docs  ",
                description="Cover the board API",
                status="todo",
                priority="high",
                tags=["docs", "docs", " ui "],
            )

            self.assertEqual(created["title"], "Write docs")
            self.assertEqual(created["status"], "todo")
            self.assertEqual(created["priority"], "high")
            self.assertEqual(created["tags"], ["docs", "ui"])
            self.assertEqual(created["position"], 0)

            reloaded = self._board(root)
            snapshot = reloaded.snapshot(self._sessions(root))
            self.assertEqual(len(snapshot["tasks"]), 1)
            self.assertEqual(snapshot["tasks"][0]["id"], created["id"])
            self.assertEqual(snapshot["tasks"][0]["title"], "Write docs")
            self.assertIsNone(snapshot["tasks"][0]["session"])

    def test_update_fields_keeps_position(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            first = self._create(board, "First")
            self._create(board, "Second")

            updated = board.update(first["id"], {
                "title": "Renamed",
                "description": "More detail",
                "priority": "urgent",
                "tags": ["ops"],
            })

            self.assertEqual(updated["title"], "Renamed")
            self.assertEqual(updated["description"], "More detail")
            self.assertEqual(updated["priority"], "urgent")
            self.assertEqual(updated["tags"], ["ops"])
            self.assertEqual(updated["position"], 0)

    def test_edit_without_status_does_not_reorder(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            first = self._create(board, "First")
            self._create(board, "Second")

            board.update(first["id"], {"title": "Renamed"})

            titles = [task["title"] for task in
                      board.snapshot(self._sessions(root))["tasks"]]
            self.assertEqual(titles, ["Renamed", "Second"])

    def test_move_within_column(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            first = self._create(board, "First")
            second = self._create(board, "Second")
            third = self._create(board, "Third")

            board.move(third["id"], "backlog", 0)
            snapshot = board.snapshot(self._sessions(root))
            self.assertEqual([task["id"] for task in snapshot["tasks"]],
                             [third["id"], first["id"], second["id"]])
            self.assertEqual([task["position"] for task in snapshot["tasks"]],
                             [0, 1, 2])

            board.move(third["id"], "backlog")
            snapshot = board.snapshot(self._sessions(root))
            self.assertEqual([task["id"] for task in snapshot["tasks"]],
                             [first["id"], second["id"], third["id"]])

    def test_move_across_columns(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            first = self._create(board, "First")
            second = self._create(board, "Second")
            third = self._create(board, "Third", status="todo")

            board.move(first["id"], "todo", 0)

            snapshot = board.snapshot(self._sessions(root))
            by_status = {}
            for task in snapshot["tasks"]:
                by_status.setdefault(task["status"], []).append(task["id"])
            self.assertEqual(by_status["backlog"], [second["id"]])
            self.assertEqual(by_status["todo"], [first["id"], third["id"]])
            self.assertEqual([task["position"] for task in snapshot["tasks"]
                              if task["status"] == "todo"], [0, 1])

    def test_move_without_position_appends(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            first = self._create(board, "First")
            self._create(board, "Second")
            target = self._create(board, "Target", status="todo")

            board.move(first["id"], "todo")

            todo = [task["id"] for task in
                    board.snapshot(self._sessions(root))["tasks"]
                    if task["status"] == "todo"]
            self.assertEqual(todo, [target["id"], first["id"]])

    def test_delete(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            task = self._create(board, "Remove me")

            self.assertTrue(board.delete(task["id"]))
            self.assertFalse(board.delete(task["id"]))
            self.assertEqual(
                board.snapshot(self._sessions(root))["tasks"], [])

    def test_validation_rejects_bad_input(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            with self.assertRaises(BoardValidationError):
                self._create(board, "   ")
            with self.assertRaises(BoardValidationError):
                self._create(board, "x" * 201)
            with self.assertRaises(BoardValidationError):
                self._create(board, "Task", status="unknown")
            with self.assertRaises(BoardValidationError):
                self._create(board, "Task", priority="unknown")
            with self.assertRaises(BoardValidationError):
                self._create(board, "Task", tags=["x" * 33])

            task = self._create(board, "Task")
            with self.assertRaises(BoardValidationError):
                board.update(task["id"], {"status": "unknown"})
            with self.assertRaises(BoardNotFoundError):
                board.update("task-missing", {"title": "Nope"})

    def test_corrupt_file_is_backed_up(self) -> None:
        with TemporaryDirectory() as root:
            path = Path(root) / "board.json"
            path.write_text("{not-json", encoding="utf-8")
            board = self._board(root)

            self.assertEqual(
                board.snapshot(self._sessions(root))["tasks"], [])
            backups = list(Path(root).glob("board.json.corrupt-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"),
                             "{not-json")

    def test_write_failure_keeps_memory_unchanged(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            task = self._create(board, "Keep me")

            with patch.object(board, "_write_atomic",
                              side_effect=BoardStorageError("disk full")):
                with self.assertRaises(BoardStorageError):
                    board.update(task["id"], {"title": "Changed"})

            stored = board.get(task["id"])
            self.assertIsNotNone(stored)
            self.assertEqual(stored.title, "Keep me")

    def test_ensure_session_creates_once_then_reuses(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            sessions = self._sessions(root)
            task = self._create(board, "Talk to the agent")

            session_id, created = board.ensure_session(
                task["id"], sessions, model="main", harness="common-v0")
            self.assertTrue(created)
            session = sessions.get(session_id)
            self.assertIsNotNone(session)
            self.assertEqual(session.title, "Talk to the agent")
            self.assertEqual(session.model, "main")
            self.assertEqual(session.harness, "common-v0")

            again, created_again = board.ensure_session(
                task["id"], sessions, model="main", harness="common-v0")
            self.assertEqual(again, session_id)
            self.assertFalse(created_again)
            self.assertEqual(board.get(task["id"]).session_id, session_id)

    def test_task_model_and_harness_round_trip(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            created = self._create(
                board, "Configured",
                model="deepseek-v4.1-flash-1017",
                harness="coding-v1",
            )
            self.assertEqual(created["model"], "deepseek-v4.1-flash-1017")
            self.assertEqual(created["harness"], "coding-v1")

            reloaded = self._board(root)
            stored = reloaded.get(created["id"])
            self.assertEqual(stored.model, "deepseek-v4.1-flash-1017")
            self.assertEqual(stored.harness, "coding-v1")

            cleared = reloaded.update(created["id"],
                                      {"model": "", "harness": None})
            self.assertIsNone(cleared["model"])
            self.assertIsNone(cleared["harness"])

    def test_ensure_session_prefers_task_choices_over_defaults(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            sessions = self._sessions(root)
            task = self._create(board, "Configured", model="task-model",
                                harness="task-harness")

            session_id, _ = board.ensure_session(
                task["id"], sessions, model="default-model",
                harness="default-harness")

            session = sessions.get(session_id)
            self.assertEqual(session.model, "task-model")
            self.assertEqual(session.harness, "task-harness")

    def test_comment_requeues_and_records_run_history(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            task = self._create(board, "Feedback", status="review")

            updated, deferred = board.add_comment(
                task["id"], "  Please add a timeout test  ")
            self.assertFalse(deferred)
            self.assertEqual(updated["status"], "todo")
            self.assertIsNotNone(updated["queued_at"])
            self.assertEqual(len(updated["comments"]), 1)
            comment = updated["comments"][0]
            self.assertEqual(comment["body"], "Please add a timeout test")
            self.assertIsNone(comment["run_id"])

            # The first run sends the description and leaves the pre-run
            # comment pending, so finishing it queues a follow-up.
            first = board.claim_next_todo()
            self.assertIsNone(board.get(task["id"]).comments[0]["run_id"])
            self.assertTrue(board.finish_run(first.id, first.run_id,
                                             response="first answer"))
            waited = board.get(task["id"])
            self.assertEqual(waited.status, "todo")
            self.assertEqual(len(waited.runs), 1)
            self.assertEqual(waited.runs[0]["response"], "first answer")

            # The follow-up consumes the comment and records its answer.
            second = board.claim_next_todo()
            self.assertEqual(board.get(task["id"]).comments[0]["run_id"],
                             second.run_id)
            self.assertTrue(board.finish_run(second.id, second.run_id,
                                             response="second answer"))
            stored = board.get(task["id"])
            self.assertEqual(stored.status, "review")
            self.assertEqual([run["response"] for run in stored.runs],
                             ["first answer", "second answer"])
            self.assertEqual([run["status"] for run in stored.runs],
                             ["completed", "completed"])

            reloaded = self._board(root)
            persisted = reloaded.get(task["id"])
            self.assertEqual(len(persisted.comments), 1)
            self.assertEqual(len(persisted.runs), 2)

    def test_comment_while_doing_waits_for_the_current_run(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            task = self._create(board, "Running", status="todo")
            claimed = board.claim_next_todo()

            updated, deferred = board.add_comment(task["id"], "More feedback")
            self.assertTrue(deferred)
            self.assertEqual(updated["status"], "doing")

            self.assertTrue(board.finish_run(claimed.id, claimed.run_id,
                                             response="answer"))
            stored = board.get(task["id"])
            # The first run leaves the new comment pending and requeues.
            self.assertEqual(stored.status, "todo")
            self.assertEqual(stored.comments[0]["run_id"], None)

            follow_up = board.claim_next_todo()
            self.assertEqual(board.get(task["id"]).comments[0]["run_id"],
                             follow_up.run_id)
            self.assertTrue(board.finish_run(follow_up.id, follow_up.run_id,
                                             response="follow-up"))
            self.assertEqual(board.get(task["id"]).status, "review")

    def test_requeue_and_human_move_release_consumed_comments(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            task = self._create(board, "Release", status="todo")
            first = board.claim_next_todo()
            board.finish_run(first.id, first.run_id, response="first")
            board.add_comment(task["id"], "Next")

            claimed = board.claim_next_todo()
            self.assertEqual(board.get(task["id"]).comments[0]["run_id"],
                             claimed.run_id)
            self.assertTrue(board.requeue_run(claimed.id, claimed.run_id))
            self.assertIsNone(board.get(task["id"]).comments[0]["run_id"])

            claimed = board.claim_next_todo()
            self.assertEqual(board.get(task["id"]).comments[0]["run_id"],
                             claimed.run_id)
            board.move(claimed.id, "review")
            self.assertIsNone(board.get(task["id"]).comments[0]["run_id"])

    def test_comment_validation(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            task = self._create(board, "Validate")
            with self.assertRaises(BoardValidationError):
                board.add_comment(task["id"], "   ")
            with self.assertRaises(BoardValidationError):
                board.add_comment(task["id"], "x" * 10_001)
            with self.assertRaises(BoardNotFoundError):
                board.add_comment("task-missing", "hello")

    def test_snapshot_hides_activity_and_detail_returns_it(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            sessions = self._sessions(root)
            task = self._create(board, "Activity", status="todo")
            claimed = board.claim_next_todo()
            board.finish_run(claimed.id, claimed.run_id, response="answer")
            board.add_comment(task["id"], "Another look")

            snapshot = board.snapshot(sessions)
            view = snapshot["tasks"][0]
            self.assertEqual(view["comment_count"], 1)
            self.assertEqual(view["run_count"], 1)
            self.assertNotIn("comments", view)
            self.assertNotIn("runs", view)

            detail = board.detail(task["id"], sessions)
            self.assertEqual(detail["comments"][0]["body"], "Another look")
            self.assertEqual(detail["runs"][0]["response"], "answer")
            self.assertIn("session", detail)

    def test_bad_activity_records_are_skipped_on_load(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            task = self._create(board, "Bad records")
            path = Path(root) / "board.json"
            record = json.loads(path.read_text(encoding="utf-8"))
            record["tasks"][0]["comments"] = [
                "not a dict",
                {"body": ""},
                {"id": "comment-1", "body": "kept",
                 "created_at": 1.0, "run_id": None},
            ]
            record["tasks"][0]["runs"] = [
                {"run_id": "", "response": "missing id"},
                {"run_id": "run-1", "status": "weird", "response": "kept",
                 "finished_at": 2.0},
            ]
            path.write_text(json.dumps(record), encoding="utf-8")

            reloaded = self._board(root)
            stored = reloaded.get(task["id"])
            self.assertEqual([comment["body"] for comment in stored.comments],
                             ["kept"])
            self.assertEqual(len(stored.runs), 1)
            self.assertEqual(stored.runs[0]["status"], "error")
            self.assertEqual(stored.runs[0]["response"], "kept")

    def test_unlink_session(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            sessions = self._sessions(root)
            task = self._create(board, "Linked")
            session_id, _ = board.ensure_session(task["id"], sessions)

            board.unlink_session(session_id)

            self.assertIsNone(board.get(task["id"]).session_id)
            reloaded = self._board(root)
            self.assertIsNone(reloaded.get(task["id"]).session_id)

    def test_reconcile_drops_missing_sessions(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            sessions = self._sessions(root)
            task = self._create(board, "Linked")
            board.ensure_session(task["id"], sessions)

            reloaded = self._board(root)
            reloaded.reconcile(self._sessions(root))

            self.assertIsNone(reloaded.get(task["id"]).session_id)
            persisted = self._board(root)
            self.assertIsNone(persisted.get(task["id"]).session_id)

    def test_session_view_is_derived(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            sessions = self._sessions(root)
            task = self._create(board, "Linked")
            session_id, _ = board.ensure_session(task["id"], sessions)
            sessions.get(session_id).busy = True

            snapshot = board.snapshot(sessions)
            self.assertEqual(snapshot["tasks"][0]["session"]["id"],
                             session_id)
            self.assertTrue(snapshot["tasks"][0]["session"]["busy"])

    def test_snapshot_sorts_columns_and_positions(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            done = self._create(board, "Done", status="done")
            todo = self._create(board, "Todo", status="todo")
            backlog = self._create(board, "Backlog", status="backlog")

            snapshot = board.snapshot(self._sessions(root))
            self.assertEqual(
                [task["id"] for task in snapshot["tasks"]],
                [backlog["id"], todo["id"], done["id"]])

    def test_claim_orders_by_priority_then_position(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            low = self._create(board, "Low", status="todo", priority="low")
            urgent = self._create(board, "Urgent", status="todo",
                                  priority="urgent")
            normal = self._create(board, "Normal", status="todo",
                                  priority="normal")

            self.assertEqual(board.next_todo().id, urgent["id"])
            first = board.claim_next_todo()
            self.assertEqual(first.id, urgent["id"])
            self.assertEqual(first.status, "doing")
            self.assertIsNotNone(first.run_id)
            self.assertIsNotNone(first.started_at)
            self.assertIsNone(first.queued_at)

            second = board.claim_next_todo()
            self.assertEqual(second.id, normal["id"])
            third = board.claim_next_todo()
            self.assertEqual(third.id, low["id"])
            self.assertIsNone(board.claim_next_todo())

    def test_priority_change_while_queued_changes_claim_order(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            low = self._create(board, "Low", status="todo", priority="low")
            normal = self._create(board, "Normal", status="todo",
                                  priority="normal")
            self.assertEqual(board.next_todo().id, normal["id"])

            board.update(low["id"], {"priority": "urgent"})
            self.assertEqual(board.next_todo().id, low["id"])
            claimed = board.claim_next_todo()
            self.assertEqual(claimed.id, low["id"])

    def test_claim_uses_position_for_equal_priority(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            first = self._create(board, "First", status="todo")
            second = self._create(board, "Second", status="todo")
            third = self._create(board, "Third", status="todo")

            # Reorder so the column position disagrees with creation order;
            # otherwise "position" and "age" would be indistinguishable.
            board.move(third["id"], "todo", position=0)
            self.assertEqual(board.get(third["id"]).position, 0)
            self.assertEqual(board.get(first["id"]).position, 1)
            self.assertEqual(board.get(second["id"]).position, 2)

            claimed = board.claim_next_todo()
            self.assertEqual(claimed.id, third["id"])
            claimed = board.claim_next_todo()
            self.assertEqual(claimed.id, first["id"])
            claimed = board.claim_next_todo()
            self.assertEqual(claimed.id, second["id"])

    def test_finish_and_requeue_require_run_ownership(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            task = self._create(board, "Run me", status="todo")
            claimed = board.claim_next_todo()
            self.assertEqual(claimed.id, task["id"])

            self.assertTrue(board.finish_run(claimed.id, claimed.run_id,
                                             status="review"))
            stored = board.get(claimed.id)
            self.assertEqual(stored.status, "review")
            self.assertIsNotNone(stored.finished_at)
            self.assertIsNone(stored.last_error)
            self.assertFalse(board.finish_run(claimed.id, claimed.run_id,
                                              status="done"))

    def test_user_status_change_wins_over_finish(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            task = self._create(board, "Run me", status="todo")
            claimed = board.claim_next_todo()
            board.move(claimed.id, "done")

            self.assertFalse(board.finish_run(claimed.id, claimed.run_id,
                                              status="review"))
            self.assertEqual(board.get(claimed.id).status, "done")

    def test_requeue_interrupted_run(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            task = self._create(board, "Run me", status="todo")
            claimed = board.claim_next_todo()

            self.assertTrue(board.requeue_run(claimed.id, claimed.run_id,
                                              error="boom"))
            stored = board.get(claimed.id)
            self.assertEqual(stored.status, "todo")
            self.assertIsNotNone(stored.queued_at)
            self.assertIsNone(stored.run_id)
            self.assertEqual(stored.last_error, "boom")

    def test_recover_interrupted_returns_doing_to_todo(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            task = self._create(board, "Run me", status="todo")
            claimed = board.claim_next_todo()

            reloaded = self._board(root)
            self.assertEqual(reloaded.recover_interrupted(), 1)
            stored = reloaded.get(task["id"])
            self.assertEqual(stored.status, "todo")
            self.assertIsNotNone(stored.queued_at)
            self.assertIsNone(stored.run_id)
            self.assertIn("restart", stored.last_error or "")
            self.assertEqual(claimed.id, task["id"])

    def test_manual_doing_is_not_recovered(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            task = self._create(board, "Manual", status="doing")
            self.assertIsNone(board.next_todo())

            reloaded = self._board(root)
            self.assertEqual(reloaded.recover_interrupted(), 0)
            stored = reloaded.get(task["id"])
            self.assertEqual(stored.status, "doing")
            self.assertIsNone(stored.run_id)

    def test_old_todo_without_queued_at_is_not_claimed(self) -> None:
        with TemporaryDirectory() as root:
            path = Path(root) / "board.json"
            path.write_text(json.dumps({
                "version": 1,
                "updated_at": 1.0,
                "tasks": [{
                    "id": "task-old", "title": "Old", "description": "old",
                    "status": "todo", "priority": "normal",
                }],
            }), encoding="utf-8")
            board = self._board(root)

            self.assertIsNone(board.next_todo())
            self.assertIsNone(board.claim_next_todo())

    def test_description_is_required_and_queue_markers_follow_status(self) -> None:
        with TemporaryDirectory() as root:
            board = self._board(root)
            with self.assertRaises(BoardValidationError):
                BoardStore.create(board, "No detail", description="  ")

            task = self._create(board, "Backlog", status="backlog")
            self.assertIsNone(task["queued_at"])
            queued = board.update(task["id"], {"status": "todo"})
            self.assertIsNotNone(queued["queued_at"])
            cleared = board.update(task["id"], {"status": "backlog"})
            self.assertIsNone(cleared["queued_at"])

            with self.assertRaises(BoardValidationError):
                board.update(task["id"], {"description": "  "})

    def test_loaded_record_with_invalid_status_is_skipped(self) -> None:
        with TemporaryDirectory() as root:
            path = Path(root) / "board.json"
            path.write_text(json.dumps({
                "version": 1,
                "updated_at": 1.0,
                "tasks": [
                    {"id": "task-1", "title": "Good", "status": "todo"},
                    {"id": "task-2", "title": "Bad", "status": "nope"},
                    {"id": "task-3", "title": "", "status": "todo"},
                ],
            }), encoding="utf-8")

            board = self._board(root)
            tasks = board.snapshot(self._sessions(root))["tasks"]
            self.assertEqual([task["id"] for task in tasks], ["task-1"])


class BoardApiTests(unittest.TestCase):
    """Exercise the HTTP wiring, not just the store."""

    def setUp(self) -> None:
        self._root = TemporaryDirectory()
        self.addCleanup(self._root.cleanup)
        root = Path(self._root.name)
        self.sessions = SessionStore(root / "sessions")
        self.board = BoardStore(root)
        # The repository's .env may enable auth; these tests are about
        # the board routes, so run the app unauthenticated.
        with patch.object(server, "credentials_from_env", return_value=None):
            app = server.create_app(self.sessions, self.board,
                                    enable_executor=False)
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def _tasks(self) -> list[dict]:
        return self.client.get("/api/board").json()["tasks"]

    def test_create_edit_move_and_delete(self) -> None:
        response = self.client.post("/api/board/tasks", json={
            "title": "Ship the board",
            "description": "Five columns",
            "status": "todo",
            "priority": "high",
            "tags": ["webui"],
        })
        self.assertEqual(response.status_code, 201)
        task = response.json()["tasks"][0]
        self.assertEqual(task["title"], "Ship the board")
        self.assertEqual(task["status"], "todo")

        response = self.client.patch(
            f"/api/board/tasks/{task['id']}",
            json={"title": "Ship it", "priority": "urgent"},
        )
        self.assertEqual(response.status_code, 200)
        updated = response.json()["tasks"][0]
        self.assertEqual(updated["title"], "Ship it")
        self.assertEqual(updated["priority"], "urgent")
        self.assertEqual(updated["position"], 0)

        response = self.client.patch(
            f"/api/board/tasks/{task['id']}",
            json={"status": "doing"},
        )
        self.assertEqual(response.status_code, 200)
        moved = response.json()["tasks"][0]
        self.assertEqual(moved["status"], "doing")

        response = self.client.delete(f"/api/board/tasks/{task['id']}")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["deleted_session"])
        self.assertEqual(response.json()["board"]["tasks"], [])

    def test_validation_and_missing_task(self) -> None:
        response = self.client.post("/api/board/tasks", json={
            "title": " ", "description": "desc",
        })
        self.assertEqual(response.status_code, 400)

        response = self.client.post("/api/board/tasks", json={
            "title": "Task", "description": "desc", "status": "nope",
        })
        self.assertEqual(response.status_code, 400)

        response = self.client.post("/api/board/tasks", json={
            "title": "Task", "description": "   ",
        })
        self.assertEqual(response.status_code, 400)

        response = self.client.post("/api/board/tasks", json={
            "title": "Task",
        })
        self.assertEqual(response.status_code, 422)

        response = self.client.patch(
            "/api/board/tasks/task-missing", json={"title": "Nope"})
        self.assertEqual(response.status_code, 404)

        response = self.client.delete("/api/board/tasks/task-missing")
        self.assertEqual(response.status_code, 404)

    def test_executor_pause_state_is_in_the_snapshot(self) -> None:
        response = self.client.get("/api/board")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["executor"]["paused"])

        response = self.client.patch("/api/board/executor",
                                     json={"paused": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["executor"]["paused"])
        self.assertTrue(self.client.get("/api/board").json()["executor"]["paused"])

    def test_task_conversation_is_created_lazily_and_can_be_deleted(self) -> None:
        created = self.client.post(
            "/api/board/tasks",
            json={"title": "Talk to the agent",
                  "description": "Talk to the agent description"},
        ).json()
        task = created["tasks"][0]

        response = self.client.post(
            f"/api/board/tasks/{task['id']}/session", json={})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["created"])
        session_id = payload["session_id"]
        self.assertIsNotNone(self.sessions.get(session_id))
        linked = payload["board"]["tasks"][0]
        self.assertEqual(linked["session_id"], session_id)
        self.assertEqual(linked["session"]["id"], session_id)

        again = self.client.post(
            f"/api/board/tasks/{task['id']}/session", json={}).json()
        self.assertEqual(again["session_id"], session_id)
        self.assertFalse(again["created"])

        response = self.client.delete(
            f"/api/board/tasks/{task['id']}?delete_session=true")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["deleted_session"])
        self.assertIsNone(self.sessions.get(session_id))

    def test_task_model_and_harness_are_validated_and_stored(self) -> None:
        response = self.client.post("/api/board/tasks", json={
            "title": "Configured",
            "description": "Configured description",
            "model": "main",
            "harness": "coding-v1",
        })
        self.assertEqual(response.status_code, 201)
        task = response.json()["tasks"][0]
        self.assertEqual(task["model"], "main")
        self.assertEqual(task["harness"], "coding-v1")

        response = self.client.post("/api/board/tasks", json={
            "title": "Unknown model",
            "description": "Unknown model description",
            "model": "no-such-model",
        })
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/board/tasks", json={
            "title": "Unknown harness",
            "description": "Unknown harness description",
            "harness": "no-such-harness",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self._tasks()), 1)

        response = self.client.patch(
            f"/api/board/tasks/{task['id']}",
            json={"model": "no-such-model"})
        self.assertEqual(response.status_code, 400)
        response = self.client.patch(
            f"/api/board/tasks/{task['id']}",
            json={"harness": "no-such-harness"})
        self.assertEqual(response.status_code, 400)

        # An empty string clears the choice back to the service default.
        response = self.client.patch(
            f"/api/board/tasks/{task['id']}",
            json={"model": "", "harness": ""})
        self.assertEqual(response.status_code, 200)
        cleared = response.json()["tasks"][0]
        self.assertIsNone(cleared["model"])
        self.assertIsNone(cleared["harness"])

    def test_task_conversation_uses_the_task_choices(self) -> None:
        task = self.client.post("/api/board/tasks", json={
            "title": "Configured",
            "description": "Configured description",
            "model": "main",
            "harness": "coding-v1",
        }).json()["tasks"][0]

        payload = self.client.post(
            f"/api/board/tasks/{task['id']}/session", json={}).json()
        self.assertTrue(payload["created"])
        session = self.sessions.get(payload["session_id"])
        self.assertEqual(session.model, "main")
        self.assertEqual(session.harness, "coding-v1")

        linked = payload["board"]["tasks"][0]
        self.assertEqual(linked["session"]["model"], "main")
        self.assertEqual(linked["session"]["harness"], "coding-v1")
        self.assertFalse(linked["session"]["harness_locked"])

    def test_patch_syncs_a_linked_conversation(self) -> None:
        task = self.client.post("/api/board/tasks", json={
            "title": "Configured",
            "description": "Configured description",
            "model": "main",
            "harness": "coding-v1",
        }).json()["tasks"][0]
        session_id = self.client.post(
            f"/api/board/tasks/{task['id']}/session", json={},
        ).json()["session_id"]

        response = self.client.patch(
            f"/api/board/tasks/{task['id']}",
            json={"model": "glm-5.3-flash", "harness": "general-v1"})
        self.assertEqual(response.status_code, 200)
        session = self.sessions.get(session_id)
        self.assertEqual(session.model, "glm-5.3-flash")
        self.assertEqual(session.harness, "general-v1")

        # Clearing the card also clears the linked conversation.
        response = self.client.patch(
            f"/api/board/tasks/{task['id']}",
            json={"model": "", "harness": ""})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(session.model)
        self.assertIsNone(session.harness)

    def test_locked_harness_rejects_sync_but_model_still_changes(self) -> None:
        task = self.client.post("/api/board/tasks", json={
            "title": "Active",
            "description": "Active description",
            "model": "main",
            "harness": "coding-v1",
        }).json()["tasks"][0]
        session_id = self.client.post(
            f"/api/board/tasks/{task['id']}/session", json={},
        ).json()["session_id"]
        # A transcript freezes the harness but not the model.
        session = self.sessions.get(session_id)
        session.append_segment({"id": "seg-1", "type": "user",
                                "content": "hello"})
        self.sessions.save(session)

        response = self.client.patch(
            f"/api/board/tasks/{task['id']}",
            json={"harness": "general-v1"})
        self.assertEqual(response.status_code, 409)
        stored = self._tasks()[0]
        self.assertEqual(stored["harness"], "coding-v1")
        self.assertEqual(self.sessions.get(session_id).harness, "coding-v1")

        # Clearing is a harness change too, so it is rejected as well.
        response = self.client.patch(
            f"/api/board/tasks/{task['id']}",
            json={"harness": ""})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self._tasks()[0]["harness"], "coding-v1")
        self.assertEqual(self.sessions.get(session_id).harness, "coding-v1")

        response = self.client.patch(
            f"/api/board/tasks/{task['id']}",
            json={"model": "glm-5.3-flash"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._tasks()[0]["model"], "glm-5.3-flash")
        self.assertEqual(self.sessions.get(session_id).model, "glm-5.3-flash")

    def test_comment_endpoint_requeues_and_returns_history(self) -> None:
        task = self.client.post("/api/board/tasks", json={
            "title": "Feedback",
            "description": "Feedback description",
            "status": "review",
        }).json()["tasks"][0]

        response = self.client.post(
            f"/api/board/tasks/{task['id']}/comments",
            json={"body": "Please add a timeout test"})
        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertFalse(payload["deferred"])
        self.assertEqual(payload["board"]["tasks"][0]["status"], "todo")
        self.assertEqual(
            payload["detail"]["comments"][0]["body"],
            "Please add a timeout test")
        self.assertEqual(payload["board"]["tasks"][0]["comment_count"], 1)
        self.assertNotIn("comments", payload["board"]["tasks"][0])

        detail = self.client.get(
            f"/api/board/tasks/{task['id']}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["comments"][0]["body"],
                         "Please add a timeout test")

        response = self.client.post(
            f"/api/board/tasks/{task['id']}/comments", json={"body": " "})
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/api/board/tasks/task-missing/comments", json={"body": "hi"})
        self.assertEqual(response.status_code, 404)
        response = self.client.get("/api/board/tasks/task-missing")
        self.assertEqual(response.status_code, 404)

    def test_comment_on_a_running_task_is_deferred(self) -> None:
        task = self.client.post("/api/board/tasks", json={
            "title": "Running",
            "description": "Running description",
            "status": "todo",
        }).json()["tasks"][0]
        claimed = self.board.claim_next_todo()
        self.assertEqual(claimed.id, task["id"])

        response = self.client.post(
            f"/api/board/tasks/{task['id']}/comments",
            json={"body": "Wait for me"})
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()["deferred"])
        self.assertEqual(self._tasks()[0]["status"], "doing")

        self.assertTrue(self.board.finish_run(
            claimed.id, claimed.run_id, response="first answer"))
        self.assertEqual(self._tasks()[0]["status"], "todo")

        follow_up = self.board.claim_next_todo()
        self.assertTrue(self.board.finish_run(
            follow_up.id, follow_up.run_id, response="second answer"))
        detail = self.client.get(
            f"/api/board/tasks/{task['id']}").json()
        self.assertEqual(self._tasks()[0]["status"], "review")
        self.assertEqual([run["response"] for run in detail["runs"]],
                         ["first answer", "second answer"])
        self.assertEqual(detail["comments"][0]["run_id"],
                         follow_up.run_id)

    def test_deleting_a_conversation_unlinks_its_task(self) -> None:
        task = self.client.post(
            "/api/board/tasks",
            json={"title": "Linked", "description": "Linked description"},
        ).json()["tasks"][0]
        session_id = self.client.post(
            f"/api/board/tasks/{task['id']}/session", json={},
        ).json()["session_id"]

        response = self.client.delete(f"/api/sessions/{session_id}")
        self.assertEqual(response.status_code, 200)

        stored = self._tasks()[0]
        self.assertIsNone(stored["session_id"])
        self.assertIsNone(stored["session"])


class _BlockingTurn:
    """A fake turn that blocks until the test releases a call."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.started: list[threading.Event] = []
        self.releases: list[threading.Event] = []

    async def __call__(self, message, *args, **kwargs):
        index = len(self.calls)
        self.calls.append(message)
        while len(self.started) <= index:
            self.started.append(threading.Event())
            self.releases.append(threading.Event())
        self.started[index].set()
        await asyncio.to_thread(self.releases[index].wait, 5.0)
        return f"answer-{index}"

    def release_all(self) -> None:
        for event in self.releases:
            event.set()

    def wait_started(self, index: int, timeout: float = 2.0) -> bool:
        return self.started[index].wait(timeout)


class BoardServerExecutionTests(unittest.TestCase):
    def _wait_started(self, turn: _BlockingTurn, index: int,
                      timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(turn.started) > index and turn.wait_started(index, 0.01):
                return
            time.sleep(0.01)
        self.fail(f"turn {index} did not start")

    def _wait_status(self, client: TestClient, task_id: str,
                     status: str, timeout: float = 3.0) -> dict:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            tasks = client.get("/api/board").json()["tasks"]
            last = next((task for task in tasks if task["id"] == task_id), None)
            if last is not None and last["status"] == status:
                return last
            time.sleep(0.02)
        self.fail(f"task {task_id} did not reach {status}: {last}")
        return {}

    def test_todo_task_runs_its_description_and_moves_to_review(self) -> None:
        calls: list[str] = []

        async def fake_run_turn(message, *args, **kwargs):
            calls.append(message)
            return "done"

        with TemporaryDirectory() as root:
            path = Path(root)
            sessions = SessionStore(path / "sessions")
            board = BoardStore(path)
            with patch.object(server, "credentials_from_env",
                              return_value=None), \
                    patch.object(server, "run_turn", fake_run_turn):
                app = server.create_app(sessions, board)
                with TestClient(app) as client:
                    response = client.post("/api/board/tasks", json={
                        "title": "Auto run",
                        "description": "do the thing",
                        "status": "todo",
                    })
                    self.assertEqual(response.status_code, 201)
                    task_id = response.json()["tasks"][0]["id"]

                    deadline = time.time() + 3.0
                    status = "todo"
                    while time.time() < deadline:
                        tasks = client.get("/api/board").json()["tasks"]
                        status = tasks[0]["status"]
                        if status == "review":
                            break
                        time.sleep(0.02)

                    self.assertEqual(calls, ["do the thing"])
                    self.assertEqual(status, "review")
                    stored = client.get("/api/board").json()["tasks"][0]
                    self.assertEqual(stored["id"], task_id)
                    self.assertIsNotNone(stored["session_id"])
                    self.assertIsNone(stored["last_error"])

    def test_comment_while_running_is_sent_after_the_run(self) -> None:
        turn = _BlockingTurn()
        with TemporaryDirectory() as root:
            path = Path(root)
            sessions = SessionStore(path / "sessions")
            board = BoardStore(path)
            with patch.object(server, "credentials_from_env",
                              return_value=None), \
                    patch.object(server, "run_turn", turn):
                app = server.create_app(sessions, board)
                with TestClient(app) as client:
                    try:
                        response = client.post("/api/board/tasks", json={
                            "title": "Feedback",
                            "description": "first prompt",
                            "status": "todo",
                        })
                        task_id = response.json()["tasks"][0]["id"]
                        self._wait_started(turn, 0)

                        for body in ("first feedback", "second feedback"):
                            response = client.post(
                                f"/api/board/tasks/{task_id}/comments",
                                json={"body": body})
                            self.assertEqual(response.status_code, 201)
                            self.assertTrue(response.json()["deferred"])

                        # The current run keeps its original prompt.
                        self.assertEqual(turn.calls, ["first prompt"])
                        turn.releases[0].set()

                        self._wait_started(turn, 1)
                        self.assertEqual(
                            turn.calls,
                            ["first prompt",
                             "first feedback\n\nsecond feedback"])
                        turn.releases[1].set()
                    finally:
                        turn.release_all()

                    stored = self._wait_status(client, task_id, "review")
                    self.assertEqual(stored["run_count"], 2)
                    detail = client.get(
                        f"/api/board/tasks/{task_id}").json()
                    self.assertEqual(
                        [run["response"] for run in detail["runs"]],
                        ["answer-0", "answer-1"])
                    self.assertEqual(
                        [run["status"] for run in detail["runs"]],
                        ["completed", "completed"])
                    self.assertEqual(
                        [comment["run_id"] for comment in detail["comments"]],
                        [detail["runs"][1]["run_id"]] * 2)

    def test_comment_from_review_becomes_the_next_turn(self) -> None:
        turn = _BlockingTurn()
        with TemporaryDirectory() as root:
            path = Path(root)
            sessions = SessionStore(path / "sessions")
            board = BoardStore(path)
            with patch.object(server, "credentials_from_env",
                              return_value=None), \
                    patch.object(server, "run_turn", turn):
                app = server.create_app(sessions, board)
                with TestClient(app) as client:
                    try:
                        response = client.post("/api/board/tasks", json={
                            "title": "Another pass",
                            "description": "initial prompt",
                            "status": "todo",
                        })
                        task_id = response.json()["tasks"][0]["id"]
                        self._wait_started(turn, 0)
                        turn.releases[0].set()
                        self._wait_status(client, task_id, "review")

                        response = client.post(
                            f"/api/board/tasks/{task_id}/comments",
                            json={"body": "one more thing"})
                        self.assertEqual(response.status_code, 201)
                        self.assertFalse(response.json()["deferred"])

                        self._wait_started(turn, 1)
                        self.assertEqual(turn.calls,
                                         ["initial prompt", "one more thing"])
                        turn.releases[1].set()
                    finally:
                        turn.release_all()

                    stored = self._wait_status(client, task_id, "review")
                    self.assertEqual(stored["run_count"], 2)

    def test_runtime_error_records_the_answer_and_error(self) -> None:
        calls: list[str] = []

        async def fake_run_turn(message, *args, **kwargs):
            calls.append(message)
            kwargs["events"].emit("runtime.error", stage="test",
                                  error="boom")
            return "looks like an answer"

        with TemporaryDirectory() as root:
            path = Path(root)
            sessions = SessionStore(path / "sessions")
            board = BoardStore(path)
            with patch.object(server, "credentials_from_env",
                              return_value=None), \
                    patch.object(server, "run_turn", fake_run_turn):
                app = server.create_app(sessions, board)
                with TestClient(app) as client:
                    response = client.post("/api/board/tasks", json={
                        "title": "Runtime error",
                        "description": "fail this run",
                        "status": "todo",
                    })
                    self.assertEqual(response.status_code, 201)
                    task_id = response.json()["tasks"][0]["id"]

                    stored = self._wait_status(client, task_id, "review")
                    self.assertEqual(calls, ["fail this run"])
                    self.assertEqual(stored["last_error"], "boom")
                    detail = client.get(
                        f"/api/board/tasks/{task_id}").json()
                    self.assertEqual(detail["runs"][0]["status"], "error")
                    self.assertEqual(detail["runs"][0]["error"], "boom")
                    self.assertEqual(detail["runs"][0]["response"],
                                     "looks like an answer")
                    time.sleep(0.1)
                    self.assertEqual(len(calls), 1)

    def test_running_task_moved_to_todo_is_cancelled_and_rerun(self) -> None:
        turn = _BlockingTurn()
        with TemporaryDirectory() as root:
            path = Path(root)
            sessions = SessionStore(path / "sessions")
            board = BoardStore(path)
            with patch.object(server, "credentials_from_env",
                              return_value=None), \
                    patch.object(server, "run_turn", turn):
                app = server.create_app(sessions, board)
                with TestClient(app) as client:
                    try:
                        response = client.post("/api/board/tasks", json={
                            "title": "Rerun",
                            "description": "run me twice",
                            "status": "todo",
                        })
                        self.assertEqual(response.status_code, 201)
                        task_id = response.json()["tasks"][0]["id"]
                        self._wait_started(turn, 0)
                        session_id = client.get(
                            "/api/board").json()["tasks"][0]["session_id"]
                        self.assertIsNotNone(session_id)

                        response = client.patch(
                            f"/api/board/tasks/{task_id}",
                            json={"status": "todo"})
                        self.assertEqual(response.status_code, 200)

                        # The second turn must start while the first fake
                        # turn is still blocked: that proves the first
                        # run was cancelled rather than allowed to finish.
                        self._wait_started(turn, 1)
                        self.assertEqual(turn.calls,
                                         ["run me twice", "run me twice"])
                    finally:
                        turn.release_all()

                    stored = self._wait_status(client, task_id, "review")
                    self.assertEqual(stored["session_id"], session_id)

    def test_deleting_running_task_cancels_and_keeps_conversation(self) -> None:
        turn = _BlockingTurn()
        with TemporaryDirectory() as root:
            path = Path(root)
            sessions = SessionStore(path / "sessions")
            board = BoardStore(path)
            with patch.object(server, "credentials_from_env",
                              return_value=None), \
                    patch.object(server, "run_turn", turn):
                app = server.create_app(sessions, board)
                with TestClient(app) as client:
                    try:
                        response = client.post("/api/board/tasks", json={
                            "title": "Delete running",
                            "description": "keep the conversation",
                            "status": "todo",
                        })
                        self.assertEqual(response.status_code, 201)
                        task_id = response.json()["tasks"][0]["id"]
                        self._wait_started(turn, 0)
                        task = client.get(
                            "/api/board").json()["tasks"][0]
                        session_id = task["session_id"]
                        self.assertIsNotNone(session_id)

                        response = client.delete(
                            f"/api/board/tasks/{task_id}")
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(
                            client.get("/api/board").json()["tasks"], [])
                        self.assertEqual(len(turn.calls), 1)
                    finally:
                        turn.release_all()

                    time.sleep(0.1)
                    self.assertIsNotNone(sessions.get(session_id))
                    self.assertEqual(
                        client.get("/api/board").json()["tasks"], [])

    def test_deleting_running_session_cancels_and_unlinks_task(self) -> None:
        turn = _BlockingTurn()
        with TemporaryDirectory() as root:
            path = Path(root)
            sessions = SessionStore(path / "sessions")
            board = BoardStore(path)
            with patch.object(server, "credentials_from_env",
                              return_value=None), \
                    patch.object(server, "run_turn", turn):
                app = server.create_app(sessions, board)
                with TestClient(app) as client:
                    try:
                        response = client.post("/api/board/tasks", json={
                            "title": "Delete session",
                            "description": "cancel this turn",
                            "status": "todo",
                        })
                        self.assertEqual(response.status_code, 201)
                        task_id = response.json()["tasks"][0]["id"]
                        self._wait_started(turn, 0)
                        task = client.get(
                            "/api/board").json()["tasks"][0]
                        session_id = task["session_id"]
                        self.assertIsNotNone(session_id)

                        response = client.delete(
                            f"/api/sessions/{session_id}")
                        self.assertEqual(response.status_code, 200)
                    finally:
                        turn.release_all()

                    self.assertIsNone(sessions.get(session_id))
                    stored = self._wait_status(client, task_id, "review")
                    self.assertIsNone(stored["session_id"])
                    self.assertIsNone(stored["session"])

    def test_busy_session_completion_wakes_the_executor(self) -> None:
        turn = _BlockingTurn()
        with TemporaryDirectory() as root:
            path = Path(root)
            sessions = SessionStore(path / "sessions")
            board = BoardStore(path)
            with patch.object(server, "credentials_from_env",
                              return_value=None), \
                    patch.object(server, "run_turn", turn):
                app = server.create_app(sessions, board)
                with TestClient(app) as client:
                    try:
                        response = client.post("/api/board/tasks", json={
                            "title": "Wake me",
                            "description": "board task description",
                            "status": "backlog",
                        })
                        self.assertEqual(response.status_code, 201)
                        task_id = response.json()["tasks"][0]["id"]
                        linked = client.post(
                            f"/api/board/tasks/{task_id}/session",
                            json={})
                        self.assertEqual(linked.status_code, 200)
                        session_id = linked.json()["session_id"]

                        response = client.post(
                            f"/api/sessions/{session_id}/messages",
                            json={"message": "manual turn"})
                        self.assertEqual(response.status_code, 200)
                        self._wait_started(turn, 0)

                        response = client.patch(
                            f"/api/board/tasks/{task_id}",
                            json={"status": "todo"})
                        self.assertEqual(response.status_code, 200)
                        time.sleep(0.1)
                        self.assertEqual(
                            client.get("/api/board").json()["tasks"][0][
                                "status"], "todo")

                        # No explicit executor.wake(): the done callback
                        # from send_message must wake it promptly, well
                        # before the one-second fallback poll.
                        turn.releases[0].set()
                        self._wait_started(turn, 1, timeout=0.7)
                        self.assertEqual(turn.calls, [
                            "manual turn",
                            "board task description",
                        ])
                    finally:
                        turn.release_all()

                    stored = self._wait_status(client, task_id, "review")
                    self.assertEqual(stored["session_id"], session_id)

    def test_startup_recovers_interrupted_task_but_not_legacy_todo(self) -> None:
        calls: list[str] = []

        async def fake_run_turn(message, *args, **kwargs):
            calls.append(message)
            return "done"

        with TemporaryDirectory() as root:
            path = Path(root)
            (path / "board.json").write_text(json.dumps({
                "version": 1,
                "updated_at": 1.0,
                "tasks": [
                    {
                        "id": "task-interrupted",
                        "title": "Interrupted",
                        "description": "resume me",
                        "status": "doing",
                        "priority": "normal",
                        "tags": [],
                        "position": 0,
                        "run_id": "run-old",
                        "created_at": 1.0,
                        "updated_at": 1.0,
                    },
                    {
                        "id": "task-legacy",
                        "title": "Legacy",
                        "description": "do not run",
                        "status": "todo",
                        "priority": "normal",
                        "tags": [],
                        "position": 0,
                        "created_at": 1.0,
                        "updated_at": 1.0,
                    },
                ],
            }), encoding="utf-8")
            sessions = SessionStore(path / "sessions")
            board = BoardStore(path)
            with patch.object(server, "credentials_from_env",
                              return_value=None), \
                    patch.object(server, "run_turn", fake_run_turn):
                app = server.create_app(sessions, board)
                with TestClient(app) as client:
                    stored = self._wait_status(
                        client, "task-interrupted", "review")
                    self.assertEqual(calls, ["resume me"])
                    self.assertEqual(stored["last_error"], None)

                    legacy = next(
                        task for task in
                        client.get("/api/board").json()["tasks"]
                        if task["id"] == "task-legacy")
                    self.assertEqual(legacy["status"], "todo")
                    time.sleep(0.1)
                    self.assertEqual(calls, ["resume me"])


if __name__ == "__main__":
    unittest.main()