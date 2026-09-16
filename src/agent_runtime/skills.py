"""Harness skills: named markdown bundles injected into the system prompt.

A harness declares ``skills: [coder]``; each name resolves to
``skills/<name>/SKILL.md`` (front-matter ``name``/``version`` plus a
markdown body). The runtime renders them as ``<skill>`` blocks appended
after ``prompt.system`` (injection point A) and records content hashes
on the trace so a run stays attributable when body text changes but
names do not.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

import yaml

if TYPE_CHECKING:
    from agent_runtime.harness import HarnessSpec

DEFAULT_SKILLS_DIR = Path("skills")

ENV_SKILLS_DIR = "AGENT_RUNTIME_SKILLS_DIR"

#: Per-skill budget; longer bodies are truncated with an explicit marker.
MAX_SKILL_CHARS = 4000

TRUNCATION_MARKER = "\n\n…[truncated: skill body exceeded budget]"


@dataclass(frozen=True)
class Skill:
    """One resolved skill: identity plus the text injected into context."""

    name: str
    version: str
    content: str
    content_hash: str
    source: str | None = None
    truncated: bool = False


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def default_skills_dir(harness: HarnessSpec | None = None) -> Path:
    """Where to look for ``skills/<name>/SKILL.md``.

    Precedence: ``AGENT_RUNTIME_SKILLS_DIR`` env, then ``<harness.source
    parent>/../skills`` (so ``harnesses/*.yaml`` and ``skills/`` stay
    siblings regardless of cwd), then ``skills`` relative to cwd.
    """
    override = os.getenv(ENV_SKILLS_DIR)
    if override:
        return Path(override)
    source = getattr(harness, "source", None)
    if source:
        candidate = Path(source).parent.parent / "skills"
        if candidate.is_dir():
            return candidate
    return DEFAULT_SKILLS_DIR


def _parse_skill_file(path: Path, skill_dir: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    body = text
    version = ""
    name = skill_dir.name
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end == -1:
            from agent_runtime.harness import HarnessError

            raise HarnessError(
                f"skill {name}: unterminated front-matter in {path}")
        raw_header = text[3:end].strip()
        body = text[end + len("\n---"):].lstrip("\n")
        try:
            header = yaml.safe_load(raw_header) or {}
        except yaml.YAMLError as exc:
            from agent_runtime.harness import HarnessError

            raise HarnessError(
                f"skill {name}: invalid front-matter in {path}: {exc}") from exc
        if not isinstance(header, Mapping):
            from agent_runtime.harness import HarnessError

            raise HarnessError(
                f"skill {name}: front-matter must be a mapping in {path}")
        declared = header.get("name")
        if declared is not None and declared != name:
            from agent_runtime.harness import HarnessError

            raise HarnessError(
                f"skill {name}: front-matter name {declared!r} does not "
                f"match directory {path.parent.name!r}")
        raw_version = header.get("version", "")
        version = str(raw_version) if raw_version is not None else ""
    content = body.strip()
    if not content:
        from agent_runtime.harness import HarnessError

        raise HarnessError(f"skill {name}: empty body in {path}")
    truncated = False
    if len(content) > MAX_SKILL_CHARS:
        content = content[:MAX_SKILL_CHARS] + TRUNCATION_MARKER
        truncated = True
    return Skill(name=name, version=version, content=content,
                 content_hash=_content_hash(content), source=str(path),
                 truncated=truncated)


def resolve_skills(names: Sequence[str],
                   skills_dir: str | Path | None = None,
                   harness: HarnessSpec | None = None) -> list[Skill]:
    """Resolve skill names to loaded skills; unknown names raise HarnessError."""
    from agent_runtime.harness import HarnessError

    if not names:
        return []
    base = Path(skills_dir) if skills_dir is not None else default_skills_dir(harness)
    resolved: list[Skill] = []
    seen: set[str] = set()
    for raw in names:
        name = raw.strip() if isinstance(raw, str) else ""
        if not name:
            raise HarnessError("skill entries must be non-empty text")
        if name in seen:
            continue
        seen.add(name)
        skill_file = base / name / "SKILL.md"
        if not skill_file.is_file():
            raise HarnessError(
                f"unknown skill: {name!r} (expected {skill_file})")
        resolved.append(_parse_skill_file(skill_file, base / name))
    return resolved


def render_skills_block(skills: Sequence[Skill]) -> str:
    """Render resolved skills as appended system-prompt blocks."""
    if not skills:
        return ""
    blocks = [f'<skill name="{skill.name}">\n{skill.content}\n</skill>'
              for skill in skills]
    return "\n\n".join(blocks)


def skills_content_hash(skills: Sequence[Skill]) -> str:
    """Short hash over resolved skill contents for trace attribution."""
    canonical = ";".join(f"{skill.name}:{skill.content_hash}"
                         for skill in skills)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def skills_trace_meta(skills: Sequence[Skill]) -> dict[str, Any]:
    """Compact trace metadata describing the resolved skills."""
    return {
        "skills": [skill.name for skill in skills],
        "skills_content_hash": skills_content_hash(skills) if skills else "",
        "skills_truncated": {skill.name: skill.truncated for skill in skills
                             if skill.truncated},
    }


__all__ = [
    "DEFAULT_SKILLS_DIR", "ENV_SKILLS_DIR", "MAX_SKILL_CHARS", "Skill",
    "default_skills_dir", "render_skills_block", "resolve_skills",
    "skills_content_hash", "skills_trace_meta",
]
