# SunAgent

> A modular AI Agent runtime implementation built from scratch for understanding, experimenting, and extending modern AI Agent architectures.

SunAgent is an open-source project that explores the core components behind AI Agent systems.

Instead of treating agents as black-box applications, this project focuses on understanding and implementing the underlying runtime mechanisms:

- Agent Loop
- Tool Calling
- Context Management
- Memory
- Planning
- Execution Runtime
- Reliability Engineering
- Observability


## Why This Project?

Modern AI Agents are moving from short-lived interactions toward long-running autonomous workflows.

The main challenge is no longer only model capability, but also:

- How agents manage state
- How agents execute actions reliably
- How agents recover from failures
- How agents observe and update their environment
- How developers build trustworthy agent systems


This project aims to build a modular Agent Runtime from first principles, where each component can be understood, tested, and extended independently.


## Project Structure


### docs

Contains usage guides for the main subsystems (harness manifest & lineage, user-facing events & CLI renderer).

It explains the "why" behind the Agent Runtime design, including core concepts, implementation decisions, and engineering trade-offs.

### src

Contains the core modular implementation of the Agent Runtime.

Each component is designed as an independent and reusable module: agent loop (`agent/`), tools (`tools/`), harness manifests (`harness.py`), tracing (`trace.py`), user-facing events (`events.py`), CLI channel (`channels/`), executors (`execution/`), LLM providers (`providers/`), skills (`skills.py`), and benchmark integration (`integrations/`, `evaluation.py`).

### harnesses

Contains declarative agent configuration files (YAML) used to define and run
agent setups without changing code. Each harness describes the prompt, enabled
tools, control limits (e.g. `max_iterations`), memory strategy, recovery
behavior, and verification settings. Harnesses can extend others via `parent`,
making it easy to iterate on variants (e.g. `baseline-v0`, `meta-v3`) for
experiments and evaluations.

### evaluation

Contains benchmark task definitions and the records produced by running the
agent harness against them. `terminal_bench` and `swe_bench` hold the two
benchmark setups (each with its own readme), `records` stores timestamped JSON
result files for each evaluation run, and `analysis` holds the written
per-run comparisons — useful for comparing agent configurations over time.

### scripts

Helper scripts around the runtime: benchmark runner with Docker image GC
(`run_with_image_gc.py`), standalone LLM diagnostics, a deterministic diff
harness for refactors, and log/trace stats analysis.

### skills

Named markdown skill bundles injected into the system prompt via the
harness's `skills` gene (e.g. `coder`).

### memory

Session memory artifact (`session_memory.md`) written by the `llm_summary`
memory strategy into the workspace.

### jobs

Benchmark outputs: one directory per Harbor run (`result.json`, per-task
trials, and the agent's full `agent-runtime.jsonl` trace).

### tests

Contains unit tests and integration tests to verify the correctness and reliability of the runtime components.


## Quick Start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

Copy the environment variable template and fill in your configuration:

```bash
cp .env.example .env
```

Then run:

```bash
uv run python run_agent.py "List the files in the current directory"
```

`run_agent.py` is interactive when no prompt is given. Use `--harness` to run
a manifest from `harnesses/` (file path or id) and `--trace PATH` to append the
run's events as JSONL. If another project's virtual environment is active, use
`uv run --active` or activate this project's `.venv` first — the
`VIRTUAL_ENV` warning comes from uv, not the agent runtime.

Harbor lives in the `eval` dependency group, which plain `uv sync` does not
install (a sync without it would remove Harbor from `.venv`). Install it with:

```bash
uv sync --group eval
```

Then run a benchmark through `uv run` so Harbor shares this project's `.venv`
with `agent_runtime`:

```bash
uv run harbor run \
  -d terminal-bench/terminal-bench-2 \
  --agent agent_runtime.integrations.harbor:HarborAgent \
  -l 1 \
  --debug
```

`uv run` puts the project's `.venv` on `sys.path` (where `agent_runtime` is
installed editable), so no `PYTHONPATH` prefix is needed. Full benchmark
workflows (single-task smoke runs, image GC, job outputs) are documented in
`evaluation/terminal_bench/readme.md` and `evaluation/swe_bench/readme.md`.



## RunTrace

The agent loop emits a provider-independent event stream for each run. Keep it
in memory for tests, or append it as JSONL:

```python
from agent_runtime.agent import RunTrace, run_turn

trace = RunTrace(run_id="run-001", output_path="runs/run-001.jsonl")
answer = await run_turn("What's the weather in Singapore?", llm, tools, trace=trace)
```

When a harness is attached, the first JSONL record is a `record_type: harness`
header (harness id + `genes_hash`), so a trace stays attributable to the
configuration that produced it. Every event record then includes `run_id`,
`event_id`, `event_type`, `timestamp`, `iteration`, and `data`.

The event model covers three families:

- spans: `agent.start/end/error`, `llm.start/end/error`, `tool.start/end/error`
- LLM streaming/retry detail: `llm.chunk`, `llm.stream.finish`, `llm.retry`,
  `llm.reasoning_budget`
- run policy events: `memory.{strategy}`, `verification.passed/failed/rerun/aborted`,
  `budget.reminder`, `loop.guard`, `skills.loaded`

Payloads are compact but not empty: counts, names, IDs, durations, statuses,
and errors are always recorded; `llm.start` carries the request messages and
`llm.chunk` carries streamed text, so a full JSONL trace can reproduce what the
model saw and said. Tool arguments and tool results are not stored on tool
spans.

This is the internal/debug stream. The user-facing stream (`EventEmitter` +
`CLIRenderer`, events like `agent.started`, `assistant.delta`, `tool.completed`)
is a separate, channel-oriented model — see
`docs/user-facing-events-and-cli-renderer.md`.

Use `trace.span(name, ...)` to record an operation with explicit start, error,
and end events. The end and error events include `duration_ms`; result metadata
can be added through the yielded metadata dictionary.

## Environment Variables

The project uses an OpenAI-compatible Chat Completions API:

```dotenv
LLM_API_KEY=your-api-key
LLM_BASE_URL=https://api.example.com/v1
MODEL_ID=your-model

```

Supports OpenAI as well as model providers offering compatible interfaces.
Optional knobs (temperature, workspace root, output caps, command guard) are
documented in `.env.example`. The two most relevant to running:

- `AGENT_RUNTIME_HARNESS` — default harness id when `--harness` is omitted
  (also used by the Harbor adapter and `scripts/run_with_image_gc.py`)
- `AGENT_RUNTIME_STREAM` — set to `0` to disable streaming LLM consumption

Keep your actual API keys strictly in your local `.env` file and do not commit them to Git.

## Learning Roadmap

Implemented so far:

* Message history and context management (`full_history`, `compact_observations`, `llm_summary`)
* Tool calling, parameter validation, and a command guard
* Error handling, retries, and backoff policies
* Streaming output
* Harness manifests with lineage (`derive`, `tree`, `record`, `report`)
* Memory, session summaries, and skill injection
* Observability, evaluation, and debugging (RunTrace, user-facing events, Harbor benchmarks)

Still to explore:

* Multi-agent collaboration
* Retrieval augmentation
* Task planning and state machines
* External tool protocols such as MCP

## Project Philosophy

This project follows a simple principle:

> Understand agents by rebuilding the fundamental components from scratch.

By decomposing complex agent systems into independent modules, developers can:

- Learn how modern agents work internally
- Experiment with different architectures
- Reuse components like building blocks
- Build customized agent systems


## Who Is This For?

- Engineers learning AI Agent architecture
- Developers building custom agent systems
- Researchers exploring agent runtime design
- Anyone interested in reliable autonomous AI systems

## License

MIT
