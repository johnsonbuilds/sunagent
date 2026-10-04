# terminal-bench-2-1-tb-v2 Analysis (tb-v2 harness)

> Job: `jobs/terminal-bench-2-1-tb-v2`
> Previous job: `jobs/terminal-bench-2-tb-v1` (see `terminal-bench-2-tb-v1.md`)
> Harness: `tb-v2 (parent tb-v1)`: `max_iterations 80→120`, `budget_reminder.enabled false→true`,
>   `provider_error.max_retries 0→3`, `stream_idle_timeout.max_retries 2→5`,
>   `stream_empty/truncated.max_retries 2→3`
> Job timeouts: `timeout_multiplier=3.0`, `agent_timeout_multiplier=4.0` (new)
> Dataset change (comparison caveat): `terminal-bench-2@c6fc…` → `terminal-bench-2-1@7d7b…`
> Harness confirmed in-trace: first jsonl line reports
>   `tb-v2, control={max_iterations:120, budget_reminder:{enabled:true}}, verification.require=[solution_description,evidence]`

## 1. Effect: 0.494 → 0.596

`jobs/terminal-bench-2-1-tb-v2/result.json`:

- `n_total_trials=89, n_trials(evaluated)=88, n_errors=9, mean=0.5955056179775281`
- `reward 1.0: 53`, `reward 0.0: 35` (listed below), plus 1 excluded trial
- `exception_stats: AgentTimeoutError=8`, `EnvironmentStartTimeoutError=1 (mteb-retrieve__4bQSCjQ)`
- Errors: 11 (all agent-timeout) → 9. Budget-exhausted: 15 (at 80 steps) → 4 (at 120 steps)

Fixed, v1 zero → v2 pass (13 by short name, +`install-windows` renamed `3-11→3.11` = 14 effective):
`build-pov-ray, feal-linear-cryptanalysis, filter-js-from-html, financial-document-processor, mcmc-sampling-stan, mteb-leaderboard, polyglot-c-py, polyglot-rust-c, pytorch-model-cli, pytorch-model-recovery, query-optimize, sam-cell-seg, tune-mjcf (+ install-windows)`.
Category C was nearly eliminated — the `budget_reminder + 120 steps + agent×4` combination worked.
`filter-js-from-html` passed despite `AgentTimeoutError` (work done before the timeout), mirroring v1 `extract-elf`.

Regressions, v1 pass → v2 zero (5, variance/flakiness, not a harness regression):
`headless-terminal, large-scale-text-editing, model-extraction-relu-logits, reshard-c4-data, torch-tensor-parallelism`.
Three of them died on early LLM idle (`torch-tensor 11 steps, model-extraction 22, reshard 4`) and will likely pass on rerun.

Net of the +10pp is harness × agent-timeout-×4 × dataset-drift combined, not harness alone.

## 2. The 35 zeros (same taxonomy as tb-v1: A15 / D9 / B7 / C4)

Method: per-trial counts of `llm.end / tool.end / verification.*` in `agent-runtime.jsonl`,
plus `result.json:exception_info` and `verifier/{test-stdout.txt,ctrf.json}`.

### Category A: LLM stream idle, `no chunk for 120s` (15 — grew from 11)

`cancel-async-tasks(2), circuit-fibsqrt(2), distribution-search(1), dna-assembly(19), count-dataset-tokens(44), llm-inference-batching-scheduler(5), model-extraction-relu-logits(22), raman-fitting(20), regex-chess(3), reshard-c4-data(4), torch-pipeline-parallelism(49), torch-tensor-parallelism(11), write-compressor(2), path-tracing-reverse(95), path-tracing(121)`.
`max_retries 2→5` delayed death but did not prevent it. Next step is the stream timeout env / model choice, not more retries.

### Category D: internal `verification.passed` but official `reward=0` (9 — unchanged, as expected)

`caffe-cifar-10(62, rerun+passed), dna-insert(53), headless-terminal(42), large-scale-text-editing(61), protein-assembly(89), qemu-alpine-ssh(93), qemu-startup(54), rstan-to-pystan(57), sanitize-git-repo(79)`.
`tb-v2 verification.require` is still `[solution_description,evidence]`, so no movement is expected:

- `caffe-cifar-10`: 5/6 pass, only `test_model_accuracy_verification` fails (test 0.46 vs train 0.55, gap 0.09 > 0.05 limit).
- `dna-insert`: annealed primer segment 55nt over the 45 limit.
- `sanitize-git-repo`: same `SHA d6987af missing` dangling-object failure as v1.
- `qemu-alpine-ssh/qemu-startup`: still `bullseye-security 404 + curl: command not found + uvx: command not found` — Debian bullseye EOL repos, unrelated to the agent.
- `large-scale-text-editing` (4/5) and `headless-terminal` (5/7) each miss by one check (`:wq` macro form; `mkdir /server` already exists).

### Category B: agent timeout even at ×4 (7)

`adaptive-rejection-sampler(7), cobol-modernization(53), extract-moves-from-video(65), gcode-to-text(73), gpt2-codegolf(42), schemelike-metacircular-eval(35), train-fasttext(35)`.
Compile/train-style tasks. Options: split the work, checkpoint artifacts, or raise the agent multiplier again.

### Category C: budget exhausted at 120 steps (4 — down from 15, the biggest win)

`mailman(121), make-doom-for-mips(121), make-mips-interpreter(121), video-processing(121)`,
all with `verification.failed: budget exhausted without a valid submit_result call`.
Remaining long-chain (MIPS/QEMU/mail) tasks need an artifact-existence gate next.

## 3. Next steps

1. Group A is now the largest (15/35): set `AGENT_RUNTIME_STREAM_IDLE_TIMEOUT=120→180/300` (`chat.py:19`) and run this group on a stronger reasoning model; stop adding retries.
2. Group D (9) + 2 near-miss regressions: implement coding auto-detection in `loop.py:_validate_submission` + `verification.py` (`has_source_edit`/`executed_commands`), and an artifact-existence gate for `sol.sql/result.txt/model.bin/:wq`. `caffe/dna-insert/headless/large-scale` are each one check away.
3. `qemu-*` (2): switch the build to `archive.debian.org` and preinstall `curl` — agent-independent.
4. Rerun the 3 early-idle regressions (`torch-tensor-parallelism, model-extraction-relu-logits, reshard-c4-data`); all stopped at ≤22 steps and will likely flip back to 1.
