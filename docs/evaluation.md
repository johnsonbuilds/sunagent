# 评测契约：run.config、run.metrics 与指标口径

本文是评测链路的**契约文档**：观测埋点写什么、指标怎么算、诊断报告长什么样。
代码实现见 `src/agent_runtime/metrics.py`（记录结构）、`src/agent_runtime/trace.py`
（trace 写入）、`webui/evaluation.py`（聚合与 API，Phase 2 起）。

设计取舍与最终方案见 `docs/harness-evaluation.md`，本文只固定契约。

## 1. 非目标

- 不做 workspace 快照，不做后验 verify，不做 SQLite，不做综合总分。
- 不自动诊断：诊断只由用户点击触发，一次点击 = 一个普通会话。
- 不宣称安全边界：guard 拦截次数只是诊断信号。

## 2. 存储与写入责任

```
runtime_home/                     # AGENT_RUNTIME_HOME，默认 workspace/
├── traces/run-*.jsonl            # 权威：完整可回放，含 run.config / run.metrics
├── metrics/runs.jsonl            # 索引：一行一条 run.metrics
├── sessions/*.json               # 权威：会话（含诊断会话的 origin/target）
├── board.json                    # 当前状态权威
└── board_events.jsonl            # board 生命周期事件（不含诊断）
```

| 文件 | 写入者 | 说明 |
|---|---|---|
| `traces/run-*.jsonl` | `RunTrace`（`src/agent_runtime/trace.py`） | 每 run 一个文件，事件 append-only |
| `metrics/runs.jsonl` | **调用方**：`webui/server.py`、`run_agent.py`、`integrations/harbor.py` | 调 `metrics.finalize_run()`，它在 `finally` 里写 `run.metrics` trace 事件 + `append_run_metrics()`，两者不可能不一致 |
| `sessions/*.json` | `SessionStore.save()` | 会话元数据（`origin`/`target`/`report_segment_id` 等） |
| `board.json` | `BoardStore` | 只存当前状态 |
| `board_events.jsonl` | `BoardStore` | 只记 board 生命周期，键为 `task_id` |

- `metrics/runs.jsonl` 里的一行可以**由 trace 重建**；删掉 `metrics/` 不丢信息（见 §2.2）。
- 追加用 `O_APPEND` 写一行（短写会补完/记录 warning），行内不会被其他进程插入；写入方保证一行不超过 1MB。
- 脏行（非法 JSON、合法 JSON 但不是对象、截断行）在读取时跳过并记 warning，不会变成幽灵 run。
- `RunTrace` 只管 trace，不决定 metrics 索引位置。

### 2.1 trace 保留与事件字段（Phase 1）

| 项 | 契约 |
|---|---|
| `retain_events` | `all`（测试）/ `control`（WebUI、Harbor，只留控制事件）/ `none`；payload 事件不入内存但一定落盘 |
| `llm.start` | 每次真实 attempt 一条：`messages` / `request_chars` / `request_hash` / `prefix_hash` / `attempt` |
| `llm.end` | 同一 attempt 的组装响应：`content` / `reasoning_content` / `tool_calls` / `usage` / `ttft_ms` / `duration_ms` / `finish_reason` / `status` |
| `llm.chunk` | 默认关闭（`RunTrace(trace_chunks=True)` 或 CLI `--trace-chunks on`），也是唯一可被丢弃并计入 `dropped_events` 的事件 |
| `tool.start` | 每次已校验调用一条：`tool` / `tool_call_id` / `arguments`（`arguments` 只在这里写，`tool.end` 不重复） |
| `tool.end` | `result`（尾部预览）/ `result_chars` / `spill_path` / `status` / `error_kind` / `exit_code` / `edited_files` / `exec_ms`（调用本身）/ `duration_ms`（含 render 与 spill I/O）；先 render 后发事件，spill 路径与结果同一记录 |
| `context.{strategy}` | 每次实际发生 compaction 一条：`duration_ms` / `original_chars` / `request_chars` / `request_count` / `budget`；`run.metrics` 的 `latency_ms.summarizer` 就是这些 `duration_ms` 之和 |
| `guard.block` / `policy.block` | 保留监听的通用拦截事件；当前实现不发这两个事件，拦截以 `tool.end` 的 `status="error"` + `error_kind="command_blocked"` 出现，`RunStats` 按此计 `policy_blocks` |
| `run.config` / `run.metrics` | 每个 run 各一条；`run.config` 在首个 LLM 请求前发出（git 信息缓存 + 2s 超时） |
| 丢弃与 flush | 队列满时只丢 `llm.chunk`；控制事件改写进 flush 待写区，不丢；`flush` 用 `await queue.put` 投停止位 |

### 2.2 重建索引（`scripts/rebuild_metrics.py`）

`runs.jsonl` 是派生文件：每条 trace 末尾的 `run.metrics` 事件就是它的那一行。
所以索引被删、写坏或换机器时，从 `traces/` 重建即可，无需重算任何指标。

```
uv run python scripts/rebuild_metrics.py                  # dry run，只报告不写
uv run python scripts/rebuild_metrics.py --write           # 原子替换 runs.jsonl
uv run python scripts/rebuild_metrics.py --write --keep-existing
```

| 行为 | 契约 |
|---|---|
| 数据源 | 每条 trace 里**最后一条** `run.metrics` 事件；同一个 `run_id` 出现两次时以 `finished_at` 更晚的为准 |
| 没有 `run.metrics` 的 trace | 跳过并计数（Phase 1 之前的 trace 属于此类）；不重放事件、不估数 |
| 写入 | 先写 `<name>.tmp` 再 `os.replace`，中断不会留下写了一半的索引；行格式与 `append_run_metrics` 完全一致 |
| `--keep-existing` | 合并现有行，**已有行优先**（那行是 run 结束时写的，trace 可能已被截断）；不加则只信 trace |
| 顺序 | 按 `(finished_at, run_id)` 排序，与追加顺序一致 |

默认 dry run：不加 `--write` 只打印将要写多少行、跳过了哪些 trace，
避免恢复操作把人吓一跳地覆盖掉唯一副本。

## 3. Join keys 与 `origin` 切片

每条 `run.config`、每条 `run.metrics`、每个 trace 头部都带同一组键：

| 键 | 含义 |
|---|---|
| `run_id` | 一次 run 的唯一 id；board `runs[].run_id` 必须与 trace 文件同名 |
| `origin` | `cli` / `webui` / `harbor` / `diagnosis` / `improvement` |
| `session_id` | 会话 id；CLI/Harbor 为 `null` |
| `task_id` | board 任务 id；普通聊天、CLI、Harbor 为 `null` |
| `run_index` | 该任务第几次执行，1-based；非 board run 为 `null` |
| `harness_id` / `genes_hash` | harness 身份与基因组指纹 |
| `model` | catalog id |

`origin` 是**切片维度，不是排除**：诊断/改进 run 全量保留在 `runs.jsonl`，
只是不进 harness 指标与 board 指标。

- **Run 级指标**（TCR、工具错误、参数合法性、tokens、latency、TTFT、retry、summary invalid）
  聚合 `origin in {webui, cli, harbor}`，按 `origin / harness / model / day` 切片，
  **不绑** `task_id != null`。
- **Board outcome**（TSR / first-pass / rework / queue wait）只从 `board_events.jsonl` 算。
- **Harbor outcome**（accuracy / reward）复用 `evaluation/records/`。
- 三类指标在页面上分视图展示，不混成一个成功率。

## 4. `run.config`

每次 run 一条事件（`event_type == "run.config"`，`iteration == 0`），记录
「这一跑是在什么条件下发生的」。没有它，trace 再全也复现不了。

| 字段 | 类型 | 含义 |
|---|---|---|
| `run_id` / `origin` / `session_id` / `task_id` / `run_index` | 见上表 | join keys |
| `harness_id` / `genes_hash` / `model` | str | harness 与模型身份 |
| `model_params` | object | `temperature` / `max_tokens` / `stream` 等实际请求参数 |
| `tools` | array | **完整 tool schemas**（名称、描述、参数 schema） |
| `runtime` | object | 白名单后的 runtime 配置（模型目录 id、预算、压缩阈值、`streaming`） |
| `env` | object | 白名单环境变量（`AGENT_RUNTIME_*`），**不含密钥** |
| `git` | object | `commit` / `dirty` / `status_count`；异步 + 超时 + 失败记 `null` |

示例（截断）：

```json
{
  "event_type": "run.config", "iteration": 0,
  "data": {
    "run_id": "run-3f1a...", "origin": "webui",
    "session_id": "sess-20261121-101500-ab12cd", "task_id": "task-9d2f...",
    "run_index": 2, "harness_id": "coding-v1", "genes_hash": "g8c1...",
    "model": "deepseek-v4.1-flash",
    "model_params": {"temperature": 0.2, "max_tokens": 8192, "stream": true},
    "tools": [{"name": "read_file", "description": "...", "parameters": {"type": "object"}}],
    "runtime": {"context_budget_tokens": 60000, "summary_max_tokens": 2048},
    "env": {"AGENT_RUNTIME_HARNESS": "coding-v1"},
    "git": {"commit": "1626ef1", "dirty": true, "status_count": 3}
  }
}
```

## 5. `run.metrics`

计数由 `RunStats` 在**事件发出时**累加（`metrics.RunStats.observe`），不是 run 结束后回扫 `trace.events`；因此 `retain_events=control`（WebUI/Harbor）丢弃 payload 事件不影响任何数字。`finalize_run()` 是唯一汇总入口：`agent.end` 给出 token 台账，`tool.end` 给出工具计数与产物证据，`llm.end` 给出延迟/首字/缓存 token，`context.llm_summary.*` 给出摘要尝试与无效次数。
每次 run 一条事件（`event_type == "run.metrics"`），并原样 append 到
`metrics/runs.jsonl`。结构由 `metrics.RunMetrics` 定义。

| 字段 | 类型 | 含义 |
|---|---|---|
| join keys | 见 §3 | 切片与下钻 |
| `status` | `completed` / `error` / `cancelled` | run 结局；TCR 分子只用 `completed` |
| `has_answer` | bool | 本轮是否产出非空 final answer；正文不重复存（WebUI 在 `session.segments` / board `runs[].response`，CLI/Harbor 在 `agent.end`） |
| `started_at` / `finished_at` / `duration_ms` | float | 端到端时长 |
| `iterations` | int | LLM 迭代次数 |
| `tool_calls` / `tool_errors` | int | 工具调用总数与失败数（按 `tool.end.status` 判定） |
| `error_kinds` | object | `error_kind -> count`（`validation` / `not_found` / `exit_code` / `command_blocked` / …）；schema 校验拒绝统一记 `validation` |
| `edited_files` | array | 成功的 `write_file` / `edit_file` / `apply_patch` 产生的文件；产物证据 |
| `tokens` | object | `prompt` / `completion` / `cached` / `summarizer` / `total` |
| `latency_ms` | object | `llm` / `tool` / `summarizer` / `overhead` |
| `ttft_ms` | float \| null | 首 token 延迟中位数 |
| `retries` | int | LLM 重试次数 |
| `summary_invalid` | int | 摘要被判无效的次数 |
| `summary_attempts` | int | 摘要生成尝试次数；`summary invalid rate` 的分母（一次压缩可能重试，也可能走不调模型的分支） |
| `compactions` | int | 上下文压缩次数（含不调摘要模型的分支） |
| `policy_blocks` | int | guard 拦截次数（仅信号） |
| `error` | `{stage, code}` \| null | 失败阶段与错误类别 |
| `trace_bytes` | int | trace 文件大小，用于体积告警 |
| `dropped_events` | int | 写入队列满时丢弃的事件数（只会是 `llm.chunk`；控制事件永不丢） |

示例（`runs.jsonl` 一行，未格式化）：

```json
{"run_id":"run-3f1a...","origin":"webui","harness_id":"coding-v1","genes_hash":"g8c1...","model":"deepseek-v4.1-flash","session_id":"sess-...","task_id":"task-9d2f...","run_index":2,"status":"completed","has_answer":true,"started_at":1763700000.1,"finished_at":1763700182.4,"duration_ms":182300.0,"iterations":17,"tool_calls":24,"tool_errors":2,"error_kinds":{"validation":1,"not_found":1},"edited_files":["src/app/main.py"],"tokens":{"prompt":412000,"completion":18000,"cached":300000,"summarizer":0,"total":430000},"latency_ms":{"llm":150000.0,"tool":21000.0,"summarizer":0.0,"overhead":11300.0},"ttft_ms":820.0,"retries":1,"summary_invalid":0,"summary_attempts":1,"compactions":1,"policy_blocks":0,"error":null,"trace_bytes":2400000}
```

`tokens` 的两个不变量（Phase 3 堆叠柱状图直接按此渲染，不要重复叠加）：

- `cached` 是 `prompt` 的子集（OpenAI / DeepSeek 口径），只是并列展示，**不计入** `total`；
  数值汇总自 `agent.end` 的台账（`turn_history.token_totals`），provider 适配层按 `cached_tokens` /
  `prompt_tokens_details.cached_tokens` / `prompt_cache_hit_tokens` 三种写法兼容解析，缺失记 `0`；
- `summarizer` 是独立计费的一次调用，`total = prompt + completion + summarizer`，即
  `prompt` / `completion` 里**不含**摘要消耗。

浮点字段（`started_at` / `finished_at` / `duration_ms` / `ttft_ms` / `latency_ms.*`）写入前
必须有限：`NaN` / `Infinity` 不是标准 JSON，写入方会归一为 `0`（`ttft_ms` 归一为 `null`），
`append_run_metrics()` 以 `allow_nan=False` 兜底。

兼容规则：新增字段必须带默认值；读取方忽略未知键、缺失键取默认值。所以历史行
不因加字段而失效，加字段也不算破坏性变更。

## 6. 指标口径

| 指标 | 分母 | 数据源 | 判定 |
|---|---|---|---|
| TSR | 进入过执行的任务（有 `run_claimed`） | `board_events.jsonl` | 人工验收：`done` |
| First-pass acceptance | 进入过执行的任务 | `board_events.jsonl` | 首次 run 后直接 `done`，无返工 |
| Rework rate | 进入过 `review` 的任务 | `board_events.jsonl` | 规则：首次 run 完成后 `review → todo/doing` |
| TCR | 发起的 run | `run.metrics` | 规则：`status == completed` |
| VCR | 声明的"需要产物"的 run | `run.metrics` | 规则：`completed` + `has_answer` + `edited_files` 非空 + `policy_blocks == 0` |
| Hallucinated completion | 声称完成且该 run 有 `run.metrics` 的任务（`claiming_tasks`） | `board_events` + `run.metrics` | 规则：`runs[].response` 声称完成但该 run `edited_files` 为空，且任务随后被退回（`review → todo/doing`，或执行中追加评论）；给**计数 + 率** |
| Tool error rate | 工具调用总数 | `run.metrics`（总览）/ trace `tool.end`（按工具拆分） | 规则：`tool_errors / tool_calls`；拆到具体工具时读 trace |
| Argument validity | 工具调用总数 | `run.metrics`（总览）/ trace `tool.end`（按工具拆分） | 规则：`error_kinds` 占比、schema 通过率（`error_kinds["validation"] / tool_calls`） |
| Tokens / task | 进入过执行的任务 | `run.metrics` | 规则：任务内 run 求和 |
| Latency p50 / p95 | 发起的 run | `run.metrics` | 规则：`duration_ms` 分位 |
| TTFT | LLM 请求数 | trace `llm.end` | 规则：`ttft_ms` 分位 |
| Queue wait | 被 claim 的 run | `board_events.jsonl` | 规则：`run_claimed.queue_wait_ms` |
| Error / timeout / retry rate | 发起的 run | `run.metrics` | 规则：按阶段与 provider 分组 |
| Summary invalid rate | 摘要尝试次数（`summary_attempts`） | `run.metrics` | 规则：`summary_invalid / summary_attempts` |
| Policy block count | 命令总数 | trace | 规则：仅诊断信号 |

边界规则：

- **TSR 分子必须"进入过执行且 done"**，否则 backlog 直接拖到 done 会让 TSR > 100%。
- **`done` 只看当前列**：卡片被退回 `todo` / `review` / `doing` 后不再计入 `funnel.done` 与
  TSR；历史里「曾到过 done」不算验收（`reached_review` 仍按历史算，它描述的是"被审阅过"）。
- **返工只算执行过之后**：`review → todo/doing`，或 doing/review 期间追加评论触发下一轮；
  未执行就加评论不算返工。
- **Queue wait 必须在 claim 时算**：`claim_next_todo()` 会清空 `queued_at`。
- **产物指标允许 `null`**：`BoardTask.expects_artifact` 显式为 `true/false` 时优先；
  为 `null` 时按 harness 判定（`coding-v1` / `terminal-v1` 需要产物，
  `general-v1` / `readonly-v1` 不需要）。不需要产物的任务，VCR / hallucinated
  completion 记 `null`，不判失败。
- **没有 `run.metrics` 的声明不算幻觉**：Phase 1 之前的历史 run 无法判定产物，只计进
  `artifact.unmeasured_claiming_tasks`，既不进分子也不进分母（`task_detail` 对应 run 标
  `evidence: "missing"`）。
- **VCR 分母靠 `task_id` 关联 `board.json`**：`run.metrics` 不重复存 `expects_artifact`，
  查询时用 `run.task_id → BoardTask.expects_artifact` 判定（`null` 时按 harness 规则）；
  非 board run（CLI / Harbor）不参 VCR。
- **产物证据**来自成功的编辑类工具调用产生的 `edited_files`，不依赖事后 `git diff`。
- 本表是本期落地的 15 项（P0 + 少量 P1）。§3.1 里 `Partial progress` / `Answer correctness` /
  `Recovery rate` / `Step efficiency` / `Consistency` 等 P1/P2 项暂不实现，补齐时按本表格式追加。
- 不做 Satisfaction、不做综合总分。

## 7. 诊断报告格式契约

诊断是一次普通 run turn：会话 `harness=evaluation-v1`、`origin=diagnosis`、
`target={type: task|run|cohort, ...}`。报告就是该会话首轮 assistant 消息，
`Session.report_segment_id` 指向它；追问不覆盖该指针。

模型输出 Markdown，四个二级标题**必须出现且顺序固定**：

```markdown
## 结论
一句话结论。

## 根因
- 类别：tool_argument
- 置信度：0.7
- 影响 run：`run-3f1a...`
- 说明：...

## 证据
- `run-3f1a...` / iteration 12 / `evt-8a2c...` — 引文；为什么支持该根因

## Top 5 改进建议
1. **收紧 apply_patch 的 old_text 提示** — 影响指标：`tool_error_rate`；影响面：cohort；成本：low；验证：重跑 7d 内 apply_patch 失败任务
2. ...
3. ...
4. ...
5. ...（最多 5 条；不足 5 条就停，不要凑数）
```

规则：

- `类别` 是建议词表（`tool_argument` / `context_loss` / `loop_stall` / `runtime` /
  `model_capability` / `task_environment` / `ambiguity` / `safety_signal` / `other`），
  不是规则表；模型可用 `other` + 自由文本。
- 每条证据尽量带 `run_id` / `iteration` / `event_id`；引用不到就明说是推断。
- 建议必须绑定：影响指标、影响面、成本、验证方式。模型不自动改 harness。
- 解析失败（标题缺失、顺序错、条数不足）只在 UI 标注，**不拒绝**诊断，
  报告原样渲染。

## 8. 聚合与 API（Phase 2）

实现在 `webui/evaluation.py`（`EvaluationStore`），路由挂在 `webui/server.py`。
前端页面见 `docs/webui.md` §7（`/evaluation`，八个 Tab）。
三类指标**分视图返回，不合并成一个成功率**：

| 视图 | 端点 | 数据源 |
|---|---|---|
| Run 级 | `GET /api/evaluation/overview` | `metrics/runs.jsonl`，`origin in {cli, webui, harbor}` |
| Board outcome | `GET /api/evaluation/board-overview` | `board_events.jsonl` + `board.json` |
| Harbor outcome | `GET /api/evaluation/benchmarks` | `evaluation/records/*.json` |

- 索引在首次查询时按文件大小增量刷新（只解析新追加的整行）；文件变小，或 inode 变了
  （原子替换式轮转，新文件可能更大），都整体重建。半行不会被解析，补齐后再读。
- 行按 `run_id` 去重（`dict` 保序，后行覆盖前行）：重复的一行不会把指标算两次。
- 失败的行（非法 JSON、非对象、缺 `run_id`）计进 `skipped_lines`，不进任何指标；
  `POST /api/evaluation/refresh`（`force=True`）重建时该计数归零重数。
- 分母为空的率一律返回 `null`，不返回 `0`：`0%` 和“没有可判定的样本”是两件事。
- `overview()` 对选中的 run 只走一趟循环：所有 KPI、trends 与 slices 在一次遍历里累加，
  每条日/切片时长序列只排序一次并复用于 p50/p95（旧实现按 KPI 分组重复遍历、重复排序，
  10k run 约 0.4 s）；日期分桶按“分钟”缓存到本地日期——本地 UTC 偏移都是整分钟，
  一分钟不会跨越本地午夜，所以结果与逐条 `strftime` 完全一致。
  10k run 的 `overview()` 约 70 ms，`tests/webui/test_evaluation.py` 用它守住交互预算。
- Run 级不绑 `task_id`；`origin=diagnosis/improvement` 的 run 保留在文件里但从
  Run 级聚合中排除，排除条数见 `excluded.runs_by_origin`。
- `range` 取 `24h` / `7d` / `30d` / `90d` / `all`（默认 `7d`）；Run 级按
  `finished_at`（缺失回落到 `started_at`）过滤，Board 视图按任务 `updated_at` 过滤。
- Board 视图的 `artifact` 段给出 VCR 与 hallucinated completion：分母是
  `expects_artifact` 解析为 `true` 的任务（显式值优先，否则 `coding-v1` /
  `terminal-v1` 视为需要产物，`general-v1` / `readonly-v1` 记 `null` 不判定）。
  VCR 按 run 计（`vcr_runs` / `vcr_passed`），hallucinated 按任务计
  （`claiming_tasks` = 声称完成且至少有一条带 `run.metrics` 的 run；
  没 metrics 的历史任务进 `unmeasured_claiming_tasks`，不算幻觉）。
- Board 视图的 `kpis` 含 `tokens_per_task` 与 `runs_per_task`（分母为进入过执行的任务），
  Phase 3 页面直接消费，不另算。
- `overview()` 还返回固定分桶的 `histograms`（`iterations` 与 `duration_ms`）：
  分桶边界写死在 `ITERATION_BUCKETS` / `DURATION_BUCKETS`，标签语言中立（`0–1`、
  `33+`、`<1s`、`5m+`），前端原样渲染，不做二次翻译。分桶在同一趟循环里累加，
  所以页面要直方图不必再多读一遍 trace。
- `GET /api/evaluation/tasks` 显式传 `origin` 时只保留有匹配 origin run 的任务
  （未传时保留尚未执行的 backlog/todo 卡片）。
- 返工事件来源：`status_changed`（`review → todo/doing`）与 `comment_added`
  （`from_status` 为 `review` 或 `doing`），只统计首次 `run_claimed` 之后的；
  `comment_added.from_status` 为空的历史行不计。两份事件用两种键名（`from`/`to` 与
  `from_status`/`to_status`），读侧两种都认。
- `queue_wait` 只在 `run_claimed.queue_wait_ms` 上取（claim 会清空 `queued_at`）。
- `GET /api/evaluation/runs/{run_id}` 返回 `run.metrics` + trace digest：
  事件类型计数、工具聚合、LLM 请求/重试/finish_reason、错误样本、`run.config` 摘要。
  digest **不含 payload**（messages / response / 工具结果），最多读 32MB，
  超出标 `truncated`，并按 `(mtime, size)` 缓存。
- 诊断会话只从 `SessionStore` 的 `origin`/`target` 建索引（`/diagnoses`），
  不写入 `board_events.jsonl`。

## 9. 诊断：bundle、只读工具与 `/diagnose`（Phase 4）

实现在 `src/agent_runtime/diagnosis.py`（bundle + 只读工具 + 报告解析）与
`webui/evaluation.py`（`EvaluationStore.diagnose` / `diagnoses`）、`webui/server.py`
（`POST /api/evaluation/diagnose` 起 turn）。

### 9.1 存储与目标

```
<runtime_home>/diagnosis/<diagnosis_id>/
├── bundle.json     # 结构化证据，≤ 1 MB
└── summary.md      # 指标口径 + 聚合结果，便于模型快速定位
```

- `diagnosis_id` 形如 `diag-<hex12>`，只接受 `[A-Za-z0-9._-]{1,64}`；路径不能穿越。
- 产物属 `storage.runtime_home()`（`AGENT_RUNTIME_HOME`），不是 sandbox 根：模型通过
  `read_diagnosis` 读，不靠 `read_file`。
- 目标三选一：`{"type":"task","id":...}`、`{"type":"run","id":...}`、
  `{"type":"cohort","filters":{range,status,harness,model,origin}}`；未知键被丢弃。

### 9.2 bundle 结构

`target` / `task`（或 `cases`）/ `runs` / `environment` / `human_feedback` /
`aggregate` / `generated_at`，与 `docs/harness-evaluation.md` §5.2 一致，补充：

- 每个 run 带 `metrics`（`status` / `tokens` / `tool_calls` / `tool_errors` /
  `ttft_ms` / `policy_blocks` / `edited_files` / `reworked`…）、`failure`、
  `iterations`（每次迭代的 `llm` 汇总、`tools[]`、`context[]`，**每条带 `event_id`**）、
  `tool_errors`、`context_events` 与 `trace`（`available` / `events` / `bytes` / `truncated`）。
- `environment.edited_files` 只来自成功的编辑类工具调用；`diff_summary` 保持 `null`
  （共享 workspace 的后验 `git diff` 会串味，见 9.2）。
- `human_feedback.verdict` 取 board 现状：`rework` / `accepted` / 其他状态名。
- `truncated_input=true` 时按 **失败 run > 返工 run > 通过 run** 依次丢细节；
  通过 run 最先失去 `iterations`。`bundle_hash` 是证据指纹，**不含 `generated_at`**，
  所以同一份证据隔几分钟再诊断仍命中 `duplicate`。截断标记一旦置位就不再被清除
  （`write_bundle` 会二次压缩，清除会让落盘证据和 `session.bundle_hash` 各说各话），
  字节预算按落盘格式（`indent=2`）计算。
- cohort 的 `cases` 最多 20 张紧凑卡片（`task_id` / `status` / `runs` / `metrics` /
  `top_signal` / `run_ids` / `evidence[]`），不塞完整 trace；`evidence` 里是每个
  run 的 `run_id` + 可引用的 `event_id`（从 trace 抽取）。`filters` 支持
  `range` / `status` / `harness` / `model` / `origin`，后两者口径与任务表一致。

### 9.3 只读工具

`harnesses/evaluation-v1.yaml` 声明 `internal: true`，只在该 harness 下注入
（`webui/turn_context._turn_tools`），通用 registry 不认识这些名字：

| 工具 | 行为 |
|---|---|
| `read_diagnosis(diagnosis_id, file, offset, limit)` | 只读 `bundle.json` / `summary.md`，单次 ≤ 20 000 字符 |
| `read_trace(run_id, event_type?, event_id?, iteration?, limit?)` | 按条件抽事件，`data` 截断到 600 字符 |
| `search_trace(run_id, pattern, limit?)` | 大小写不敏感子串搜索，返回 `event_id` + 上下文片段 |

三者都直读 `storage`，绕开 sandbox（`AGENT_RUNTIME_WORKSPACE` 指到子目录时也读得到）。

### 9.4 `/diagnose` 契约

```
POST /api/evaluation/diagnose  {"target": {...}, "force": false}
→ {"duplicate": false, "session_id", "bundle_hash", "diagnosis_id", "run_id", "bundle_path"}
→ {"duplicate": true,  "existing_session_id", "bundle_hash", "diagnosis_id"}
```

- `force=false` 且同 `target + bundle_hash` 已有**正在运行或已有报告**的会话：不新建，
  返回 `existing_session_id`。失败/取消且没有报告的会话不参与去重，否则这份证据
  再也诊断不了。
- 新建时顺序固定：写 bundle → 建会话（`harness=evaluation-v1`、`origin=diagnosis`、
  `target`、`diagnosis_id`、`bundle_hash`）→ 发首条消息（短指令 + `diagnosis_id`）→
  立即返回 `session_id`。turn **正常结束**后才把最后一个非空 assistant segment id
  写进 `report_segment_id`（仅当为 `None`）；报错/取消不冻结残缺正文，追问不覆盖它。
- 模型用 `AGENT_RUNTIME_DIAGNOSIS_MODEL`（catalog `id`，`catalog.require()` 解析），
  未设置时用默认模型；模型不可用返回 400/503，此时已建的会话与 bundle 一并清理。
- 诊断不写 `board_events.jsonl`，不进 TSR 分母；trace 的 `origin` 从 `session.origin`
  继承，所以追问 run 仍按 `diagnosis` 切片。

### 9.5 报告解析与证据标注（best-effort）

`diagnosis.parse_report` 按四个二级标题切段并抽取 `Top 5` 的编号条目
（条目后的续行并入同一条）；`diagnosis.verify_report` 校验 `evt-…` / `run-…`
引用。校验范围是 **bundle + 相关 trace**：bundle 里找不到的 `event_id` 会再去
bundle 涉及（以及报告正文引用）的 run trace 里核对，只在两处都不存在时才标
`verified: false`——模型被要求用 `read_trace` 核实，只查 bundle 会误伤真实引用。
每条 trace 的 `event_id` 集合按 `(mtime, size)` 缓存，**只有完整扫描的结果入缓存**，
所以缓存命中就足以回答任意引用：报告里引用了一个根本不存在的 `evt-…` 也不会
让 `/diagnoses` 每次刷新都重扫 trace（模型自造的 id 正好是最常见的情形）。

`GET /api/evaluation/diagnoses` 的每行带 `report`：

```json
{"available": true, "markdown": "## 结论\n…",
 "verification": {"citations": [{"kind": "event", "value": "evt-…", "verified": false}],
                  "unverified": ["evt-…"], "missing_sections": [], "order_ok": true,
                  "complete": true, "suggestion_count": 5},
 "suggestions": [{"index": 1, "text": "**动作** — 影响指标：…"}]}
```

标注只影响展示：标题缺失、顺序错、引用找不到都**不拒绝**诊断，报告原样渲染。
前端 `MarkdownAnswer` 分段渲染并列出未验证引用，第 4 节直接渲染
`SuggestionList`（父组件不再重复渲染一遍）；不足 5 条时提示实际条数，不补造。
