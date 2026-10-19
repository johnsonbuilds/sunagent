# terminal-bench-2-1-tb-v2-mimo-2.6-pro-ultraspeed 得分为 0 case 分析

> Job: `jobs/terminal-bench-2-1-tb-v2-mimo-2.6-pro-ultraspeed`
> Job ID: `b925bc26-4dfd-49c8-a453-b14cf9816ef0`
> Agent: `sunagent` v0.1.0（`agent_runtime.integrations.harbor:HarborAgent`）
> `timeout_multiplier=3.0`
> 数据来源：各 `<trial>/result.json`（`verifier_result.rewards.reward`）、`verifier/{ctrf.json,reward.txt,test-stdout.txt}`、`agent/agent-runtime.jsonl`、`trial.log`

## 0. 总体统计

- 89 个 trial，平均分 **0.7191**
- reward = 1.0：**64** 个；reward = 0.0：**25** 个
- `stats.n_errored_trials=3`，全部为 `AgentTimeoutError`

25 个 0 分 case 的原因归为 **4 类**：

| 类别 | 数量 | 性质 |
|---|---|---|
| 1. LLM API 429 限流导致 agent 中途崩溃 | 9 | 基础设施问题 |
| 2. AgentTimeoutError 超时 | 2 | 预算/任务耗时问题 |
| 3. Verifier 环境故障（apt 装包失败） | 2 | 基础设施问题（冤案） |
| 4. Agent 正常完成但测试不通过 | 12 | 真实能力差距 + 部分边界惜败 |

## 1. LLM API 429 限流导致 agent 中途崩溃 —— 9 个（基础设施问题）

`caffe-cifar-10__fLRsUEZ`、`mteb-leaderboard__8SjwPwp`、`llm-inference-batching-scheduler__knexmhz`、`pytorch-model-recovery__toSj4Gp`、`regex-chess__XTgti5g`、`cobol-modernization__pzuViSG`、`filter-js-from-html__v3vub4S`、`circuit-fibsqrt__ZsKuD9e`、`gpt2-codegolf__69qFGRL`

- 这些 trial 的 `agent-runtime.jsonl` 末尾都是 `agent.end` 且 status=`error`，错误为 `LLM error: Error code: 429 - Too Many Requests`，agent 进程直接死掉，任务没做完就交卷，verifier 得 0。
- 全部集中爆发在 **2026-10-19 01:31–02:02 UTC** 这半小时内，是典型的 API 限流窗口。
- 佐证：同一窗口还有 2 个 trial（`kv-store-grpc__Ufmjtbd`，00:34；`regex-log__Liy53Ue`，01:44）同样以 429 结束，但因为死之前已经把解做完了，verifier 仍给了 1.0 —— 说明限流影响面比 0 分名单更大，纯粹是"死的时机"决定了分数。全 job 实际受 429 影响约 **11** 个 trial。

## 2. AgentTimeoutError 超时 —— 2 个

- `torch-pipeline-parallelism__iPsnako`：2700s 超时（timeout_multiplier=3.0）。
- `train-fasttext__TAQ8Tvh`：10800s 超时。

（注：`query-optimize__mBH2YMY` 也 2700s 超时了，但解已经完成，反而拿了 1.0。）

## 3. Verifier 环境故障（与 agent 无关）—— 2 个

`qemu-alpine-ssh__LvUiGBy`、`qemu-startup__9fQAmAP`

这两个没有 ctrf.json，test-stdout 显示 **verifier 自己的测试脚本就没跑起来**：

```
E: Failed to fetch .../libnghttp2-14_1.43.0-1+deb11u3_amd64.deb  404  Not Found
E: Failed to fetch .../curl_7.74.0-1.3+deb11u16_amd64.deb  404  Not Found
/tests/test.sh: line 8: curl: command not found
/root/.local/bin/env: No such file
/tests/test.sh: line 19: uvx: command not found
```

Debian bullseye-security 源的包索引与镜像不同步导致 `curl`/`uvx` 装不上，测试工具链安装即失败 → 无论 agent 做得对不对都是 0。全 89 个 trial 中只有这 2 个命中此问题。**这两个是纯 infra 冤案，建议重跑。**

## 4. Agent 正常完成但 verifier 判定测试不通过 —— 12 个

reward 是全对才给分（all-or-nothing），所以哪怕只差 1 个 test 也是 0：

| Case | 通过率 | 失败原因 |
|---|---|---|
| `schemelike-metacircular-eval` | 59/63 子测试 | `closures.scm`、`05-simple-io.scm`（各含 shadow_test）4 个子测试失败，`test_interp` assert 4==0 |
| `sam-cell-seg` | 8/9 | `test_mask_alignment`：**IoU = 0.4960，阈值 0.5，差 0.004 惜败** |
| `dna-insert` | 0/1 | `test_primers`：**引物 Tm 差 5.268，要求 ≤5，惜败** |
| `mcmc-sampling-stan` | 5/6 | `test_hierarchical_model_implemented`：校验 Stan 模型结构超时 |
| `raman-fitting` | 2/3 | `test_2D_Peak`：x0/gamma/A 对了，**offset 参数不对** |
| `pytorch-model-cli` | 5/6 | `test_cli_tool_output`：预测值与期望不符 |
| `gcode-to-text` | 1/2 | flag 写错：`flag{gc0de_iz_ch4LLenging}` vs 正确 `flag{gc0d3_iz_ch4LLenGiNg}`（细节转写错误） |
| `mteb-retrieve` | 1/2 | `result.txt` 内容错：写成了 `HumanEval: ...` 而不是 `MTEB: Massive...`（拿了错误数据集的结果） |
| `extract-elf` | 1/2 | 输出与参考匹配度 **0%**，要求 ≥75%（输出格式完全不对） |
| `make-mips-interpreter` | 1/3 | 缺 DOOM 启动输出；`frame.bmp` 相似度 **0.756 < 0.95** |
| `make-doom-for-mips` | 0/3 | VM 执行超时，`frame.bmp` 根本没生成 |
| `extract-moves-from-video` | 1/2 | 解法内容相似度 **27.8% < 90%**（走偏/取巧解法） |

## 5. 结论

- **约 44%（11/25）的 0 分与模型能力无关**：9 个 429 限流 + 2 个 verifier 装包失败；其中 2 个 429 case（`kv-store-grpc`、`regex-log`）侥幸做完拿了满分，说明限流窗口实际伤害了约 11 个 trial。剔除 infra 影响，真实均分约在 **0.80** 附近（(0.7191×89 + 11) / 89 ≈ 75/89）。
- **能力类失败集中在"差一点"**：IoU 0.496 vs 0.5、Tm 5.27 vs 5、相似度 0.756 vs 0.95、flag 字符串抄错 —— 说明模型在数值精确性和逐字符严格性上仍不稳；另外 2 个 QEMU/DOOM 系（MIPS 仿真）任务全军覆没，这类重系统任务是明显短板。

## 6. 建议

1. 对 429 影响的 11 个 trial 及 2 个 apt 故障 trial 重跑（或在统计中剔除）。
2. 给 agent SDK 加 429 退避重试（指数退避 + 抖动，或将 `provider_error.max_retries` 覆盖到 429 场景）。
3. verifier 镜像固定 apt 源版本或预装 `curl`/`uvx`，避免测试端 404。
