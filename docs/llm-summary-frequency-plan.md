# llm_summary 高频触发：根因与改进方案（tb-v3 job 实证）

来源 job：`jobs/terminal-bench-2-1-tb-v3-deepseekv4.1-flash-1017`
（harness `harnesses/tb-v3.yaml`，`memory.strategy: llm_summary`，
其余走默认：`context_budget=60_000` chars、`summary_tail_rounds=2`）。

## 1. 是否存在频繁 llm_summary：是

对 89 个有 trace 任务的 `agent-runtime.jsonl` 统计 `event_type == memory.llm_summary`：

- 触发过 summary 的任务：**52 / 89（58%）**；
- 事件总数：**202 次**；
- 有触发任务的均值 3.88 / 中位数 2.0 / 最大 **18**（`video-processing`）；
- ≥5 次 11 个任务，≥3 次 23 个；
- 相邻 summary 间隔大量为 **1~3 个 iteration**
  （如 `schemelike-metacircular-eval` 连续 9 次间隔为 1，
  `video-processing` 多次间隔为 1）。
- Top：`video-processing` 18、`schemelike-metacircular-eval` 15、
  `extract-moves-from-video` 14、`mailman` 13、`fix-ocaml-gc` 12。
- 202 次里 168 次成功折叠（view 消息数减少），34 次回退
  （`request_count == original_count`，summarizer 失败或校验不通过→
  `compact_observations` 但几乎没压住；`schemelike` 15 次全部回退）。

## 2. 压缩时机：当前代码怎么避免每次都压缩

调用链：`loop.py:229` 每个 iteration 都调
`history.refresh_view()` → `turn_history.py:67-74`
→ `memory.py:269-323 apply_memory_strategy`。

现状唯一的节流机制（`memory.py:299-303`）：

- 判定的不是全量 history，而是**上轮留下的 view**
 （`working = view if view else messages`）；
- `working ≤ budget` 就直接返回旧 view，不压缩；
- 一旦 view 又超 budget，就**再次对全量 history 做 summary**，
  旧 summary 被丢弃，summarizer 输入是 O(全量)
  （实测 `original_chars` 中位数 135k、最大 550k）。

结论：refresh-view 是对的，但 budget（60k）相对 TB 任务输出量太小
（单轮 tool 输出常 5~9KB，"最近 2 轮原文尾巴"本身就 30~50KB），
view 压完（中位数 ~24k）经 1~3 轮新输出就反弹超限 → 高频重压。
回退路径（`memory.py:456-462`）压不住时下一轮必定再触发，白烧 LLM 调用。
无冷却、无回滞（hysteresis）、无增量复用。

附带：本 job 实测 `reasoning_content` 占比为 0，不是推高 budget 的因素；
纯粹是 tool observation 体量问题。

## 3. 改进方案 1 / 2 / 5 的具体实现与冲突处理

三个方案互相兼容。推荐顺序：1 → 5 → 2。

### 方案 1：回滞 + 冷却（最先做，无冲突）

- `MemoryGenome` 新增 2 个基因（`harness.py`：`MemoryGenome` +
  `_memory_genome` + `DEFAULT_*` 常量 + `tb-v3.yaml` 配置）：
  - `compact_target_chars`（压缩目标水位，默认 `0.7 × context_budget`）；
  - `summary_cooldown_rounds`（默认 5）：距上次 `memory.llm_summary`
    不足 N 轮时，即使超 budget 也跳过，沿用旧 view + 追回新消息。
- 状态放哪：`apply_memory_strategy` 现在是无状态纯函数，
  状态放 **`TurnHistory`**（新增 `last_summary_iteration` /
  `summary_count` 字段，`refresh_view` 里读写）。
  不污染 `Conversation.messages`（跨 turn 共享的 source of truth），
  不进持久化；不传状态 = 老行为（cooldown=0），纯函数单测不受影响。
- 冷却逻辑放在 `apply_memory_strategy` 层（而不是 summary 分支），
  `compact_observations` 策略同样受益。

### 方案 2：增量 summary（复用上次 summary，只归纳增量）

- `TurnHistory` 持有 `last_summary: {text, covered_rounds}`；
  触发时 summarizer 输入 = 上次 summary + 新增 rounds（而非全量），
  输出新 summary 覆盖旧的；view = `[system, task, summary_msg, tail]`。
- 冲突 1：`session_memory.md` 是单文件覆盖写
  （`storage.py:70-76`，"one file per run, overwritten by each new summary"），
  多 task 并发跑时会互相覆盖。
  处理：**增量状态只放内存（`TurnHistory`），不读文件**；
  `session_memory.md` 保持纯观测副作用，或改 `{run_id}.md`。
  不让文件成为正确性依赖。
- 冲突 2：`summarize_history(messages, ...)` 签名是全量输入，
  调用方和单测都按全量写。
  处理：保留全量语义（首次/回退用），新增
  `summarize_incremental(last_summary, new_rounds, ...)`；
  `apply_memory_strategy` 按 `TurnHistory` 是否有可用 summary 做分支；
  `_validate_summary` 复用。
- 冲突 3：tail 口径与现状一致（summary 覆盖到 `starts[-tail]` 之前），
  `total_rounds <= tail_rounds` 早返（`memory.py:450`）保持不变。
- 成本收益：summarizer 输入从 O(全量，中位数 135k chars）降到 O(增量）；
  代价是 summary 漂移 → 建议每 K（如 5）次增量后做一次全量校准。

### 方案 5：调 `context_budget`

- 代码里 budget 按 **chars** 计。英文/代码约 1 token ≈ 4 chars。
- 本次模型 `deepseekv4.1-flash` 系 128k 上下文（非 100 万；
  100 万是别的模型，换模型后另算）：
  当前 60k chars ≈ 15k tokens，占 128k 的 ~12%，偏保守。
- 本次数据：202 个触发点的 `original_chars` p50=135k（≈34k tokens）、
  p90=327k（≈82k tokens）。
- 建议（128k 上下文模型）：
  - `context_budget`：**60k → 120k chars**（≈30k tokens，占 ~23%，
    留足输出 + 工具 schema 余量）。
    按本次数据触发次数从 202 降到约 110（-45%）。
  - 小步验证可先 90k（≈22k tokens，-~30%）。
  - 不建议直接 200k+：view 本身叠加 tool schema/系统提示后更大，
    且 summarizer 全量输入同步变大，单次 summary 更贵；
    budget 越大单次主请求越贵——这是用"主请求 token 费"换"summary 次数"，
    要配合方案 1/2 一起做才划算。
- 若未来换 100 万 token 模型：按同比例（~25% 上下文）可设
  **400k chars**（≈100k tokens），但那时 summarizer 全量输入可达
  550k+ chars（本次 max），必须先做方案 2，否则单次 summary 输入
  就 ~140k tokens。

## 4. spill：是，任何时候都有（有一个阈值）

- `observations.py:159-168`：tool 输出 `> memory.head_chars`
  （默认 **200 chars**）就归档到 `.outputs/` 并记 `spill_path`。
  200 chars 以下的小输出才没有——"任何时候都有"基本成立。
- 两层独立：
  - `control.max_observation_chars`（16k）：进 history **之前**就做
    head/tail 开窗，超 16k 的观测在 history 里已是窗口版；
  - `memory.head_chars`（200）：压缩时保留头部 200 chars + spill 路径提示，
    模型可用 `read_output` 分页取回（`tools.py:471-474`，
    `read_output` 是常驻工具，不占 harness 基因位）。
- 结论："只用 `compact_observations`"对比实验的信息安全网完整，
  压缩掉的中段都可回读，不会真丢；这也给方案 2 的增量漂移兜了底。

## 5. 待确认事项（需用户拍板）

1. 冷却状态放 `TurnHistory`（内存态，推荐）还是放 trace/metadata
   （可观测但要改 trace 结构）？
2. `context_budget` 先调到 120k 还是 90k？
   是否同时把 `compact_target` 定为 `0.7 × budget`？
3. 增量 summary 的全量校准频率 K=5 是否可接受？
4. 对比实验：产出 `tb-v4.yaml`（llm_summary + 新基因）vs
   `tb-v4-compact.yaml`（compact_observations）两个 harness，
   还是只改代码不建新 harness？
