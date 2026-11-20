# 最终方案与实施计划

## 0. 已锁定的决策

| 决策 | 结论 |
|---|---|
| workspace 快照 | 不做 |
| `done` 语义 | 人工验收通过 |
| 后验 verify | 不做 |
| SQLite | 不做；纯文件 + 内存索引 |
| 安全边界 | 不做独立安全评测，只保留 guard 拦截计数作为诊断信号 |
| Satisfaction | 不做 |
| 诊断形态 | 一次真实 run turn：手动点击 → 新建会话（`harness=evaluation-v1`）→ 只读工具核实 → Markdown 输出 |
| 诊断触发 | 仅手动触发；不自动、不轮询、不进 board 执行队列 |
| 诊断输出 | Markdown + 固定标题（结论 / 根因 / 证据 / Top 5 改进建议），不强制 JSON |
| 诊断链接 | 链接记录在 session 元数据（`origin` + `target`），不写入 `board_events.jsonl` |
| 诊断入口 | 一个 `POST /api/evaluation/diagnose`；task / run / cohort 只改 `target` |
| 交付物 | 评测 / 诊断 / 改进三大块 + WebUI 评测页 |

---

# 一、总体架构

```
                    ┌─────────────────────────────┐
                    │        Harness 评测          │
                    │                             │
  运行现场 ────────▶│ 观测：trace / board / session│
                    │ 判定：有效性/过程/效率/可靠性 │
                    │ 实验：重复、配对、统计        │
                    └──────────────┬──────────────┘
                                   │ 指标 + case
                                   ▼
                    ┌─────────────────────────────┐
                    │   诊断（手动，独立会话）       │
                    │ bundle 落文件 + 只读工具核实   │
                    │ → 根因 + 证据 + Top 5         │
                    └──────────────┬──────────────┘
                                   │ 诊断会话 + 根因
                                   ▼
                    ┌─────────────────────────────┐
                    │           改进               │
                    │ 汇总诊断会话 → Top 5 建议     │
                    │ → 人工确认 → 单 gene 变异     │
                    │ → 配对复测                   │
                    └─────────────────────────────┘
```

观测是评测的子能力，不是并列项；诊断消费评测结果；改进消费诊断结果。

---

# 二、观测设计

## 2.1 Trace 保留策略

| 事件 | 保留内容 | 频率 |
|---|---|---|
| `run.config` | 模型参数、完整 tool schemas、runtime 配置、env 白名单、git commit/status | 每 run 一条 |
| `llm.start` | **完整 `messages`** + `request_chars` / `request_hash` / `prefix_hash` | 每 LLM 请求一条 |
| `llm.end` | **拼装后的 response**：`content` / `reasoning_content` / `tool_calls` / `usage` / `ttft_ms` / `duration_ms` / `finish_reason` | 每 LLM 请求一条 |
| `llm.chunk` | **默认关闭** | 需要时 `--trace-chunks on` |
| `tool.end` | 参数、结果（超限落 spill）、`status`、`error_kind`、`exit_code`、`edited_files`、`exec_ms` | 每工具调用一条 |
| `context.*` | 触发原因、压缩前后大小、summary invalid、成本 | 每次压缩 |
| `verification.*` | 结果、缺失项、rerun 成本 | 每次验证 |
| `run.metrics` | 运行级指标汇总，不含 final answer | 每 run 一条 |

关键点：

- `messages` **只写一次**，放在 `llm.start`；`llm.end` 不再重复。
- final answer 不重复存：WebUI 在 `session.segments` / board `runs[].response`；CLI/Harbor 在 `agent.end` 带一份。
- 全量 messages 保留后，`run.config` 才是“可复现”的另一半；没有它，模型参数、tool schema、runtime 条件仍然对不上。
- `llm.start` / `llm.end` 下沉到单次真实 attempt（`_chat_response`），重试另发 `llm.retry`；`llm.start` 只带请求 `messages`，`llm.end` 只带响应/usage/timing，避免同一份 meta 写两次。

## 2.2 内存与写入

- `RunTrace.events` 增加 `retain_events = all | control | none`：
  - 测试：`all`
  - WebUI / Harbor：`control`（只留控制事件；payload 事件不常驻）
- Harbor 不再依赖 `trace.events` 全量，改用 `trace.agent_error()` / `trace.event_count`；sink 的 `last_event` 不再塞全量 `messages`。
- `EventEmitter` 不再常驻每个 `assistant.delta`；实时消费靠订阅，历史靠 session segment。
- 写入仍走异步 writer；只有 `llm.chunk` 可丢并计 `trace.dropped`，关键事件（`run.config` / `run.metrics` / `agent.end`）永不丢；`flush()` 用 `await queue.put(_STOP)`。
- 序列化用 `ensure_ascii=False, separators=(",", ":")`，减少体积和 CPU。
- 不做 gzip、不做快照；只记录 `trace_bytes`，超过阈值告警。

## 2.3 Join keys 与 board 事件

Trace 头部和 `run.config` 增加：

```
origin: cli | webui | harbor | diagnosis | improvement
run_id
session_id
task_id            # 诊断/改进会话为 null
run_index
harness_id
genes_hash
model
```

`origin` 是**切片维度，不是排除**：诊断/改进 run 全量保留在 `runs.jsonl`；Run 级指标聚合 `origin in {webui, cli, harbor}`，按 `origin/harness/model/day` 切片，不绑 `task_id != null`；Board outcome 与 Harbor outcome 分开算（见 9.4）。

Board 增加 append-only 事件流 `workspace/board_events.jsonl`（只记录 board 生命周期，不记录诊断）：

```json
{"task_id":"task-...","at":...,"type":"status_changed","from":"review","to":"todo","actor":"user"}
{"task_id":"task-...","at":...,"type":"comment_added","run_id":"run-..."}
{"task_id":"task-...","at":...,"type":"run_finished","run_id":"...","status":"completed"}
```

事件至少覆盖：`task_created`、`task_deleted`、`status_changed`、`comment_added`、`run_claimed`（带 `queued_at` / `queue_wait_ms`）、`run_requeued`、`run_finished`。

`board.json` 仍是当前状态权威；事件流只用于历史与指标，丢了可以降级，不影响运行。

## 2.4 存储：纯文件 + 内存索引

```
runtime_home/  (默认 workspace/)
├── traces/run-*.jsonl                # 权威，完整可回放
├── sessions/*.json                   # 权威
├── board.json                        # 当前状态权威
├── board_events.jsonl                # board 生命周期（不含诊断）
├── diagnosis/<id>/                   # 诊断证据快照（bundle.json + summary.md）
└── metrics/
    └── runs.jsonl                    # 一行一个 run.metrics，评测主数据源
```

- 评测服务启动时只读 `runs.jsonl` + `board_events.jsonl`，并从 `SessionStore` 读取诊断会话的 `origin/target`。
- 详情页按 `run_id` 读对应 trace，带 mtime 缓存。
- 诊断结果不单独缓存：结果就是诊断会话首轮报告，`Session.report_segment_id` 指向它。
- 不做 SQLite；数据量到“几十万 run、查询 >10 秒”再考虑。
- `runs.jsonl` 按月轮转即可，当前不需要。

---

# 三、评测指标体系（最终版）

## 3.1 指标表

| 维度 | 指标 | 定义 | 数据源 | 判定 | 优先级 |
|---|---|---|---|---|---|
| **有效性** | **TSR** | 进入过执行且当前/验收为 `done` / 进入过执行 | board events | 人工验收 | P0 |
| | **First-pass acceptance** | 首次 run 后直接 `done` / 进入过执行 | board events | 人工验收 | P0 |
| | **Rework rate** | 完成过至少一次 run 后，`review → todo/doing` 或 doing/review 期间追加评论触发下一轮 / 进入 review | board events | 规则 | P0 |
| | **TCR** | 正常结束 run / 发起 run | run.metrics | 规则 | P0 |
| | **VCR** | 正常结束 + 非空答复 + 有产物证据 + 无策略拦截 / 发起 run（仅声明需要产物的任务） | run.metrics + tool.end | 规则 | P0 |
| | **Hallucinated completion** | 声称完成但无产物证据，且被退回（仅声明需要产物的任务） | board + tool.end | 规则 | P0 |
| | Partial progress | 勾选 acceptance 项 / 总项（仅当 description 有 checklist） | description + board | 解析 | P1 |
| | Answer correctness | 0/1 或 0–1 | 人工 / 模型判定 | 人工优先 | P1 |
| | Groundedness | 有证据支撑的结论 / 结论总数 | answer + trace | 模型判定（按需） | P2 |
| **过程** | **Tool error rate** | 失败调用 / 总调用 | trace | 规则 | P0 |
| | **Argument validity** | schema 通过率、执行/编辑成功率 | trace | 规则 | P0 |
| | Recovery rate | 错误后 K 步内恢复 / 错误数 | trace | 规则 | P1 |
| | Step efficiency | 实际 steps / 参考 steps；重复调用率 | trace | 规则 | P1 |
| | Tool selection accuracy | 工具类别正确 / 总调用 | trace | 规则 + 抽样 | P2 |
| | Trajectory quality | 探索→编辑→验证→提交 rubric | trace | 模型判定（按需） | P2 |
| | Plan adherence | 覆盖计划项 / 总项 | trace | 模型判定（按需） | P2 |
| **效率** | **Tokens / task** | prompt / completion / cached / summarizer | run.metrics | 规则 | P0 |
| | **Latency p50/p95** | 端到端 + 分阶段 | run.metrics | 规则 | P0 |
| | TTFT | 首 token 延迟 | trace | 规则 | P1 |
| | Queue wait | `started_at − queued_at` | board events | 规则 | P1 |
| | Cost / success | 估算成本 / 验收通过数 | run.metrics + pricing | 规则 | P2（需先加 pricing） |
| **可靠性** | **Error / timeout / retry rate** | 分阶段、分 provider | run.metrics | 规则 | P0 |
| | Summary invalid rate | invalid / attempts | run.metrics | 规则 | P1 |
| | Consistency | 同一任务 N 次 pass^k / 方差 | 重复运行 | 实验 | P1 |
| | Rephrase robustness | 原任务 vs 改写任务成功率差 | 改写集 | 实验 | P2 |
| **安全** | Policy block count | guard 拦截次数 / 总命令 | trace | 规则 | P1（仅信号） |
| | Unauthorized side effects | 工作区外写入 / 破坏性操作 | diff + audit | 规则 | P2 |
| **UX** | **Rework rate** | 见上 | board events | 规则 | P0 |
| | Handoff rate | 手动改文件 / 新开会话 / 退回 | board + session | 规则 | P1 |
| | Rephrase rate | 相邻 user 消息相似/纠正词 | session | 规则 + 抽样 | P2 |
| | Abandonment | 出错后无后续 / 任务删除 | session + board | 规则 | P2 |

说明：

- 安全本期只做 **Policy block count** 作为诊断信号，不宣称安全边界。
- `Partial progress` 不强制用户写 acceptance；没有 checklist 就是 `null`，不臆造。
- `Groundedness` / `Trajectory quality` 依赖模型判定或人工，默认不自动评分。
- `Consistency` / `Rephrase robustness` 属于实验，不是日常观测。
- 产物证据 = 成功的 `write_file` / `edit_file` / `apply_patch` 产生的 `edited_files` 非空，或存在有效 `submit_result`；不依赖事后 git diff。
- 没有产物语义的任务（调研、问答、只读）VCR / Hallucinated completion 记 `null`，不判失败。
- TSR 分子必须约束“进入过执行”，否则 backlog 直接拖到 done 会让 TSR > 100%。

## 3.2 不算的指标

- Satisfaction / NPS：样本不足，去掉。
- 综合总分：不做；用“TSR 最大化 + tokens/latency 约束”。

---

# 四、WebUI 评测页

## 4.1 路由与入口

现有 `/observability` 是占位页，直接改造成评测页：

| 项 | 改动 |
|---|---|
| `routes.ts` | `observability` → `evaluation`，path `/evaluation`，label `navEvaluation` |
| `views.tsx` | `ObservabilityPage` → `EvaluationPage` |
| `IconRail.tsx` | 复用 `ObservabilityIcon`，或新增 `EvaluationIcon` |
| `i18n.tsx` | 中英文 `navEvaluation` / `eval*` 文案 |
| 删除 | `views/ObservabilityPage.tsx` 占位页 |

`evaluation-v1` 建议加 `internal: true`，在 `/api/config` 的 harness 列表里过滤；不加也能跑，只是会出现在普通聊天和看板任务的下拉里。

## 4.2 页面信息架构

```
评测                                      [时间范围] [Harness] [模型] [刷新]
────────────────────────────────────────────────────────────────────────
KPI: TSR | First-pass | Rework | TCR | VCR | Tokens/task | P95 | Runs/task
────────────────────────────────────────────────────────────────────────
Tab: 总览 | 有效性 | 过程 | 效率 | 可靠性 | 任务 | 诊断与建议
```

### 总览

- 任务漏斗：`创建 → 进入执行 → run 完成 → review → done`
- TSR 趋势（按天）
- 失败/返工原因分布（堆叠条）
- Top offenders：耗时、token、返工最多的任务

### 有效性

- TSR / first-pass / rework 趋势
- Partial progress 分布（有 checklist 的任务）
- Hallucinated completion 计数与列表
- 按 harness / model / tag 切片

### 过程

- 工具错误率 by tool（横向条）
- 参数合法性：schema / 语义 / 编辑命中
- 恢复率、错误后重复率
- iterations 直方图、冗余调用 Top

### 效率

- Tokens/task 堆叠趋势（prompt / completion / cached / summarizer）
- 延迟拆分：LLM / tool / summarizer / verify / overhead
- TTFT p50/p95、queue wait
- Cost/success

### 可靠性

- error / timeout / retry 按阶段和 provider
- summary invalid、compaction 次数
- 运行时长分布
- 有重复运行时显示 consistency

### 任务

表格列：

```
任务 | 状态 | Harness | 模型 | Runs | TSR | Tokens | Wall | Rework | 最后错误 | 操作
```

操作：查看详情、打开会话、查看 trace、开始诊断（手动）。

### 诊断与建议

- 选择任务 / run / cohort
- 「开始诊断」（手动）→ 新建诊断会话 → 模型用只读工具核实 → 输出 Markdown（结论 / 根因 / 证据 / Top 5）
- 诊断会话与普通会话同等：出现在会话列表、可继续追问、可换模型重跑
- 任务详情显示「历史诊断」列表，来自 `Session.target`
- cohort 诊断走同一入口，`target.type=cohort`：bundle 带 N 个 case 的紧凑摘要 + 聚合指标，仍在同一个会话里给 Top 5
- 不自动运行：页面加载、轮询、任务完成都不会触发诊断

## 4.3 前端组件

不新增图表库，用轻量 SVG/CSS：

| 组件 | 用途 |
|---|---|
| `EvaluationPage.tsx` | 页面骨架、筛选、Tab |
| `EvaluationKpis.tsx` | KPI 卡片 |
| `EvaluationFunnel.tsx` | 任务漏斗 |
| `TrendChart.tsx` | 折线/面积趋势 |
| `BarList.tsx` | 横向条形（工具错误、失败原因） |
| `StackedBars.tsx` | token 构成、延迟拆分 |
| `Histogram.tsx` | iterations / 时长分布 |
| `TaskTable.tsx` | 任务列表、排序、筛选 |
| `TaskDetailDrawer.tsx` | 生命周期 + run 详情 + 证据 |
| `RunTimeline.tsx` | iteration 时间线 |
| `DiagnosisPanel.tsx` | 诊断触发、展示诊断会话最后一条消息、打开会话 |
| `DiagnosisHistory.tsx` | 某任务的历史诊断会话列表（`Session.target`） |
| `MarkdownAnswer.tsx` | 复用现有 `components/Markdown.tsx`，外层做标题分段和证据标注 |
| `SuggestionList.tsx` | 从 Markdown 中提取 Top 5 建议（best-effort） |
| `useEvaluation.ts` | 数据获取、轮询、筛选状态 |

样式新增 `app/styles/evaluation.css`，在 `globals.css` 引入。

## 4.4 后端 API

新增 `webui/evaluation.py`，在 `webui/server.py` 挂载：

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/evaluation/overview?range=7d&harness=&model=` | KPI、趋势、漏斗、失败分布 |
| GET | `/api/evaluation/tasks?...` | 任务行、分页、排序 |
| GET | `/api/evaluation/tasks/{task_id}` | 生命周期、runs、证据摘要 |
| GET | `/api/evaluation/runs/{run_id}` | run.metrics + trace digest |
| POST | `/api/evaluation/diagnose` | 手动触发：`{target:{type:"task"|"run"|"cohort", id?, filters?}}` → 写 bundle → 建会话 → 返回 `{session_id}` |
| GET | `/api/evaluation/diagnoses?task_id=` | 该任务的诊断会话列表（读 `Session.target`） |
| POST | `/api/evaluation/refresh` | 重建内存索引（可选） |

后端用 `EvaluationStore`：

- 启动时加载 `metrics/runs.jsonl` + `board_events.jsonl`，并从 `SessionStore` 读取诊断会话的 `origin/target` 建索引；
- 按文件大小增量刷新；
- 按 `run_id/task_id/harness/model/day` 建内存索引；
- 详情按需读 trace，带 mtime 缓存；
- 诊断会话不进 `board_events.jsonl`；task→诊断的链接来自 `Session.target`；
- 单进程内 `threading.Lock` 保护刷新与读取。

诊断接口只负责建会话并发消息，立即返回 `session_id`；前端订阅该会话的事件流，或轮询 `GET /api/sessions/{id}`。

## 4.5 手动诊断交互（唯一入口）

1. 用户在任务详情、run 详情或 cohort 页点击「开始诊断」。
2. `POST /api/evaluation/diagnose`，只传 `target`；task / run / cohort 走同一个入口。
3. 后端构建 evidence bundle，写 `runtime_home/diagnosis/<id>/bundle.json` 和 `summary.md`；cohort 额外带上 N 个 case 的紧凑摘要（不是要求先跑 N 次诊断）和聚合指标。
4. 新建会话：`harness=evaluation-v1`、`origin=diagnosis`、`target={type,id|filters}`、标题「诊断: xxx」。
5. 发出第一条消息：短指令 + bundle 路径。模型用只读工具核实：`read_trace` / `search_trace` 查 trace，`read_file` / `list_dir` 查 bundle。
6. `run_turn` 返回 Markdown，按约定标题输出：结论 / 根因 / 证据 / Top 5 改进建议。
7. 会话出现在会话列表；评测页嵌入 `report_segment_id` 指向的首轮报告，并提供「打开会话」。
8. 任务详情通过 `Session.target` 显示历史诊断，不写 `board_events.jsonl`。

**不是两次接口调用**：同一个会话、同一份 Markdown 里先给根因、再给 Top 5；cohort 只是 `target` 不同。

**不会自动运行**：页面加载、轮询、任务完成、run 结束都不触发诊断；只有按钮点击会新建会话并发出消息。

---

# 五、诊断与改进建议（模型驱动，手动触发）

## 5.1 原则

- **不写硬编码规则表**；诊断由模型基于已有观测数据完成。
- **只在用户点击时触发一次**；页面加载、轮询、任务完成、run 结束都不自动诊断。
- **诊断就是一次普通 run turn + 一个普通会话**：与 board task 会话同等对待，可查看、追问、换模型重跑、看 trace。
- **不是 board task**：没有验收语义，不进 TSR 分母，不写 `board_events.jsonl`。
- 模型用只读工具自行核实证据（`read_trace` / `search_trace` + `read_file` / `list_dir`），不直接相信 bundle 摘要。
- 输出 Markdown，带固定标题约定；不强制 JSON。
- 每次诊断输出：根因 + 证据 + **Top 5 改进建议**。
- 建议必须绑定：影响指标、影响面、成本、验证方式；模型不自动改 harness。
- 模型不可用或超预算：返回明确错误，不伪造诊断；用户可手动重试。

## 5.2 Evidence bundle

诊断输入不是整条 trace，而是压缩后的 case bundle，写到 workspace 文件：

```json
{
  "target": {"type": "run|task|cohort", "id": "...", "filters": {}},
  "task": {
    "title": "...", "description": "...", "acceptance": ["..."],
    "status": "...", "comments": ["..."]
  },
  "runs": [
    {
      "run_id": "...", "harness": "...", "model": "...",
      "metrics": {"tsr": 0, "iterations": 42, "tokens": 0,
                  "tool_errors": 4, "ttft_p50": 0},
      "failure": {"stage": "...", "code": "..."},
      "iterations": [
        {"n": 12, "llm": {"tokens": 0, "duration_ms": 0},
         "tools": [{"tool": "...", "status": "error",
                    "error": "...", "event_id": "evt-..."}],
         "context": [{"trigger": "...", "before": 0, "after": 0,
                      "event_id": "evt-..."}],
         "verification": [{"outcome": "...", "event_id": "evt-..."}]}
      ],
      "tool_errors": [{"tool": "...", "error": "...",
                       "iteration": 12, "event_id": "evt-..."}],
      "context_events": [],
      "verification_events": []
    }
  ],
  "environment": {
    "diff_summary": "...", "changed_files": ["..."],
    "tests_seen": ["pytest -q ..."]
  },
  "human_feedback": {"verdict": "rework", "comment": "..."},
  "aggregate": {
    "cohort_size": 20, "tsr": 0.4,
    "top_failure_signals": [{"signal": "...", "count": 0}]
  }
}
```

文件布局：

```
<runtime_home>/diagnosis/<id>/
├── bundle.json     # 结构化证据（目标 ≤ 1 MB）
└── summary.md      # 指标口径与聚合结果，便于模型快速定位
```

诊断产物归属 `storage.runtime_home()`（`AGENT_RUNTIME_HOME`），不是 `LocalWorkspace.root`（`AGENT_RUNTIME_WORKSPACE`）。两者在代码里是分开的；`AGENT_RUNTIME_WORKSPACE` 指向子目录时，sandbox 读不到 runtime home。模型通过专用只读工具 `read_trace` / `search_trace` 读取，不靠通用 `grep_search`（它跳过 > 1 MB 文件和 `workspace/` 目录）。

Bundle 规则：

- 每条证据带 `event_id`；模型应引用它，后端做 best-effort 校验。
- 大结果只给摘要/尾部 + spill 路径，避免一次读入过多。
- cohort 诊断先读多条诊断会话的结论，再汇总，不把所有 trace 塞进一次请求。
- 单文件建议 ≤ 1 MB；超出时按“失败 run > 返工 run > 通过 run”排序并截断，同时标 `truncated_input`。

cohort 时 `task` 为空，`runs` 换成 `cases`，每个 case 一张紧凑卡片：

```json
{
  "target": {"type": "cohort", "filters": {"range": "7d", "status": "review"}},
  "cases": [
    {
      "task_id": "task-...",
      "status": "review",
      "runs": 2,
      "metrics": {"tsr": 0, "tokens": 0, "tool_errors": 3},
      "top_signal": "apply_patch: old_text not found (x3)",
      "evidence": ["evt-...", "evt-..."]
    }
  ],
  "aggregate": {"cohort_size": 20, "tsr": 0.4, "rework_rate": 0.35}
}
```

- 卡片由 metrics + trace digest 现场生成，不塞完整 trace；
- 已有诊断会话时直接复用其结论，避免重复分析；
- 模型看到的是“群体”，输出共同根因 + 对群体收益最大的 Top 5。

## 5.3 输出格式：Markdown + 固定标题

诊断和改进都用 Markdown 输出，标题固定、顺序固定：

```markdown
## 结论
一句话结论。

## 根因
- 类别：tool_argument | context_loss | loop_stall | runtime | contract | verification | model_capability | task_environment | ambiguity | safety_signal | other
- 置信度：0.0–1.0
- 影响 run：`run-...`
- 说明：...

## 证据
- `run-...` / iteration 12 / `evt-...` — 引文；为什么支持该根因
- ...

## Top 5 改进建议
1. **动作** — 影响指标：`tool_error_rate`；影响面：cohort/task/run；成本：low/medium/high；验证：重跑哪些任务、看哪个指标
2. ...
3. ...
4. ...
5. ...
```

规则：

- 四个标题必须出现且顺序不变；评测页按标题解析，解析失败时原样渲染，不影响阅读。
- `类别` 是一组建议词表，方便聚合，**不是规则表**；模型可用 `other` + 自由文本。
- `Top 5 改进建议` 固定 5 条；不足 5 条时必须说明原因。
- 排序依据：预期收益 × 影响面 / 成本；这是假设，不是结论。
- case 和 cohort 都在同一个会话里输出：`target` 不同，模板相同；cohort 的 bundle 里带 N 个 case 摘要 + 聚合指标。

## 5.4 Prompt 约束

`harnesses/evaluation-v1.yaml` 的 system prompt 要点：

- 你是 harness 评测诊断器，不是任务 agent；可以读文件核实，但不修改、不执行、不提交。
- 先读 `bundle.json` / `summary.md`，再用 `read_trace` / `search_trace` 查原始 trace，必要时 `read_file` / `list_dir` 核实细节。
- 每条结论尽量引用 `run_id` / `iteration` / `event_id`；引用不到就明说是推断。
- 区分“事实”和“推断”；推断要标置信度。
- 不要因为 TSR 低就直接归因模型；先检查契约、工具、上下文、runtime、验证。
- 建议必须可验证：写清改哪个 gene/tool/prompt、看哪个指标、重跑哪些任务。
- 输出 Markdown，严格使用 `## 结论` / `## 根因` / `## 证据` / `## Top 5 改进建议` 四个标题；不要输出 JSON。

## 5.5 证据校验（best-effort，不做门禁）

有读工具后，模型可以自己核实证据；后端只做轻量标注，不改变模型结论：

| 校验 | 动作 |
|---|---|
| `event_id` 是否出现在 bundle/trace | 不出现 → UI 标“未验证”，不删除 |
| `run_id/iteration` 是否匹配 | 不匹配 → 标“未验证” |
| 四个固定标题是否齐全 | 缺失 → 提示格式不完整，仍原样展示 |
| `Top 5` 条数 | 不足 → 提示，不补造 |
| 指标名是否在指标表 | 未知 → 标“未知指标” |

这些标注只影响展示，不作为拒绝诊断的理由。

## 5.6 模型与预算

诊断不是后台自动评分器，而是一次手动 run turn。

- 只手动触发；没有后台任务、没有轮询触发、不进 board 执行队列。
- harness `evaluation-v1` 决定 prompt、只读工具、`max_iterations` 和上下文预算。
- 模型仍从 `model.json` 的 catalog 选，可用 `AGENT_RUNTIME_DIAGNOSIS_MODEL` 单独指定。
- 单次诊断 = 一个会话；cohort 只是 `target.type=cohort`，仍是一个会话、一次调用。
- 不写 `diagnosis_cache.jsonl`：结果就是诊断会话首轮报告；`Session.report_segment_id` 指向它，追问不覆盖报告。
- `Session` 同时记录 `target` 和 `bundle_hash`；相同 `target + bundle_hash` 已有诊断时，UI 提示“已有诊断，打开 / 重新诊断”。
- `AGENT_RUNTIME_DIAGNOSIS_MODEL` 填 catalog `id`，用 `catalog.require()` 解析完整 endpoint/key，不能复用主模型的 endpoint。
- 预算旋钮：
  1. **范围**：task / run / cohort；cohort 最多 20 个 case。
  2. **单次成本**：模型选择、bundle 大小、输出上限。
  3. **总量**：`RuntimeConfig.diagnosis_daily_limit`；`/diagnose` 返回 `remaining`，超限 429。
- 默认建议：`max_iterations: 6`，`bundle.json` ≤ 1 MB，`max_output_tokens ≤ 4k`，cohort ≤ 20 个 case。
- 主循环目前没有 `max_tokens` 控制；诊断请求单独加 `max_output_tokens`，不改全局 agent 默认。
- 失败/超预算：会话里明确报错，不返回伪诊断；可手动重试。

## 5.7 改进建议如何落地

模型只给建议，不自动改 harness。人工确认后：

1. 选 1 条建议，改一个 gene/tool/prompt；
2. 在固定 smoke 集上跑配对对比；
3. 指标验证通过后，再进全量；
4. 把结果写回 `evaluation/records`，形成闭环。

模型给出的 `expected_effect` 是假设，不是结论；必须用评测验证。改进优先级仍按 ROI：预期收益 × 影响面 / 成本，但最终由人拍板。

---

# 六、实施计划

## Phase 0：契约与骨架（2–3 天）

| 任务 | 文件 | 验收 |
|---|---|---|
| 定义 `run.config` / `run.metrics` schema | `docs/evaluation.md`、`src/agent_runtime/metrics.py` | 字段有定义、有示例 |
| 定义指标口径与诊断 Markdown 约定 | 同上 | 每个指标有分母/来源/判定；四个固定标题有示例 |
| `Session` 增加 `origin` / `target` / `report_segment_id` / `bundle_hash` | `webui/sessions.py` | 诊断会话可索引；追问不覆盖报告；不写 `board_events` |
| 新增 metrics 路径 | `src/agent_runtime/storage.py` | `workspace/metrics/runs.jsonl` 可写 |
| 测试骨架 | `tests/agent/test_metrics.py` | schema 序列化通过 |

## Phase 1：观测落地（4–5 天）

| 任务 | 文件 | 验收 |
|---|---|---|
| `RunTrace` 支持 `run.config` / `run.metrics` / retain 策略 | `trace.py` | 每 run 两条事件；内存不常驻 payload |
| `llm.start` 全量 messages；`llm.end` assembled response；chunk 默认关 | `loop.py`、`chat.py` | messages 只写一次；chunk 0 条 |
| tool 统计（error_kind/exit/edited_files/exec） | `tool_runner.py`、`tools/` | 结果错误不再记 success；spill_path 进 tool.end |
| usage/cache/prefix/finish_reason | `providers/llm.py`、`agent/chat.py`、`agent/tokens.py`、`turn_history.py` | cached tokens、finish_reason 透传 |
| join keys + trace level + `origin` 切片 + run_id 贯穿 | `webui/server.py`、`webui/executor.py` | board run_id == trace run_id；诊断/改进可切片 |
| `EventEmitter` 不常驻 delta | `events.py` | 内存不随 delta 增长 |
| CLI 交互每 turn 独立 trace | `run_agent.py` | 每 turn 一个 run_id、run.config/metrics |
| Harbor sink 裁剪大 payload | `integrations/harbor.py` | `last_event` 不再塞全量 messages |
| board 生命周期事件 | `webui/board.py`、`webui/executor.py` | 状态变更/评论/run 完成都有事件 |
| Harbor 去掉全量 events 依赖 | `integrations/harbor.py` | 仍能拿到 agent.error/event_count |
| 测试 | `tests/test_trace.py`、`tests/webui/test_board.py` | 覆盖新事件与内存策略 |

## Phase 2：评测聚合与 API（4–5 天）

| 任务 | 文件 | 验收 |
|---|---|---|
| `EvaluationStore` 内存索引 | `webui/evaluation.py` | 10k run 启动 < 1s；含 `Session.origin/target` 索引 |
| KPI/趋势/漏斗/切片（Board / Harbor 双轨） | 同上 | Run 级不绑 task_id；Board 与 Harbor 分视图 |
| run 详情 trace digest | 同上 | 按需读取，带缓存 |
| API 路由 | `webui/server.py` | 评测端点全部可用 |
| 测试 | `tests/webui/test_evaluation.py` | fixture 覆盖空数据/正常/异常 |

## Phase 3：前端评测页（5–6 天）

| 任务 | 文件 | 验收 |
|---|---|---|
| 路由/导航/i18n | `routes.ts`、`views.tsx`、`IconRail.tsx`、`i18n.tsx` | `/evaluation` 可访问 |
| 页面骨架 + KPI + Tab | `views/EvaluationPage.tsx`、`components/evaluation/*` | 各维度可切换 |
| 图表组件 | `TrendChart` / `BarList` / `Funnel` / `Histogram` | 无新依赖，主题适配 |
| 任务表 + 详情抽屉 | `TaskTable.tsx`、`TaskDetailDrawer.tsx` | 点击可下钻 |
| 样式 | `app/styles/evaluation.css` | 窄屏可用 |
| 校验 | `npm run typecheck`、`npm run build` | 通过 |

## Phase 4：模型诊断与 Top 5 建议（4–5 天）

| 任务 | 文件 | 验收 |
|---|---|---|
| evidence bundle builder | `src/agent_runtime/diagnosis.py` | 从 run.metrics + trace + board + session 构建，写 `runtime_home/diagnosis/<id>/` |
| trace 只读工具 | `src/agent_runtime/diagnosis.py` | `read_trace` / `search_trace` 直读 `storage.traces_dir()`，绕开 sandbox 限制 |
| `harnesses/evaluation-v1.yaml` | `harnesses/` | 只读工具、诊断 prompt、Markdown 约定 |
| 诊断会话编排 | `webui/evaluation.py` | 写 bundle → 建会话（`origin/target`）→ 发首条消息 → 返回 `session_id` |
| 诊断模型配置 | `runtime_config.py`、`model.json` | 默认 catalog，可用 `AGENT_RUNTIME_DIAGNOSIS_MODEL` 覆盖 |
| 证据标注（best-effort） | `webui/evaluation.py` | 未验证 `event_id` 在 UI 标注，不拒绝诊断 |
| API | `webui/evaluation.py` | `/diagnose` 支持 task/run/cohort；无后台触发 |
| 前端诊断面板 | `DiagnosisPanel.tsx`、`MarkdownAnswer.tsx` | 手动按钮；展示会话最后一条消息；可打开会话 |
| 测试 | `tests/test_diagnosis.py` | mock 模型覆盖正常/标题缺失/伪造证据/超预算 |

## Phase 5：回归、回填与文档（3–4 天）

| 任务 | 文件 | 验收 |
|---|---|---|
| 历史 trace 回填 `runs.jsonl` | `scripts/backfill_metrics.py` | 旧 job 可出现在评测页 |
| 基线快照 | `evaluation/baselines/` | 有当前 TSR/tokens/p95 |
| 文档 | `docs/evaluation.md`、`docs/storage.md`、`docs/webui.md` | 指标口径、API、页面说明 |
| 回归测试 | `pytest` + `npm run build` | 全绿 |

**总工期：单人 4 周左右；后端/前端并行约 2.5 周。**

---

# 七、验收标准

1. 每个 run 产出 `run.config`、全量 `llm.start`、assembled `llm.end`、`run.metrics`。
2. `messages` 不再重复写；`llm.chunk` 默认 0 条；长 run 内存不随事件数线性增长。
3. trace 能按 `task_id/session_id/harness/model` join。
4. `board_events.jsonl` 能算出 TSR、first-pass、rework、queue wait。
5. `/api/evaluation/overview` 在 10k run 下 < 200ms（内存索引）。
6. 评测页能看六个维度，能从任务下钻到 run/trace。
7. 手动点击「开始诊断」会新建一个普通会话（`origin=diagnosis`），出现在会话列表，可继续追问。
8. 诊断会话用只读工具核实证据，输出 Markdown，包含 `## 结论` / `## 根因` / `## 证据` / `## Top 5 改进建议`。
9. 任务详情能通过 `Session.target` 列出历史诊断；诊断不写 `board_events.jsonl`，不进 TSR 分母。
10. 诊断/改进 run 在 `runs.jsonl` 中按 `origin` 可切片；默认 harness 指标不混入它们。
11. cohort 诊断（`target.type=cohort`）在同一会话里输出 Top 5 建议，每条绑定指标、影响面、成本和验证方式。
12. 无 SQLite；删除 `metrics/` 后可由 trace 重建。
13. `pytest` 与 `npm run build` 全绿。
14. board run 的 `board.json runs[].run_id` 与 `traces/run-*.jsonl` 的 `run_id` 一致。
15. VCR / Hallucinated completion 只对声明需要产物的任务计算；产物证据来自 `tool.end`，不依赖事后 git diff。
16. 诊断报告由 `Session.report_segment_id` 固定；追问不改变评测页读取的报告。
17. Harbor 的 accuracy/reward 与 Board 的 TSR 分视图展示，不混算。

---

# 八、风险与边界

| 风险 | 应对 |
|---|---|
| 全量 messages 让 trace 变大 | 只写一次、关 chunk、`trace_bytes` 告警；存储不设硬上限 |
| 指标被“拖卡片”游戏 | TSR 以人工验收为准，但要求产物/diff 证据；定期抽样审计 |
| 用户忘记更新状态 | 记录 stale review；指标页显示数据质量提示 |
| 模型诊断幻觉 | 只读工具自行核实；引用不到标“未验证”；标注置信度；允许人工覆盖；不自动改 harness |
| 诊断成本 | 仅手动触发；`max_iterations` 限制；bundle/输出上限；cohort 限制 case 数 |
| 诊断会话污染指标 | `origin` 切片；Run 级不绑 `task_id`；Board 与 Harbor outcome 分视图 |
| Markdown 解析失败 | 四个标题为约定，解析失败原样渲染，不影响阅读 |
| 内存索引增长 | 按月轮转；超过阈值再评估 SQLite |
| 安全被误解 | 明确 guard 只是 fool-proofing，不对外宣称安全 |

---

# 九、与现有代码对齐的修正（实现前置）

以下问题与当前源码直接冲突，必须在 Phase 0/1 修掉；否则指标会失真或链路断掉。

## 9.1 Run 身份：board run_id 必须贯穿 trace

现状：

- `webui/board.py` 的 `claim_next_todo()` 生成 `task.run_id`；
- `webui/executor.py` 调 `_run_turn(session, prompt)`，没有传 `task.id` / `task.run_id`；
- `webui/server.py` 的 `_prepare_turn()` 又生成一个新的 `RunTrace.run_id`；
- `finish_run()` 只用 `task.run_id`，丢弃 `outcome.run_id`。

修正：

- `run_board_task` 把 `task.id`、`task.run_id`、`run_index` 传给 `_prepare_turn`；
- `_prepare_turn(..., run_id=..., task_id=..., run_index=...)` 不再自己生成；只有 CLI / 普通聊天才生成新 `run_id`；
- `finish_run()` 校验 `outcome.run_id == task.run_id`；
- 历史数据只能按 `(session_id, 时间窗)` best-effort 回填，不承诺逐条对齐。

验收：`board.json` 的 `runs[].run_id` 与 `traces/run-*.jsonl` 的 `run_id` 一致。

## 9.2 产物证据：不要依赖事后 git diff

现状：不做快照、所有任务共用 `LocalWorkspace`；事后 `git diff` 会串味，调研/问答任务也没有 diff。

修正：

- `tool.end` 记录 `edited_files`（成功的 `write_file` / `edit_file` / `apply_patch`，复用 `harness.verification.edit_tools`）；
- 产物证据 = `edited_files` 非空，或存在有效 `submit_result`；
- VCR / Hallucinated completion 只对“声明需要产物”的任务/harness 计算；其他任务记 `null`；
- bundle 的 `environment` 用 `edited_files + diff_summary（若可得）`，不把 git diff 当唯一证据。

## 9.3 诊断工具与路径：绕开 sandbox 限制

现状：

- `storage.runtime_home()`（默认 `workspace/`，受 `AGENT_RUNTIME_HOME` 控制）与 `LocalWorkspace.root`（`AGENT_RUNTIME_WORKSPACE` 或 cwd）是两套根；
- `execution/base.py` 的 `SKIP_DIRS` 含 `"workspace"`；
- `grep_search` 跳过 > 1 MB 文件；
- 保留全量 `messages` 后，`traces/run-*.jsonl` 普遍 > 1 MB。

修正：

- 诊断产物固定写 `storage.runtime_home() / "diagnosis"`；
- 给 `evaluation-v1` 提供专用只读工具 `read_trace(run_id, event_type/event_id/iteration)`、`search_trace(run_id, pattern)`，直接读 `storage.traces_dir()`，不走 `LocalWorkspace`；这两个工具不进通用 `builtin_tool_specs`，只在 `evaluation-v1` 的 registry 里注册；
- `bundle.json` 目标 ≤ 1 MB，超出则截断并标 `truncated_input`；
- 会话消息里给 runtime-home 相对路径，不给 sandbox 路径。

## 9.4 指标分层：Board 与 Harbor 分开

现状：Harbor `jobs/` 与普通聊天都没有 `task_id`；Harbor 的成败是 `reward/accuracy`，不是 `done`。

修正：

- **Run 级**（TCR、工具错误、参数合法性、tokens、latency、TTFT、retry、summary invalid）：聚合 `origin in {webui, cli, harbor}`，按 `origin/harness/model/day` 切片，不绑 `task_id != null`；
- **Board outcome**：TSR / first-pass / rework，只从 `board_events.jsonl` 算；
- **Harbor outcome**：accuracy / reward，复用 `evaluation/records/`；
- 评测页分「Harness 运行」和「任务结果」两个视图，不混成一个 TSR。

## 9.5 Trace / Tool / LLM 埋点修正

| 问题 | 现状 | 修正 |
|---|---|---|
| 工具错误被记 success | `LocalShellExecutor` / `read_file` 返回 `{"error": ...}` 不抛异常 | `tool.end` 增加 `status`、`error_kind`、`exit_code`，按结果判定 |
| `spill_path` 迟到 | `observations.render()` 在 `tool` span 之后 | 先 `render` 再写 `tool.end`，`exec_ms` 单独计 |
| `queue_ms` 不存在 | `ToolRunner` 严格串行 | 删除 `queue_ms`，或定义为同 iteration 等待前序工具 |
| `messages` 写两次 | `RunTrace.span()` 把同一 `span_meta` 给 `.start` 和 `.end` | `llm.start` 单独 emit 请求 payload；`llm.end` 只写响应/usage/timing |
| 重试混入时长 | span 包住 `chat_with_recovery` | 事件下沉到单次 `_chat_response` attempt，或加 `attempt` 字段 |
| `maxsize` 丢关键事件 | `flush()` 用 `put_nowait(_STOP)` | 只丢 `llm.chunk`；关键事件不丢；`flush` 用 `await queue.put` |
| usage 丢缓存 token | `_usage_dict` / `_consume_stream` 只留两项 | 透传 `cached_tokens` / `prompt_cache_hit_tokens` |
| `finish_reason` 丢失 | `_consume_stream` 未放进 response | 放进 response，`llm.end` 记录 |

## 9.6 诊断会话的元数据与配额

- `Session` 增加 `origin`、`target`、`report_segment_id`、`bundle_hash`、`diagnosis_id`；
- 追问后评测页只读 `report_segment_id` 指向的首轮报告，不读“最后一条”；
- `AGENT_RUNTIME_DIAGNOSIS_MODEL` 填 catalog `id`，用 `catalog.require()` 解析完整 endpoint/key；
- 配额：`RuntimeConfig` 增加 `diagnosis_daily_limit`、`diagnosis_max_output_tokens`；`/diagnose` 返回 `remaining`，超限 429；
- 主循环目前没有 `max_tokens` 控制；诊断请求单独加 `max_output_tokens`，不改全局 agent 默认。

## 9.7 Board 指标边界

- **TSR**：分子必须是“进入过执行且 done”，否则 backlog 直接拖到 done 会让 TSR > 100%；
- **Rework**：完成过至少一次 run 后，`review → todo/doing`，或 doing/review 期间追加评论触发下一轮；未执行就加评论不算返工；
- **Queue wait**：`claim_next_todo()` 会清空 `queued_at`，必须在 claim 时先算；
- **事件补齐**：`task_created`、`task_deleted`、`status_changed`、`comment_added`、`run_claimed`（带 `queued_at` / `queue_wait_ms`）、`run_requeued`、`run_finished`。

## 9.8 其它落地项

- `harnesses/evaluation-v1.yaml`：建议加 `internal: true` 并在 `/api/config` 过滤；不加也能跑，只是会出现在普通聊天/看板的下拉里；
- `SessionStore.create()` 加随机后缀，避免同秒 ID 冲突；
- 前端 `selectSession()` 用 `detail.busy` 初始化 busy，避免运行中的诊断会话被当成空闲；
- `Cost / success` 暂降 P2；要做就先给 `model.json` 加可选 `pricing`；
- `run_agent.py` 交互模式每 turn 新建 trace / run_id；
- `runs.jsonl` 写入责任写清楚：由调用方（`webui/server.py` / `run_agent.py` / `harbor.py`）写，或由 `RunTrace` 写但测试隔离 `AGENT_RUNTIME_HOME`；Harbor 多进程用原子 append；
- `run.config` 的 git 信息异步 + 超时 + env 白名单；
- Harbor sink 的 `last_event` 不再塞全量 `messages`；
- 前端复用 `components/Markdown.tsx`，不重写渲染。

## 9.9 Phase 1 必须补列的文件

- `src/agent_runtime/providers/llm.py`（cached tokens）
- `src/agent_runtime/agent/chat.py`（`finish_reason`、usage 透传）
- `src/agent_runtime/agent/tokens.py`（usage 结构）
- `src/agent_runtime/events.py`（`EventEmitter` 不常驻 delta）
- `run_agent.py`（每 turn 独立 trace）
- `src/agent_runtime/integrations/harbor.py`（sink 裁剪）
- `webui/executor.py` / `webui/server.py`（run_id 贯穿）

---

这版方案已经按你的决定收敛：**不做快照、不做后验 verify、不做 SQLite、不做安全边界、不做满意度；保留全量 messages、只关 chunk；评测页直接替换现有 Observability 占位页；诊断是用户手动触发的一次普通 run turn + 普通会话，只读工具核实证据，输出带固定标题的 Markdown（结论 / 根因 / 证据 / Top 5 改进建议），不自动执行、不写 board_events、不单独缓存。**