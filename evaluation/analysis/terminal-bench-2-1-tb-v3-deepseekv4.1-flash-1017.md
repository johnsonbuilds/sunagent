# terminal-bench-2-1-tb-v3-deepseekv4.1-flash-1017 得分为 0 case 分析

> Job: `jobs/terminal-bench-2-1-tb-v3-deepseekv4.1-flash-1017`
> Job ID: `88e36da2-ed1d-4f57-9a82-a918fa0da700`
> Agent: `sunagent` v0.1.0（`agent_runtime.integrations.harbor:HarborAgent`）
> Model: `deepseek/deepseek-v4.1-flash-1017`（OpenRouter，`LLM_TEMPERATURE=0`）
> Harness: `tb-v3 (parent tb-v2)`，`mutation: provider_error.max_retries 3->6, base_delay 0.5->2`
> `timeout_multiplier=3.0`
> 数据来源：各 `<trial>/result.json`（`verifier_result.rewards.reward`、`agent_result.metadata.agent_runtime`）、
> `verifier/{ctrf.json,reward.txt,test-stdout.txt}`、`agent/agent-runtime.jsonl`、`trial.log`、
> `~/.cache/harbor/tasks/packages/terminal-bench/<task>/*/task.toml`（基准超时）

## 0. 总体统计

- 89 个 trial，平均分 **0.7191**
- reward = 1.0：**64** 个；reward = 0.0：**25** 个

25 个 0 分 case 的原因归为 **3 类**：

| 类别 | 数量 | 性质 |
|---|---|---|
| 1. Agent 被 wall-clock 超时杀掉 | 8 | 运行时预算问题（工具超时 / 摘要压缩吃满预算） |
| 2. LLM 流 120s 无首包 → 重试耗尽 → 整个任务判死 | 7 | 运行时/网关问题 |
| 3. Agent 正常完成但 verifier 判定不通过 | 10 | 其中 2 个是题目环境故障（冤案），8 个是真实能力差距 |

**核心结论：15/25（60%）的 0 分与模型能力无关。**

横向对比（同数据集 `terminal-bench-2-1`）：

| Job | Harness / Model | 0 分 | 基础设施类失败 | 真实能力类失败 |
|---|---|---|---|---|
| `terminal-bench-2-1-tb-v2` | tb-v2 / deepseek-v4 | 35 (39.8%) | 22（15 流超时 + 7 超时） | 13 |
| `terminal-bench-2-1-tb-v2-mimo-2.6-pro-ultraspeed` | tb-v2 / mimo-2.6-pro | 25 (28.1%) | **0** | 25 |
| **`terminal-bench-2-1-tb-v3-deepseekv4.1-flash-1017`** | tb-v3 / deepseek-v4.1-flash | **25 (28.1%)** | **15** | 10 |

`tb-v3` 把基础设施类失败从 22 降到 15（`provider_error` 重试 3→6 起效），但 **模型换成 deepseek-v4.1-flash 后仍然有 15 个基础设施失败**；而同一 harness 换 mimo-2.6-pro 则是 **0 个**。说明这 15 个失败是 **deepseek-v4.1-flash 在 OpenRouter 上的特定行为**（首包极慢），不是 harness 本身的问题。

## 1. Agent 被 wall-clock 超时杀掉 —— 8 个

`result.json` 中 `agent_result.metadata.agent_runtime.error = {"type": "CancelledError", "message": ""}`，
且 `agent_execution` 的墙钟时长**恰好等于** `task.toml` 的 `[agent] timeout_sec × 3.0`：

| Case | base timeout | ×3 实际 | 摘要压缩耗时 | 工具长命令 | 迭代数 |
|---|---|---|---|---|---|
| `cobol-modernization` | 900s | 2700s | 1.0 min | — | 56 |
| `count-dataset-tokens` | 900s | 2700s | 0.1 min | 2×901s | 37 |
| `gcode-to-text` | 900s | 2700s | **22.6 min** | 2×120s | 63 |
| `make-doom-for-mips` | 900s | 2700s | 8.9 min | 4×(250~380)s | 39 |
| `large-scale-text-editing` | 1200s | 3600s | 0 min | **51.2 min** | 45 |
| `make-mips-interpreter` | 1800s | 5400s | **46.7 min** | — | 86 |
| `schemelike-metacircular-eval` | 2400s | 7200s | **104.8 min** | — | 28 |
| `train-fasttext` | 3600s | 10800s | 0 min | **176.8 min** | 34 |

> 另有 4 个任务同样被超时杀掉，但因为解已完成而拿了 1.0：
> `filter-js-from-html`(5400s)、`torch-pipeline-parallelism`(2700s)、`torch-tensor-parallelism`(2700s)、`tune-mjcf`(2700s)。
> 即全 job 实际有 **12** 个 trial 撞上 agent 墙钟超时，8 个因此 0 分。

### 1.1 根因一：`memory.llm_summary` 上下文压缩是隐形时间黑洞

`src/agent_runtime/agent/memory.py:378-396`（`_generate_summary`）把**整段历史**用 **非流式 `llm.chat()`** 重新发给模型做摘要，
既没有 idle timeout，也没有任何体积上限。实测单次摘要耗时 **250~700 秒**：

- `schemelike-metacircular-eval`：**15 次摘要吃掉 104.8 / 120 分钟（占预算 87%）**，真正的 agent 推理只剩 ~15 分钟
- `make-mips-interpreter`：46.7 / 90 分钟
- `gcode-to-text`：22.6 / 45 分钟
- `video-processing`（完成但验证失败）：55.2 / 85 分钟
- **全 job：202 次摘要调用 = 4.98 小时纯等待**

而且压缩并不总是生效：`memory.llm_summary` 事件的 `request_chars` 仍有 70k~78k > `DEFAULT_CONTEXT_BUDGET=60k`
（如 `video-processing` 的 `original_chars` 一路涨到 394k），导致下一轮又触发摘要 —— 成本呈 O(n²) 增长。

### 1.2 根因二：工具 `timeout` 参数无上限

`src/agent_runtime/tools/tools.py:92`（`run_command` schema）与 `code.py:223`（`execute_code`）的 `timeout` 是一个无约束数字，
默认 30s，但**模型可以传 600/900/1200 秒**，executor 原样执行：

- `train-fasttext`：17 次长命令（600~1201s），**176.8 / 180 分钟全耗在工具上**，模型反复重跑同一条超时的训练命令
- `large-scale-text-editing`：51.2 / 60 分钟在工具上
- `count-dataset-tokens`：2 次 901s 的 `execute_code`

没有"剩余预算感知"：工具超时与 agent 墙钟预算之间无联动。

### 1.3 排查陷阱：trace 把被取消的任务记成 success

`src/agent_runtime/trace.py:99-109` 的 `span()` 只 `except Exception`，捕不到 `asyncio.CancelledError`（Py3.8+ 继承自 `BaseException`），
于是 `finally` 里 `status = "error" if failed else "success"` 恒为 `success`。
结果这 8 个被超时杀掉的任务，trace 末尾明明写着 `agent.end {status: "success"}`，而 `result.json` 里是 `CancelledError`。
**只看 trace 会得出"agent 正常结束"的错误结论。**

## 2. LLM 流 120s 无首包 → 重试耗尽 → 整个任务判死 —— 7 个

`adaptive-rejection-sampler`、`circuit-fibsqrt`、`feal-linear-cryptanalysis`、`gpt2-codegolf`、
`path-tracing-reverse`、`regex-chess`、`write-compressor`

- 报错统一为 `LLM error: LLM stream produced no chunk for 120 seconds`（`src/agent_runtime/agent/chat.py:143-148`）。
- `AGENT_RUNTIME_STREAM_IDLE_TIMEOUT` 默认 **120s**；`tb-v3` 的 `recovery.llm_errors.stream_idle_timeout.max_retries = 5`，
  即**每次失败重放同一个请求 5 次 = 白等 10 分钟**，然后 `loop.py:273-277` 抛 `_TurnFailure` → **整个 turn 直接结束**，
  此前已完成的 13~40 轮工作全部作废。
- 关键证据：失败迭代里**收到的 chunk 数 = 0**（不是流中断，是**首包 TTFT 超时**）。
  统计全 job 4247 次成功调用的 TTFT：p50=2.4s，p90=18.7s，p99=**119.4s**，max=**708.9s** —— 120s 阈值正好卡在悬崖边。
- **全 job：99 次重试 = 3.3 小时纯等待**；24/89 个 trial 至少触发过一次。
- 5 次重试全部是**同一个请求**（`chat_with_recovery` 原样重放），对于"思考首包本来就 >120s"的请求，重试注定复现同样的失败。

## 3. Agent 正常完成但 verifier 判定不通过 —— 10 个

reward 是 all-or-nothing，差一个子测试就是 0。

### 3.1 题目环境故障（与 agent 无关，冤案）—— 2 个

`qemu-alpine-ssh`、`qemu-startup`

`verifier/test-stdout.txt` 显示**verifier 自己的测试脚本就没跑起来**：

```
E: Failed to fetch .../libnghttp2-14_1.43.0-1+deb11u3_amd64.deb  404  Not Found
E: Failed to fetch .../curl_7.74.0-1.3+deb11u16_amd64.deb  404  Not Found
/tests/test.sh: line 8: curl: command not found
/root/.local/bin/env: No such file or directory
/tests/test.sh: line 19: uvx: command not found
```

`tests/test.sh` 需要 `apt-get install -y curl expect` → Debian bullseye-security 源已 EOL、包索引与 pool 不同步导致 404 →
curl 装不上 → uv 装不上 → `uvx` 不存在 → **pytest 根本没执行**，无论 agent 做得对不对都是 0。
全 job 只有这 2 个命中此问题（与 `tb-v2`、`tb-v2-mimo` 的同一对 task 表现一致）。
**这两个是纯 infra 冤案，建议重跑或修镜像。**

### 3.2 真实能力差距 —— 8 个

| Case | 失败子测试 | 原因 |
|---|---|---|
| `caffe-cifar-10` | `test_model_accuracy_verification` | 测试精度 0.4700 vs 训练精度 0.5523，gap **0.0823 > 0.05** 阈值（泛化 gap 超标） |
| `dna-assembly` | `test_primers` | 引物 BsaI 位点 `ggtctc` 前**缺少 ≥1nt clamp**（`assert i >= 1` 失败），NEB 硬性要求未满足 |
| `extract-moves-from-video` | `test_solution_file_exists` + `test_solution_content_similarity` | **迭代用尽（121/120）**，全程在抽帧 OCR 采集，**从没写出 `/app/solution.txt`** |
| `install-windows-3.11` | `test_windows_keys_with_visual_feedback` | VNC 注入按键后画面差异 < 10%，`No key caused >=10% image difference` |
| `pytorch-model-cli` | `test_cli_tool_output` | 15 张测试图预测类别几乎全错（如 image 0 预测 7，期望 2） |
| `rstan-to-pystan` | `test_rho_estimation_accuracy` + `test_beta_estimation_accuracy` | `beta[0]` 后验均值 **-0.1455** 超出期望区间 `[-0.07, 0]`（MCMC 采样未收敛到真值） |
| `sam-cell-seg` | `test_mask_alignment` | IoU **0.4930 < 0.5** 阈值，差 0.007 惜败 |
| `video-processing` | `test_jump_analyzer_test_video` | 起跳帧 **98**，期望区间 `[219, 223]`（视频时序分析定位错误） |

## 4. 25 个 0 分的完整清单

| Case | 类别 | 状态 | llm.retry | 摘要次数 | 摘要耗时 | trace 总时长 |
|---|---|---|---|---|---|---|
| `adaptive-rejection-sampler` | 2 流超时判死 | failed | 5 | 0 | 0.0 min | 32.6 min |
| `circuit-fibsqrt` | 2 流超时判死 | failed | 5 | 0 | 0.0 min | 14.0 min |
| `feal-linear-cryptanalysis` | 2 流超时判死 | failed | 5 | 0 | 0.0 min | 12.3 min |
| `gpt2-codegolf` | 2 流超时判死 | failed | 6 | 0 | 0.0 min | 17.3 min |
| `path-tracing-reverse` | 2 流超时判死 | failed | 8 | 4 | 1.5 min | 30.5 min |
| `regex-chess` | 2 流超时判死 | failed | 5 | 0 | 0.0 min | 12.3 min |
| `write-compressor` | 2 流超时判死 | failed | 8 | 0 | 0.0 min | 19.1 min |
| `cobol-modernization` | 1 超时被杀 | failed | 8 | 3 | 1.0 min | 45.0 min |
| `count-dataset-tokens` | 1 超时被杀 | failed | 0 | 1 | 0.1 min | 45.0 min |
| `gcode-to-text` | 1 超时被杀 | failed | 0 | 7 | 22.6 min | 45.0 min |
| `large-scale-text-editing` | 1 超时被杀 | failed | 0 | 0 | 0.0 min | 60.0 min |
| `make-doom-for-mips` | 1 超时被杀 | failed | 1 | 7 | 8.9 min | 45.0 min |
| `make-mips-interpreter` | 1 超时被杀 | failed | 10 | 11 | 46.7 min | 90.0 min |
| `schemelike-metacircular-eval` | 1 超时被杀 | failed | 2 | 15 | 104.8 min | 120.0 min |
| `train-fasttext` | 1 超时被杀 | failed | 0 | 0 | 0.0 min | 180.0 min |
| `caffe-cifar-10` | 3 验证失败 | completed | 0 | 1 | 0.3 min | 73.4 min |
| `dna-assembly` | 3 验证失败 | completed | 6 | 3 | 1.3 min | 49.5 min |
| `extract-moves-from-video` | 3 验证失败 | completed | 0 | 14 | 6.9 min | 68.9 min |
| `install-windows-3.11` | 3 验证失败 | completed | 0 | 6 | 1.7 min | 27.8 min |
| `pytorch-model-cli` | 3 验证失败 | completed | 0 | 1 | 0.2 min | 6.7 min |
| `qemu-alpine-ssh` | 3 验证失败（环境故障） | completed | 0 | 4 | 1.4 min | 29.0 min |
| `qemu-startup` | 3 验证失败（环境故障） | completed | 0 | 1 | 2.6 min | 22.6 min |
| `rstan-to-pystan` | 3 验证失败 | completed | 0 | 3 | 0.7 min | 81.3 min |
| `sam-cell-seg` | 3 验证失败 | completed | 0 | 4 | 0.8 min | 160.7 min |
| `video-processing` | 3 验证失败 | completed | 0 | 18 | 55.2 min | 85.2 min |

## 5. 结论

- **60%（15/25）的 0 分是运行时问题，不是模型能力问题**：8 个撞 agent 墙钟超时（其中 12 个 trial 撞超时，4 个侥幸做完拿满分），
  7 个被 120s 流空闲超时直接判死。
- 剔除这 15 个 + 2 个题目环境故障，真实均分约 **0.80**（72/89 = 0.809）。
- 两大时间黑洞：
  1. `memory.llm_summary` 上下文压缩（202 次调用 / 4.98 h，单次最长 700s，**非流式、无超时、无体积上限**）
  2. `stream_idle_timeout=120s` 的首包阈值卡在模型 TTFT 分布的 p99（119.4s）上（99 次重试 / 3.3 h）
- 能力类失败集中在"差一点"和"没交付"：IoU 0.493 vs 0.5、泛化 gap 0.082 vs 0.05、起跳帧 98 vs 219-223、BsaI clamp 漏写，
  以及 `extract-moves-from-video` 在 120 轮迭代耗尽前始终没写出交付文件。

## 6. 建议（按优先级）

1. **修 `trace.span` 的异常捕获**（`src/agent_runtime/trace.py:99`）：改成 `except BaseException`，
   否则被超时杀掉的任务在 trace 里永远显示 `status=success`，掩盖真实原因。
2. **摘要层限时限量**：
   - 用 `AGENT_RUNTIME_SUMMARY_MODEL` 指定更快的模型做摘要，或
   - 摘要走流式 + 独立超时，或改回确定性 `compact_observations` 策略；
   - 给摘要输入设上限（当前把 186k~394k 字符整段喂进去），并让 `request_chars` 稳定低于 `context_budget`。
3. **流空闲超时 120s → 300s+**（`chat.py:19` `AGENT_RUNTIME_STREAM_IDLE_TIMEOUT`）；
   并让重试耗尽后**降级**（把这一轮当作一次空回复继续迭代）而不是直接终结整个 turn。
   重试应做**退避 + 请求改写**（例如降低 reasoning 长度），而不是原样重放同一个请求。
4. **给工具 `timeout` 加上限并与剩余预算联动**：`min(用户值, 剩余 agent 预算 × 20%)`；
   并在 system prompt 里明确"不要把 timeout 设成几百秒"。
5. **修 `qemu-alpine-ssh` / `qemu-startup` 的 verifier 镜像**：固定 apt 源到 `archive.debian.org` 或预装 `curl`/`expect`/`uv`。
6. **对 8 个被超时杀掉 + 2 个环境故障的 trial 重跑**，再评估真实能力水位。
