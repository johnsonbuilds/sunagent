import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory



sys.path.insert(0, str(Path(__file__).parents[2]))
sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.channels.webui import WebUIChannel
from agent_runtime.events import AgentEvent

from webui.sessions import SessionStore


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


class SessionStoreTests(unittest.TestCase):
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
            path = store.root / f"{session.id}.json"
            self.assertTrue(path.exists())

            self.assertTrue(store.delete(session.id))
            self.assertFalse(path.exists())
            self.assertIsNone(store.get(session.id))

    def test_a_new_conversation_takes_its_title_from_the_first_message(self) -> None:
        with TemporaryDirectory() as root:
            store = SessionStore(Path(root))
            session = store.create()
            session.append_user_message("Explain the agent loop to me")
            self.assertEqual(session.title, "Explain the agent loop to me")

    def test_sessions_are_listed_newest_first(self) -> None:
        with TemporaryDirectory() as root:
            store = SessionStore(Path(root))
            first = store.create()
            second = store.create()
            second.updated_at = first.updated_at + 10
            listed = store.list()
            self.assertEqual(listed[0].id, second.id)


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
    unittest.main()
