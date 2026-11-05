# Terminal-Bench 2.1 × tb-v9 全量验证报告

- **job**: `jobs/terminal-bench-2-1-tb-v9-deepseek-v4.1-flash-1017`（89/89 trials 完成，6 个 `AgentTimeoutError`）
- **harness**: `harnesses/tb-v9.yaml`（parent `tb-v8`；`context_budget` 60k tokens，`head_chars` 200，`window_rounds` 12，`keep_recent_rounds` 2，`summary_tail_rounds` 2，`user_preserve_budget` 8000 chars，`summary_max_tokens` 2048，`max_iterations` 120，`provider_error` retry 6，`context_window` retry 2，verification off）
- **模型**: deepseek-v4.1-flash（1M 上下文）
- **评测结果**: **70/89 通过（0.7865）**，3,973 次 LLM 调用，4,392 次工具调用；36 个任务触发 compaction（132 次 summary），28 个任务产出 53 次真快照；175 次 invalid（93 空回包 / 55 缺 §2 / 25 缺 §3 / 2 缺 §1），62 次 `llm.error`。

与上一轮 v4（69/89，0.775）相比：新过 6 个（dna-assembly、filter-js-from-html、large-scale-text-editing、protein-assembly、raman-fitting、torch-pipeline-parallelism），新挂 5 个（build-pmars、mteb-retrieve、nginx-request-logging、schemelike-metacircular-eval、video-processing）。其中 **只有 `build-pmars` 是 harness bug 直接导致**，其余更像任务方差。

**0 分任务（19 个）的原因分布**：

| 原因 | 数量 | 任务 |
|---|---:|---|
| 验证器/环境问题（与模型无关） | 3 | `qemu-alpine-ssh`、`qemu-startup`：bullseye-security 源 404 导致 curl 装不上、测试脚本未执行；`mteb-retrieve`：验证器下载 nvidia-cudnn 超时 |
| Agent 超时 | 4 | `gpt2-codegolf`、`gcode-to-text`（45min），`extract-moves-from-video`（90min），`train-fasttext`（180min） |
| harness bug | 1 | `build-pmars`：400 `tool_calls` 配对错误终止（见 §2.1） |
| 模型未完成/实现错误 | 11 | `regex-chess` 未产出 `re.json`（33 次 reasoning 超长）、`make-doom-for-mips` 未产出 `frame.bmp`、`make-mips-interpreter` 帧相似度 0.806<0.95、`schemelike-metacircular-eval` 解释器结果错、`mcmc-sampling-stan` 分层模型未实现、`sam-cell-seg` IoU 0.468<0.5、`nginx-request-logging` 返回默认页、`video-processing` 检出 219 段（要求≤4）、`install-windows-3.11` qemu 未运行、`pytorch-model-cli` CLI 输出不符、`headless-terminal` 后台 HTTP 服务未起 |

其中真正“差一点”的只有 `sam-cell-seg`（0.468 vs 0.5）和 `make-mips-interpreter`（0.806 vs 0.95）；其余要么超时，要么关键产物根本没生成。

### Token 消耗

- 总计 **126,697,665 tokens**（prompt 122,231,629 + completion 4,466,036）。其中主 LLM 119,026,488（prompt 115,018,824 / completion 4,007,664），summarizer 子调用 7,671,177（prompt 7,212,805 / completion 458,372）。
- 89 个 trial 平均 1,423,569、中位 980,095；3,973 次主调用平均每次 prompt 28,950 / completion 1,009。
- 70 个通过任务消耗 80,291,819（63.4%，均值 1,147,026）；19 个未通过任务消耗 46,405,846（36.6%，均值 2,442,413），失败任务平均消耗约为通过任务的 2.1 倍。
- 6 个 cancelled（AgentTimeout）+ 2 个 error 共 13,489,825（10.6%）；Top 10 任务合计 48,288,088（38.1%）。

| 任务 | token 总量 | reward |
|---|---:|---|
| make-doom-for-mips | 6,788,959 | 0.0 |
| path-tracing-reverse | 5,655,118 | 1.0 |
| video-processing | 4,931,461 | 0.0 |
| make-mips-interpreter | 4,885,747 | 0.0 |
| winning-avg-corewars | 4,620,328 | 1.0 |
| gcode-to-text | 4,603,562 | 0.0 |
| schemelike-metacircular-eval | 4,439,646 | 0.0 |
| install-windows-3.11 | 4,264,362 | 0.0 |
| dna-assembly | 4,159,836 | 1.0 |
| sam-cell-seg | 3,939,069 | 0.0 |

> 口径：取每个 trial 的 `agent.end` 累计值（主 LLM + summarizer 子调用）；已与 3,973 条 `llm.end` 的主调用总量逐项对平（115,018,824 / 4,007,664）。

---

## 1. 各改进点的生产验证结论

| 改进 | 生产证据 | 结论 |
|---|---|---|
| Summarizer 输入 cap（150k chars） | 53 次开火（42 次 invalid 的 attempt 2 + 11 次 yield）；42 次带字符数的截后最大 149,462、中位 112,117、压缩比中位 0.47；`make-mips-interpreter` invalid 41→11、yield 1→3；attempt 1 不 cap，最大输入仍 522,937 | ✅ 生效，重试输入被压到 150k 内；只治重试、不治首压 |
| Usage 锚点（char_budget） | 53 次 yield 反推比率 2.03–3.80，中位 2.84，没有一次是默认 4.0 | ✅ 全量生效 |
| Verbatim 保全（user_preserve） | 53 次 yield 的 `preserved_users` 全为 0（v4 为 31/45）；初始任务指令固定在 `leading` 并逐字完整，runtime user 消息不再计入 | ⚠️ 口径变化，不是回归 |
| 增量 summary + boundary | 28 个任务 53 次 yield；11 个多 yield 任务 boundary 全部严格递增；summary md5 复用 0 | ✅ 冻结 bug 未复发 |
| invalid-retry（1 次） | 175 次 invalid（attempt 1 96 / attempt 2 79），最终仍产出 53 次快照；93 空回包 / 55 缺 §2 / 25 缺 §3 / 2 缺 §1 | ✅ 有用；一次成功率仍偏低 |
| error/invalid 观测事件 | 175 次 invalid 全部分类，62 次 `llm.error` 全部有归因（60 reasoning_50k + 2 toolcalls_400） | ✅ 靠它定位了本次 400 |
| summary prompt 透传 | tb-v9 未配自定义 summary prompt，生产零覆盖；链路与单测状态同 v4 | ⚠️ 仅单测 |
| submit 悬空修复（`dangling_tool_calls_dropped`） | 3,973 次发送逐条回放：悬空调用 0、命中发送 0；v4 有 7 个任务各 1 次 | ✅ 旧 submit 路径已清掉 |
| apply_patch 新 JSON schema | 参数改为 `{"path": ..., "edits": [{"old_text": ..., "new_text": ...}]}`；v9 成功率 **159/167 = 95.2%**（v4 为 110/146 = 75.3%），失败数 36→8（-78%）；v9 的 8 次失败为 syntax 3 / `cannot write file` 3 / `old_str` not found 1 / `old_str` 出现 3 次 1，**参数形状错误 0**；v4 的 36 次失败中 29 次是旧 SEARCH/REPLACE 文本解析类 | ✅ 显著提升（+19.9pp），格式类失败基本清除 |
| Hard gate（context_window） | 3,973 次调用里 `context_window` 重试 0 次 | ⚠️ 与 v4 一致：60k vs 1M 注定不开火 |

apply_patch 的用法结构也随之改变：v4 是 `edit_file` 169 次 / `apply_patch` 146 次，v9 变成 `edit_file` 23 次 / `apply_patch` 167 次。新 schema 让 apply_patch 成为默认的批量编辑路径，调用样本反而更多（146→167），验证更充分；`write_file` 171/171、`edit_file` 21/23 也保持高位。剩余 8 次失败均为编辑内容本身的问题（改后语法不过、写盘 CommandError、上下文匹配不到/不唯一），不是调用格式问题。

---

## 2. 新发现

### 2.1 raman 类 400：没有归零，而且换了条路径

v9 有 **2 个 trial 被同一条 400 杀死**：

| 任务 | 表现 | 代价 | reward |
|---|---|---|---|
| `build-pmars` | iteration 18 连续 6 次 `provider_error` 400，随后 `agent.error` | 约 2 分钟重试，整轮终止 | **0.0**（v4 是 1.0） |
| `constraints-scheduling` | 同一错误，6 次重试后终止 | 文件此前已写对 | 1.0（运气） |

错误原文仍是：

```
An assistant message with 'tool_calls' must be followed by tool messages
responding to each 'tool_call_id'. (insufficient tool messages following tool_calls message)
```

#### 根因：一轮里同时出现“被拒调用 + 合法调用”，拒绝消息以 user 角色插到了中间

以 `build-pmars` iteration 17→18 为例：

1. 模型一轮发出两个调用：`bash`（未知工具，被拒）和 `run_command`（合法）。
2. `loop.py` 的 `canonical_tool_calls()` 只把合法调用写进 assistant 历史，所以历史里的 assistant 消息只剩 `call_01`。
3. `ToolRunner.dispatch()` 按 outcome 顺序处理：
   - 先遇 `bash` 拒绝 → `rejection_observation()` 生成一条 **role: user** 的“Tool call to bash was rejected...”消息并立即 append；
   - 再执行合法调用 → append 一条 role: tool 的结果。
4. wire 上实际是：

```
assistant(tool_calls=[call_01])  →  user(工具拒绝说明)  →  tool(call_01)
```

DeepSeek 要求 assistant 的 `tool_calls` 后面必须**紧跟**对应 tool 消息，中间不能夹 user，于是 400。

`constraints-scheduling` 完全同型：`execute_code`（缺 `code` 参数，被拒）+ `run_command`（合法），assistant→user(拒绝)→tool。

#### 为什么 B 没兜住

`_drop_dangling_tool_calls` 只检查“这个 call_id 在整个列表里有没有 tool 响应”，不管顺序。这里 `call_01` 的 tool 响应确实存在（只是被 user 消息隔开），所以它判定“无悬空”，一条也不丢。重试 6 次发的是同一份非法 payload，所以 6 次全 400。

#### 与 §8 建议的排查路径对照

报告说“若 400 再现，先查被 contract 打回的 submit 与 `dangling_tool_calls_dropped` 是否同时出现”。这次两者都没有：**不是 submit，`dangling` 也是 0**。真正的新观测维度是“**assistant tool_calls 与 tool 响应之间是否被非 tool 消息打断**”。我按这个规则重放 v9：正好只有这 2 次发送违规，且 2 次都 400，预测精度 100%。

---

## 3. 建议修复

1. **修顺序（最小改动）**：`ToolRunner.dispatch` 改两阶段——先执行并 append 所有合法调用的 tool 消息，再统一 append 被拒调用的 user 说明；这样 assistant 的 `tool_calls` 后永远先接满 tool 消息。
   - 位置：`src/agent_runtime/agent/tool_runner.py:78-88`、`src/agent_runtime/agent/tool_dispatch.py:200-212`。
2. **wire 层加“紧邻配对”校验**：现有 `_drop_dangling_tool_calls`（`providers/llm.py:149`）只做全局 id 存在性判断，建议增加顺序校验/重排，并把修复动作记为独立事件（如 `llm.wire.contract_repaired`），别只复用 dangling。
3. **400 invalid_request 不要重试 6 次**：这类请求级 400 重发同一 payload 不可能成功，应在第一次就 fail fast，或先做本地修复再重试。
4. **补回归测试**：一轮内 `[非法调用, 合法调用]` 的混合批次，断言 wire 上 assistant `tool_calls` 后紧跟对应 tool 消息；可直接用 `build-pmars` iteration 17 的形态做 fixture。
5. **新增观测点**：在 trace 里记录“adjacency violation”计数（assistant tool_calls 到 tool 响应之间夹了非 tool 消息），这是本次 §8 三条观测点都漏掉的一维。

---

## 4. 后续观测点

- 重跑 `build-pmars` 和 `constraints-scheduling` 这两个任务，验证 `LLM error 400 ... tool_calls must be followed by tool messages` 归零。
- 每轮同时确认 `llm.wire.dangling_tool_calls_dropped` 仍为 0；若 400 再现而 dangling 为 0，按 §2.1 的 `assistant → user → tool` 顺序违规继续定位。
