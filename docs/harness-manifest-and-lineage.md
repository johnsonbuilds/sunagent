# Harness Manifest, Lineage & Evaluation Records — Usage Guide

## 1. The Idea: Harness as Versioned Data

Everything that decides *how* the agent works — prompt, tools, control
flow, memory, recovery, verification — is a **harness genome**: named
data in a YAML manifest, not code.

```text
Agent Runtime Engine        (stable: loop, trace, events, executors)
        +
HarnessSpec                 (versioned: 7 genes, one YAML per version)
        +
Benchmark                   (judges how well it works)
```

The research question this enables:

> Same runtime, same model, same benchmark — flip **one gene**, and what
> happens to the score?

Seven genes (all extracted from what the code used to hardcode):

| Gene | Fields | baseline-v0 value |
|---|---|---|
| `prompt` | `system`, `iteration_limit_notice` | `""` (no system prompt), hardcoded notice |
| `tools` | `enabled` | `[run_command]` |
| `control` | `max_iterations` | `10` |
| `memory` | `strategy` | `full_history` |
| `recovery` | `tool_error` | `feed_error_and_continue` |
| `verification` | `enabled` | `false` |
| `skills` | list | `[]` |

Lineage metadata lives next to the genes: `id`, `parent`, `mutation`,
`reason`. The `genes_hash` is computed over the genes only — editing
`reason` never changes behavior or the hash, while flipping any gene
field always does.

## 2. Running Under a Harness

```bash
# CLI
python run_agent.py --harness baseline-v0 "list the files"

# or by manifest path
python run_agent.py --harness harnesses/baseline-v0.yaml

# Harbor / Terminal-Bench (read by the HarborAgent adapter)
export AGENT_RUNTIME_HARNESS=baseline-v0
```

Unset means the built-in default (`baseline-v0`). A test enforces that
`harnesses/baseline-v0.yaml` and the built-in default stay in sync.

Every run's trace now leads with a harness header (the first JSONL
line), so any trace file can always be traced back to the exact genome
that produced it:

```json
{"record_type": "harness", "run_id": "run-...",
 "harness_id": "baseline-v0", "genes_hash": "b6bc636c51d80d60",
 "harness": { ...full genome... }}
```

## 3. The CLI

All harness management goes through one module:

```bash
python -m agent_runtime.harness tree      # family tree + lineage warnings
python -m agent_runtime.harness derive    # create a child manifest
python -m agent_runtime.harness record    # archive a benchmark run
python -m agent_runtime.harness annotate  # failure analysis on a record
python -m agent_runtime.harness report    # scores + deltas per harness
```

## 4. `tree`: Inspect the Family

```bash
python -m agent_runtime.harness tree
```

```text
baseline-v0  (root)
  baseline-v1 <- baseline-v0  enable verification
```

`tree` also validates the lineage and prints warnings (exit code 1):

- manifest `id` does not match its filename
- `parent` refers to a manifest that does not exist
- the parent chain contains a cycle
- a child's genes are **identical** to its parent (no-op mutation)
- a declared mutation touches **more than one gene** — prefer one gene
  per mutation so score changes stay attributable

Warnings do not block anything; they exist to catch hand-edited
mistakes. Prefer `derive`, which makes the disciplined path the easy
path.

## 5. `derive`: Create the Next Harness

```bash
python -m agent_runtime.harness derive baseline-v1 \
    --from baseline-v0 \
    --mutation "enable verification" \
    --reason "H0 failure analysis: 6/10 failures were false-success" \
    --set verification.enabled=true
```

`derive` copies all parent genes, applies each `--set PATH=VALUE`
(dotted gene path; YAML-parsed values, comma lists for `tools.enabled`
and `skills`), then shows a **field-level diff** before writing:

```text
deriving baseline-v1 from baseline-v0
  verification.enabled: False -> True

write harnesses/baseline-v1.yaml? [y/N]
```

That confirmation screen is the manual enforcement of "one mutation at
a time" — you see exactly which fields are about to change. Use `--yes`
to skip the prompt in scripts.

Rules enforced on write: the child id must be new, the parent must
exist, and every `--set` path must be a real gene field (`control.timeout`
is rejected, for example).

## 6. `record`: Archive a Benchmark Run

After a Harbor/Terminal-Bench job finishes:

```bash
python -m agent_runtime.harness record jobs/2026-08-18__19-45-39 \
    --model qwen3-coder \
    --notes "first recorded run; fix-git passed"
```

The record is extracted from the job directory — never hand-typed:

- `harness_id` / `genes_hash` are read from the trial's own trace
  header (`agent/agent-runtime.jsonl`)
- per-task status comes from the job's `result.json` and reward stats
- a job that mixes harnesses is rejected (one record = one harness)

Output: one immutable JSON in `evaluation/records/`:

```json
{
  "record_id": "2026-08-18__19-45-39__baseline-v0__terminal-bench-2",
  "harness_id": "baseline-v0",
  "genes_hash": "b6bc636c51d80d60",
  "model": "qwen3-coder",
  "benchmark": "terminal-bench/terminal-bench-2",
  "score": {"passed": 1, "failed": 0, "total": 1, "rate": 1.0},
  "tasks": [
    {"task": "terminal-bench/fix-git", "status": "passed",
     "trace": "jobs/.../agent/agent-runtime.jsonl",
     "failure_mode": null}
  ],
  "git_commit": "b39370c",
  "notes": "first recorded run; fix-git passed"
}
```

Records are append-only: re-running a benchmark creates a new record,
never rewrites an old one. Deleting one is a deliberate `git rm`.

## 7. `annotate`: Failure Analysis

While reviewing a failed task, tag the failure mode so patterns become
visible across records:

```bash
python -m agent_runtime.harness annotate 2026-08-18__19-34-37 fix-git \
    --mode false-success
```

Task references can be a prefix of the task name or trial name; the
record can be referenced by file, full id, or unique prefix. Annotation
is the only mutation allowed on a record — that is its purpose.

Useful mode vocabulary (not enforced): `false-success`,
`false-failure`, `wrong-artifact`, `loop-stuck`, `timeout`,
`env-broken`.

## 8. `report`: Explainable Evolution

```bash
python -m agent_runtime.harness report
```

```text
ID           PARENT       MUTATION              RATE   DELTA
------------------------------------------------------------
baseline-v0  -            -                     100%       -
baseline-v1  baseline-v0  enable verification      -       -
```

Rows follow the family tree (parents first). `RATE` is the judged pass
rate (`passed / (passed + failed)`) of the **latest** record per
harness; `DELTA` compares against the parent's latest rate. `-` means
no record yet. A harness with records but no manifest shows up as
`(manifest missing)` — keep manifests committed.

## 9. The Complete Loop

```text
python -m agent_runtime.harness tree
        ↓
derive child (see the gene diff, change one gene)
        ↓
AGENT_RUNTIME_HARNESS=<id> → run Terminal-Bench via Harbor
        ↓
python -m agent_runtime.harness record jobs/<dir>
        ↓
annotate failures → you are the selection operator
        ↓
python -m agent_runtime.harness report   (which genes actually help?)
```

This is the human-in-the-loop evolution loop: every harness version is
a manifest, every run is a record, every score change is attributable
to a declared mutation. Automating the selection step later replaces
*you* in this diagram — nothing else needs to change.
