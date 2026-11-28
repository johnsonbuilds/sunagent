# SunAgent

> A small, readable Agent Runtime built from first principles.

SunAgent is a modular, provider-agnostic runtime for tool-calling agents. The engine (loop, tracing, events, executors) stays stable; everything that shapes an agent's behavior — prompt, tools, limits, context, recovery, skills — is versioned data in a YAML **harness manifest**. Change one gene, rerun a benchmark, and attribute the score change to that mutation.

- Any OpenAI-compatible Chat Completions endpoint, with streaming and reasoning replay
- Harness genome with lineage, content hashes, and immutable evaluation records
- Schema-validated tool calls; malformed model output never enters history
- Context strategies (`full_history`, `compact_observations`, `llm_summary`) with output spilling
- Per-category LLM retries, tool-error feedback, and a context-window hard gate
- Two event streams: internal `RunTrace` JSONL and user-facing `EventEmitter`
- Run-level metrics index plus an evaluation page with funnels, drill-down, and model-driven diagnosis
- CLI and browser UI over the same runtime; Harbor adapters for benchmarks

## Architecture

```text
CLI · Web UI · Harbor adapter
        │  AgentEvent / frames
Channels (CLIRenderer · WebUIChannel)
        │  run_turn(...)
AgentTurn (loop · context · recovery)
        │
HarnessSpec · LLM provider · Tool registry · Execution
        │
RunTrace JSONL · metrics/runs.jsonl · workspace/
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
| `src/agent_runtime/agent/` | Turn orchestration, tool loop, context, observations |
| `src/agent_runtime/tools/` | Shell, file, search, patch, code, and symbol tools |
| `src/agent_runtime/harness/` | Genome schema, YAML loader, lineage CLI |
| `src/agent_runtime/execution/` | Local/Harbor executors, workspaces, command guard |
| `src/agent_runtime/` | Events, tracing, run metrics/config, diagnosis, memory, skills, storage, channels |
| `webui/` | FastAPI server, session store, static-export hosting |
| `webui/frontend/` | Next.js front end (title bar, sidebar, transcript, composer) |
| `harnesses/` | Versioned agent manifests |
| `evaluation/` | Benchmark tasks, immutable records, analyses |
| `scripts/` | Harbor runner with Docker image GC |
| `docs/` | Subsystem design and usage guides |

## Harnesses

Six behavior genes define a run: `prompt`, `tools`, `control`, `context`, `recovery`, and `skills`. Manifests also reserve a no-op `verification: {enabled: false}` block for a future gate; it is parsed and versioned but ignored at runtime. Lineage lives beside them as `id`, `parent`, `mutation`, and `reason`. `genes_hash` covers genes only, so editing `reason` changes nothing behaviorally. Every trace starts with a harness header, keeping each run attributable.

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

Scripts are saved under `.scripts/`; long observations spill to `.outputs/`.

## Context, Memory, Skills

Context strategies are `full_history`, `compact_observations` (deterministic folding over budget), and `llm_summary` (structured LLM snapshot plus a verbatim recent tail). Set `AGENT_RUNTIME_MEMORY=1` to inject a `workspace/memory/` listing; the agent maintains it with file tools. The directory lives under the runtime home (`<home>/memory`), not under the sandbox root, so it survives a change of `AGENT_RUNTIME_WORKSPACE`; the prompt spells the paths relative to the sandbox because the file tools reject anything outside it. A harness declaring `skills: [name]` loads `skills/<name>/SKILL.md` (relative to the process working directory by default; `AGENT_RUNTIME_SKILLS_DIR` overrides the directory). Each skill needs front-matter `name` and `version`, and its core body is capped at 4000 characters: put longer material in `skills/<name>/references/` and point to it from `SKILL.md`. The injected block declares playbook boundaries, versions, directories and precedence. No skills ship with the repo today.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `LLM_API_KEY` / `LLM_BASE_URL` / `MODEL_ID` | — | provider connection |
| `AGENT_RUNTIME_MODEL_CATALOG` | `./model.json` | per-model endpoints for the Web UI picker |
| `AGENT_RUNTIME_HARNESS` | `common-v0` | default harness |
| `AGENT_RUNTIME_HOME` | `<repo>/workspace` | sessions, traces, runtime state |
| `AGENT_RUNTIME_WORKSPACE` | cwd | agent sandbox for file tools and scripts |
| `AGENT_RUNTIME_STREAM` | `1` | set `0` to disable streaming |
| `AGENT_RUNTIME_COMMAND_GUARD` | `blocklist` | `off`, `blocklist`, or `allowlist` |
| `AGENT_RUNTIME_MEMORY` | off | set `1` for persistent memory |
| `AGENT_RUNTIME_MEMORY_DIR` | `<home>/memory` | where persistent memory lives (keep it inside the sandbox) |
| `AGENT_RUNTIME_WEBUI_HOST` / `PORT` | `127.0.0.1` / `8000` | Web UI bind address |

See [`.env.example`](.env.example) for all knobs. Keep real keys out of Git.

## Evaluation

Every finished run writes two machine-written records: `run.config` (the
conditions) and `run.metrics` (the measurements, appended to
`workspace/metrics/runs.jsonl`). Everything below reads from those plus the
board event stream and `evaluation/records/` — nothing re-parses traces on
demand.

### The evaluation page

`/evaluation` in the Web UI keeps three metric families apart and never
blends them into one success rate:

| View | Endpoint | Source |
|---|---|---|
| Run level | `GET /api/evaluation/overview` | `metrics/runs.jsonl`, `origin in {cli, webui, harbor}` |
| Board outcome | `GET /api/evaluation/board-overview` | `board_events.jsonl` + `board.json` |
| Harbor outcome | `GET /api/evaluation/benchmarks` | `evaluation/records/*.json` |

Filters are `range` (`24h` / `7d` / `30d` / `90d` / `all`), `origin`,
`harness`, and `model`; harness and model options come from the data, so they
only list values that occur. Eight tabs: overview (funnel, acceptance and
clean-run trends, rework reasons, top offenders, data quality), validity
(artifact evidence, VCR, hallucinated completions), process (tool error
kinds, iterations histogram, errors by stage), efficiency (token mix, latency
split, tokens per day, TTFT, queue wait), reliability (errors, cancels,
retries, summary-invalid, compactions), tasks (sortable, paged, filterable),
benchmarks, and diagnosis history.

Run-level KPIs: TCR, tool error rate, schema pass rate (argument validity),
tokens per run and by kind, latency p50/p95, TTFT, retries, summary-invalid
rate, compactions. Board KPIs: TSR, first-pass acceptance, rework rate, queue
wait, VCR. A rate with an empty denominator renders `—`, never `0%`.

Each task drills into `GET /api/evaluation/tasks/{id}` (lifecycle from the
event stream, per-run metrics, evidence state) and each run into
`GET /api/evaluation/runs/{id}` (trace digest: event counts, tool aggregates,
LLM requests/retries/finish reasons, error samples, `run.config` summary — no
payloads).

### Diagnosis

`POST /api/evaluation/diagnose` runs an ordinary turn on the `evaluation-v1`
harness over a compact evidence bundle (task, up to 5 runs, per-iteration
evidence with `event_id`s, environment, human feedback, aggregates). The model
verifies claims against the raw trace with the read-only tools
`read_diagnosis`, `read_trace`, and `search_trace`, then answers with four
fixed headings — Conclusion / Root cause / Evidence / Top 5 improvements — citing `run_id` /
`iteration` / `event_id`. Bundles live in `workspace/diagnosis/<id>/`; reports
are the session's first assistant message. Parse failures are annotated in the
page, never used to reject a report. Diagnosis runs are stored with
`origin=diagnosis` and excluded from the run-level and board aggregates.

### Benchmarks

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

Details: [`docs/evaluation.md`](docs/evaluation.md),
[`evaluation/terminal_bench/readme.md`](evaluation/terminal_bench/readme.md), and
[`evaluation/swe_bench/readme.md`](evaluation/swe_bench/readme.md).

## Observability and Tests

Two recording layers per run:

- **`RunTrace`** (`workspace/traces/<run_id>.jsonl`) — the harness header on
  line 1, a `run.config` record at run start, then spans (`agent`, `llm`,
  `tool`) and policy events (context, retries, budgets, guards, skills), and a
  final `run.metrics` record. Each record carries `run_id`, `event_id`,
  `event_type`, `timestamp`, `iteration`, and `data`; tool arguments and
  results are intentionally not stored.
- **Run metrics index** (`workspace/metrics/runs.jsonl`) — one compact line per
  run: outcome, iterations, tool calls and error kinds, token usage, latency
  breakdown, TTFT, retries, compactions, policy blocks, edited files, error
  stage/code. It carries the join keys (`origin`, `run_id`, `session_id`,
  `task_id`, `run_index`, `harness_id`, `genes_hash`, `model`), so views slice
  by origin, harness, model, or day without touching traces. Readers tolerate
  unknown and missing fields, writes never fail a run, and
  `uv run python scripts/rebuild_metrics.py --write` regenerates the index from
  the traces (dry run without the flag) — deleting `metrics/` loses no
  information.

```python
from agent_runtime.agent import RunTrace, run_turn

trace = RunTrace(run_id="run-001", output_path="workspace/traces/run-001.jsonl")
answer = await run_turn("What's the weather in Singapore?", llm, tools, trace=trace)
```

```bash
uv run pytest -q
uv run pytest tests/agent -q       # one area while iterating
uv run pytest tests/webui -q       # evaluation store, API, and index budgets
```

## Docs

[`harness-manifest-and-lineage.md`](docs/harness-manifest-and-lineage.md) · [`user-facing-events-and-cli-renderer.md`](docs/user-facing-events-and-cli-renderer.md) · [`webui.md`](docs/webui.md) · [`evaluation.md`](docs/evaluation.md) · [`storage.md`](docs/storage.md) · [`agent-runtime-architecture-review.md`](docs/agent-runtime-architecture-review.md)

## Contributing

This repository is maintained by humans and AI coding agents. Coding rules and principles live in [`AGENTS.md`](AGENTS.md).

## License

Apache 2.0
