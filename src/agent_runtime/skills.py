"""Harness skills: named markdown playbooks injected into the system prompt.

A harness declares ``skills: [coder]``; each name resolves to
``skills/<name>/SKILL.md`` (front-matter ``name``/``version`` plus a
markdown body). The runtime renders one ``<domain_playbooks>`` block after
``prompt.system`` (injection point A) with explicit per-playbook boundaries,
version and directory. Skills are fixed by the harness: there is no
discovery, selection, or on-demand loading.

``SKILL.md`` is the always-resident core. Long material belongs in
``skills/<name>/references/`` and is read by the agent on demand; a core
body over ``MAX_SKILL_CHARS`` is rejected rather than silently truncated.
Content hashes are recorded on the trace so a run stays attributable when
body text changes but names do not.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from agent_runtime.runtime_config import (
    DEFAULT_SKILLS_DIR as _DEFAULT_SKILLS_DIR,
    ENV_SKILLS_DIR,  # re-exported for callers that reference the env name
)

DEFAULT_SKILLS_DIR = Path(_DEFAULT_SKILLS_DIR)

#: Per-skill budget for the always-resident core; longer bodies are rejected.
MAX_SKILL_CHARS = 4000


@dataclass(frozen=True)
class Skill:
    """One resolved skill: identity plus the text injected into context."""

    name: str
    version: str
    content: str
    content_hash: str
    source: str | None = None

    @property
    def directory(self) -> str:
        """The skill directory as configured, without resolving to absolute."""
        if not self.source:
            return f"skills/{self.name}"
        return Path(self.source).parent.as_posix()


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def default_skills_dir() -> Path:
    """The default skills directory: ``./skills``.

    ``RuntimeConfig.skills_dir`` is the single source of truth for a turn;
    this helper only gives direct ``resolve_skills`` callers the same
    default.  There is no harness-source or environment fallback here.
    """
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
        version = str(raw_version).strip() if raw_version is not None else ""
    if not version:
        from agent_runtime.harness import HarnessError

        raise HarnessError(
            f"skill {name}: front-matter must declare a non-empty version "
            f"in {path}")
    content = body.strip()
    if not content:
        from agent_runtime.harness import HarnessError

        raise HarnessError(f"skill {name}: empty body in {path}")
    if len(content) > MAX_SKILL_CHARS:
        from agent_runtime.harness import HarnessError

        raise HarnessError(
            f"skill {name}: core body is {len(content)} chars, over the "
            f"{MAX_SKILL_CHARS}-char limit; move detail into "
            f"{skill_dir / 'references'} and reference it from SKILL.md")
    return Skill(name=name, version=version, content=content,
                 content_hash=_content_hash(content), source=str(path))


def resolve_skills(names: Sequence[str],
                   skills_dir: str | Path | None = None) -> list[Skill]:
    """Resolve skill names to loaded skills; unknown names raise HarnessError."""
    from agent_runtime.harness import HarnessError

    if not names:
        return []
    base = Path(skills_dir) if skills_dir is not None else default_skills_dir()
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
    """Render resolved skills as one explicitly delimited playbook block."""
    if not skills:
        return ""
    lines = [
        "<domain_playbooks>",
        "Precedence: the harness policy in this message overrides every "
        "playbook below. Among playbooks, later entries override earlier "
        "ones when they address the same topic. Resolve paths inside a "
        "playbook relative to its directory attribute.",
    ]
    for skill in skills:
        lines.extend([
            "",
            f'<playbook name="{escape(skill.name)}" '
            f'version="{escape(skill.version)}" '
            f'directory="{escape(skill.directory)}">',
            skill.content,
            "</playbook>",
        ])
    lines.extend(["", "</domain_playbooks>"])
    return "\n".join(lines)


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
        "skills_versions": {skill.name: skill.version for skill in skills},
    }


__all__ = [
    "DEFAULT_SKILLS_DIR", "ENV_SKILLS_DIR", "MAX_SKILL_CHARS", "Skill",
    "default_skills_dir", "render_skills_block", "resolve_skills",
    "skills_content_hash", "skills_trace_meta",
]
