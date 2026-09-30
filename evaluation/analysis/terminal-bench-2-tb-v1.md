# terminal-bench-2-tb-v1 Analysis (latest job)

> Job: `jobs/terminal-bench-2-tb-v1` | id `db043075-984d-4865-be0d-8bdb12bf6d3d`
> Period: 2026-09-29T06:41:02Z → 2026-09-29T17:21:45Z
> Agent: `agent_runtime.integrations.harbor:HarborAgent`, harness `tb-v1 (parent code-v16)`, `max_iterations=80`
> Model: `deepseek/deepseek-v4.1-flash via OpenRouter` (see `.env: MODEL_ID`)

## 1. Overview (correcting the "0 score" misconception)

`jobs/terminal-bench-2-tb-v1/result.json`:

- `n_total_trials=89, n_completed_trials=89, n_errored_trials=11, n_running=0`
- `mean=0.4943820224719101 = 44/89`
- `reward 1.0: 44`, `reward 0.0: 45`
- `exception_stats.AgentTimeoutError: 11` (10 in the zero-score set + 1 `extract-elf__FgeLQjp` which timed out but was still scored 1)

Conclusion: it is not an overall zero. It is **44 passed / 45 zero**, mean 0.49.

`jobs/terminal-bench-2-tb-v1/config.json`: `timeout_multiplier=3.0`, `lock.json: n_concurrent_trials=4`.

Trial directory structure (same for every trial):

```
<TASK>__<hash>/
  agent/agent-runtime.jsonl  # primary analysis target
  trial.log
  config.json / lock.json / result.json
  verifier/{reward.txt, test-stdout.txt, ctrf.json}
  artifacts/logs/
```

## 2. agent-runtime.jsonl event model

- First line `record_type=harness`: contains `control.max_iterations=80, budget_reminder={enabled:false, at_fractions:[0.25,0.6,0.85]}, verification.require=[solution_description,evidence], tools=[run_command,read_file,edit_file,...execute_code,submit_result]`.
- Main chain: `agent.start → llm.start/llm.chunk/llm.end → tool.start/tool.end → … → verification.{failed,rerun,passed} → agent.end`.
- Counting `tool.end.data.tool` distinguishes `run_command`-dominated vs missing `execute_code`.
- `verification.failed missing=[submit_result] reasons=budget exhausted` = Category C marker.
- `agent.end {status:error, stage:llm, error:'LLM stream produced no chunk for 120 seconds'}` = Category A marker.
- `result.json: exception_info.exception_type=AgentTimeoutError` = Category B marker (outer harbor timeout, stack stuck at `chat.py:_consume_stream → httpx aiter_bytes → asyncio.wait_for`).

Key harness genes (`harnesses/tb-v1.yaml`):

- `control.max_iterations=80, finish_violation_limit=3, budget_reminder.enabled=false` (thresholds are configured but disabled; `loop.py:_maybe_emit_budget_reminder` returns immediately).
- `verification.mode=task_result, require=[solution_description,evidence]` (the default 3 fields have `command_to_verify` cut out; see `harness.py:223`).
- `recovery: provider_error.max_retries=0, stream_idle_timeout/empty/truncated.max_retries=2`.
- `memory.strategy=llm_summary`.
- Prompts: coding tasks fill in `command_to_verify` (already run, exit 0, names a test file); non-coding tasks leave it empty to skip rerun. Classification is left entirely to the model.

Timeout formula (verified — not a flat 15 minutes), `harbor/trial/trial.py:1102 _compute_agent_timeout_sec → 433 _resolve_timeout_sec`:

```python
base = config.agent.override_timeout_sec or task.config.agent.timeout_sec  # TB tasks vary: 900/1200/2400s, None = unlimited
resolved = min(base, max_sec or inf) * (agent_timeout_multiplier or timeout_multiplier)
```

This run had `timeout_multiplier=3.0, agent_timeout_multiplier=None` → ×3.0 took effect:

- `caffe-cifar-10: 05:19:14→06:19:14 = 3600s = 1200×3`
- `count-dataset/gcode/gpt2/tune-mjcf/raman: ~2700s = 900×3`
- `schemelike: 09:19→11:19 = 7200s = 2400×3`
- `llm-batching: 5399s ≈ 5400s = 1800×3`
- `VerifierConfig.timeout_sec` defaults to `600s×3=1800s`.

`AgentConfig.timeout_sec` defaults to `None` (`harbor/models/task/config.py:340`).

## 3. Attribution of the 45 zero-score trials (full-script statistics)

Method: iterate over `jobs/terminal-bench-2-tb-v1/*/agent/agent-runtime.jsonl`, counting `llm.end/tool.end/verification.*`, combined with `result.json:exception_info` and `verifier/{reward.txt,test-stdout.txt,ctrf.json}`. Total: `15 + 11 + 10 + 9 = 45`.

### Category A: LLM stream idle timeout (11)

Signature: `agent.end error='LLM stream produced no chunk for 120 seconds'`, usually with no `verification.*`.

List: `adaptive-rejection-sampler(4 steps), cancel-async-tasks(2), circuit-fibsqrt(2), distribution-search(1), dna-assembly(12), feal-linear-cryptanalysis(2), polyglot-rust-c(1), protein-assembly(15), regex-chess(2), write-compressor(4), path-tracing(81 steps but the tail has the same error, also Category C)`.

Root cause: `tb-v1.yaml: recovery.provider_error.max_retries=0, stream_idle_timeout.max_retries=2` + `chat.py:19 AGENT_RUNTIME_STREAM_IDLE_TIMEOUT=120`. Giving up after 120s×3, durations `~361–557s`. Dying in 1–2 steps is not task difficulty — it is a provider/model stream interruption.

Representative: `path-tracing__HRVJm5B`, official 5/5 FAIL, `FileNotFound: /jail/reconstructed.ppm`; after getting the path wrong the model hung.

### Category B: outer AgentTimeoutError (10 zero-score + 1 passed)

Zero-score list: `caffe-cifar-10(68 steps/3600s), count-dataset-tokens(44/2700s), financial-document-processor(16/3600s), gcode-to-text(55/2700s), gpt2-codegolf(79/2700s), llm-inference-batching-scheduler(53/5400s), pytorch-model-recovery(26/2700s), raman-fitting(56/2700s), tune-mjcf(66/2700s), schemelike-metacircular-eval(7/7200s)`.

Also `extract-elf__FgeLQjp` has the same error but `reward=1`: the work was done and only submit was missing, yet the verifier still passed. Timeout ≠ no work done.

Root cause: compile/train-style tasks (Caffe source + 500 iters, GPT2 training, `count-dataset execute_code 39 times`) exceed `base×3`. `n_concurrent=4` aggravates resource contention. `result.json:agent_result.metadata.agent_runtime.status=failed, error.type=CancelledError → harbor AgentTimeoutError`.

### Category C: budget exhausted at max 80 with no submit (15, the largest group)

List: `build-pov-ray(81), cobol-modernization(81), extract-moves-from-video(81/4892s), install-windows-3-11(81), mailman(81/148 tools), make-doom-for-mips(81), make-mips-interpreter(81/112), mteb-leaderboard(81/126), path-tracing-reverse(81), query-optimize(81), rstan-to-pystan(81), sam-cell-seg(81/3990s), torch-pipeline-parallelism(81), train-fasttext(81/5800s), video-processing(81, execute_code 76 times)`.

Signature: `llm.end=81, verification.failed missing=[submit_result]`. `harness.control.budget_reminder.enabled=false` → 80 steps burned with no reminder.

Official failure examples:

- `query-optimize`: `AssertionError: /app/sol.sql not found`, 4 of 6 checks failed — 81 steps of tuning and the file was never written.
- `sam-cell-seg`: `IsADirectoryError: /app/test_output.csv is a directory`, 6 of 9 failed.
- `train-fasttext`: `FileNotFound: /app/model.bin`, both checks failed.
- `mailman`: `ConnectionRefused` — the service never started, so delivery could not be tested.
- Tool imbalance: `mailman execute_code 0 times, build-pov-ray 3, mteb-leaderboard 2` — everything done by piping `run_command` together, violating the prompt's `execute_code` requirement.

Contrast: the PASS group `build-cython-ext/fix-ocaml-gc/largest-eigenval/winning-avg-corewars` also had no submit yet scored via files landing on disk.

### Category D: internal verification.passed but official reward=0 (9, the most valuable)

List: `dna-insert, filter-js-from-html, mcmc-sampling-stan(verification.passed), mteb-retrieve, polyglot-c-py, pytorch-model-cli(41 steps/5-6 pass), qemu-alpine-ssh(passed), qemu-startup, sanitize-git-repo(passed)`.

Root cause: `tb-v1` cuts `command_to_verify`, `loop.py:535` treats empty as pass and skips rerun, so the self-check command ≠ the official `test_outputs.py`:

- `mteb-retrieve`: `result.txt=HumanEval…` while the expected output is `MTEB: Massive…` — the wrong dataset was read.
- `pytorch-model-cli`: 5/6 passed; only `test_cli_tool_output` failed on output format.
- `sanitize-git-repo`: 2/3 passed; `test_no_other_files_changed FAIL: SHA d6987af missing` — filter-branch left a dangling object.
- `mcmc`: internal `missing=[]` but official `hierarchical_model.stan` fails to compile.
- `qemu-*`: not an agent error — `bullseye-security 404 + curl/uvx not found`; the Debian bullseye EOL repos are dead. `test.sh:8 curl not found`.

Coding/non-coding note: the current harness does not know the classification (`require` has no `command_to_verify`, empty = pass) and relies entirely on the prompt telling the model to choose (`FOR CODING fill in the command / FOR NON-CODING leave empty`) — this is the direct cause of Category D false passes. It should be inferred from code instead (`has_source_edit`/`executed_commands`, see `verification.py:118,140`): force rerun for coding, and force existence checks for artifact-style outputs (`sol.sql/result.txt/model.bin`).

## 4. Score-improvement checklist (sorted by ROI)

Target: `44/89 (0.494) → 65–70/89 (0.73–0.78)`.

### P0: config changes only (+12~18 points)

1. Turn on budget_reminder at `harnesses/tb-v1.yaml:93`:
```yaml
control:
  budget_reminder: {enabled: true, at_fractions: [0.25, 0.6, 0.85]}
```
Addresses the 15 Category C trials. With it enabled, `loop.py:446` injects `Budget x/80 + Source edit? + write to disk + submit` at steps 20/48/68. Expected +5~6. Optionally raise `max_iterations` from 80→100.

2. Recovery settings in the same file (addresses the 11 Category A trials):
```yaml
provider_error: {max_retries: 0→3}
stream_idle_timeout: {max_retries: 2→5}
stream_empty/truncated: {max_retries: 2→3}
```
Plus env: `AGENT_RUNTIME_STREAM_IDLE_TIMEOUT=120→300` (`chat.py:19`). Expected +6~8.

3. Timeouts and concurrency (addresses the 10 Category B trials): next run with `--agent-timeout-multiplier 2.0 --timeout-multiplier 4.0` or `agent.override_timeout_sec=7200`; `n_concurrent_trials 4→2`. Expected +3~4.

4. qemu bullseye repos (2 free points): at build time `sed deb.debian.org→archive.debian.org` and preinstall `curl/uvx`. `qemu-alpine-ssh/qemu-startup` test.sh will then pass.

### P1: small code changes (+6~10 points)

5. `loop.py:_validate_submission` + `verification.py`: automatic coding detection (addresses the 9 Category D trials):
```python
is_coding = has_source_edit(msgs) or any('pytest|test_' in c for c in executed_commands(msgs))
if is_coding and not command.strip():
    gaps['command_to_verify'] = 'coding task must declare rerun command'
# artifact-style (instruction mentions /app/sol.sql|result.txt|model.bin): missing file → failed immediately
```
Force file-level rerun for coding (`_RERUN_TIMEOUT=600`). Expected +4~5.

6. Force flush to disk before timeout + an artifact-existence gate: `harbor.py:run` catch the timeout and `trace.flush` first; `query-optimize/sol.sql, sam/test_output.csv, train/model.bin` missing → `verification.failed`. `extract-elf` proves that landing files on disk earns points.

7. Model: `MODEL_ID=deepseek/deepseek-v4.1-flash` struggles on long chains (`path-tracing`); switch to a stronger reasoning model for hard tasks. Highest cost, so do it last; expected +2~3.

Execution order: do `1 + 2 + 4` first (three lines of yaml + a mirror source), and immediately see `0.49→0.65`; then do 5 + 3 to push `0.73+`.
