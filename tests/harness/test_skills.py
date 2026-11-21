import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.agent.turn_factory import run_turn
from agent_runtime.agent.turn_history import (
    Conversation,
    ensure_system_prompt,
)
from agent_runtime.harness import HarnessError, from_dict
from agent_runtime.skills import (
    ENV_SKILLS_DIR,
    MAX_SKILL_CHARS,
    render_skills_block,
    resolve_skills,
    skills_content_hash,
    skills_trace_meta,
)
from agent_runtime.tools.tools import ToolRegistry, ToolSpec
from agent_runtime.trace import RunTrace


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
    def test_resolve_skill_from_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = _write_skill(root, "coder", "close with a final answer",
                                version="v3")
            harness = from_dict({"skills": ["coder"]})
            (skill,) = resolve_skills(["coder"], skills_dir=root,
                                      harness=harness)
        self.assertEqual(skill.name, "coder")
        self.assertEqual(skill.version, "v3")
        self.assertEqual(skill.content, "close with a final answer")
        self.assertEqual(skill.source, str(path))
        self.assertFalse(skill.truncated)

    def test_default_dir_is_harness_source_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "harnesses").mkdir()
            _write_skill(root / "skills", "coder", "sibling body")
            harness = replace(from_dict({"skills": ["coder"]}),
                              source=str(root / "harnesses" / "code-v1.yaml"))
            with patch.dict(os.environ, {ENV_SKILLS_DIR: ""}):
                (skill,) = resolve_skills(["coder"], harness=harness)
        self.assertEqual(skill.content, "sibling body")

    def test_empty_names_resolve_to_empty(self) -> None:
        self.assertEqual(resolve_skills([]), [])
        self.assertEqual(render_skills_block([]), "")

    def test_render_is_plain_text_without_tags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _write_skill(Path(directory), "a", "first body")
            _write_skill(Path(directory), "b", "second body")
            skills = resolve_skills(["a", "b"], skills_dir=Path(directory))
            self.assertEqual(render_skills_block(skills),
                             "first body\n\nsecond body")

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
        inserted = ensure_system_prompt(conversation, "sys", "skill-text")
        self.assertTrue(inserted)
        self.assertEqual(conversation.messages,
                          [{"role": "system", "content": "sys\n\nskill-text"}])

    def test_skill_only_becomes_system(self) -> None:
        conversation = Conversation()
        inserted = ensure_system_prompt(conversation, "", "skill-text")
        self.assertTrue(inserted)
        self.assertEqual(conversation.messages,
                          [{"role": "system", "content": "skill-text"}])

    def test_empty_stays_empty(self) -> None:
        conversation = Conversation()
        inserted = ensure_system_prompt(conversation, "", "")
        self.assertFalse(inserted)
        self.assertEqual(conversation.messages, [])

    def test_existing_system_not_duplicated(self) -> None:
        conversation = Conversation()
        conversation.append({"role": "system", "content": "x"})
        inserted = ensure_system_prompt(conversation, "y", "z")
        self.assertFalse(inserted)
        self.assertEqual(conversation.messages,
                         [{"role": "system", "content": "x"}])


class SkillTurnTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # A rendered memory block would also ride the system message.
        env = patch.dict(os.environ, {"AGENT_RUNTIME_MEMORY": "0"})
        env.start()
        self.addCleanup(env.stop)

    async def test_skill_block_reaches_llm_and_trace(self) -> None:
        llm = FakeLLM([{"content": "done", "tool_calls": []}])
        trace = RunTrace(run_id="skill-turn")
        harness = from_dict({"prompt": {"system": ""}, "skills": ["coder"]})
        registry = ToolRegistry(
            [ToolSpec("t", "", {"type": "object", "properties": {}},
                      _noop_handler)])
        body = "work step by step; close with a final answer"
        with tempfile.TemporaryDirectory() as directory:
            _write_skill(Path(directory), "coder", body)
            answer = await run_turn(
                "hi", llm, registry, harness=harness, trace=trace,
                skills_dir=Path(directory))
        self.assertEqual(answer, "done")
        system = llm.seen[0][0]
        self.assertEqual(system["role"], "system")
        self.assertEqual(system["content"], body)
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


class SkillsGeneTests(unittest.TestCase):
    """``skills`` is a genome field: declarable without repo assets."""

    def test_skills_gene_leaves_other_genes_untouched(self) -> None:
        plain = from_dict({})
        with_skills = from_dict({"skills": ["coder"],
                                 "prompt": {"system": ""}})
        self.assertEqual(list(with_skills.skills), ["coder"])
        self.assertEqual(with_skills.prompt.system, "")
        for gene in ("tools", "control", "context", "recovery"):
            self.assertEqual(getattr(with_skills, gene), getattr(plain, gene))

    def test_skills_change_moves_genes_hash(self) -> None:
        self.assertNotEqual(from_dict({"skills": ["coder"]}).genes_hash,
                            from_dict({}).genes_hash)


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
