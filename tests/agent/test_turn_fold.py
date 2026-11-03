import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.agent.context.turn_fold import turn_fold_messages
from agent_runtime.agent.runtime_message import runtime_user_message
from agent_runtime.agent.tool_dispatch import (
    rejected_observation_message,
    tool_observation_message,
)


def _tool_call(name: str = "weather", call_id: str | None = "1") -> dict:
    call = {"type": "function",
            "function": {"name": name, "arguments": "{}"}}
    if call_id is not None:
        call["id"] = call_id
    return call


class TurnFoldTests(unittest.TestCase):
    def test_single_turn_is_identity(self) -> None:
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task1"},
            {"role": "assistant", "content": "thinking",
             "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": "out"},
            {"role": "assistant", "content": "final1"},
        ]

        self.assertIs(turn_fold_messages(messages), messages)

    def test_completed_turn_folds_to_user_plus_final(self) -> None:
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task1"},
            {"role": "assistant", "content": "thinking",
             "tool_calls": [{"id": "1", "type": "function",
                             "function": {"name": "run_command",
                                          "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "1", "content": "output1"},
            {"role": "assistant", "content": "final1 done"},
            {"role": "user", "content": "task2"},
            {"role": "assistant", "content": "thinking2",
             "tool_calls": [{"id": "2"}]},
            {"role": "tool", "tool_call_id": "2", "content": "output2"},
        ]

        view = turn_fold_messages(messages)

        self.assertEqual(
            [(m["role"], m.get("content")) for m in view],
            [("system", "sys"), ("user", "task1"),
             ("assistant", "final1 done"), ("user", "task2"),
             ("assistant", "thinking2"), ("tool", "output2")])
        # Folded final strips tool calls; full history untouched.
        self.assertNotIn("tool_calls", view[2])
        self.assertEqual(len(messages), 8)

    def test_loop_nudges_neither_open_turns_nor_leak(self) -> None:
        messages = [
            {"role": "user", "content": "task1"},
            {"role": "assistant", "content": "work"},
            runtime_user_message(
                "To finish, call the submit_result tool (and only that tool)",
                kind="submit_nudge",
            ),
            {"role": "assistant", "content": "final1"},
            {"role": "user", "content": "task2"},
        ]

        view = turn_fold_messages(messages)

        self.assertEqual(len(view), 3)
        self.assertEqual(view[0]["content"], "task1")
        self.assertEqual(view[1]["content"], "final1")
        self.assertEqual(view[2]["content"], "task2")

    def test_rejected_tool_observation_does_not_open_a_turn(self) -> None:
        rejection = rejected_observation_message(
            _tool_call(),
            "Tool call to weather was rejected and not executed. "
            "missing required argument: location",
        )
        messages = [
            {"role": "user", "content": "task1"},
            {"role": "assistant", "content": "", "tool_calls": [_tool_call()]},
            rejection,
            {"role": "assistant", "content": "final1"},
            {"role": "user", "content": "task2"},
            {"role": "assistant", "content": "final2"},
        ]

        view = turn_fold_messages(messages)

        self.assertEqual(
            [(m["role"], m.get("content")) for m in view],
            [("user", "task1"), ("assistant", "final1"),
             ("user", "task2"), ("assistant", "final2")],
        )

    def test_tool_observation_without_call_id_does_not_open_a_turn(self) -> None:
        observation = tool_observation_message(
            _tool_call(call_id=None), "tool_call_id is required")
        messages = [
            {"role": "user", "content": "task1"},
            {"role": "assistant", "content": "", "tool_calls": [_tool_call()]},
            observation,
            {"role": "assistant", "content": "final1"},
            {"role": "user", "content": "task2"},
            {"role": "assistant", "content": "final2"},
        ]

        view = turn_fold_messages(messages)

        self.assertEqual(
            [(m["role"], m.get("content")) for m in view],
            [("user", "task1"), ("assistant", "final1"),
             ("user", "task2"), ("assistant", "final2")],
        )

    def test_runtime_marker_controls_turn_boundaries(self) -> None:
        messages = [
            {"role": "user", "content": "task1"},
            {"role": "assistant", "content": "work"},
            runtime_user_message(
                "Budget planning for Q4 starts tomorrow.",
                kind="budget_reminder",
            ),
            {"role": "assistant", "content": "final1"},
            {"role": "user", "content": "task2"},
            {"role": "assistant", "content": "final2"},
        ]

        view = turn_fold_messages(messages)

        self.assertEqual(
            [(m["role"], m.get("content")) for m in view],
            [("user", "task1"), ("assistant", "final1"),
             ("user", "task2"), ("assistant", "final2")],
        )

    def test_real_user_budget_message_is_a_turn_boundary(self) -> None:
        messages = [
            {"role": "user", "content": "task0"},
            {"role": "assistant", "content": "final0"},
            {"role": "user", "content": "Budget planning for Q4"},
            {"role": "assistant", "content": "final1"},
            {"role": "user", "content": "task2"},
            {"role": "assistant", "content": "final2"},
        ]

        view = turn_fold_messages(messages)

        self.assertEqual(
            [(m["role"], m.get("content")) for m in view],
            [("user", "task0"), ("assistant", "final0"),
             ("user", "Budget planning for Q4"), ("assistant", "final1"),
             ("user", "task2"), ("assistant", "final2")],
        )


if __name__ == "__main__":
    unittest.main()
