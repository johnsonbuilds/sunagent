# terminal-bench-2-tb-v1 分析（最新一次 job）

> Job: `jobs/terminal-bench-2-tb-v1` | id `db043075-984d-4865-be0d-8bdb12bf6d3d`
> 周期：2026-09-29T06:41:02Z → 2026-09-29T17:21:45Z
> Agent: `agent_runtime.integrations.harbor:HarborAgent`，harness `tb-v1 (parent code-v16)`，`max_iterations=80`
> 模型：`deepseek/deepseek-v4.1-flash via OpenRouter`（见 `.env: MODEL_ID`）

## 1. 总览（纠正“0分”误解）

`jobs/terminal-bench-2-tb-v1/result.json`：

- `n_total_trials=89, n_completed_trials=89, n_errored_trials=11, n_running=0`
- `mean=0.4943820224719101 = 44/89`
- `reward 1.0: 44个`，`reward 0.0: 45个`
- `exception_stats.AgentTimeoutError: 11个`（10个在0分集 + 1个 `extract-elf__FgeLQjp` 超时但仍判1分）

结论：不是整体0分，是 **44通过 / 45零分**，均值0.49。

`jobs/terminal-bench-2-tb-v1/config.json`: `timeout_multiplier=3.0`，`lock.json: n_concurrent_trials=4`。

Trial 目录结构（每个 trial 通用）：

```
<TASK>__<hash>/
  agent/agent-runtime.jsonl  # 主分析对象
  trial.log
  config.json / lock.json / result.json
  verifier/{reward.txt, test-stdout.txt, ctrf.json}
  artifacts/logs/
```

## 2. agent-runtime.jsonl 事件模型

- 首行 `record_type=harness`：含 `control.max_iterations=80, budget_reminder={enabled:false, at_fractions:[0.25,0.6,0.85]}, verification.require=[solution_description,evidence], tools=[run_command,read_file,edit_file,...execute_code,submit_result]`。
- 主链：`agent.start → llm.start/llm.chunk/llm.end → tool.start/tool.end → … → verification.{failed,rerun,passed} → agent.end`。
- `tool.end.data.tool` 计数可区分 `run_command` 主导 vs `execute_code` 缺失。
- `verification.failed missing=[submit_result] reasons=budget exhausted` = C类标志。
- `agent.end {status:error, stage:llm, error:'LLM stream produced no chunk for 120 seconds'}` = A类标志。
- `result.json: exception_info.exception_type=AgentTimeoutError` = B类标志（外层 harbor 超时，堆栈卡在 `chat.py:_consume_stream → httpx aiter_bytes → asyncio.wait_for`）。

Harness 关键基因（`harnesses/tb-v1.yaml`）：

- `control.max_iterations=80, finish_violation_limit=3, budget_reminder.enabled=false`（配了阈值但关闭，`loop.py:_maybe_emit_budget_reminder` 直接 return）。
- `verification.mode=task_result, require=[solution_description,evidence]`（默认3字段被砍掉 `command_to_verify`，见 `harness.py:223`）。
- `recovery: provider_error.max_retries=0, stream_idle_timeout/empty/truncated.max_retries=2`。
- `memory.strategy=llm_summary`。
- Prompt：coding任务填 `command_to_verify`（已运行、exit 0、命名测试文件），non-coding留空跳过 rerun。分类权完全交给模型。

超时公式（已验证，非15分钟一刀切），`harbor/trial/trial.py:1102 _compute_agent_timeout_sec → 433 _resolve_timeout_sec`：

```python
base = config.agent.override_timeout_sec or task.config.agent.timeout_sec  # TB各任务 900/1200/2400s不等，None=不限
resolved = min(base, max_sec or inf) * (agent_timeout_multiplier or timeout_multiplier)
```

本次 `timeout_multiplier=3.0, agent_timeout_multiplier=None` → ×3.0 已生效：

- `caffe-cifar-10: 05:19:14→06:19:14 =3600s =1200×3`
- `count-dataset/gcode/gpt2/tune-mjcf/raman: ~2700s =900×3`
- `schemelike: 09:19→11:19 =7200s =2400×3`
- `llm-batching: 5399s ≈5400s =1800×3`
- `VerifierConfig.timeout_sec` 默认 `600s×3=1800s`。

`AgentConfig.timeout_sec` 默认 `None`（`harbor/models/task/config.py:340`）。

## 3. 45个零分归因（全量脚本统计）

统计方法：遍历 `jobs/terminal-bench-2-tb-v1/*/agent/agent-runtime.jsonl` 计 `llm.end/tool.end/verification.*`，结合 `result.json:exception_info` 与 `verifier/{reward.txt,test-stdout.txt,ctrf.json}`。总数：`15 + 11 + 10 + 9 = 45`。

### A类：LLM 流空闲超时（11个）

特征：`agent.end error='LLM stream produced no chunk for 120 seconds'`，多无 `verification.*`。

名单：`adaptive-rejection-sampler(4步), cancel-async-tasks(2), circuit-fibsqrt(2), distribution-search(1), dna-assembly(12), feal-linear-cryptanalysis(2), polyglot-rust-c(1), protein-assembly(15), regex-chess(2), write-compressor(4), path-tracing(81步但尾部同错，兼C类)`。

根因：`tb-v1.yaml: recovery.provider_error.max_retries=0, stream_idle_timeout.max_retries=2` + `chat.py:19 AGENT_RUNTIME_STREAM_IDLE_TIMEOUT=120`。120s×3即放弃，时长 `~361–557s`。1–2步早死非任务难，是 provider/模型流中断。

代表：`path-tracing__HRVJm5B` 官方5/5 FAIL，`FileNotFound: /jail/reconstructed.ppm`，路径搞错后模型卡死。

### B类：外层 AgentTimeoutError（零分10个 + 通过1个）

零分名单：`caffe-cifar-10(68步/3600s), count-dataset-tokens(44/2700s), financial-document-processor(16/3600s), gcode-to-text(55/2700s), gpt2-codegolf(79/2700s), llm-inference-batching-scheduler(53/5400s), pytorch-model-recovery(26/2700s), raman-fitting(56/2700s), tune-mjcf(66/2700s), schemelike-metacircular-eval(7/7200s)`。

另 `extract-elf__FgeLQjp` 同错但 `reward=1`：活干完只差 submit，verifier仍过。超时≠没干活。

根因：编译/训练型（Caffe源码+500iter、GPT2训练、`count-dataset execute_code 39次`）超 `base×3`。`n_concurrent=4` 资源争抢加重。`result.json:agent_result.metadata.agent_runtime.status=failed, error.type=CancelledError → harbor AgentTimeoutError`。

### C类：预算耗尽 max80 无 submit（15个，最大头）

名单：`build-pov-ray(81), cobol-modernization(81), extract-moves-from-video(81/4892s), install-windows-3-11(81), mailman(81/148 tools), make-doom-for-mips(81), make-mips-interpreter(81/112), mteb-leaderboard(81/126), path-tracing-reverse(81), query-optimize(81), rstan-to-pystan(81), sam-cell-seg(81/3990s), torch-pipeline-parallelism(81), train-fasttext(81/5800s), video-processing(81, execute_code 76次)`。

特征：`llm.end=81, verification.failed missing=[submit_result]`。`harness.control.budget_reminder.enabled=false`，80步无提醒烧干。

官方失败实例：

- `query-optimize`: `AssertionError: /app/sol.sql not found`，6项挂4项，81步调优没写文件。
- `sam-cell-seg`: `IsADirectoryError: /app/test_output.csv 是目录`，9项挂6项。
- `train-fasttext`: `FileNotFound: /app/model.bin`，2项全挂。
- `mailman`: `ConnectionRefused`，服务没起测投递。
- 工具失衡：`mailman execute_code 0次、build-pov-ray 3次、mteb-leaderboard 2次`，全用 `run_command` 拼管道，违背 prompt `execute_code` 要求。

对照：PASS组 `build-cython-ext/fix-ocaml-gc/largest-eigenval/winning-avg-corewars` 同样无 submit 但靠文件落地得分。

### D类：内部 verification.passed 但官方 reward=0（9个，最有价值）

名单：`dna-insert, filter-js-from-html, mcmc-sampling-stan(verification.passed), mteb-retrieve, polyglot-c-py, pytorch-model-cli(41步/5-6过), qemu-alpine-ssh(passed), qemu-startup, sanitize-git-repo(passed)`。

根因：`tb-v1` 砍 `command_to_verify`，`loop.py:535 空即过跳过rerun`，自检命令≠官方 `test_outputs.py`：

- `mteb-retrieve`: `result.txt=HumanEval…` 期望 `MTEB: Massive…`，读错数据集。
- `pytorch-model-cli`: 5/6过，仅 `test_cli_tool_output` 输出格式错。
- `sanitize-git-repo`: 2/3过，`test_no_other_files_changed FAIL: SHA d6987af missing`，filter-branch 留悬空对象。
- `mcmc`: 内部 `missing=[]` 但官方 `hierarchical_model.stan` 编译失败。
- `qemu-*`: 非Agent错，`bullseye-security 404 + curl/uvx not found`，Debian bullseye EOL源失效。`test.sh:8 curl not found`。

coding/non-coding补充：当前 harness 不知分类（`require` 无 `command_to_verify`，空即过），全靠 prompt 让模型自选（`FOR CODING填命令 / FOR NON-CODING留空`），是D类假通过的直接原因。应改为代码推断（`has_source_edit`/`executed_commands`，见 `verification.py:118,140`），coding强制 rerun，产物型（`sol.sql/result.txt/model.bin`）强制存在检查。

## 4. 提分清单（按ROI排序）

目标：`44/89 (0.494) → 65–70/89 (0.73–0.78)`。

### P0：只改配置（+12~18分）

1. `harnesses/tb-v1.yaml:93` budget_reminder 打开：
```yaml
control:
  budget_reminder: {enabled: true, at_fractions: [0.25, 0.6, 0.85]}
```
对应C类15个。`loop.py:446` 打开后 20/48/68步注入 `Budget x/80 + Source edit? + 落盘+submit`。预期 +5~6。可选 `max_iterations 80→100`。

2. 同文件 recovery（对应A类11个）：
```yaml
provider_error: {max_retries: 0→3}
stream_idle_timeout: {max_retries: 2→5}
stream_empty/truncated: {max_retries: 2→3}
```
+ env：`AGENT_RUNTIME_STREAM_IDLE_TIMEOUT=120→300`（`chat.py:19`）。预期 +6~8。

3. 超时与并发（对应B类10个）：下次 run `--agent-timeout-multiplier 2.0 --timeout-multiplier 4.0` 或 `agent.override_timeout_sec=7200`；`n_concurrent_trials 4→2`。预期 +3~4。

4. qemu bullseye 源（白送2分）：构建时 `sed deb.debian.org→archive.debian.org` + 预装 `curl/uvx`。`qemu-alpine-ssh/qemu-startup` test.sh 即过。

### P1：小代码改动（+6~10分）

5. `loop.py:_validate_submission` + `verification.py`：coding自动判定（对应D类9个）：
```python
is_coding = has_source_edit(msgs) or any('pytest|test_' in c for c in executed_commands(msgs))
if is_coding and not command.strip():
    gaps['command_to_verify'] = 'coding task must declare rerun command'
# 产物型（instruction 提到 /app/sol.sql|result.txt|model.bin）不存在直接 failed
```
coding强制 file-level rerun（`_RERUN_TIMEOUT=600`）。预期 +4~5。

6. 超时前强制落盘 + 产物存在门：`harbor.py:run` catch 超时前 `trace.flush`；`query-optimize/sol.sql、sam/test_output.csv、train/model.bin` 缺失即 `verification.failed`。`extract-elf` 证明落盘即得分。

7. 模型：`MODEL_ID=deepseek/deepseek-v4.1-flash` 长链吃力（`path-tracing`），难任务切强推理模型。成本最高，放最后，预期 +2~3。

执行顺序：`1 + 2 + 4`（三行 yaml + 镜像源）先做，即见 `0.49→0.65`；再做 5 + 3 冲 `0.73+`。
