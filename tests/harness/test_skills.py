import asyncio
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.agent.loop import run_turn
from agent_runtime.agent.turn_history import (
    Conversation,
    _ensure_system_prompt,
)
from agent_runtime.harness import HarnessError, from_dict, resolve_harness
from agent_runtime.skills import (
    MAX_SKILL_CHARS,
    render_skills_block,
    resolve_skills,
    skills_content_hash,
    skills_trace_meta,
)
from agent_runtime.tools.tools import ToolRegistry, ToolSpec
from agent_runtime.trace import RunTrace


REPO_ROOT = Path(__file__).parents[2]


async def _noop_handler(**kwargs: object) -> str:
    return "ok"


class FakeLLM:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses
        self.seen: list[list[dict]] = []

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        self.seen.append(messages.copy())
        return self.responses.pop(0)


def _write_skill(directory: Path, name: str, body: str,
                 version: str = "v1") -> Path:
    skill_dir = directory / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(f"---\nname: {name}\nversion: {version}\n---\n\n{body}\n",
                    encoding="utf-8")
    return path


class SkillLoadingTests(unittest.TestCase):
    def test_resolve_coder_from_repo(self) -> None:
        skills = resolve_skills(["coder"], harness=resolve_harness("code-v14"))
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].name, "coder")
        self.assertEqual(skills[0].version, "v1")
        self.assertIn("submit_result", skills[0].content)
        self.assertFalse(skills[0].truncated)

    def test_coder_content_matches_v13_system(self) -> None:
        v13 = resolve_harness("code-v13")
        skills = resolve_skills(["coder"], harness=resolve_harness("code-v14"))
        self.assertEqual(skills[0].content, v13.prompt.system)

    def test_empty_names_resolve_to_empty(self) -> None:
        self.assertEqual(resolve_skills([]), [])
        self.assertEqual(render_skills_block([]), "")

    def test_unknown_skill_raises_harness_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(HarnessError) as caught:
                resolve_skills(["nope"], skills_dir=Path(directory))
            self.assertIn("unknown skill", str(caught.exception))

    def test_name_mismatch_raises(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            skill_dir = Path(directory) / "coder"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: other\nversion: v1\n---\n\nbody\n",
                encoding="utf-8")
            with self.assertRaises(HarnessError):
                resolve_skills(["coder"], skills_dir=Path(directory))

    def test_long_body_truncates_with_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _write_skill(Path(directory), "big", "x" * (MAX_SKILL_CHARS + 10))
            (skill,) = resolve_skills(["big"], skills_dir=Path(directory))
            self.assertTrue(skill.truncated)
            self.assertIn("truncated", skill.content)
            meta = skills_trace_meta([skill])
            self.assertEqual(meta["skills_truncated"], {"big": True})

    def test_content_hash_stable_and_sensitive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _write_skill(Path(directory), "a", "hello")
            first = resolve_skills(["a"], skills_dir=Path(directory))
            again = resolve_skills(["a"], skills_dir=Path(directory))
            self.assertEqual(skills_content_hash(first),
                             skills_content_hash(again))
            _write_skill(Path(directory), "a", "hello!")
            changed = resolve_skills(["a"], skills_dir=Path(directory))
            self.assertNotEqual(skills_content_hash(first),
                                skills_content_hash(changed))


class EnsureSystemPromptTests(unittest.TestCase):
    def test_system_and_block_combine(self) -> None:
        conversation = Conversation()
        _ensure_system_prompt(conversation, "sys", "<skill/>")
        self.assertEqual(conversation.messages,
                         [{"role": "system", "content": "sys\n\n<skill/>"}])

    def test_skill_only_becomes_system(self) -> None:
        conversation = Conversation()
        _ensure_system_prompt(conversation, "", "<skill/>")
        self.assertEqual(conversation.messages,
                         [{"role": "system", "content": "<skill/>"}])

    def test_empty_stays_empty(self) -> None:
        conversation = Conversation()
        _ensure_system_prompt(conversation, "", "")
        self.assertEqual(conversation.messages, [])

    def test_existing_system_not_duplicated(self) -> None:
        conversation = Conversation()
        conversation.append({"role": "system", "content": "x"})
        _ensure_system_prompt(conversation, "y", "z")
        self.assertEqual(conversation.messages,
                         [{"role": "system", "content": "x"}])


class SkillTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_skill_block_reaches_llm_and_trace(self) -> None:
        llm = FakeLLM([{"content": "done", "tool_calls": []}])
        trace = RunTrace(run_id="skill-turn")
        harness = from_dict({"prompt": {"system": ""}, "skills": ["coder"]})
        registry = ToolRegistry(
            [ToolSpec("t", "", {"type": "object", "properties": {}},
                      _noop_handler)])
        answer = await run_turn(
            "hi", llm, registry, harness=harness, trace=trace,
            skills_dir=REPO_ROOT / "skills")
        self.assertEqual(answer, "done")
        system = llm.seen[0][0]
        self.assertEqual(system["role"], "system")
        self.assertIn('<skill name="coder">', system["content"])
        self.assertIn("submit_result", system["content"])
        loaded = [e for e in trace.events if e.event_type == "skills.loaded"]
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].data["skills"], ["coder"])
        self.assertTrue(loaded[0].data["skills_content_hash"])

    async def test_empty_skills_emit_no_event(self) -> None:
        llm = FakeLLM([{"content": "done", "tool_calls": []}])
        trace = RunTrace(run_id="no-skill")
        harness = from_dict({"prompt": {"system": "hello"}})
        registry = ToolRegistry(
            [ToolSpec("t", "", {"type": "object", "properties": {}},
                      _noop_handler)])
        await run_turn("hi", llm, registry, harness=harness, trace=trace)
        self.assertEqual(llm.seen[0][0]["content"], "hello")
        self.assertFalse([e for e in trace.events
                          if e.event_type == "skills.loaded"])


class CodeV14Tests(unittest.TestCase):
    def test_code_v14_moves_v13_system_into_skill(self) -> None:
        v13 = resolve_harness("code-v13")
        v14 = resolve_harness("code-v14")
        self.assertEqual(v14.parent, "code-v13")
        self.assertEqual(v14.prompt.system, "")
        self.assertEqual(list(v14.skills), ["coder"])
        skills = resolve_skills(list(v14.skills), harness=v14)
        self.assertEqual(skills[0].content, v13.prompt.system)
        for gene in ("tools", "control", "memory", "recovery",
                     "verification"):
            self.assertEqual(getattr(v14, gene), getattr(v13, gene))


if __name__ == "__main__":
    unittest.main()
