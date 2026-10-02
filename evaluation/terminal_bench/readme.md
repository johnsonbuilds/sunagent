# Terminal-Bench Evaluation

Run the `sunagent` agent loop on Terminal-Bench through Harbor. The agent runs
inside the task's Docker container, then Harbor runs the task's own verifier;
`reward 1.0` means the task passed.

## Prerequisites

- Python 3.12+, `uv`, Docker.
- Harbor is an `eval` dependency, so always launch through `uv run` (a globally
  installed `harbor` runs in a different environment without `agent_runtime`).
- Configure the OpenAI-compatible provider in the project root `.env`:

```dotenv
LLM_API_KEY=your-api-key
LLM_BASE_URL=https://your-compatible-provider.example/v1
MODEL_ID=your-model
```

## Commands

### Full Terminal-Bench 2.0 run

```bash
uv run python scripts/run_with_image_gc.py \
  --dataset terminal-bench/terminal-bench-2 \
  --harness tb-v1 --job-name terminal-bench-2-tb-v1 -n 4 \
  --timeout-multiplier 3
```

### Smoke run (recommended before a full batch)

```bash
# First 3 tasks only
uv run python scripts/run_with_image_gc.py \
  --dataset terminal-bench/terminal-bench-2 \
  --harness tb-v1 --job-name tb2-smoke -n 1 -l 3

# A single task by name
uv run python scripts/run_with_image_gc.py \
  --dataset terminal-bench/terminal-bench-2 \
  --harness tb-v1 --job-name tb2-one -n 1 -i extract-elf
```

`scripts/run_with_image_gc.py` is a superset of `harbor run`: it starts Harbor,
then runs `docker rmi` on each task image once that trial's `result.json` shows
`finished_at` (otherwise repeated full runs fill the disk).

| Flag | Meaning |
|------|---------|
| `--dataset` | Local directory (Harbor `-p`) or registry name (Harbor `-d`, e.g. `terminal-bench/terminal-bench-2`) |
| `--harness` | Sets `AGENT_RUNTIME_HARNESS`; Terminal-Bench uses `tb-v1` (`harnesses/tb-v1.yaml`) |
| `--job-name` | Output directory `jobs/<job-name>/`. Must be unique per run — reusing a name raises a `lock.json` error |
| `-n` | Concurrent trials (default 4) |
| `--timeout-multiplier` | Scales the task timeouts (TB tasks are 900/1200/2400s; `3` makes them 2700/3600/7200s) |
| `--no-gc` | Keep the Docker images instead of removing them after each trial |
| anything else | Forwarded to `harbor run` as-is (`-l` task count, `-i` task name, `--debug`, ...) |

## Outputs

```text
jobs/<job-name>/
├── result.json                 # overall score: mean reward, pass/fail counts
├── config.json                 # resolved job config (timeout_multiplier, ...)
├── lock.json
└── <task>__<hash>/
    ├── result.json             # per-trial status, reward, exception_info
    ├── verifier/reward.txt     # 1 = passed, 0 = failed
    ├── verifier/test-stdout.txt
    ├── verifier/ctrf.json
    ├── agent/agent-runtime.jsonl   # full agent trace: llm/tool/verification events
    └── trial.log
```

Start with `jobs/<job-name>/result.json` for the score, then open the failing
task's `agent-runtime.jsonl` to see where it broke.

Trial states:

- `completed` + `reward 1.0`: passed. `completed` + `reward 0.0`: the agent
  finished but the verifier rejected it.
- exception (e.g. `AgentTimeoutError`): the trial errored, see
  `result.json.exception_info`.
- `cancelled`: interrupted manually, no verifier result.

## Troubleshooting

- **`No module named agent_runtime`**: run from the project root with
  `uv run python scripts/run_with_image_gc.py ...`.
- **Missing OpenAI credentials**: check `.env` in the project root contains a
  real `LLM_API_KEY`, or export `LLM_API_KEY` / `LLM_BASE_URL` / `MODEL_ID`.
- **`lock.json` already exists**: the `--job-name` was used before — pick a new
  name for the new run.
- **Trial runs a long time**: check the last line of
  `<trial>/agent/agent-runtime.jsonl`; `llm.start` means waiting on the model,
  `tool.start` means inspect the command in `trial.log`.
