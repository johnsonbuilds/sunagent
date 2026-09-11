# Lab 006: Meta-Tools (Search, Symbols, Todo, Patch)

## Purpose

This lab exercises the six meta-tools introduced to close the gaps
identified in `docs/concepts/002.md` (code search, planning, and batched
editing):

| Tool | Module | One-liner |
|---|---|---|
| `grep_search` | `agent_runtime/tools/search.py` | Regex line search with structured `path/line/preview` matches |
| `find_files` | `agent_runtime/tools/search.py` | Recursive filename matching in one call |
| `find_symbol` | `agent_runtime/tools/symbols.py` | Tree-sitter symbol lookup (fault-tolerant) |
| `find_references` | `agent_runtime/tools/symbols.py` | All identifier usages, excluding definitions |
| `todo_write` | `agent_runtime/tools/todo.py` | Structured task list persisted to `.todo.json` |
| `apply_patch` | `agent_runtime/tools/patch.py` | Batched SEARCH/REPLACE edits, all-or-nothing |

Verification is **end-to-end through the real agent loop**
(`run_agent.py` → LLM → tool calls → observations): every case is a
natural-language prompt driven by the `meta-v1` harness, run inside a
sandbox workspace, and followed by a scripted check of the resulting
workspace state — not a direct call into the tool registry.

## Quick Start

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and a `.env`
with `LLM_API_KEY` / `LLM_BASE_URL` / `MODEL_ID`.

```bash
uv sync
cp .env.example .env   # then fill in your LLM credentials
```

Run every case (1–6), each against a fresh sandbox:

```bash
bash scripts/e2e_meta_tools.sh
```

Run a single case:

```bash
bash scripts/e2e_meta_tools.sh 6
```

The sandbox lives at `/tmp/agent-runtime-e2e` and the JSONL traces (one
per case) at `/tmp/agent-runtime-e2e-traces/`; watch the CLI renderer
stream the model's reasoning, tool calls, and results live. The script
prints a `PASS`/`FAIL` line per assertion and a summary at the end.

Note: `run_command` now defaults its working directory to the workspace
root (passing `cwd` still overrides), so shell commands and file tools
operate on the same tree — this is what lets CASE 6 run tests inside
the sandbox without path gymnastics.

## The `meta-v1` Harness

`harnesses/meta-v1.yaml` (derived from `code-v3`) enables all twelve
built-in tools and teaches the model when to reach for each: search
first (`grep_search` / `find_files` / `find_symbol`), edit with
`apply_patch` for batched changes, plan with `todo_write`, and fall
back to `execute_code` / `run_command` for execution. Harness genes are
data, so this behavior profile is diffable and versionable like any
other harness.

## What Each Case Verifies

### CASE 1 — grep_search

The model searches for `serve` across the sandbox (source, tests, and
a plain-text note) and must report file:line:content triples. Verifies
the model can drive structured search results instead of shelling out
to `grep`, and that read-only search leaves the workspace untouched.

### CASE 2 — find_files

"List all `.py` files" in one call. Compares against `list_dir`, which
would cost one tool round trip (one LLM turn) per directory level.
Checks the sandbox is unmodified.

### CASE 3 — find_symbol + find_references + read_file

Locate `class User`, find who references it, then read the definition
"page" using the line number the symbol tools returned — the
grep/symbol → `read_file(offset=...)` hand-off that mirrors
`docs/concepts/002.md`'s call chain.

### CASE 4 — todo_write

A three-step task where the model must build a checklist first, update
statuses as it works, and finish with everything `completed`. Checks
`.todo.json` is persisted in the workspace with all items completed and
that the side-task (a typo fix in `notes.txt`) actually happened.

### CASE 5 — apply_patch

One `apply_patch` call that both renames a function **and** creates a
new file. Checks both effects landed in the workspace; the
all-or-nothing property itself (a bad block writes nothing) is covered
by `tests/tools/test_patch.py`.

### CASE 6 — the full tool-chain loop

A realistic bug-fix task: `grep_search` locates `serve`, `read_file`
pulls the exact lines, a patch fixes the return value, then
`run_command` runs the tests — which fail, because the assertion still
expects the old value, forcing the model to fix the test too. This is
`docs/concepts/002.md`'s "fix the bug and make tests pass" scenario
played out with the new meta-tools.

## UnitTest

```bash
uv run -m pytest tests/tools -v
```
