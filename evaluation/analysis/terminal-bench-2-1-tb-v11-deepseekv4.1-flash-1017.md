# Terminal-Bench 2.1 × tb-v11 全量验证报告

- **job**: `jobs/terminal-bench-2-1-tb-v11-deepseekv4.1-flash-1017`（89/89 trials 完成，6 个 `AgentTimeoutError`，0 retry）
- **harness**: `harnesses/tb-v11.yaml`（parent `tb-v10`；`context_budget` 60k tokens，`head_chars` 200，`window_rounds` 12，`keep_recent_rounds` 2，`summary_tail_rounds` 2，`user_preserve_budget` 8000 chars，`summary_max_tokens` 4096，`max_iterations` 120，`provider_error` retry 6，`context_window` retry 2，`verification` off，`budget_reminder` 25%/60%/85%）
- **模型**: deepseek-v4.1-flash（1M 上下文；job 产物未记录模型名，与 v9 相同口径）
- **评测结果**: **69/89 通过（0.7753）**，4,263 次主 LLM 调用，4,644 次工具调用；40 个任务触发 compaction（84 次 summary，72 次 yield，12 次回退到 `compact_observations`）；35 次 invalid（22 空回包 / 8 缺 §2 / 3 缺 §3 / 2 缺 §1），31 次 `llm.error`，19 次 `tool.error`；**0 次 400 / 0 次 wire 配对修复**。

与上一轮 v9（70/89，0.7865）相比：新过 6 个（`build-pmars`、`gcode-to-text`、`headless-terminal`、`install-windows-3.11`、`nginx-request-logging`、`sam-cell-seg`），新挂 7 个（`dna-insert`、`filter-js-from-html`、`large-scale-text-editing`、`path-tracing-reverse`、`prove-plus-comm`、`raman-fitting`、`torch-pipeline-parallelism`）。净 -1 题（-1.1pp），落在单次 89 题运行的波动范围内；v11 的 prompt 改动**没有带来可测的通过率增益**。v11 与 v4 的 69/89（0.775）持平。

> **2026-11-11 追加复验**：三项修复（summarizer 关闭 thinking、`execute_code` 输入宽容化、reasoning 熔断文案）已合入 `a28fd46`，并用 3 个真实 TB 2.1 job 复验；生产证据见 §5。第 1 点有效，但仍有正文 4096 截断；第 2 点已在真实容器中直接命中；第 3 点文案已生效，但 `regex-chess` 仍未救回。

**0 分任务（20 个）的原因分布**：

| 原因 | 数量 | 任务 |
|---|---:|---|
| 验证器/环境问题（与模型无关） | 3 | `qemu-alpine-ssh`：curl 装完后仍 `curl: (7) Couldn't connect to server`、`uvx` 缺失（同时触发 Agent 超时）；`qemu-startup`：bullseye-security 源 404 导致 curl 装不上、测试脚本未执行；`mcmc-sampling-stan`：验证器内 `stan_model()` 编译超过 180s，整套 verifier 跑了 25 分钟 |
| Agent 超时 | 4 | `gpt2-codegolf`（45min）、`train-fasttext`（180min）、`extract-moves-from-video`（90min）、`torch-pipeline-parallelism`（45min） |
| 模型未完成/实现错误 | 13 | `make-doom-for-mips`、`make-mips-interpreter` 帧通过但 `node vm.js` 无 stdout；`mteb-retrieve` 抄错 `data.txt` 行；`pytorch-model-cli` 预测不匹配；`regex-chess` 推理超长导致 `re.json` 未产出；`schemelike-metacircular-eval` 61/63；`video-processing` takeoff 224 vs ≤223；`dna-insert` Tm 差 5.398 vs ≤5；`filter-js-from-html` 改坏 3 个干净 HTML；`large-scale-text-editing` 缺 `:wq`/`:x`；`path-tracing-reverse` 迭代耗尽且末轮输出原始 DSML；`prove-plus-comm` 证明通过但注释含 `admit`；`raman-fitting` 2D 峰 x0 拟合错 |

其中真正“差一点”的是 `dna-insert`（5.398 vs 5）、`video-processing`（224 vs 223）、`prove-plus-comm`（仅注释里的 `admit` 触发字符串检查）、`schemelike-metacircular-eval`（61/63）、`make-doom-for-mips` / `make-mips-interpreter`（帧相似度已通过，只差 VM stdout）。`qemu-alpine-ssh` 同时命中“环境 + 超时”，上表按环境归类，避免重复计数。

**Token 消耗**

- 总计 **126,025,757 tokens**（prompt 121,607,839 + completion 4,417,918）。其中主 LLM 122,304,096（prompt 118,233,705 / completion 4,070,391），summarizer 子调用 3,721,661（prompt 3,374,134 / completion 347,527，占 2.95%）。
- 89 个 trial 平均 1,416,020、中位 1,177,062；4,263 次主调用平均每次 prompt 27,735 / completion 955。
- 69 个通过任务消耗 84,690,528（67.2%，均值 1,227,399）；20 个未通过任务消耗 41,335,229（32.8%，均值 2,066,761），失败任务平均消耗约为通过任务的 **1.68 倍**。
- 6 个 `AgentTimeoutError` 合计 8,880,801（7.0%，其中 `rstan-to-pystan` 超时但仍通过）；Top 10 任务合计 41,620,921（33.0%）。
- 与 v9 的 126,697,665 基本持平（-0.5%）；均值从 1,423,569 降到 1,416,020，但中位从 980,095 升到 1,177,062，说明本轮 token 分布更集中、少题更贵。

| 任务 | token 总量 | reward |
|---|---:|---|
| make-mips-interpreter | 5,646,766 | 0.0 |
| make-doom-for-mips | 4,992,342 | 0.0 |
| path-tracing-reverse | 4,844,168 | 0.0 |
| dna-assembly | 4,269,831 | 1.0 |
| sam-cell-seg | 4,194,905 | 1.0 |
| schemelike-metacircular-eval | 4,129,630 | 0.0 |
| largest-eigenval | 4,049,218 | 1.0 |
| video-processing | 3,614,744 | 0.0 |
| install-windows-3.11 | 3,151,715 | 1.0 |
| build-pov-ray | 2,727,602 | 1.0 |

> 口径：取每个 trial 的 `agent.end` 累计值（主 LLM + summarizer 子调用）；已与 4,263 条 `llm.end` 的主调用总量逐项对平（118,233,705 / 4,070,391）。

---

## 1. 各改进点的生产验证结论

| 改进 | 生产证据 | 结论 |
|---|---|---|
| **tb-v11 task-authority prompt**（把首条 user 消息定义为完整规格，结果/输入/约束/验收标准均绑定） | 89 题 69 过，较 v9 少 1 题；6 过 / 7 挂；任务级成功/失败几乎完全被“工具时长 + 单题实现细节”解释，没有可归因于该 prompt 的修复 | ⚠️ 无通过率增益，单次运行内与 v9 持平 |
| **finish rules**（区分“已实际运行并看到通过”与“未验证”） | 83 条非空最终报告：79 条提到 verified，60 条带 `Not verified`/caveat，13 条有独立 `## Not verified` 段；15 个非超时失败里 11 个写了显式 caveat，另 2 个写了 partial/失败，只有 2 个（`regex-chess`、`path-tracing-reverse`）没有可用报告 | ✅ 报告结构基本达成；⚠️ 但“已验证”部分仍常与 grader 冲突（见 §2.1） |
| **budget reminder（v11 新文案）** | 52 个任务共 84 次提醒（iter 30/72/102）。30 次文案为“还可以探索，先设 cutoff”；72/102 次改为“接近上限，停止探索，转入产出与验证”。85% 与 60% 文案完全相同 | ✅ 生效；⚠️ 缺少 85% 的进一步升级 |
| **summary_max_tokens 2048→4096 + 新的 summarizer 契约**（commit `68602ca`） | 84 次 compaction 产出 72 次 yield（85.7%），v9 为 53/132（40%）；invalid 从 175 次降到 35 次。仍有 22 次空回包，34/35 次 invalid 的 `finish_reason=length` 且 `completion_tokens=4096` | ✅ 大幅改善；⚠️ 空回包被推理吃掉的问题未根治 |
| **Summarizer 输入 cap（150k chars）** | 9 次 attempt 2 折叠：输入从 159,685–356,121 chars 降到 80,059–107,846 chars；其中 6 次仍 invalid、3 次成功 yield。attempt 1 不折叠，最大 398,425 chars | ✅ 生效，只治重试、不治首压 |
| **Usage 锚点（char_budget）** | 107 次 summary attempt 反推比率 1.93–3.76，中位 2.90，没有一次落到默认 4.0 | ✅ 全量生效 |
| **Verbatim 保全（user_preserve）** | 72 次 yield 的 `preserved_users` 全为 0（与 v9 一致） | ⚠️ 口径未变，不是回归 |
| **增量 summary + boundary** | 40 个任务 72 次 yield；13 个多 yield 任务的 boundary 全部严格递增；72 条 summary 的 md5 无重复 | ✅ 冻结/复用 bug 未复发 |
| **invalid-retry（1 次）** | 35 次 invalid = 23 attempt 1 + 12 attempt 2；重试救回 11 次，12 次回退到 `compact_observations`。最终 84 次 compaction 全部有可用视图 | ✅ 有用；一次成功率 72/84 |
| **error/invalid 观测事件** | 31 次 `llm.error` 全部分类（31/31 为 reasoning 50k）；19 次 `tool.error` 全部有工具名与原因 | ✅ 足以定位本轮的 DSML / reasoning / 工具路径问题 |
| **apply_patch 新 JSON schema** | 142 次调用 / 7 次失败 = **95.1%**（v9 为 95.2%）；失败为 `old_str` not found 3、`cannot write file` 4，**参数形状错误 0** | ✅ 稳定保持 |
| **tool_calls 紧邻配对修复（v9 PR #8）** | 4,263 次调用 0 次 400，0 次 `contract_repaired`，无 `insufficient tool messages` | ✅ 存量/新增路径均未复发 |
| **Hard gate（context_window）** | trace 中 0 次 `llm.retry` 事件，也就没有 `context_window` 重试（60k vs 1M，注定不开火） | ⚠️ 与 v9 一致：无覆盖 |

工具结构：`run_command` 3,126 / `execute_code` 938 / `write_file` 246 / `read_file` 153 / `apply_patch` 142 / `edit_file` 20 / `read_output` 10 / `find_files` 5 / `grep_search` 3 / `bash` 1，共 4,644 次；`tool.error` 19 次，占 0.41%。

---

## 2. 新发现

### 2.1 prompt 把“验证”写进了报告格式，但没有写进事实

v11 的 finish rules 明显改变了最终报告的形态：83 条非空报告里 79 条带 verified、60 条带显式 caveat，`make-doom-for-mips` 甚至直接写“partial success, with one defect I did not fix”。这是 v9 没做到的。

但 **13 个 reward=0 的 trial 最终报告仍包含 verified 表述**；代表性例子：

| 任务 | 最终报告的说法 | 实际失败 |
|---|---|---|
| `dna-insert` | “deliverable is complete and verified… ALL CHECKS PASSED” | forward/reverse Tm 差 5.398 > 5 |
| `filter-js-from-html` | “SUMMARY: 0 failures for both harnesses” | 1 个 XSS 向量未过滤，3/12 干净 HTML 被改写 |
| `large-scale-text-editing` | “Full transform verified byte-identical” | `apply_macros.vim` 缺 `:wq`/`:x` |
| `mcmc-sampling-stan` | “all 18 checks PASSED” | verifier 的 `stan_model()` 编译超时 |
| `mteb-retrieve` | “matches data line 14 byte-for-byte” | 抄的是 HumanEval 行，期望 MTEB 行 |
| `pytorch-model-cli` | “predictions match” | verifier 预测不匹配 |
| `raman-fitting` | “written and verified… bootstrap confirms” | 2D x0=3745.4 vs 期望 2670.08 |
| `video-processing` | “output.toml parses to exactly the two required integer keys” | takeoff 224，期望 [219,223] |

结论不是“prompt 没生效”，而是：**模型会认真写它自己设计的检查，却无法保证这些检查等于 grader 的检查**。当前 prompt 能约束“报告格式”，不能约束“验证口径”。`filter-js-from-html` 是最典型的例子：自建 harness 报 0 failure，官方测试报 4 个失败。

### 2.2 summarizer 空回包仍在，且每次失败固定烧掉 4096 completion tokens

35 次 invalid 中 34 次 `finish_reason=length`、`attempt_completion_tokens=4096`（唯一例外 1,914），`reasoning_chars` 中位 10,853、最大 16,086。也就是说：summarizer 把 4096 token 全部花在 reasoning 上，正文还没来得及写就被截断。

- 22 次 `summary_chars=0`（完全空），8 次缺 `## 2`，3 次缺 `## 3`，2 次缺 `## 1`。
- 12 次 attempt 2 仍失败，只能回退到 `compact_observations`；这 12 次共消耗约 49k completion tokens。
- 全部 invalid attempt 合计 141,178 completion tokens，占 summarizer completion（347,527）的 **40.6%**。

提升 `summary_max_tokens` 到 4096 把 yield 成功率从 v9 的 40% 拉到 85.7%，方向正确；但“length 截断型空回包”本身没有被消除，只是每次更贵。根因是 summarizer 复用了带 reasoning 的主模型，而不是 prompt 缺约束。

### 2.3 迭代耗尽时，模型把工具调用写成了正文（DSML 泄漏）

`path-tracing-reverse` 在 iter 121（120 次上限后的最后一次调用）输出：

```
<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="run_command">
<｜｜DSML｜｜ parameter name="command" string="true">ls -l /app/mystery.c ...
```

这段文本没有 tool_calls 字段，因此被 runtime 当作 `final=true` 的最终回答接受，agent 直接结束。实际任务此时还缺 `/app/reverse`，reward 0；最终报告只有 207 字符，没有任何结果说明。

这是本轮唯一一次 DSML 泄漏（全量 grep 只命中此 trial），但它暴露了一个通用缺口：**“finish_reason=stop 且 content 为空/含工具调用标记”没有被当作协议异常**。如果没有迭代限制兜底，这类回答会以“正常完成”结束。

### 2.4 7 个回归任务：1 个超时、1 个 verifier 陷阱、5 个模型错误

| 任务 | v9 | v11 失败原因 |
|---|---|---|
| `torch-pipeline-parallelism` | 1.0 | 45min AgentTimeout；36 次调用，最后在对比 `attn_implementation="eager"` |
| `dna-insert` | 1.0 | Tm 差 5.398，超阈值 0.398 |
| `filter-js-from-html` | 1.0 | sanitizer 改写干净 HTML；verifier 跑了 778s |
| `large-scale-text-editing` | 1.0 | `apply_macros.vim` 缺 `:wq`/`:x`，前 4 项测试均通过 |
| `path-tracing-reverse` | 1.0 | 迭代耗尽 + DSML 泄漏，`/app/reverse` 未产出 |
| `prove-plus-comm` | 1.0 | `coqc` 通过，但测试断言 `"admit" not in content`，被注释 “(no axioms, no admits)” 触发 |
| `raman-fitting` | 1.0 | 2D 峰识别错误：x0 3745.4 vs 2670.08（G 峰正常） |

除 `torch-pipeline-parallelism` 外都不是 harness 回归。`prove-plus-comm` 更像任务侧检查过严：证明已完成、`coqc` 通过，仅注释里的英文单词被判失败。

### 2.5 `execute_code` 的路径/语言限制制造了 11 次可避免失败

19 次 `tool.error` 中 11 次来自 `execute_code`：

- 路径必须位于 `.scripts/` 下：5 次（`validate.R`、`patch_starts.R`、`checkframe.py`、`proj.py`、`/app/proj.py`）；
- 语言别名不支持：5 次（`python3`×2、`c`×2、`ai`×1）；
- 参数名错误：1 次（`execute_code() got an unexpected keyword argument 'command'`）。

另有 1 次 `apply_patch` 写 `/tmp/...` 被拒、1 次 `bash` 未知工具。这些错误本身不致命，但在 `path-tracing-reverse` 这类迭代耗尽的任务里，每一次都在消耗最后的预算。

### 2.6 reasoning 50k 仍然是 `regex-chess` 的单点死因

31 次 `llm.error` 全部是 `reasoning exceeded 50000 chars`：

- `regex-chess` 10 次，iter 18 后耗尽，最终只有 19 次 LLM 调用、8 次工具调用，`/app/re.json` 根本没生成；
- `adaptive-rejection-sampler` 6 次、`circuit-fibsqrt` 6 次，均恢复并通过；
- 另外，`make-mips-interpreter` 的 11 次、`path-tracing-reverse` 的 8 次是 summarizer invalid（§2.2），不是主调用 reasoning，不在这 31 次里。

同一模型在同一 harness 下可以“错 6 次后恢复”，也可以在 `regex-chess` 上“错 10 次后彻底失败”。这说明需要的是**连续 reasoning 失败后的降级策略**，而不是单纯调大 50k。

### 2.7 超时是工具执行时长驱动的，不是模型空转

6 个超时 trial 的 LLM 调用数只有 25–95，而工具执行占了 86%–99% 的墙钟：

| 任务 | 时限 | LLM 调用 | 工具总时长 / 占比 | 最长单次工具 |
|---|---:|---:|---:|---:|
| `gpt2-codegolf` | 45min | 26 | 2,556s / 94.7% | 601s ×2（execute_code） |
| `train-fasttext` | 180min | 25 | 10,653s / 98.6% | 2,364s（execute_code） |
| `extract-moves-from-video` | 90min | 95 | 4,861s / 90.0% | 600s（run_command） |
| `qemu-alpine-ssh` | 45min | 69 | 2,323s / 86.0% | 246s（run_command） |
| `torch-pipeline-parallelism` | 45min | 36 | 2,327s / 86.2% | 708s（run_command） |
| `rstan-to-pystan` | 90min | 74 | 5,074s / 94.0% | 541s（run_command） |

`gpt2-codegolf` 的两次 601s `execute_code`、`train-fasttext` 的 2,364s 单次执行，说明这些任务是被单条长命令卡住，而不是模型在迭代里空转。单纯提高 `max_iterations` 对这类任务无效；需要的是按任务放宽 agent timeout，或识别长工具调用并给出不同的预算。

---

## 3. 建议修复

1. **最终回答协议守卫**：当 `finish_reason=stop` 但 `content` 为空、或含 `<｜｜DSML｜｜` / `<|tool_call|>` 等工具调用标记时，不要当成 final。把它作为一次协议违例：记录 `llm.final.invalid`，回注一条“你的 tool call 没有按协议发出”的 user 消息并继续（至少给一次重试）。这可以直接救回 `path-tracing-reverse` 的最后一轮。
   - 位置：`src/agent_runtime/agent/chat.py` / `turn_finisher.py`。
2. **summarizer 关闭/限制 reasoning**：给 summarizer 单独传 provider 侧的“无思考”参数；若模型不支持，则在 attempt 1 出现 `finish_reason=length && content==""` 时，attempt 2 直接改用 `compact_observations`，不再用同样的 4096 预算重试。当前 12 次 attempt 2 全部是同一失败模式的重复付费。
   - 位置：`src/agent_runtime/agent/context/summary.py:351-425`。
3. **`execute_code` 输入宽容化**：接受 `python3`/`py`/`c` 等别名（或给出明确的可用语言提示）；允许 workspace 内绝对路径与相对路径，默认落到 `.scripts/`，不要要求调用方先知道这个目录。可消除本轮 10/11 次 `execute_code` 错误。
   - 位置：`src/agent_runtime/tools/`（`execute_code` 参数解析与路径校验）。
4. **budget reminder 分级**：60% 与 85% 共用“接近上限”文案没问题，但 85% 应单独升级为“现在只做三件事：确认产物存在、跑一次验收、报告结果”，并在 `edited=false` 时附上“你还没有改动任何源文件”的提示。本轮 24 次提醒发生在 `edited=false` 时，仍未触发任何强制动作。
   - 位置：`src/agent_runtime/agent/turn_guard.py:130-150`。
5. **reasoning 失败熔断**：同一 trial 连续 3 次 `reasoning exceeded 50000 chars` 后，注入一条“停止扩展推理，先写出最小可交付产物”的 user 消息；连续 5 次则强制结束并标记为模型侧失败，避免 `regex-chess` 式的 10 次空转。
   - 位置：`src/agent_runtime/agent/chat.py` 的 `llm.reasoning_budget` 处理。
6. **长工具任务单独放宽 timeout**：对 `train-fasttext`、`gpt2-codegolf`、`extract-moves-from-video`、`torch-pipeline-parallelism` 这类“调用数少但单次工具长”的任务，使用 per-task `agent_timeout_multiplier`，而不是统一 3×。
7. **任务侧修复（非 harness）**：
   - `prove-plus-comm`：测试改为检查 `admit.`/`Admitted` 的 tactic 级出现，或忽略注释；
   - `mcmc-sampling-stan`：验证器 `stan_model()` 的 180s 超时放宽到 600s，或提前编译缓存；
   - `qemu-startup` / `qemu-alpine-ssh`：固定可用的 Debian snapshot 源，避免 bullseye-security 404 与 curl 缺失。
8. **观测“自证 vs 官方验收”的偏差**：为最终报告增加一个轻量统计（不改变评分）：报告声称 verified、但会话内没有任何 `run_command`/`execute_code` 成功退出码覆盖验收命令时，记录 `final.unverified_claim`。本轮至少 8 个失败 trial 会命中，可用于后续 prompt 迭代。

---

## 4. 后续观测点

- **复跑验证**：优先复跑 5 个“差一点/可修复”任务——`dna-insert`、`video-processing`、`prove-plus-comm`、`make-doom-for-mips`、`make-mips-interpreter`，以及 2 个 prompt 相关回归 `filter-js-from-html`、`large-scale-text-editing`。这些不是能力缺口，复跑可区分任务方差与真实回归。
- **summarizer**：修复后观察 `context.llm_summary.invalid` 是否从 35 降到个位数，尤其 `summary_chars=0 && finish_reason=length`；同时观察 summarizer completion 是否从 347k 明显下降。
- **DSML/最终回答**：上线协议守卫后，确认 `llm.final.invalid` 出现次数，以及是否还有“空 final + reward 0”的 trial。
- **400 配对**：继续保持 0 次 `insufficient tool messages` / 0 次 `contract_repaired`；v9 的修复在本轮 4,263 次调用中未被击穿。
- **apply_patch**：保持 ≥95% 成功率；若 `cannot write file` 仍集中在 `/tmp` 与仓库外路径，考虑在工具层直接把可写根目录报给模型。
- **budget reminder**：85% 文案升级后，观察 6 个迭代耗尽任务（3 过 3 挂）是否出现“最后一轮只做验收”的行为，以及 `edited=false` 的 24 次提醒后是否仍无源文件改动。
- **超时**：按任务类型拆分 timeout 后，观察 4 个纯超时任务是否转为“能跑完但结果不一定对”，避免把工具时长误判为模型能力。

---

## 5. 2026-11-11 追加复验：三项修复的生产证据

**代码提交**：`a28fd46`（`feat: configurable summarizer reasoning effort; tolerate execute_code language/path input; sharpen reasoning-budget nudge`）

**复验 job**：

| job | 任务 | 结果 | 用时 |
|---|---|---|---:|
| `jobs/tb21-verify-3chg-20261111` | `gcode-to-text` / `db-wal-recovery` / `regex-chess` | 1/3 通过 | 37m10s |
| `jobs/tb21-verify-3chg-r2-20261111` | `adaptive-rejection-sampler` / `llm-inference-batching-scheduler` | 1/2 通过 | 25m15s |
| `jobs/tb21-verify-execode-probe-20261111` | `db-wal-recovery` + 强制工具探针 | 1/1 通过 | 5m24s |

### 5.1 Summarizer 关闭 thinking：目标失败模式已消除，残留正文截断

新增 `context.summary_reasoning_effort`（默认 `None`，tb-v11 设 `none`），summarizer 的 per-call `reasoning_effort` 透传到 provider，并记录在 `context.llm_summary*` trace 中。

同一道 `gcode-to-text` 前后对比：

| 指标 | 旧运行 | 新运行 |
|---|---:|---:|
| summarizer 尝试 | 2 | 8 |
| 有效 yield | 1 | 6 |
| `summary_chars=0` 空摘要 | 1 | **0** |
| summarizer `reasoning_chars` | **16,619** | **0** |
| `summary_reasoning_effort` | 未发送 | 全部 `none` |
| 回退 `compact_observations` | 0 次 | 1 次压缩 |

另外三题同样验证了 `effort: none` + `reasoning_chars=0`：

- `db-wal-recovery`：1 次 summary，正常 yield，reward 1.0；
- `adaptive-rejection-sampler`：2 次 summary，全部 yield，reward 1.0；
- `llm-inference-batching-scheduler`：2 次 summary，全部 yield（任务失败原因见 5.3）。

结论：旧的“4096 token 全被 reasoning 吃掉、正文为空”失败模式已经消除；`summary_max_tokens` 现在确实是正文上限。但暴露了新的残留问题：`gcode-to-text` 有一次压缩的 attempt 1/2 都是 `finish_reason=length`、正文 10.4k/10.1k 字符、缺 `## 3`，第二次用同样的 4096 预算重试仍然失败并回退。**这是正文长度问题，不是 thinking 回归。** 当前未实现“length 时放大 attempt 2 预算或直接回退”的后续修复。

`gcode-to-text` 本轮 reward 0 的直接原因是跑满 120 次迭代（模型换成了字体匹配 OCR 路线），不是被 summarizer 卡死；旧运行走另一条路线 66 次迭代通过。该题的任务级回归与 summarizer 改动没有足够证据建立因果，需要同题再跑来区分方差。

### 5.2 `execute_code` 输入宽容化：真实容器直接命中

在 `db-wal-recovery` 真实任务容器里，通过 `--extra-instruction-path` 强制模型先执行三个探针；三个调用全部 `status=success`，任务最终 reward 1.0：

```text
args: {"code": "print('alias-ok')",  "language": "python3"}
result: alias-ok
script: .scripts/0001.py

args: {"code": "print('bare-ok')",   "path": "probe"}
result: bare-ok
script: .scripts/probe.py

args: {"code": "print('bare-ok-2')", "path": "probe2.py"}
result: bare-ok-2
script: .scripts/probe2.py
```

验证点：

- `language: "python3"` 已归一化为 `python` 并成功执行；
- 裸文件名 `probe` 自动落到 `.scripts/probe.py`，并自动补 `.py`；
- `probe2.py` 同样落到 `.scripts/`；
- 6 个复验 trial 中 0 次 `unsupported language`、0 次旧路径错误。

保守边界保持不变：不在 `.scripts/` 下的绝对路径仍拒绝；`~` 与 workspace 逃逸仍拒绝；C/C++ 与 python2 给出明确的 `run_command`/python3 提示。

### 5.3 Reasoning 熔断文案：已送达，但单独改文案不足

`regex-chess`（旧运行 10 次 `reasoning_budget`、reward 0）：

- 新运行 23 次 `llm.reasoning_budget`、29 次主 LLM 调用、7 次工具调用；
- `Stop extending your reasoning` / `smallest deliverable` 在请求历史中出现 74 次，说明新文案确实送达；
- 但模型仍然连续 10 次、再连续 13 次烧满 50k reasoning，`/app/re.json` 未产出，reward 0。

正向案例：`adaptive-rejection-sampler` 4 次熔断后恢复，最终 reward 1.0（旧运行 6 次熔断、同样通过）。

结论：熔断注入机制正常，但仅改措辞无法打破 `regex-chess` 式的连续 reasoning 螺旋。建议的后续升级（连续 3 次后降低 per-call `reasoning_effort`、连续 5 次强制结束）**本轮未实现**。

### 5.4 任务级结果说明

- `llm-inference-batching-scheduler` 本轮 reward 0 的原因是任务侧约束 `combined unique shapes 11 exceed cap 8`，不是 harness/本次改动；该题旧运行为 1.0。
- 复验中 `adaptive-rejection-sampler` 与 `db-wal-recovery` 均通过；`gcode-to-text` 与 `regex-chess` 失败原因分别见 5.1 / 5.3。
- 提交前测试：`tests/tools/test_code.py` 27 passed；summarizer / harness / loop 聚焦测试全部通过；宽范围 `tests/agent tests/harness tests/tools` 为 **492 passed / 10 failed**，10 个失败与改动前 `git stash` 基线完全一致（persistent memory 注入 + `StaleReasoningPadTests`），与本次改动无关。

### 5.5 仍未解决的后续项

1. **summarizer 正文截断**：attempt 2 检测到 `finish_reason=length` 时应放大 `max_tokens`（1.5×/2×）或直接回退 `compact_observations`；prompt 中硬编码的 “4,096 output tokens” 应按实际 `summary_max_tokens` 留 20%–25% margin（tb-v10 实际为 2048，当前文案不一致）。
2. **reasoning 连续失败升级**：按连续次数降低 per-call reasoning effort / 强制结束，而不是重复注入同一条 nudge。
3. **`gcode-to-text` 复跑**：区分任务方差与 summarizer 改动的影响。
