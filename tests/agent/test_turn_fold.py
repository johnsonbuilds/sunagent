import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.agent.context.turn_fold import turn_fold_messages


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
            {"role": "user", "content": (
                "To finish, call the submit_result tool (and only that tool)")},
            {"role": "assistant", "content": "final1"},
            {"role": "user", "content": "task2"},
        ]

        view = turn_fold_messages(messages)

        self.assertEqual(len(view), 3)
        self.assertEqual(view[0]["content"], "task1")
        self.assertEqual(view[1]["content"], "final1")
        self.assertEqual(view[2]["content"], "task2")


if __name__ == "__main__":
    unittest.main()
