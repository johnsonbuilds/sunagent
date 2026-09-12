"""Harness lineage: validation, family tree, and derivation.

A harness manifest alone is just data; lineage makes it a node in an
experiment history.  This module answers three questions:

* ``validate_lineage``  — is the ``harnesses/`` directory consistent?
* ``format_tree``       — what does the family look like?
* ``derive``            — how do I create the next child, changing as
  little as possible?

Derivation prints a gene-level diff against the parent before writing
the new manifest, which is the manual enforcement of "one mutation at
a time".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agent_runtime.harness import (
    HarnessError,
    HarnessSpec,
    from_dict,
)


DEFAULT_HARNESSES_DIR = Path("harnesses")


def load_all_harnesses(directory: str | Path = DEFAULT_HARNESSES_DIR) -> dict[str, HarnessSpec]:
    """Load every manifest in ``directory`` keyed by id."""
    root = Path(directory)
    if not root.is_dir():
        raise HarnessError(f"harnesses directory not found: {root}")
    specs: dict[str, HarnessSpec] = {}
    for path in sorted(root.glob("*.yaml")):
        spec = _load_manifest(path)
        if spec.id in specs:
            raise HarnessError(
                f"duplicate harness id {spec.id!r} in {path} and "
                f"{specs[spec.id].source}")
        specs[spec.id] = spec
    if not specs:
        raise HarnessError(f"no harness manifests found in {root}")
    return specs


def _load_manifest(path: Path) -> HarnessSpec:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise HarnessError(f"invalid harness YAML in {path}: {exc}") from exc
    spec = from_dict(data or {})
    object.__setattr__(spec, "source", str(path))
    return spec


def validate_lineage(specs: dict[str, HarnessSpec]) -> list[str]:
    """Return a list of problems; an empty list means the lineage is sound.

    Checks: id matches filename, parents exist, no cycles, and the
    declared mutation plausibly matches the actual gene diff.
    """
    problems: list[str] = []

    for spec in specs.values():
        assert spec.source is not None
        if Path(spec.source).stem != spec.id:
            problems.append(
                f"{spec.id}: manifest id does not match filename "
                f"{Path(spec.source).name}")

    for spec in specs.values():
        visited: set[str] = set()
        node: HarnessSpec | None = spec
        while node is not None and node.parent is not None:
            if node.id in visited:
                problems.append(f"{spec.id}: lineage contains a cycle at {node.id}")
                break
            visited.add(node.id)
            parent = specs.get(node.parent)
            if parent is None:
                problems.append(
                    f"{node.id}: parent {node.parent!r} not found in harnesses/")
                break
            node = parent

    for spec in specs.values():
        if spec.parent is None:
            continue
        parent = specs.get(spec.parent)
        if parent is None:
            continue  # already reported above
        changed = changed_genes(parent, spec)
        if not changed:
            problems.append(
                f"{spec.id}: declares mutation {spec.mutation!r} but genes are "
                f"identical to parent {parent.id}")
        elif len(changed) > 1:
            names = ", ".join(gene for gene, _ in changed)
            problems.append(
                f"{spec.id}: mutation {spec.mutation!r} touches {len(changed)} "
                f"genes ({names}); prefer one gene per mutation")

    return problems


def flatten_genes(spec: HarnessSpec) -> dict[str, Any]:
    """Flatten gene values to dotted paths for diffing."""
    flat: dict[str, Any] = {}

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                walk(f"{prefix}.{key}" if prefix else str(key), item)
        else:
            flat[prefix] = value

    for gene, value in spec.genes_dict().items():
        walk(gene, value)
    return flat


def changed_genes(parent: HarnessSpec, child: HarnessSpec) -> list[tuple[str, int]]:
    """Top-level genes that differ between parent and child."""
    flat_parent = flatten_genes(parent)
    flat_child = flatten_genes(child)
    changed: dict[str, int] = {}
    for path in sorted(set(flat_parent) | set(flat_child)):
        if flat_parent.get(path) != flat_child.get(path):
            gene = path.split(".", 1)[0]
            changed[gene] = changed.get(gene, 0) + 1
    return sorted(changed.items())


def format_gene_diff(parent: HarnessSpec, child: HarnessSpec) -> str:
    """Human-readable field-level diff between two harness specs."""
    flat_parent = flatten_genes(parent)
    flat_child = flatten_genes(child)
    lines = []
    for path in sorted(set(flat_parent) | set(flat_child)):
        before, after = flat_parent.get(path), flat_child.get(path)
        if before == after:
            continue
        lines.append(f"  {path}: {_short(before)} -> {_short(after)}")
    return "\n".join(lines) if lines else "  (no gene changes)"


def _short(value: Any, limit: int = 60) -> str:
    text = repr(value)
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


def format_tree(specs: dict[str, HarnessSpec]) -> str:
    """Render the harness family tree, parents before children."""
    children: dict[str | None, list[HarnessSpec]] = {}
    for spec in specs.values():
        children.setdefault(spec.parent, []).append(spec)
    for row in children.values():
        row.sort(key=lambda spec: spec.id)

    lines: list[str] = []

    def render(spec: HarnessSpec, depth: int) -> None:
        indent = "  " * depth
        mutation = spec.mutation or "(root)"
        parent_hint = f" <- {spec.parent}" if spec.parent else ""
        lines.append(f"{indent}{spec.id}{parent_hint}  {mutation}")
        for child in children.get(spec.id, []):
            render(child, depth + 1)

    roots = children.get(None, [])
    if not roots and specs:
        # Cyclic or orphaned: still show every node rather than nothing.
        for spec in sorted(specs.values(), key=lambda s: s.id):
            lines.append(f"{spec.id}  (unreachable root, parent={spec.parent})")
    for root in roots:
        render(root, 0)
    return "\n".join(lines)


_LIST_FIELDS = {"tools.enabled", "skills"}


def _parse_set_value(path: str, raw: str, current: Any) -> Any:
    if path in _LIST_FIELDS or isinstance(current, list):
        items = [item.strip() for item in raw.split(",") if item.strip()]
        return items or []
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw


def _apply_set(spec: HarnessSpec, path: str, value: Any) -> HarnessSpec:
    """Return a copy of ``spec`` with the dotted ``path`` set to ``value``."""
    genes = spec.genes_dict()
    node: Any = genes
    parts = path.split(".")
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            raise HarnessError(f"unknown gene path: {path}")
        node = node[part]
    leaf = parts[-1]
    if not isinstance(node, dict) or leaf not in node:
        raise HarnessError(f"unknown gene path: {path}")
    node[leaf] = value
    return from_dict({
        "id": spec.id,
        "parent": spec.parent,
        "mutation": spec.mutation,
        "reason": spec.reason,
        **genes,
    })


def derive(parent: HarnessSpec, child_id: str, mutation: str, reason: str | None,
           sets: list[tuple[str, str]]) -> tuple[HarnessSpec, str]:
    """Build a child harness from ``parent`` plus dotted ``sets``.

    Returns the new spec and the gene diff text; the caller decides
    whether to write it to disk.
    """
    if not child_id.strip():
        raise HarnessError("child id must be non-empty")
    child = parent
    for path, raw in sets:
        current = flatten_genes(child).get(path, None)
        child = _apply_set(child, path, _parse_set_value(path, raw, current))
    rebuilt = from_dict({
        "id": child_id,
        "parent": parent.id,
        "mutation": mutation,
        "reason": reason,
        **child.genes_dict(),
    })
    return rebuilt, format_gene_diff(parent, rebuilt)


def manifest_text(spec: HarnessSpec) -> str:
    """Serialize a spec back to manifest YAML in canonical field order."""
    data: dict[str, Any] = {
        "id": spec.id,
        "parent": spec.parent,
        "mutation": spec.mutation,
        "reason": spec.reason,
        "prompt": {"system": spec.prompt.system,
                   "iteration_limit_notice": spec.prompt.iteration_limit_notice},
        "tools": {"enabled": list(spec.tools.enabled)},
        "control": {"max_iterations": spec.control.max_iterations},
        "memory": {"strategy": spec.memory.strategy},
        "recovery": {
            "tool_error": spec.recovery.tool_error,
            "tool_error_max_retries": spec.recovery.tool_error_max_retries,
            "llm_errors": {
                category: {"max_retries": policy.max_retries,
                           "backoff": policy.backoff,
                           "base_delay": policy.base_delay}
                for category, policy in sorted(spec.recovery.llm_errors.items())
            },
        },
        "verification": {"enabled": spec.verification.enabled,
                         "mode": spec.verification.mode,
                         "require": list(spec.verification.require),
                         "rerun_declared_command":
                             spec.verification.rerun_declared_command},
        "skills": list(spec.skills),
    }
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)


__all__ = [
    "DEFAULT_HARNESSES_DIR", "changed_genes", "derive", "flatten_genes",
    "format_gene_diff", "format_tree", "load_all_harnesses", "manifest_text",
    "validate_lineage",
]
