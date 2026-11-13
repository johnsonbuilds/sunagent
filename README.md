# SunAgent

> A small, readable Agent Runtime built from first principles.

SunAgent is a modular, provider-agnostic runtime for tool-calling agents. The engine (loop, tracing, events, executors) stays stable; everything that shapes an agent's behavior — prompt, tools, limits, context, recovery, verification — is versioned data in a YAML **harness manifest**. Change one gene, rerun a benchmark, and attribute the score change to that mutation.

- Any OpenAI-compatible Chat Completions endpoint, with streaming and reasoning replay
- Harness genome with lineage, content hashes, and immutable evaluation records
- Schema-validated tool calls; malformed model output never enters history
- Context strategies (`full_history`, `compact_observations`, `llm_summary`) with output spilling
- Per-category LLM retries, tool-error feedback, and a context-window hard gate
- Optional verification gate with evidence grounding and command re-execution
- Two event streams: internal `RunTrace` JSONL and user-facing `EventEmitter`
- CLI and browser UI over the same runtime; Harbor adapters for benchmarks

## Architecture

```text
CLI · Web UI · Harbor adapter
        │  AgentEvent / frames
Channels (CLIRenderer · WebUIChannel)
        │  run_turn(...)
AgentTurn (loop · context · recovery · verification)
        │
HarnessSpec · LLM provider · Tool registry · Execution
        │
RunTrace JSONL · workspace/
```

`RunTrace` is the compact internal stream for debugging and benchmark analysis. `EventEmitter` is the channel-facing stream that carries live text, tool cards, and thinking blocks. Channels know nothing about the loop; the loop knows nothing about HTTP or terminals.

## Quick Start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env
```

Configure an OpenAI-compatible provider in `.env`:

```dotenv
LLM_API_KEY=your-api-key
LLM_BASE_URL=https://api.example.com/v1
MODEL_ID=your-model
```

Run one prompt, or omit it for interactive mode:

```bash
uv run python run_agent.py "List the files in the current directory"
uv run python run_agent.py --harness tb-v8 "Fix the failing test"
uv run python run_agent.py --trace workspace/traces/demo.jsonl "Inspect the repo"
```

Without `--harness`, the runtime uses `AGENT_RUNTIME_HARNESS`, then `common-v0`.

### Web UI

```bash
# one-time / after front-end changes
cd webui/frontend && npm install && npm run build && cd ../..

# start the FastAPI host
uv run webui --port 8000
```

Open <http://127.0.0.1:8000>. The UI is a Next.js app (`webui/frontend/`)
exported to static files and served by FastAPI. `./run.sh start` builds
the front end automatically when its sources changed, then starts the
server. Sessions and traces live under `workspace/`. The UI can run shell
commands, so set credentials before exposing it:

```dotenv
AGENT_RUNTIME_WEBUI_AUTH_USER=admin
AGENT_RUNTIME_WEBUI_AUTH_PASSWORD=<long random string>
```

Basic auth is not encryption — keep it on localhost, use an SSH tunnel, or terminate TLS in a reverse proxy. `run.sh` wraps local development (`start`, `status`, `logs`, `restart`, `stop`).

## Project Layout

| Path | Contents |
|---|---|
| `src/agent_runtime/agent/` | Turn orchestration, tool loop, context, observations, verification |
| `src/agent_runtime/tools/` | Shell, file, search, patch, code, and symbol tools |
| `src/agent_runtime/harness/` | Genome schema, YAML loader, lineage CLI |
| `src/agent_runtime/execution/` | Local/Harbor executors, workspaces, command guard |
| `src/agent_runtime/` | Events, tracing, memory, skills, storage, channels |
| `webui/` | FastAPI server, session store, static-export hosting |
| `webui/frontend/` | Next.js front end (title bar, sidebar, transcript, composer) |
| `harnesses/` | Versioned agent manifests |
| `evaluation/` | Benchmark tasks, records, analyses |
| `scripts/` | Harbor runner with Docker image GC |
| `docs/` | Subsystem design and usage guides |

## Harnesses

Seven genes define a run: `prompt`, `tools`, `control`, `context`, `recovery`, `verification`, and `skills`. Lineage lives beside them as `id`, `parent`, `mutation`, and `reason`. `genes_hash` covers genes only, so editing `reason` changes nothing behaviorally. Every trace starts with a harness header, keeping each run attributable.

```bash
uv run python -m agent_runtime.harness tree      # family tree + warnings
uv run python -m agent_runtime.harness derive    # create a child
uv run python -m agent_runtime.harness record    # archive a benchmark run
uv run python -m agent_runtime.harness annotate  # tag a failure mode
uv run python -m agent_runtime.harness report    # scores and deltas
```

Example derivation:

```bash
uv run python -m agent_runtime.harness derive code-v17 \
  --from code-v16 \
  --mutation "raise max_iterations for long tasks" \
  --reason "smoke runs hit the iteration cap" \
  --set control.max_iterations=160
```

Never edit an existing harness in place; derive a child so changes stay attributable. See [`docs/harness-manifest-and-lineage.md`](docs/harness-manifest-and-lineage.md).

## Tools

`tools.enabled` selects what the model sees; `read_output` is always available.

| Tool | Purpose |
|---|---|
| `run_command` | One shell command; non-zero exits are normal results |
| `read_file` / `write_file` | Paged reading; full-file creation or rewrite |
| `edit_file` / `apply_patch` | One unique match, or atomic multi-block edits |
| `list_dir` / `read_output` | List directories; page spilled observations |
| `grep_search` / `find_files` | Regex content search; wildcard discovery |
| `find_symbol` / `find_references` | Tree-sitter Python symbol lookup and usages |
| `execute_code` | Run a complete Python, Bash, R, or Node script |
| `submit_result` | Structured finish signal, validated under verification harnesses |

Scripts are saved under `.scripts/`; long observations spill to `.outputs/`.

## Context, Memory, Skills

Context strategies are `full_history`, `compact_observations` (deterministic folding over budget), and `llm_summary` (structured LLM snapshot plus a verbatim recent tail). Set `AGENT_RUNTIME_MEMORY=1` to inject a `workspace/memory/` listing; the agent maintains it with file tools. A harness declaring `skills: [coder]` loads `skills/coder/SKILL.md` into the prompt.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `LLM_API_KEY` / `LLM_BASE_URL` / `MODEL_ID` | — | provider connection |
| `AGENT_RUNTIME_MODEL_CATALOG` | `./model.json` | per-model endpoints for the Web UI picker |
| `AGENT_RUNTIME_HARNESS` | `common-v0` | default harness |
| `AGENT_RUNTIME_HOME` | `<repo>/workspace` | sessions, traces, runtime state |
| `AGENT_RUNTIME_WORKSPACE` | cwd | agent sandbox for file tools and scripts |
| `AGENT_RUNTIME_STREAM` | `1` | set `0` to disable streaming |
| `AGENT_RUNTIME_REASONING_ECHO` | `auto` | `auto`, `always`, or `never` |
| `AGENT_RUNTIME_COMMAND_GUARD` | `blocklist` | `off`, `blocklist`, or `allowlist` |
| `AGENT_RUNTIME_MEMORY` | off | set `1` for persistent memory |
| `AGENT_RUNTIME_WEBUI_HOST` / `PORT` | `127.0.0.1` / `8000` | Web UI bind address |

See [`.env.example`](.env.example) for all knobs. Keep real keys out of Git.

## Evaluation

```bash
uv sync --group eval

uv run harbor run \
  -d terminal-bench/terminal-bench-2 \
  --agent agent_runtime.integrations.harbor:HarborAgent \
  -l 1 --debug
```

For longer runs, use the image-GC wrapper:

```bash
uv run python scripts/run_with_image_gc.py \
  --dataset terminal-bench/terminal-bench-2-1 \
  --harness tb-v8 --job-name tb2-1-tb-v8 -n 4 --timeout-multiplier 3
```

Then archive and compare:

```bash
uv run python -m agent_runtime.harness record jobs/<job-name> \
  --model your-model --notes "what changed"
uv run python -m agent_runtime.harness report
```

Details: [`evaluation/terminal_bench/readme.md`](evaluation/terminal_bench/readme.md) and [`evaluation/swe_bench/readme.md`](evaluation/swe_bench/readme.md).

## Observability and Tests

`RunTrace` records spans (`agent`, `llm`, `tool`) and policy events (context, retries, verification, budgets, guards, skills). Each record carries `run_id`, `event_id`, `event_type`, `timestamp`, `iteration`, and `data`; tool arguments and results are intentionally not stored. Traces default to `workspace/traces/`.

```python
from agent_runtime.agent import RunTrace, run_turn

trace = RunTrace(run_id="run-001", output_path="workspace/traces/run-001.jsonl")
answer = await run_turn("What's the weather in Singapore?", llm, tools, trace=trace)
```

```bash
uv run pytest -q
uv run pytest tests/agent -q   # one area while iterating
```

## Docs

[`harness-manifest-and-lineage.md`](docs/harness-manifest-and-lineage.md) · [`user-facing-events-and-cli-renderer.md`](docs/user-facing-events-and-cli-renderer.md) · [`webui.md`](docs/webui.md) · [`storage.md`](docs/storage.md) · [`agent-runtime-architecture-review.md`](docs/agent-runtime-architecture-review.md)

## Contributing

This repository is maintained by humans and AI coding agents. Coding rules and principles live in [`agent.md`](agent.md).

## License

Apache 2.0
