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
| LLM judge | 可选、默认关闭、按需触发、有预算上限 |
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
                    │           诊断               │
                    │ 漏斗 → case file → replay    │
                    │ → 根因码 → 证据              │
                    └──────────────┬──────────────┘
                                   │ 根因分布 + 影响面
                                   ▼
                    ┌─────────────────────────────┐
                    │           改进               │
                    │ 按 ROI 排序 → 单 gene 变异   │
                    │ → 回归 → 指标验证            │
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
| `tool.end` | 参数、结果（超限落 spill）、`exit_code`、`queue_ms` / `exec_ms` | 每工具调用一条 |
| `context.*` | 触发原因、压缩前后大小、summary invalid、成本 | 每次压缩 |
| `verification.*` | 结果、缺失项、rerun 成本 | 每次验证 |
| `run.metrics` | 运行级指标汇总，不含 final answer | 每 run 一条 |

关键点：

- `messages` **只写一次**，放在 `llm.start`；`llm.end` 不再重复。
- final answer 不重复存：WebUI 在 `session.segments` / board `runs[].response`；CLI/Harbor 在 `agent.end` 带一份。
- 全量 messages 保留后，`run.config` 才是“可复现”的另一半；没有它，模型参数、tool schema、runtime 条件仍然对不上。

## 2.2 内存与写入

- `RunTrace.events` 增加 `retain_events = all | control | none`：
  - 测试：`all`
  - WebUI / Harbor：`control`（只留控制事件；payload 事件不常驻）
- Harbor 不再依赖 `trace.events` 全量，改用 `trace.agent_error()` / `trace.event_count`。
- `EventEmitter` 不再常驻每个 `assistant.delta`；实时消费靠订阅，历史靠 session segment。
- 写入仍走异步 writer；队列加 `maxsize` 和 `trace.dropped` 计数。
- 序列化用 `ensure_ascii=False, separators=(",", ":")`，减少体积和 CPU。
- 不做 gzip、不做快照；只记录 `trace_bytes`，超过阈值告警。

## 2.3 Join keys 与 board 事件

Trace 头部和 `run.config` 增加：

```
origin: cli | webui | harbor
run_id
session_id
task_id
run_index
harness_id
genes_hash
model
```

Board 增加 append-only 事件流 `workspace/board_events.jsonl`：

```json
{"task_id":"task-...","at":...,"type":"status_changed","from":"review","to":"todo","actor":"user"}
{"task_id":"task-...","at":...,"type":"comment_added","run_id":"run-..."}
{"task_id":"task-...","at":...,"type":"run_finished","run_id":"...","status":"completed"}
```

`board.json` 仍是当前状态权威；事件流只用于历史与指标，丢了可以降级，不影响运行。

## 2.4 存储：纯文件 + 内存索引

```
workspace/
├── traces/run-*.jsonl                # 权威，完整可回放
├── sessions/*.json                   # 权威
├── board.json                        # 当前状态权威
├── board_events.jsonl                # 生命周期
└── metrics/
    ├── runs.jsonl                    # 一行一个 run.metrics，评测主数据源
    └── judge_cache.jsonl             # 可选，judge 结果缓存
```

- 评测服务启动时只读 `runs.jsonl` + `board_events.jsonl`，在内存建索引；不扫 trace。
- 详情页按 `run_id` 读对应 trace，带 mtime 缓存。
- 不做 SQLite；数据量到“几十万 run、查询 >10 秒”再考虑。
- `runs.jsonl` 按月轮转即可，当前不需要。

---

# 三、评测指标体系（最终版）

## 3.1 指标表

| 维度 | 指标 | 定义 | 数据源 | 判定 | 优先级 |
|---|---|---|---|---|---|
| **有效性** | **TSR** | `done` 任务 / 进入执行的任务 | board events | 人工验收 | P0 |
| | **First-pass acceptance** | 首次 run 后直接 `done` / 进入执行 | board events | 人工验收 | P0 |
| | **Rework rate** | `review → todo/doing` 或带反馈 comment / 进入 review | board events | 规则 | P0 |
| | **TCR** | 正常结束 run / 发起 run | run.metrics | 规则 | P0 |
| | **VCR** | 正常结束 + 非空答复 + 有产物/diff + 无策略拦截 / 发起 run | run.metrics + diff | 规则 | P0 |
| | **Hallucinated completion** | 声称完成但无产物/diff，且被退回 | board + diff | 规则 | P0 |
| | Partial progress | 勾选 acceptance 项 / 总项（仅当 description 有 checklist） | description + board | 解析 | P1 |
| | Answer correctness | 0/1 或 0–1 | 人工 / judge | 人工优先 | P1 |
| | Groundedness | 有证据支撑的结论 / 结论总数 | answer + trace | judge（按需） | P2 |
| **过程** | **Tool error rate** | 失败调用 / 总调用 | trace | 规则 | P0 |
| | **Argument validity** | schema 通过率、执行/编辑成功率 | trace | 规则 | P0 |
| | Recovery rate | 错误后 K 步内恢复 / 错误数 | trace | 规则 | P1 |
| | Step efficiency | 实际 steps / 参考 steps；重复调用率 | trace | 规则 | P1 |
| | Tool selection accuracy | 工具类别正确 / 总调用 | trace | 规则 + 抽样 | P2 |
| | Trajectory quality | 探索→编辑→验证→提交 rubric | trace | judge | P2 |
| | Plan adherence | 覆盖计划项 / 总项 | trace | judge | P2 |
| **效率** | **Tokens / task** | prompt / completion / cached / summarizer | run.metrics | 规则 | P0 |
| | **Latency p50/p95** | 端到端 + 分阶段 | run.metrics | 规则 | P0 |
| | TTFT | 首 token 延迟 | trace | 规则 | P1 |
| | Queue wait | `started_at − queued_at` | board events | 规则 | P1 |
| | Cost / success | 估算成本 / 验收通过数 | run.metrics + 模型价 | 规则 | P1 |
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
- `Groundedness` / `Trajectory quality` 依赖 judge，默认关闭。
- `Consistency` / `Rephrase robustness` 属于实验，不是日常观测。

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

操作：查看详情、打开会话、查看 trace、一键诊断。

### 诊断与建议

- 选择任务 / run / cohort
- 「一键诊断」→ 根因 + 证据 + 建议
- 「生成改进建议」→ 按根因聚合的 Top N 改进项

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
| `DiagnosisPanel.tsx` | 根因、证据、建议 |
| `SuggestionList.tsx` | 改进建议卡片 |
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
| POST | `/api/evaluation/diagnose` | `{task_id}` 或 `{run_id}`，返回根因/证据/建议 |
| GET | `/api/evaluation/suggestions?...` | cohort 级改进建议 |
| POST | `/api/evaluation/refresh` | 重建内存索引（可选） |

后端用 `EvaluationStore`：

- 启动时加载 `metrics/runs.jsonl` + `board_events.jsonl`；
- 按文件大小增量刷新；
- 按 `run_id/task_id/harness/model/day` 建内存索引；
- 详情按需读 trace，带 mtime 缓存；
- 单进程内 `threading.Lock` 保护刷新与读取。

## 4.5 一键诊断交互

1. 任务详情或 run 详情点「一键诊断」。
2. `POST /api/evaluation/diagnose`。
3. 后端构建 case file → 规则引擎 → 可选 judge。
4. 前端展示：
   - 根因码 + 置信度
   - 证据列表（可跳到对应 iteration / 工具调用 / trace 行）
   - 建议动作（按预期影响排序）
5. 「生成改进建议」在 cohort 级聚合，输出 Top N。

所有诊断结果可复制为 Markdown，方便贴进 issue / 分析报告。

---

# 五、诊断与建议引擎

## 5.1 Case file

```json
{
  "task_id": "...",
  "description": "...",
  "acceptance": ["..."],
  "runs": ["run-..."],
  "metrics": {"tsr": 0, "iterations": 42, "tokens": ..., "tool_errors": 4},
  "evidence": {"diff_summary": "...", "verify": "...", "key_tools": "..."},
  "human": {"verdict": "rework", "comment": "..."},
  "root_cause": null,
  "suggestions": []
}
```

## 5.2 根因枚举

```
E1 契约/验收不清
E2 runtime/超时/重试
E3 工具选择/参数/执行
E4 上下文丢失/压缩失败
E5 循环/停滞/过早提交
E6 验证误判
E7 模型能力
E8 任务/环境
E9 安全策略拦截（仅信号）
E10 需求歧义/UX
```

## 5.3 规则引擎 v1（确定性，优先）

| 条件 | 根因 | 建议 |
|---|---|---|
| 无编辑、无产物、iterations 高 | E5 停滞 | 检查任务描述、工具集、是否只读探索 |
| 某工具错误率 > 20% | E3 工具参数 | 检查 schema、错误提示、模型格式 |
| compaction ≥ 3 且 summary invalid > 0 | E4 上下文 | 调预算/summary prompt，检查丢信息 |
| iterations ≥ 90% 上限且未提交 | E5 循环 | 停滞检测、任务分解、预算调整 |
| run error / timeout / retry | E2 runtime | 看 provider 400、stream、timeout |
| review 退回且 acceptance 未满足 | E1 契约 | 模板引导写清验收标准 |
| submit 通过但验证失败 | E6 验证误判 | 检查验证门规则/证据 |
| 编辑后测试失败，无工具/上下文异常 | E7 模型 | 换模型/补测试反馈/缩小任务 |
| 同任务跨 harness/model 都失败 | E8 任务 | 单独排查环境/任务病理 |
| guard 拦截 > 0 | E9 策略 | 检查命令构造（不宣称安全） |
| 高 rephrase / handoff | E10 歧义 | 澄清策略、任务模板 |

每条规则输出：

```json
{
  "code": "E3.tool_argument",
  "label": "工具参数/格式问题",
  "confidence": 0.82,
  "evidence": [
    {"run_id": "...", "iteration": 12, "event": "tool.error",
     "quote": "old_text not found"}
  ],
  "suggestions": [
    {"action": "检查 apply_patch 的 old_text 匹配与错误提示",
     "metric": "tool_error_rate", "effort": "low"}
  ]
}
```

## 5.4 Judge：是什么、预算怎么定

**Judge 预算 = 给 LLM-as-judge 的调用配额**，不是 agent 的预算。

三个旋钮：

1. **范围**：哪些任务/run 需要 judge（抽样率、仅失败、仅开放式任务、手动触发）。
2. **单次成本**：用哪个模型、evidence bundle 截断到多少字符、max output tokens。
3. **总量**：每天/每次评测最多多少次调用、多少 token；相同输入缓存。

用途：在没有测试的开放式任务上，给 correctness / groundedness / trajectory 打分；或者在一键诊断时判断“这个回答是否真的有证据”。

推荐默认：

```
连续评分：judge 关闭，只用规则 + 人工验收
一键诊断：按需调用，单次
抽样趋势：10% 失败任务，可选
硬上限：50 次/天，输入 ≤ 20k chars，输出 ≤ 1k tokens
缓存键：(run_id, rubric_hash, judge_model)
```

这样既不会让评测成本失控，又保留了“需要时能自动判”的能力。如果 judge 不可用或超预算，诊断自动退回规则结果。

---

# 六、实施计划

## Phase 0：契约与骨架（2–3 天）

| 任务 | 文件 | 验收 |
|---|---|---|
| 定义 `run.config` / `run.metrics` schema | `docs/evaluation.md`、`src/agent_runtime/metrics.py` | 字段有定义、有示例 |
| 定义指标口径与根因枚举 | 同上 | 每个指标有分母/来源/判定 |
| 新增 metrics 路径 | `src/agent_runtime/storage.py` | `workspace/metrics/runs.jsonl` 可写 |
| 测试骨架 | `tests/agent/test_metrics.py` | schema 序列化通过 |

## Phase 1：观测落地（4–5 天）

| 任务 | 文件 | 验收 |
|---|---|---|
| `RunTrace` 支持 `run.config` / `run.metrics` / retain 策略 | `trace.py` | 每 run 两条事件；内存不常驻 payload |
| `llm.start` 全量 messages；`llm.end` assembled response；chunk 默认关 | `loop.py`、`chat.py` | messages 只写一次；chunk 0 条 |
| tool 统计（queue/exec/exit/output） | `tool_runner.py` | 每次工具调用有完整字段 |
| usage/cache/prefix | `turn_history.py` | cached tokens/prefix hash 可见 |
| join keys + trace level | `webui/server.py` | trace 可 join task/session |
| board 生命周期事件 | `webui/board.py`、`webui/executor.py` | 状态变更/评论/run 完成都有事件 |
| Harbor 去掉全量 events 依赖 | `integrations/harbor.py` | 仍能拿到 agent.error/event_count |
| 测试 | `tests/test_trace.py`、`tests/webui/test_board.py` | 覆盖新事件与内存策略 |

## Phase 2：评测聚合与 API（4–5 天）

| 任务 | 文件 | 验收 |
|---|---|---|
| `EvaluationStore` 内存索引 | `webui/evaluation.py` | 10k run 启动 < 1s |
| KPI/趋势/漏斗/切片 | 同上 | 结果与手工核对一致 |
| run 详情 trace digest | 同上 | 按需读取，带缓存 |
| API 路由 | `webui/server.py` | 7 个端点可用 |
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

## Phase 4：诊断与建议 v1（4–5 天）

| 任务 | 文件 | 验收 |
|---|---|---|
| case file builder | `src/agent_runtime/diagnosis.py` | 能从 run.metrics + trace 构建 |
| 规则引擎 | 同上 | 每条规则有 fixture |
| API | `webui/evaluation.py` | `/diagnose` `/suggestions` 可用 |
| 前端诊断面板 | `DiagnosisPanel.tsx`、`SuggestionList.tsx` | 一键诊断展示根因/证据/建议 |
| judge 可选接口 | `src/agent_runtime/judge.py` | 默认关闭，预算可配 |
| 测试 | `tests/test_diagnosis.py` | 覆盖 10 个根因码 |

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
7. 一键诊断能返回根因码、证据、建议；覆盖至少 10 类根因。
8. 改进建议能按 cohort 聚合，按影响面/成本排序。
9. 无 SQLite；删除 `metrics/` 后可由 trace 重建。
10. `pytest` 与 `npm run build` 全绿。

---

# 八、风险与边界

| 风险 | 应对 |
|---|---|
| 全量 messages 让 trace 变大 | 只写一次、关 chunk、`trace_bytes` 告警；存储不设硬上限 |
| 指标被“拖卡片”游戏 | TSR 以人工验收为准，但要求产物/diff 证据；定期抽样审计 |
| 用户忘记更新状态 | 记录 stale review；指标页显示数据质量提示 |
| 规则诊断误判 | 只输出高置信规则；证据可跳转；允许人工覆盖根因 |
| judge 成本/噪声 | 默认关闭；按需触发；预算+缓存；先用人工校准 |
| 内存索引增长 | 按月轮转；超过阈值再评估 SQLite |
| 安全被误解 | 明确 guard 只是 fool-proofing，不对外宣称安全 |

---

这版方案已经按你的决定收敛：**不做快照、不做后验 verify、不做 SQLite、不做安全边界、不做满意度；保留全量 messages、只关 chunk；评测页直接替换现有 Observability 占位页；诊断先用确定性规则，judge 只做按需增强。**