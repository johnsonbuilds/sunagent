"""Branching a conversation from one of its answers.

A branch copies the transcript and the message history up to a chosen
assistant block into a new, independent session.  The copying itself is a
pure function so the awkward part -- keeping the per-turn ``messages`` list
aligned with the per-block ``segments`` list -- is testable without a
server.
"""

import json
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parents[2]))
sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from starlette.testclient import TestClient

from agent_runtime.agent.runtime_message import runtime_user_message

from webui import server
from webui.sessions import SessionStore, branch_point


SYSTEM = {"role": "system", "content": "you are SunAgent"}


def chat_transcript(*, tool_calls: bool = False) -> tuple[list, list]:
    """One turn: user question, optional tool round, final answer."""
    segments = [
        {"id": "sess-x:user:0", "type": "user", "content": "list the files"},
        {"id": "run-1:reasoning:1", "type": "reasoning", "content": "think"},
    ]
    messages = [
        dict(SYSTEM),
        {"role": "user", "content": "list the files"},
    ]
    if tool_calls:
        segments += [
            {"id": "run-1:assistant:2", "type": "assistant", "content": "ok"},
            {"id": "run-1:tool:call-1", "type": "tool", "call_id": "call-1",
             "tool": "ls", "status": "ok", "output": "a.py"},
        ]
        messages += [
            {"role": "assistant", "content": "ok",
             "tool_calls": [{"id": "call-1", "type": "function",
                             "function": {"name": "ls",
                                          "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "a.py"},
        ]
    segments.append({"id": "run-1:assistant:3", "type": "assistant",
                     "content": "just a.py"})
    messages.append({"role": "assistant", "content": "just a.py",
                     "reasoning_content": "think"})
    return segments, messages


class BranchPointTests(unittest.TestCase):
    def test_the_answer_of_a_turn_is_a_branch_point(self) -> None:
        segments, messages = chat_transcript()

        point = branch_point(segments, messages, "run-1:assistant:3")

        self.assertIsNotNone(point)
        index, head = point
        self.assertEqual(index, 2)
        self.assertEqual(len(head), 3)
        self.assertEqual(head[-1], {"role": "assistant",
                                    "content": "just a.py"})

    def test_a_turn_that_ran_tools_branches_from_its_final_answer(self) -> None:
        segments, messages = chat_transcript(tool_calls=True)

        point = branch_point(segments, messages, "run-1:assistant:3")

        self.assertIsNotNone(point)
        _, head = point
        # The tool round keeps its own messages: only the tail is cut off.
        self.assertEqual([message["role"] for message in head],
                         ["system", "user", "assistant", "tool", "assistant"])
        self.assertNotIn("tool_calls", head[-1])

    def test_a_reply_in_the_middle_of_a_turn_is_refused(self) -> None:
        segments, messages = chat_transcript(tool_calls=True)

        self.assertIsNone(branch_point(segments, messages,
                                       "run-1:assistant:2"))

    def test_a_reply_followed_by_reasoning_is_still_mid_turn(self) -> None:
        segments, messages = chat_transcript()
        segments.append({"id": "run-1:reasoning:9", "type": "reasoning",
                         "content": "more"})

        self.assertIsNone(branch_point(segments, messages,
                                       "run-1:assistant:3"))

    def test_only_assistant_text_blocks_can_start_a_branch(self) -> None:
        segments, messages = chat_transcript(tool_calls=True)

        for segment_id in ("sess-x:user:0", "run-1:reasoning:1",
                           "run-1:tool:call-1", "run-9:assistant:1"):
            self.assertIsNone(branch_point(segments, messages, segment_id),
                              segment_id)

    def test_a_blank_answer_cannot_start_a_branch(self) -> None:
        segments = [{"id": "sess-x:user:0", "type": "user", "content": "hi"},
                    {"id": "run-1:assistant:1", "type": "assistant",
                     "content": "   "}]
        messages = [{"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "   "}]

        self.assertIsNone(branch_point(segments, messages,
                                       "run-1:assistant:1"))

    def test_reasoning_and_content_segments_join_into_one_message(self) -> None:
        segments = [
            {"id": "sess-x:user:0", "type": "user", "content": "hi"},
            {"id": "run-1:assistant:1", "type": "assistant", "content": "par"},
            {"id": "run-1:reasoning:2", "type": "reasoning", "content": "hm"},
            {"id": "run-1:assistant:3", "type": "assistant", "content": "tial"},
        ]
        messages = [{"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "partial"}]

        point = branch_point(segments, messages, "run-1:assistant:3")

        self.assertIsNotNone(point)
        self.assertEqual(point[1][-1], {"role": "assistant",
                                        "content": "partial"})

    def test_a_block_without_its_message_is_refused(self) -> None:
        segments, messages = chat_transcript()
        # A cancelled run leaves the block in the transcript but never
        # reached the conversation.
        messages.pop()

        self.assertIsNone(branch_point(segments, messages,
                                       "run-1:assistant:3"))

    def test_an_edited_answer_is_refused_rather_than_guessed(self) -> None:
        segments, messages = chat_transcript()
        messages[-1]["content"] = "something else"

        self.assertIsNone(branch_point(segments, messages,
                                       "run-1:assistant:3"))

    def test_earlier_turns_are_kept_and_later_turns_dropped(self) -> None:
        segments = [
            {"id": "sess-x:user:0", "type": "user", "content": "one"},
            {"id": "run-1:assistant:1", "type": "assistant", "content": "a"},
            {"id": "sess-x:user:2", "type": "user", "content": "two"},
            {"id": "run-2:assistant:1", "type": "assistant", "content": "b"},
        ]
        messages = [
            # A loop-internal nudge must not be counted as a turn.
            dict(SYSTEM),
            {"role": "user", "content": "one"},
            runtime_user_message("budget reminder", kind="reminder"),
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": "two"},
            {"role": "assistant", "content": "b"},
        ]

        point = branch_point(segments, messages, "run-1:assistant:1")

        self.assertIsNotNone(point)
        index, head = point
        self.assertEqual(index, 1)
        self.assertEqual(head[-1], {"role": "assistant", "content": "a"})
        self.assertNotIn("two", [message.get("content") for message in head])

    def test_the_system_prompt_of_the_parent_is_kept(self) -> None:
        segments, messages = chat_transcript()

        point = branch_point(segments, messages, "run-1:assistant:3")

        self.assertEqual(point[1][0], SYSTEM)

    def test_the_copy_does_not_alias_the_parent(self) -> None:
        segments, messages = chat_transcript()

        _, head = branch_point(segments, messages, "run-1:assistant:3")
        head[-1]["content"] = "mutated"

        self.assertEqual(messages[-1]["content"], "just a.py")


class BranchStoreTests(unittest.TestCase):
    def _store(self, root: str) -> SessionStore:
        store = SessionStore(Path(root))
        store.load()
        return store

    def _parent(self, store: SessionStore):
        session = store.create("Audit runner", model="main",
                               harness="general-v1")
        segments, messages = chat_transcript()
        session.segments = segments
        session.conversation.messages = messages
        store.save(session)
        return session

    def test_the_branch_is_an_independent_persisted_session(self) -> None:
        with TemporaryDirectory() as root:
            store = self._store(root)
            parent = self._parent(store)

            child = store.branch(parent, "run-1:assistant:3",
                                 title="Audit runner (branch)")

            self.assertIsNotNone(child)
            self.assertNotEqual(child.id, parent.id)
            self.assertEqual(child.title, "Audit runner (branch)")
            self.assertEqual(child.model, "main")
            self.assertEqual(child.harness, "general-v1")
            self.assertEqual(child.origin, "webui")
            self.assertIsNone(child.target)
            self.assertIsNone(child.report_segment_id)
            self.assertEqual(child.segments, parent.segments)
            self.assertEqual(child.messages[:2], parent.messages[:2])
            # The cut message keeps its text and loses the reasoning echo.
            self.assertEqual(child.messages[-1],
                             {"role": "assistant", "content": "just a.py"})
            self.assertTrue((Path(root) / f"{child.id}.json").is_file())

    def test_the_branch_survives_a_reload(self) -> None:
        with TemporaryDirectory() as root:
            store = self._store(root)
            parent = self._parent(store)
            child = store.branch(parent, "run-1:assistant:3")

            reloaded = self._store(root)
            restored = reloaded.get(child.id)

            self.assertIsNotNone(restored)
            self.assertEqual([message["role"] for message in restored.messages],
                             ["system", "user", "assistant"])
            self.assertEqual(restored.segments, parent.segments)

    def test_a_refused_branch_creates_no_session(self) -> None:
        with TemporaryDirectory() as root:
            store = self._store(root)
            parent = self._parent(store)

            self.assertIsNone(store.branch(parent, "run-1:reasoning:1"))
            self.assertEqual(len(store.list()), 1)
            self.assertEqual(list(Path(root).glob("*.json")), [Path(
                root) / f"{parent.id}.json"])

    def test_writing_to_the_branch_leaves_the_parent_alone(self) -> None:
        with TemporaryDirectory() as root:
            store = self._store(root)
            parent = self._parent(store)
            child = store.branch(parent, "run-1:assistant:3")
            before = json.loads((Path(root) / f"{parent.id}.json")
                                .read_text(encoding="utf-8"))

            child.append_user_message("now show them")
            child.conversation.messages.append({"role": "user",
                                                "content": "now show them"})
            store.save(child)

            after = json.loads((Path(root) / f"{parent.id}.json")
                               .read_text(encoding="utf-8"))
            self.assertEqual(before, after)
            self.assertEqual(len(parent.messages), 3)


class BranchRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._root = TemporaryDirectory()
        self.addCleanup(self._root.cleanup)
        self.sessions = SessionStore(Path(self._root.name) / "sessions")
        self.sessions.load()
        with patch.object(server, "credentials_from_env", return_value=None):
            app = server.create_app(self.sessions, _board(self._root.name),
                                    enable_executor=False)
        self.client = TestClient(app)

    def _branch(self, session_id: str, segment_id: str):
        return self.client.post(
            f"/api/sessions/{session_id}/branch",
            json={"segment_id": segment_id},
        )

    def test_a_branch_is_created_and_listed(self) -> None:
        segments, messages = chat_transcript()
        parent = self.sessions.create("Audit runner", model="main")
        parent.segments = segments
        parent.conversation.messages = messages
        self.sessions.save(parent)

        response = self._branch(parent.id, "run-1:assistant:3")

        self.assertEqual(response.status_code, 201)
        detail = response.json()
        self.assertNotEqual(detail["id"], parent.id)
        self.assertEqual(detail["segments"], segments)
        self.assertEqual(detail["title"], "Audit runner (branch)")
        listed = [item["id"] for item in
                  self.client.get("/api/sessions").json()]
        self.assertIn(detail["id"], listed)

        child = self.client.get(f"/api/sessions/{detail['id']}").json()
        self.assertEqual(child["segments"], segments)
        self.assertEqual(len(child["segments"]), 3)

    def test_the_client_title_wins(self) -> None:
        segments, messages = chat_transcript()
        parent = self.sessions.create("Audit runner")
        parent.segments = segments
        parent.conversation.messages = messages
        self.sessions.save(parent)

        response = self.client.post(
            f"/api/sessions/{parent.id}/branch",
            json={"segment_id": "run-1:assistant:3",
                  "title": "审计执行器（分支）"},
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["title"], "审计执行器（分支）")

    def test_an_unknown_parent_is_404(self) -> None:
        self.assertEqual(self._branch("sess-missing", "run-1:assistant:3")
                         .status_code, 404)

    def test_a_running_parent_is_409(self) -> None:
        segments, messages = chat_transcript()
        parent = self.sessions.create("Audit runner")
        parent.segments = segments
        parent.conversation.messages = messages
        parent.busy = True
        self.sessions.save(parent)

        self.assertEqual(self._branch(parent.id, "run-1:assistant:3")
                         .status_code, 409)

    def test_an_unusable_segment_is_400(self) -> None:
        segments, messages = chat_transcript(tool_calls=True)
        parent = self.sessions.create("Audit runner")
        parent.segments = segments
        parent.conversation.messages = messages
        self.sessions.save(parent)

        for segment_id in ("run-1:assistant:2", "sess-x:user:0",
                           "run-1:tool:call-1", "run-7:assistant:1"):
            response = self._branch(parent.id, segment_id)
            self.assertEqual(response.status_code, 400, segment_id)
            self.assertEqual(response.json()["detail"],
                             "this message cannot start a branch")

    def test_a_missing_segment_id_is_rejected(self) -> None:
        parent = self.sessions.create("Audit runner")

        response = self.client.post(
            f"/api/sessions/{parent.id}/branch", json={"segment_id": ""})

        self.assertEqual(response.status_code, 422)

    def test_the_branch_accepts_the_next_message(self) -> None:
        segments, messages = chat_transcript(tool_calls=True)
        parent = self.sessions.create("Audit runner", model="main")
        parent.segments = segments
        parent.conversation.messages = messages
        self.sessions.save(parent)
        child_id = self._branch(parent.id,
                                "run-1:assistant:3").json()["id"]

        child = self.sessions.get(child_id)
        # The copied history must end on a plain answer: a dangling tool
        # call would make the next request invalid.
        self.assertEqual(child.conversation.messages[-1],
                         {"role": "assistant", "content": "just a.py"})
        self.assertFalse(child.busy)
        self.assertTrue(child.harness_locked)


class BranchAfterRealTurnTests(unittest.TestCase):
    """The blocks the real channel writes must line up with the messages.

    The turn itself is scripted, but everything between the route and the
    transcript is the production code path: ``_prepare_turn`` appends the
    user block, the channel turns events into blocks.
    """

    ANSWER = "a.py is the only file"

    def setUp(self) -> None:
        self._root = TemporaryDirectory()
        self.addCleanup(self._root.cleanup)
        self.sessions = SessionStore(Path(self._root.name) / "sessions")
        self.sessions.load()
        for context in (
            patch.object(server, "credentials_from_env", return_value=None),
            patch.object(server, "run_turn", self._turn()),
        ):
            context.start()
            self.addCleanup(context.stop)
        app = server.create_app(self.sessions, _board(self._root.name),
                                enable_executor=False)
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def _turn(self):
        """One turn that calls a tool and then answers."""
        answer = self.ANSWER

        async def turn(message, *args, **kwargs):
            events = kwargs["events"]
            conversation = kwargs["conversation"]
            conversation.append({"role": "user", "content": message})
            events.emit("agent.started", 0, message=message)
            events.emit("assistant.started", 1)
            events.emit("assistant.delta", 1, content="let me look")
            events.emit("assistant.completed", 1)
            conversation.append({
                "role": "assistant", "content": "let me look",
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "ls",
                                             "arguments": "{}"}}],
            })
            events.emit("tool.started", 2, call_id="c1", tool="ls",
                        arguments={})
            events.emit("tool.completed", 2, call_id="c1", exit_code=0,
                        stdout_tail="a.py", duration=0.1)
            conversation.append({"role": "tool", "tool_call_id": "c1",
                                 "content": "a.py"})
            events.emit("assistant.started", 3)
            events.emit("assistant.delta", 3, content=answer)
            events.emit("assistant.completed", 3)
            conversation.append({"role": "assistant", "content": answer})
            events.emit("agent.completed", 3, answer=answer, iterations=3)
            return answer

        return turn

    def _session(self) -> dict:
        created = self.client.post("/api/sessions", json={"title": "Audit"})
        self.assertEqual(created.status_code, 201, created.text)
        session_id = created.json()["id"]
        sent = self.client.post(f"/api/sessions/{session_id}/messages",
                                json={"message": "what files are here?"})
        self.assertEqual(sent.status_code, 200, sent.text)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            detail = self.client.get(f"/api/sessions/{session_id}").json()
            if not detail["busy"] and len(detail["segments"]) >= 4:
                return detail
            time.sleep(0.02)
        self.fail("the scripted turn never finished")

    def test_the_answer_of_a_turn_can_be_branched(self) -> None:
        parent = self._session()
        answer_segment = parent["segments"][-1]
        self.assertEqual(answer_segment["content"], self.ANSWER)

        response = self.client.post(
            f"/api/sessions/{parent['id']}/branch",
            json={"segment_id": answer_segment["id"]},
        )

        self.assertEqual(response.status_code, 201, response.text)
        child_id = response.json()["id"]
        child = self.sessions.get(child_id)
        self.assertEqual([message["role"] for message in child.messages],
                         ["user", "assistant", "tool", "assistant"])
        self.assertEqual(child.messages[-1], {"role": "assistant",
                                              "content": self.ANSWER})
        self.assertEqual(len(child.segments), 4)

    def test_the_intermediate_reply_of_a_turn_cannot_be_branched(self) -> None:
        parent = self._session()
        intermediate = parent["segments"][1]
        self.assertEqual(intermediate["content"], "let me look")

        response = self.client.post(
            f"/api/sessions/{parent['id']}/branch",
            json={"segment_id": intermediate["id"]},
        )

        self.assertEqual(response.status_code, 400, response.text)


def _board(root: str):
    from webui.board import BoardStore

    board = BoardStore(Path(root) / "board")
    board.load()
    return board


if __name__ == "__main__":
    unittest.main()
