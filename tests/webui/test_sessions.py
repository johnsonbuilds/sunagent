import asyncio
import json
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parents[2]))
sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.channels.webui import WebUIChannel
from agent_runtime.events import AgentEvent

from webui.sessions import STREAM_STOP, Session, SessionStore


class FrameBusTests(unittest.IsolatedAsyncioTestCase):
    async def test_frames_reach_every_subscriber(self) -> None:
        store = SessionStore(Path("/tmp/does-not-matter"))
        session = store.create()
        first = session.bus.subscribe()
        second = session.bus.subscribe()

        session.bus.publish({"type": "run.started"})

        self.assertEqual(first.get_nowait()["type"], "run.started")
        self.assertEqual(second.get_nowait()["type"], "run.started")

    async def test_replay_returns_frames_after_a_cursor(self) -> None:
        store = SessionStore(Path("/tmp/does-not-matter"))
        session = store.create()
        session.bus.publish({"type": "one"})
        session.bus.publish({"type": "two"})

        replayed = session.bus.replay(1)
        self.assertEqual([frame["type"] for frame in replayed], ["two"])

    async def test_unsubscribe_stops_delivery(self) -> None:
        store = SessionStore(Path("/tmp/does-not-matter"))
        session = store.create()
        queue = session.bus.subscribe()
        session.bus.unsubscribe(queue)
        session.bus.publish({"type": "run.started"})
        self.assertTrue(queue.empty())

    async def test_stop_closes_open_streams(self) -> None:
        store = SessionStore(Path("/tmp/does-not-matter"))
        session = store.create()
        queue = session.bus.subscribe()

        session.bus.stop()

        # The sentinel tells the stream to finish; it is never rendered.
        self.assertIs(queue.get_nowait(), STREAM_STOP)

    async def test_shutdown_stops_streams_and_running_turns(self) -> None:
        store = SessionStore(Path("/tmp/does-not-matter"))
        session = store.create()
        queue = session.bus.subscribe()

        async def forever() -> None:
            await asyncio.sleep(60)

        session.task = asyncio.ensure_future(forever())

        store.shutdown()

        self.assertIs(queue.get_nowait(), STREAM_STOP)
        await asyncio.sleep(0)
        self.assertTrue(session.task.cancelled() or session.task.done())


class SessionStoreTests(unittest.TestCase):
    def test_ids_do_not_collide_within_one_second(self) -> None:
        store = SessionStore(Path("/tmp/does-not-matter"))

        ids = {store.create().id for _ in range(50)}

        self.assertEqual(len(ids), 50)

    def test_a_diagnosis_session_round_trips_its_metadata(self) -> None:
        with TemporaryDirectory() as root:
            store = SessionStore(Path(root))
            session = store.create(
                "Diagnosis: task-1", harness="evaluation-v1", origin="diagnosis",
                target={"type": "task", "id": "task-1"},
                diagnosis_id="diag-1", bundle_hash="sha256:abc")
            session.append_user_message("diagnose")
            session.append_segment({"id": "s1", "type": "assistant",
                                    "content": "## Conclusion"})
            store.save(session)

            reloaded = SessionStore(Path(root))
            reloaded.load()
            restored = reloaded.get(session.id)

            self.assertEqual(restored.origin, "diagnosis")
            self.assertEqual(restored.target, {"type": "task", "id": "task-1"})
            self.assertEqual(restored.diagnosis_id, "diag-1")
            self.assertEqual(restored.bundle_hash, "sha256:abc")
            self.assertEqual(restored.summary()["origin"], "diagnosis")
            self.assertEqual(restored.summary()["target"],
                             {"type": "task", "id": "task-1"})

    def test_a_legacy_record_is_an_ordinary_web_ui_session(self) -> None:
        with TemporaryDirectory() as root:
            path = Path(root) / "sess-legacy.json"
            path.write_text(json.dumps({
                "id": "sess-legacy", "title": "old",
                "created_at": 1.0, "updated_at": 1.0,
                "segments": [{"id": "s1", "type": "user",
                              "content": "hi"}],
                "messages": [],
            }), encoding="utf-8")

            store = SessionStore(Path(root))
            store.load()
            restored = store.get("sess-legacy")

            self.assertEqual(restored.origin, "webui")
            self.assertIsNone(restored.target)
            self.assertIsNone(restored.report_segment_id)

    def test_the_diagnosis_report_is_frozen_on_its_first_segment(self) -> None:
        store = SessionStore(Path("/tmp/does-not-matter"))
        session = store.create(origin="diagnosis")

        self.assertTrue(session.mark_report_segment("s1"))
        self.assertFalse(session.mark_report_segment("s2"))

        self.assertEqual(session.report_segment_id, "s1")

    def test_a_blank_report_segment_is_never_frozen(self) -> None:
        store = SessionStore(Path("/tmp/does-not-matter"))
        session = store.create(origin="diagnosis")

        self.assertFalse(session.mark_report_segment(""))
        self.assertFalse(session.mark_report_segment("   "))
        self.assertIsNone(session.report_segment_id)

        # A blank id must not block the real report from being recorded.
        self.assertTrue(session.mark_report_segment(" s1 "))
        self.assertEqual(session.report_segment_id, "s1")

    def test_a_target_is_detached_from_the_caller_and_the_output(self) -> None:
        store = SessionStore(Path("/tmp/does-not-matter"))
        target = {"type": "cohort", "filters": {"harness": "coding-v1"}}

        session = store.create(origin="diagnosis", target=target)
        target["filters"]["harness"] = "general-v1"

        self.assertEqual(session.target["filters"], {"harness": "coding-v1"})
        session.target["filters"]["harness"] = "gone"
        self.assertEqual(target["filters"], {"harness": "general-v1"})

    def test_an_empty_target_is_stored_as_none(self) -> None:
        store = SessionStore(Path("/tmp/does-not-matter"))

        self.assertIsNone(store.create(origin="diagnosis", target={}).target)

    def test_a_stored_empty_target_loads_as_none(self) -> None:
        with TemporaryDirectory() as root:
            path = Path(root) / "sess-empty-target.json"
            path.write_text(json.dumps({
                "id": "sess-empty-target", "title": "diag",
                "created_at": 1.0, "updated_at": 1.0,
                "origin": "diagnosis", "target": {},
                "segments": [{"id": "s1", "type": "user", "content": "hi"}],
                "messages": [],
            }), encoding="utf-8")

            store = SessionStore(Path(root))
            store.load()

            self.assertIsNone(store.get("sess-empty-target").target)

    def test_sessions_round_trip_through_disk(self) -> None:
        with TemporaryDirectory() as root:
            store = SessionStore(Path(root))
            session = store.create()
            session.append_user_message("Hello there")
            session.append_segment({"id": "s1", "type": "assistant",
                                    "content": "Hi"})
            store.save(session)

            reloaded = SessionStore(Path(root))
            reloaded.load()
            restored = reloaded.get(session.id)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.title, "Hello there")
            self.assertEqual(len(restored.segments), 2)
            self.assertEqual(restored.segments[0]["type"], "user")
            self.assertEqual(restored.segments[1]["content"], "Hi")

    def test_conversation_messages_are_restored(self) -> None:
        with TemporaryDirectory() as root:
            store = SessionStore(Path(root))
            session = store.create()
            session.conversation.messages.append({"role": "user",
                                                  "content": "hi"})
            store.save(session)

            reloaded = SessionStore(Path(root))
            reloaded.load()
            restored = reloaded.get(session.id)
            self.assertEqual(restored.conversation.messages,
                             [{"role": "user", "content": "hi"}])

    def test_deleting_a_session_removes_its_file(self) -> None:
        with TemporaryDirectory() as root:
            store = SessionStore(Path(root))
            session = store.create()
            session.append_user_message("Hello there")
            store.save(session)
            path = store.root / f"{session.id}.json"
            self.assertTrue(path.exists())

            self.assertTrue(store.delete(session.id))
            self.assertFalse(path.exists())
            self.assertIsNone(store.get(session.id))

    def test_an_empty_session_is_never_persisted(self) -> None:
        with TemporaryDirectory() as root:
            store = SessionStore(Path(root))
            session = store.create()

            store.save(session)

            self.assertFalse((store.root / f"{session.id}.json").exists())

    def test_renaming_an_empty_session_does_not_create_a_file(self) -> None:
        with TemporaryDirectory() as root:
            store = SessionStore(Path(root))
            session = store.create()

            store.rename(session.id, "Not started yet")

            self.assertFalse((store.root / f"{session.id}.json").exists())

    def test_an_empty_session_is_not_listed(self) -> None:
        with TemporaryDirectory() as root:
            store = SessionStore(Path(root))
            store.create()

            self.assertEqual(store.list(), [])

    def test_a_session_is_listed_once_it_holds_a_message(self) -> None:
        with TemporaryDirectory() as root:
            store = SessionStore(Path(root))
            session = store.create()
            session.append_user_message("Hello there")
            store.save(session)

            listed = store.list()

            self.assertEqual([item.id for item in listed], [session.id])

    def test_loading_skips_records_that_hold_no_conversation(self) -> None:
        with TemporaryDirectory() as root:
            store = SessionStore(Path(root))
            empty = store.create()
            (store.root / f"{empty.id}.json").write_text(
                json.dumps({"id": empty.id, "title": "New conversation",
                            "created_at": 0.0, "updated_at": 0.0,
                            "segments": [], "messages": []}),
                encoding="utf-8")

            reloaded = SessionStore(Path(root))
            reloaded.load()

            self.assertIsNone(reloaded.get(empty.id))
            self.assertEqual(reloaded.list(), [])

    def test_a_new_conversation_takes_its_title_from_the_first_message(self) -> None:
        with TemporaryDirectory() as root:
            store = SessionStore(Path(root))
            session = store.create()
            session.append_user_message("Explain the agent loop to me")
            self.assertEqual(session.title, "Explain the agent loop to me")

    def test_model_and_harness_round_trip_through_disk(self) -> None:
        with TemporaryDirectory() as root:
            store = SessionStore(Path(root))
            session = store.create(model="fast", harness="coding-v1")
            session.append_user_message("Hello there")
            store.save(session)

            reloaded = SessionStore(Path(root))
            reloaded.load()
            restored = reloaded.get(session.id)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.model, "fast")
            self.assertEqual(restored.harness, "coding-v1")
            self.assertTrue(restored.harness_locked)

    def test_a_legacy_record_without_a_selection_loads_as_unset(self) -> None:
        with TemporaryDirectory() as root:
            path = Path(root) / "sess-legacy.json"
            path.write_text(json.dumps({
                "id": "sess-legacy", "title": "Old",
                "created_at": 0.0, "updated_at": 0.0,
                "segments": [{"id": "s1", "type": "user", "content": "hi"}],
                "messages": [],
            }), encoding="utf-8")

            store = SessionStore(Path(root))
            store.load()
            restored = store.get("sess-legacy")

            self.assertIsNotNone(restored)
            self.assertIsNone(restored.model)
            self.assertIsNone(restored.harness)
            self.assertTrue(restored.harness_locked)

    def test_an_empty_session_is_not_harness_locked(self) -> None:
        with TemporaryDirectory() as root:
            store = SessionStore(Path(root))
            session = store.create()

            self.assertFalse(session.harness_locked)

    def test_sessions_are_listed_newest_first(self) -> None:
        with TemporaryDirectory() as root:
            store = SessionStore(Path(root))
            first = store.create()
            first.append_user_message("First")
            store.save(first)
            second = store.create()
            second.append_user_message("Second")
            store.save(second)
            second.updated_at = first.updated_at + 10
            listed = store.list()
            self.assertEqual(listed[0].id, second.id)

    def test_listing_survives_concurrent_creates_and_deletes(self) -> None:
        store = SessionStore(Path("/tmp/does-not-matter"))
        for index in range(20):
            store.create(f"seed-{index}").append_user_message(f"hi {index}")
        entered = threading.Event()
        release = threading.Event()
        done = threading.Event()
        real_is_empty = Session.is_empty.fget
        state = {"calls": 0}

        def slow_is_empty(self) -> bool:
            state["calls"] += 1
            if state["calls"] == 3:
                # ``list()`` is now iterating the dict with a generator
                # suspended inside this call.
                entered.set()
                release.wait(2.0)
            return real_is_empty(self)

        def writer() -> None:
            entered.wait(2.0)
            store.create("churn")
            done.set()
            release.set()

        with patch.object(Session, "is_empty", property(slow_is_empty)):
            thread = threading.Thread(target=writer)
            thread.start()
            try:
                listed = store.list()
            finally:
                release.set()
                thread.join()
        self.assertTrue(done.is_set())
        self.assertEqual(len(listed), 20)


class ChannelSinkTests(unittest.TestCase):
    def test_a_segment_frame_is_appended_to_the_transcript(self) -> None:
        try:
            from webui.server import _session_sink
        except ImportError:  # pragma: no cover - the web extra is optional
            self.skipTest("the web extra (FastAPI) is not installed")

        with TemporaryDirectory() as root:
            store = SessionStore(Path(root))
            session = store.create()
            channel = WebUIChannel(sink=_session_sink(session))

            channel(AgentEvent("run-1", "e1", "agent.started", 0.0, 1, None, {}))
            channel(AgentEvent("run-1", "e2", "assistant.delta", 0.0, 1, None,
                               {"content": "hello"}))
            channel(AgentEvent("run-1", "e3", "assistant.completed", 0.0, 1,
                               None, {}))

            published = [frame for frame in session.bus.replay(0)]
            self.assertEqual([frame["type"] for frame in published],
                             ["run.started", "delta", "segment"])
            self.assertEqual(len(session.segments), 1)
            self.assertEqual(session.segments[0]["type"], "assistant")
            self.assertEqual(session.segments[0]["content"], "hello")


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
