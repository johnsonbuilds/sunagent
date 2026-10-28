# Where the Project Writes to Disk

Everything the project persists is listed here.  The lists are grouped
by *what the files are for*, because that is the only question that
matters when you want to delete something, back it up, or commit it.

## The one runtime root

Everything the runtime writes at run time lives under a single root:

```
workspace/                     ← AGENT_RUNTIME_HOME (default: <repo>/workspace)
├── sessions/                  one JSON document per browser conversation
│   └── sess-<hex12>.json
├── traces/                    one JSONL file per agent turn
│   └── run-<hex12>.jsonl
└── memory/                    the latest compaction summary
    └── session_memory.md
```

The root is resolved in one place — `src/agent_runtime/storage.py` —
so "where does this write?" always has the same answer.  Set
`AGENT_RUNTIME_HOME` to move the whole tree.

`workspace/` is git-ignored: it is disposable run state.

> Naming note: on-disk `workspace/memory/` is run-state persistence and is
> unrelated to the harness `context` gene (formerly called `memory`).
> The gene shapes the per-request view sent to the LLM; the directory
> holds the latest compaction summary file.

| Artifact | Path | Written by |
|---|---|---|
| Browser conversations | `workspace/sessions/sess-*.json` | `webui/sessions.py` (`SessionStore.save`) |
| Run traces | `workspace/traces/run-*.jsonl` | `src/agent_runtime/trace.py` (`RunTrace._write_events`), via `webui/server.py` and `run_agent.py` |
| Compaction summary | `workspace/memory/session_memory.md` | `src/agent_runtime/agent/context.py` (compaction summary emission; legacy docs said `agent/memory.py`) |

An empty conversation is never written: the file appears with the
first message and disappears with the conversation.  A session that
never received a message exists only in memory.

## The agent sandbox

The agent's own working area is separate from the runtime state on
purpose: the model is told about these paths, so they are part of its
contract, not a side effect.  The root is `AGENT_RUNTIME_WORKSPACE`
(the working directory by default).

| Artifact | Path | Written by |
|---|---|---|
| Spilled tool observations | `<workspace>/.outputs/obs-<call_id>.txt` | `src/agent_runtime/agent/observations.py` (`OutputSpill.write`) |
| Generated scripts | `<workspace>/.scripts/<NNNN>.<ext>` | `src/agent_runtime/tools/code.py` |
| Anything the model writes | any workspace-relative path | `src/agent_runtime/tools/files.py`, `tools/patch.py` → `src/agent_runtime/execution/local.py` |

Both directories are git-ignored, and both are skipped by repo-wide
searches (`SKIP_DIRS` in `src/agent_runtime/tools/search.py`).

## Versioned research artefacts

These are meant to be committed — they are the record of what was
tried and what it scored.

| Artifact | Path | Written by |
|---|---|---|
| Harness manifests | `harnesses/<id>.yaml` | `src/agent_runtime/harness.py` (`derive`), `src/agent_runtime/lineage.py` |
| Evaluation records | `evaluation/records/<id>.json` | `src/agent_runtime/evaluation.py` (`write_record`) |
| Analyses | `evaluation/analysis/*.md` | by hand |
| SWE-bench selections | `evaluation/swe_bench/{dev-50,smoke-10,eval-100}.json` | `evaluation/swe_bench/select_swe_tasks.py` |
| Generated Harbor task trees | `evaluation/swe_bench/dataset-*/<instance>/**` | `evaluation/swe_bench/build_swe_dataset.py` |

Evaluation records are immutable: `write_record` refuses to overwrite an
existing one.

## Outside the project

| Artifact | Path | Written by |
|---|---|---|
| Benchmark jobs and trials | `jobs/<job>/<trial>/**` | Harbor (external tool) |
| Per-trial agent trace | `jobs/<job>/<trial>/agent/agent-runtime.jsonl` | `src/agent_runtime/integrations/harbor.py` |
| Harbor task package cache | `~/.cache/harbor/tasks/packages/**` | Harbor (external tool) |
| Verifier output | `/logs/verifier/` inside the container | `evaluation/swe_bench/build_swe_dataset.py` |

## Environment variables that steer storage

| Variable | Default | Effect |
|---|---|---|
| `AGENT_RUNTIME_HOME` | `<repo>/workspace` | the root of all runtime state |
| `AGENT_RUNTIME_WEBUI_SESSIONS_DIR` | `<home>/sessions` | overrides just the conversation directory |
| `AGENT_RUNTIME_WORKSPACE` | the working directory | the agent's sandbox (`.outputs/`, `.scripts/`, model-written files) |
| `AGENT_RUNTIME_SKILLS_DIR` | `skills/` | where `SKILL.md` bundles are *read* from |
| `AGENT_RUNTIME_SUMMARY_MODEL` | the agent's model | the model that writes the compaction summary |
| `--trace PATH` (CLI) | `<home>/traces/run-<hex12>.jsonl` | where a CLI run writes its trace |
