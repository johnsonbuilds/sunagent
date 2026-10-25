import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.agent.context_budget import ContextBudget
from agent_runtime.agent import context_mirror as cm


def user(text):
    return {"role": "user", "content": text}


def assistant_tool(call_id, name="run_command", args='{"command": "ls"}'):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": name, "arguments": args}}]}


def tool_msg(call_id, content):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def build_view(obs_chars=1000):
    return [{"role": "system", "content": "sys"},
            user("do the task"),
            assistant_tool("call-0"), tool_msg("call-0", "x" * obs_chars),
            assistant_tool("call-1"), tool_msg("call-1", "y" * obs_chars)]


class RenderTests(unittest.TestCase):
    def test_round_trip_structure(self):
        # An untouched mirror is a guaranteed noop (fast path): the lossy
        # text->view mapping must NOT rewrite the view when sections match.
        view = build_view(50)
        text = cm.render_mirror(view, protect=2, limit_chars=60000)
        result = cm.apply_mirror(text, messages=list(view), view=list(view),
                                 protect=2, limit_chars=60000)
        self.assertEqual(result.status, "rejected_noop")
        self.assertIsNone(result.new_view)

    def test_edited_mirror_stays_legal(self):
        # An actually-edited mirror (body replaced by a note) still maps
        # back to a legal view: no bare tool messages, prefix intact.
        view = build_view(50)
        text = cm.render_mirror(view, protect=2, limit_chars=60000)
        lines = text.splitlines()
        out = []
        skipping = False
        for line in lines:
            if line.startswith("## turn 1 "):
                skipping = True
                out.append(line)
                out.append("summary note: did listing, found nothing new")
                continue
            if skipping and line.startswith("## turn "):
                skipping = False
                out.append(line)
                continue
            if skipping and not line.startswith("#"):
                continue
            out.append(line)
        result = cm.apply_mirror("\n".join(out), messages=list(view),
                                 view=list(view), protect=2,
                                 limit_chars=60000)
        self.assertEqual(result.status, "applied")
        assert result.new_view is not None
        self.assertEqual(result.new_view[:2], view[:2])
        self.assertFalse(any(m.get("role") == "tool"
                             for m in result.new_view))

    def test_empty_body_drops_turn(self):
        view = build_view(50)
        text = cm.render_mirror(view, protect=2)
        # Empty the first editable turn's body.
        keep, rest = text.split("## turn 2", 1)
        text2 = keep + "## turn 2" + rest.split("## turn", 1)[0].rstrip() + "\n"
        # Simpler: drop the body of turn 1 by replacing its content line.
        lines = text.splitlines()
        out = []
        skip_next = False
        for i, line in enumerate(lines):
            if line.startswith("## turn 1 "):
                skip_next = True
                out.append(line)
                continue
            if skip_next and line.startswith("## turn "):
                skip_next = False
                out.append(line)
                continue
            if skip_next and not line.startswith("#"):
                continue
            out.append(line)
        result = cm.apply_mirror("\n".join(out), messages=list(view),
                                 view=list(view), protect=2)
        self.assertEqual(result.status, "applied")
        self.assertLess(len(result.new_view), len(view))

    def test_idempotent_render_readback_is_noop(self):
        # Regression: the workspace file holds last turn's waterline while
        # render embeds this turn's — a raw string compare is always
        # "changed". Metadata must be ignored so an untouched mirror is noop.
        view = build_view(50)
        rendered_v1 = cm.render_mirror(view, protect=2, limit_chars=1000)
        # Simulate next turn: view grew, but the FILE still has old waterline.
        view2 = view + [user("more work " * 10)]
        rendered_v2 = cm.render_mirror(view2, protect=2, limit_chars=1000)
        assert "# budget:" in rendered_v1 and "# budget:" in rendered_v2
        assert rendered_v1 != rendered_v2  # waterline differs
        # The file content (old waterline) vs current render must be noop
        # only when sections match; here the view DID grow (new message),
        # so apply must see the new turn rather than crash.
        res = cm.apply_mirror(rendered_v1, messages=list(view2),
                              view=list(view2), protect=2,
                              limit_chars=1000)
        # rendered_v1 lacks the new turn -> apply drops it; key point is it
        # doesn't error and reports a real delta, not a phantom edit.
        self.assertIn(res.status, ("applied", "rejected_noop",
                                   "rejected_grew"))
        # Untouched mirror: identical sections -> noop guaranteed.
        res2 = cm.apply_mirror(rendered_v2, messages=list(view2),
                               view=list(view2), protect=2,
                               limit_chars=100000)
        self.assertEqual(res2.status, "rejected_noop")

    def test_metadata_only_difference_is_noop(self):
        view = build_view(20)
        a = cm.render_mirror(view, protect=2, limit_chars=1000)
        b = cm.render_mirror(view, protect=2, limit_chars=9999)
        self.assertNotEqual(a.strip(), b.strip())  # waterline differs
        self.assertEqual(cm._strip_metadata(a), cm._strip_metadata(b))
        res = cm.apply_mirror(b, messages=list(view), view=list(view),
                              protect=2, limit_chars=1000)
        self.assertEqual(res.status, "rejected_noop")

    def test_stale_file_after_tool_append_is_noop(self):
        # The production bug: render happens BEFORE the LLM call, then
        # assistant+tool messages append to the view; the file is supposed
        # to lag by exactly that. Diffing the stale file against a FRESH
        # render mistakes the lag for a model edit and rolls back the
        # tool's own observation. The loop must diff against the
        # turn-start render instead.
        view0 = [{"role": "system", "content": "sys"},
                 {"role": "user", "content": "do it"}]
        file_text = cm.render_mirror(view0, protect=2, limit_chars=60000)
        # Simulate: LLM call + tool execution appended two turns.
        view1 = view0 + [
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "run_command",
                                          "arguments": '{"command": "ls"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "a.txt"}]
        # Turn-start baseline comparison -> sections differ ONLY by the
        # expected lag. apply() maps the stale file to a SHORTER candidate;
        # the loop must therefore NOT call apply on a sections-prefix: it
        # must detect "file is a prefix of current view" as noop.
        res = cm.apply_mirror(file_text, messages=list(view1),
                              view=list(view1), protect=2,
                              limit_chars=60000)
        # Stale-file guard: the file is a strict section-prefix of the
        # live view (rendered before this turn's tool calls appended), so
        # it must be a noop, never a rollback of the tool's own output.
        self.assertEqual(res.status, "rejected_noop")
        self.assertIsNone(res.new_view)

    def test_unknown_role_folds_to_user(self):
        text = ("## turn 1 [wizard]\nhello\n\n## turn 2 [assistant]\nworld\n")
        view = [{"role": "system", "content": "s"}, user("t")]
        result = cm.apply_mirror(text, messages=list(view), view=list(view),
                                 protect=2)
        self.assertEqual(result.status, "applied")
        editable = result.new_view[2:]
        self.assertTrue(all(m["role"] in ("user", "assistant") for m in editable))

    def test_no_header_is_noop(self):
        view = [{"role": "system", "content": "s"}, user("t")]
        result = cm.apply_mirror("just some prose", messages=list(view),
                                 view=list(view), protect=2)
        self.assertEqual(result.status, "rejected_noop")

    def test_bare_tool_becomes_note(self):
        view = [{"role": "system", "content": "s"}, user("t"),
                assistant_tool("call-9"), tool_msg("call-9", "out" * 10)]
        text = ("## turn 1 [tool:run_command id=call-9]\nsome output\n")
        result = cm.apply_mirror(text, messages=list(view), view=list(view),
                                 protect=2)
        self.assertEqual(result.status, "applied")
        self.assertFalse(any(m.get("role") == "tool" for m in result.new_view))

    def test_keep_pairing_restores_pair(self):
        view = [{"role": "system", "content": "s"}, user("t"),
                assistant_tool("call-9"), tool_msg("call-9", "out" * 10)]
        text = ("## turn 1 [tool:run_command id=call-9]\n"
                "[KEEP-PAIRING] keep this\n")
        result = cm.apply_mirror(text, messages=list(view), view=list(view),
                                 protect=2)
        self.assertEqual(result.status, "applied")
        roles = [m.get("role") for m in result.new_view]
        self.assertIn("tool", roles)
        kept = result.new_view[-1]
        self.assertEqual(kept.get("tool_call_id"), "call-9")

    def test_keep_pairing_unknown_id_rejected(self):
        view = [{"role": "system", "content": "s"}, user("t")]
        text = "## turn 1 [tool:run_command id=nope]\n[KEEP-PAIRING] x\n"
        result = cm.apply_mirror(text, messages=list(view), view=list(view),
                                 protect=2)
        self.assertEqual(result.status, "rejected_broke_pairing")

    def test_fit_gate_rejects_growth_over_limit(self):
        view = [{"role": "system", "content": "s"}, user("tiny")]
        text = "## turn 1 [user]\n" + ("z" * 5000) + "\n"
        result = cm.apply_mirror(text, messages=list(view), view=list(view),
                                 protect=2, limit_chars=100)
        self.assertEqual(result.status, "rejected_grew")

    def test_shrink_gate_rejects_non_shrink(self):
        view = [{"role": "system", "content": "s"}, user("tiny")]
        text = "## turn 1 [user]\n" + ("z" * 5000) + "\n"
        result = cm.apply_mirror(text, messages=list(view), view=list(view),
                                 protect=2, limit_chars=100000,
                                 edit_gate="shrink")
        self.assertEqual(result.status, "rejected_grew")


class BudgetTests(unittest.TestCase):
    def _budget(self, limit=1000, **kw):
        args = {"limit_chars": limit, "reserve_chars": 200,
                "max_rollback_retries": 2, "protect": 2}
        args.update(kw)
        return ContextBudget(**args)

    def test_ok_below_tiers(self):
        budget = self._budget()
        view = [{"role": "system", "content": "s"}, user("x" * 10)]
        dec = budget.evaluate(view)
        self.assertEqual(dec.action, "ok")

    def test_tier_nudge_fires_once_and_rearms(self):
        budget = self._budget(limit=1000)
        view = [{"role": "system", "content": "s"}, user("x" * 300)]
        first = budget.evaluate(view)
        self.assertEqual(first.action, "nudge_tier")
        second = budget.evaluate(view)
        self.assertEqual(second.action, "ok")  # same tier already fired
        small = [{"role": "system", "content": "s"}, user("x" * 10)]
        budget.evaluate(small)  # compact back below -> re-arm
        third = budget.evaluate(view)
        self.assertEqual(third.action, "nudge_tier")

    def test_urgent_fires_near_wall(self):
        budget = self._budget(limit=1000)
        # strict = 800; urgent band ~ max(80, 2*recent). Big recent obs -> early.
        view = [{"role": "system", "content": "s"}, user("task"),
                assistant_tool("c1"), tool_msg("c1", "o" * 200),
                user("x" * 300)]
        dec = budget.evaluate(view)
        self.assertEqual(dec.action, "nudge_urgent")

    def test_rollback_then_final_then_stop(self):
        budget = self._budget(limit=1000)
        view = build_view(obs_chars=400)  # well over strict=800
        first = budget.evaluate(view)
        self.assertEqual(first.action, "rollback")
        dropped, after = budget.rollback_to_margin(view)
        self.assertGreater(len(dropped), 0)
        # Prefix survives; every remaining tool message keeps its pair.
        self.assertEqual(view[0]["role"], "system")
        pending: set[str] = set()
        for message in view:
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    if call.get("id"):
                        pending.add(call["id"])
            elif message.get("role") == "tool":
                self.assertIn(message.get("tool_call_id"), pending,
                              "bare tool message left without its assistant call")
                pending.discard(message.get("tool_call_id"))
        ledger = budget.ledger_message(dropped, after)
        self.assertEqual(ledger["role"], "user")

        big = build_view(obs_chars=5000)
        budget.evaluate(big)  # retry 2
        dec = budget.evaluate(big)  # retries exhausted -> final
        self.assertEqual(dec.action, "final_turn")
        self.assertEqual(budget.evaluate(big).action, "stop")
        # Compact below, then overflow again -> new cycle: rollback (retries
        # are per-turn-instance state; a fresh budget restarts the ladder).
        budget.evaluate([{"role": "system", "content": "s"}, user("x")])
        fresh = self._budget(limit=1000)
        fresh.n_retries = fresh.max_retries
        fresh.evaluate(big)
        self.assertEqual(fresh.evaluate(big).action, "stop")

    def test_chars_ruler_matches_memory(self):
        from agent_runtime.agent.memory import _message_chars
        from agent_runtime.providers import llm as llm_mod
        orig = llm_mod._reasoning_echo_mode
        llm_mod._reasoning_echo_mode = lambda: "never"  # noqa: E731
        try:
            message = {"role": "assistant", "content": "hi",
                       "reasoning_content": "think" * 100,
                       "tool_calls": [{"id": "1", "type": "function",
                                       "function": {"name": "t",
                                                    "arguments": '{"a": 1}'}}]}
            self.assertEqual(cm.message_chars(message, False),
                             _message_chars(message, False))
        finally:
            llm_mod._reasoning_echo_mode = orig


if __name__ == "__main__":
    unittest.main()
