"""Code intelligence meta-tools backed by tree-sitter.

Tree-sitter gives IDE-grade symbol extraction with the key property an
agent runtime needs: parsing is fault-tolerant, so a half-edited file
with a syntax error still yields every symbol defined before the error.
(A real LSP server tends to give up on code that does not compile.)

The index is built lazily per query by walking the workspace (via the
shared walker in :mod:`agent_runtime.tools.search`) and parsing each
``.py`` file.  Parsed files are cached keyed by path+size, so repeat
queries only re-parse files that changed.  Grammars are registered per
extension; Python ships today and more ``tree-sitter-*`` language
packs can be added to :data:`GRAMMARS` without other changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import tree_sitter
import tree_sitter_python

from agent_runtime.execution.base import Workspace
from agent_runtime.execution.local import LocalWorkspace
from agent_runtime.tools.spec import ToolSpec
from functools import partial

from .search import MAX_FILE_BYTES, walk_files


GRAMMARS: dict[str, Any] = {".py": tree_sitter_python}

MAX_RESULTS = 200


def _parser_for(extension: str) -> tree_sitter.Parser | None:
    pack = GRAMMARS.get(extension)
    if pack is None:
        return None
    language = tree_sitter.Language(pack.language())
    return tree_sitter.Parser(language)


@dataclass(frozen=True)
class Symbol:
    path: str
    name: str          # qualified, e.g. "AgentTurn.run" or "run_turn"
    kind: str          # "class" | "function" | "method"
    line: int          # 1-based, feeds read_file(offset=...)
    end_line: int


class TreeSitterIndex:
    """Lazy, size-keyed symbol index over one workspace."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self._cache: dict[str, dict[str, Any]] = {}

    async def _sources(self, extension: str) -> dict[str, Any] | list[tuple[str, str, tree_sitter.Tree]]:
        files = await walk_files(self.workspace, ".")
        if isinstance(files, dict):
            return files
        sources: list[tuple[str, str, tree_sitter.Tree]] = []
        for file_entry in files:
            path, size = file_entry["path"], file_entry.get("size", 0)
            if not path.endswith(extension) or size > MAX_FILE_BYTES:
                continue
            cached = self._cache.get(path)
            if cached and cached["size"] == size:
                sources.append((path, cached["text"], cached["tree"]))
                continue
            read = await self.workspace.read_file(path)
            if "error" in read:
                continue
            text = read["content"]
            parser = _parser_for(extension)
            if parser is None:
                continue
            tree = parser.parse(text.encode("utf-8"))
            self._cache[path] = {"size": size, "text": text, "tree": tree}
            sources.append((path, text, tree))
        return sources

    def _symbols(self, path: str, node: tree_sitter.Node, prefix: str,
                 out: list[Symbol]) -> None:
        if node.type in ("class_definition", "function_definition"):
            name_node = node.child_by_field_name("name")
            name = name_node.text.decode("utf-8", "replace") if name_node else ""
            if name:
                kind = ("class" if node.type == "class_definition"
                        else "method" if prefix else "function")
                out.append(Symbol(path, f"{prefix}{name}", kind,
                                  node.start_point[0] + 1,
                                  node.end_point[0] + 1))
            child_prefix = f"{prefix}{name}." if name else prefix
            for child in node.children:
                self._symbols(path, child, child_prefix, out)
        else:
            for child in node.children:
                self._symbols(path, child, prefix, out)

    async def find_symbol(self, name: str, kind: str | None = None) -> dict[str, Any]:
        """Locate definitions by exact short or qualified name."""
        if not name:
            raise ValueError("name must not be empty")
        if kind is not None and kind not in ("class", "function", "method"):
            raise ValueError("kind must be one of class, function, method")
        symbols: list[Symbol] = []
        sources = await self._sources(".py")
        if isinstance(sources, dict):
            return {"name": name, **sources}
        for path, _text, tree in sources:
            self._symbols(path, tree.root_node, "", symbols)
        matches = [symbol for symbol in symbols
                   if symbol.name == name or symbol.name.endswith("." + name)
                   if kind is None or symbol.kind == kind]
        return {
            "name": name,
            "kind": kind,
            "matches": [
                {"path": symbol.path, "name": symbol.name, "kind": symbol.kind,
                 "line": symbol.line, "end_line": symbol.end_line}
                for symbol in matches[:MAX_RESULTS]
            ],
            "match_count": len(matches),
            "truncated": len(matches) > MAX_RESULTS,
        }

    async def find_references(self, name: str) -> dict[str, Any]:
        """Find identifier usages of ``name``, excluding its definitions."""
        if not name:
            raise ValueError("name must not be empty")
        target = name.encode("utf-8")
        references: list[dict[str, Any]] = []
        sources = await self._sources(".py")
        if isinstance(sources, dict):
            return {"name": name, **sources}
        for path, text, tree in sources:
            lines = text.splitlines()
            definition_bytes: set[tuple[int, int]] = set()

            def collect(node: tree_sitter.Node) -> None:
                if node.type in ("class_definition", "function_definition"):
                    name_node = node.child_by_field_name("name")
                    if name_node is not None:
                        definition_bytes.add((name_node.start_byte,
                                              name_node.end_byte))
                for child in node.children:
                    collect(child)

            collect(tree.root_node)
            stack = [tree.root_node]
            while stack:
                node = stack.pop()
                if (node.type == "identifier" and node.text == target
                        and (node.start_byte, node.end_byte) not in definition_bytes):
                    line = node.start_point[0]
                    preview = lines[line].strip() if line < len(lines) else ""
                    references.append({"path": path, "line": line + 1,
                                       "preview": preview})
                stack.extend(node.children)
        references.sort(key=lambda ref: (ref["path"], ref["line"]))
        truncated = len(references) > MAX_RESULTS
        result: dict[str, Any] = {
            "name": name,
            "references": references[:MAX_RESULTS],
            "match_count": min(len(references), MAX_RESULTS),
            "total_matches": len(references),
            "truncated": truncated,
        }
        if truncated:
            result["note"] = "results truncated; raise the limit or narrow the search"
        return result


async def find_symbol(name: str, kind: str | None = None, *,
                      index: TreeSitterIndex | None = None,
                      workspace: Workspace | None = None) -> dict[str, Any]:
    """Tool-bound wrapper; the index is normally injected by the registry."""
    return await (index or TreeSitterIndex(workspace or LocalWorkspace())
                  ).find_symbol(name, kind)


async def find_references(name: str, *,
                          index: TreeSitterIndex | None = None,
                          workspace: Workspace | None = None) -> dict[str, Any]:
    return await (index or TreeSitterIndex(workspace or LocalWorkspace())
                  ).find_references(name)


def find_symbol_spec(index: TreeSitterIndex) -> ToolSpec:
    return ToolSpec("find_symbol",
                    "Find where a Python class or function is defined "
                    "(.py files only), using "
                    "fault-tolerant tree-sitter parsing (works even in "
                    "files with syntax errors). Matches an exact short or "
                    "qualified "
                    "name like 'ClassName.method' and returns "
                    "(path, name, kind, line, end_line) for read_file.",
                    {"type": "object", "properties": {
                        "name": {"type": "string",
                                 "description": "Symbol name (short or "
                                                "qualified)"},
                        "kind": {"type": "string", "enum": ["class", "function",
                                                            "method"],
                                 "description": "Filter by symbol kind"}},
                     "required": ["name"]},
                    partial(find_symbol, index=index))


def find_references_spec(index: TreeSitterIndex) -> ToolSpec:
    return ToolSpec("find_references",
                    "Find identifier usages of a simple Python name across "
                    ".py files (tree-sitter based textual match, up to 200 "
                    "results with a truncated flag), excluding its "
                    "definition sites. Dotted or qualified names are not "
                    "resolved. Returns (path, line, preview) per "
                    "reference.",
                    {"type": "object", "properties": {
                        "name": {"type": "string",
                                 "description": "Simple identifier to look for "
                                                "(Python only; dotted names "
                                                "are not resolved)"}},
                     "required": ["name"]},
                    partial(find_references, index=index))


__all__ = [
    "GRAMMARS", "MAX_RESULTS", "Symbol", "TreeSitterIndex", "find_references",
    "find_symbol", "find_symbol_spec", "find_references_spec",
]
