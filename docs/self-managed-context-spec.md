# L2 自管上下文 Spec（self_managed_file，可执行）

> Clean-room spec：只借鉴 CLM 论文思想（context-as-file + budget/nudge/rollback），
> 不拷贝 `context-language-models` 的任何代码 / prompt / 文本 / 格式。
> 本文是后续实现的唯一依据，含自创 mirror 格式、状态机、`MemoryGenome` 新增 gene、
> `TurnHistory` 契约、测试矩阵与验收标准。

## 0. 目标与非目标

* 目标：
  1. 上下文管理从“按步数提醒 + 按轮次截头”升级为“按 `view` 用量 + 模型主导选压哪里”。
  2. 不破坏 SunAgent 不变量：`messages` 全量不可变、`tool_call_id` 配对合法、
     `verification evidence/command_to_verify` 可 grounding、默认关闭零影响。
  3. 为 P2（skill 进化）/ P3（效率 reward）铺数据闭环：`mirror 意图 → view 操作 →
     chars/cost → trace 事件`。
* 非目标：不复刻论文数字；不做 RL 训练；不做 serving 侧 SCR；不引入 `tiktoken`；
  不改存量 harness yaml（按 repo 规则 derive 新版本）。

## 1. 术语

* `messages`：`TurnHistory.messages`，全量对话，source of truth，只 `append`，永不改写。
  审计 / UI / verification grounding 只读它。
* `view`：`TurnHistory.view`，实际发给 LLM 的投影，可被 mirror 同步替换。
* `mirror`：workspace 内文件 `.live_ctx/LIVE_CTX_MAIN.md`，`view[protect:]` 的可编辑渲染。
* `protect`：前 `N` 条消息（system + task），pin 住不可编辑，默认 `2`。

## 2. Mirror 格式（自创，v1）

```text
# LIVE-CTX v1 (editable below; header is metadata, do not treat as task text)
# budget: <used>/<limit> chars | protect: <N> | turns: <M>
# RULES: edit turn BODIES only; keep every `## turn <k> [role]` line byte-identical;
#   empty body drops that turn; never paste secrets verbatim twice.

## turn 1 [assistant]
<assistant content + tool_calls(name+args) text>

## turn 2 [tool:run_command id=call-7]
<observation text>

## turn 3 [user]
<user text or prior compaction note>
```

* turn 编号从 `protect` 之后按 `1..M` 重排（display-only，parse 时忽略数字，以 header 顺序为准）。
* `role` 只允许 `assistant/user/tool`；`tool` 行携带 `tool:<name> id=<call_id>`，用于回填配对。
* 保护：`render` 不含 `messages[:protect]`；`apply` 时强制 `result[:protect] == messages[:protect]`（对象恒等或深相等）。

## 3. 编解码契约（`agent/context_mirror.py`，新建）

```python
def render_mirror(view: list[dict], *, protect: int) -> str: ...
def apply_mirror(text: str, *, messages: list[dict], view: list[dict], protect: int) -> ApplyResult: ...
```

`ApplyResult = {status: applied|rejected_noop|rejected_grew|rejected_broke_pairing,
before_chars, after_chars, turns, note}`。

* `render_mirror(view)`：`view[protect:]` 按 §2 渲染；`reasoning_content` 是否计入与
  `providers/llm.py:reasoning_echo_enabled()` 对齐（echo 才渲染/计数）。
* `apply_mirror` 容错规则（按序）：
  1. 缺 header / 全空 → `rejected_noop`。
  2. 未知 role → 折为 `user`。
  3. 空 body 的 turn → 丢弃。
  4. 连续同 role → 合并（`assistant+assistant`，`user/tool` 统一并成 `user`，见 §5 配对规则）。
  5. assistant 文本 turn（无 `tool_calls` 结构）保持 `assistant`；其余全部为 `user`
     纯文本——**tool_call 结构不重建**，只在 §5 的豁免路径下保留。
  6. 结果必须以 `messages[:protect]` 开头，否则 `rejected_noop`。
* 度量：`chars(msg) = len(content) + len(reasoning_content if echo) + sum(len(tool args))`，
  与 `memory.py:_message_chars` 同口径。

## 4. Budget 状态机（`agent/context_budget.py`，新建，chars 口径）

* 输入：`used = chars(view)`，`limit = memory.context_budget`，
  `strict = limit - reserve_chars`（默认 `reserve = 20% * limit`，MVP 保守）。
* 输出动作：`ok | nudge_tier | nudge_urgent | rollback | final_turn | stop`。
* Nudge：
  * `nudge_tier`：`used/limit` 过 `25/50/75%` 各 fire 一次，压回线下后重 arm。
  * `nudge_urgent`：自适应带 `need = max(10%*strict, mult * max(近3个tool obs chars))`，
    上限 `50%*strict`；`strict - used < need` 时每轮 fire。`mult` 默认 `2.0`。
* `rollback`：`used > strict` 且 `retries < max_rollback_retries`（默认 `8`，MVP 照常计步故设小）：
  丢最新 turn 直到 `used <= strict - retry_margin`（默认 `10%*strict`），保 `protect` 前缀、
  保 `assistant tool_calls ↔ tool` 配对（成对丢）。被丢的 view-turn 记录到
  pinned ledger（一条 `user` 消息插在 `protect` 后，内容为“丢了哪几个 command，它们输出多大，
  别原样重跑，要 head/grep/分页”)，ledger 本身计入 `protect`。
* `final_turn/stop`：重试用尽仍 `used > strict` → 本轮附 `final_turn` notice 允许交卷，
  下轮仍超 → `stop`（loop 收尾进 `_summary_turn`，不再截断）。
* 服务端溢出（`context_window_exceeded / prompt too long / too many tokens`）→ 视为 `final_turn`，
  不重试 model call。实现方式：`chat.py` 内专用异常、`_llm_error_category` 返回 `None`
  （= dedicated handling，永不重试，与 `_ReasoningBudgetExceeded` 同模式），**不新增
  `LLM_RETRY_CATEGORIES` 条目**（`tests/harness/test_harness.py` 对该集合做精确断言，
  新增 category 会炸回归测试；如未来确需可配重试，再走 genome 变更流程）。

## 5. 与多工具协议的互操作（L2 关键约束）

* 默认 `apply` 结果中 **不得出现裸 `tool` role 消息**（无 `tool_calls` 配对即 400）。
  因此 §3-5 把非 assistant 统一并成 `user` 文本——这是与 CLM 原生最大的有意差距。
* 豁免路径（可选，MVP 默认关）：若某 `## turn k [tool:name id=X]` 的 body 以
  `[KEEP-PAIRING]` 开头且 `X` 在 `messages` 中存在对应 `assistant tool_calls`，
  则原样保留该 `assistant+tool` 对进 `view`（不压缩）。用于“这段 observation 还要用，别压”。
  实现：在 `apply` 后做一次配对校验，非法 `tool` 消息 → 整单 `rejected_broke_pairing`。
* `ObservationFormatter`：mirror 文件读写（`.live_ctx/`）不 spill、不计 observation；
  对 mirror 的 `read/write/edit` 工具结果不进压缩候选（`metadata.source=mirror` 标记跳过）。

## 6. Loop 接线（`agent/loop.py + turn_history.py`，最小侵入）

每轮 `iteration` 内（`refresh_view` 之后、`chat_with_recovery` 之前）：

```text
if strategy == self_managed_file:
    budget.evaluate(view) → 注 nudge/final_turn 消息（进 view+messages 双写，与现有 nudge 一致）
    if action == rollback: view = rollback(view); pin ledger; trace budget.rollback
    render_mirror(view) → workspace.write(.live_ctx/LIVE_CTX_MAIN.md)
```

每次 `tool.execute` 之后、`history.append(observation)` 之前：

```text
baseline = self._last_rendered  # 本轮 render 时的文本（_render_mirror 内设置）
new_text = workspace.read(mirror)   # 失败/无变化 → 跳过
if strip_metadata(new_text) != strip_metadata(baseline):
    # 再过 apply_mirror 内的 stale-file guard（文件是当前 view 的
    # section 前缀 → 只是 tool 输出还没来得及渲染，不是模型编辑）：
    res = apply_mirror(new_text, messages, view, protect)
    gate: fit(默认) → after<=strict 才 applied；shrink → after<before 才 applied
    applied → TurnHistory.replace_view(new_view); 更新 _last_rendered;
              trace context.mirror_sync{before,after,turns};
              仅当 |after-before|>=32 chars 才发 receipt（view-only，不进 messages）
    rejected → observation 追加一行 receipt（view-only）或静默，view 不变
```

* `TurnHistory` 新增 `replace_view(new_view)`：要求 `new_view[:protect] == messages[:protect]`；
  `messages` 永不改。`append_to_view` 供 mirror receipt 等 transient notice 单写 view
  （不污染 audit 记录）。`refresh_view` 在新 strategy 下改为 `view = apply_budget(view)` 而非旧压缩。
* 计步：压缩 turn **照常计步**（已定决策），不引入 `free-turn/lm_call_cap`。
* 快照：每次 `applied` 写 `.live_ctx/snapshots/<iter>.md`（审计 diff 用，不发给模型）。

## 7. Harness Gene（`harness.py`，向后兼容）

```yaml
memory:
  strategy: self_managed_file   # 新增枚举值，旧三种不变
  context_budget: 60000         # 复用 chars 口径
  reserve_chars: 12000          # 新增，默认 20%
  mirror_path: .live_ctx/LIVE_CTX_MAIN.md  # 新增
  protect_turns: 2              # 新增
  nudge_ratios: [0.25, 0.5, 0.75]  # 新增
  urgent_mult: 2.0              # 新增
  retry_margin_chars: 6000      # 新增
  max_rollback_retries: 8       # 新增
  edit_gate: fit                # 新增 fit|shrink
```

校验：未知 `strategy` 仍报错；新字段缺省走 `DEFAULT_*`；`genes_hash` 自动覆盖新字段。
新 harness：`derive common-v0 → common-v1-selfctx`（`memory.strategy=self_managed_file` + 上述默认值 +
system prompt 追加 `docs` 化的压缩指引段，自写文案）。

## 8. Prompt 指引（自写，不抄 CLM 文案）

* 位置：`common-v1-selfctx.yaml prompt.system` 末尾追加 `CONTEXT FILE` 一节。
* 内容要点（自写，~15 行）：mirror 路径；只改 body 不改 header；用 `execute_code(python)`
  按 header 切片替换、禁全文重贴；批量压优于多次小压；`[KEEP-PAIRING]` 豁免语义；
  每次 tool 结果尾部有 `[context: used/limit]` 水位线。

## 9. Trace / Events（复用现有模型）

* `trace.emit("context.mirror_sync", iter, before_chars, after_chars, turns, status)`。
* `trace.emit("budget.nudge", iter, tier|urgent, used, limit)`，
  `trace.emit("budget.rollback", iter, dropped, after)`。
* 用户事件不新增：receipt 走 tool observation 文本；UI 默认渲染 `messages`（无感），
  另提供 `debug view` 切到 `view`（实现时再定，spec 不强制）。

## 10. 测试矩阵（`tests/agent/test_context_*.py`）

| 文件 | 用例 |
|---|---|
| `test_context_mirror.py` | round-trip 恒等；丢空 turn；合并同 role；未知 role→user；缺 header→noop；protect 被改→noop；tool body 无豁免→并成 user 文本；`[KEEP-PAIRING]` 合法对保留、非法对→`rejected_broke_pairing`；`reasoning echo on/off` 口径 |
| `test_context_budget.py` | 三档 nudge fire/重 arm；urgent 自适应（小 obs≈90% 线、大 obs 提前）；rollback 保前缀+成对丢+ledger pin；重试耗尽→final→stop；`chars` 口径与 `memory._message_chars` 一致 |
| `test_loop_selfctx.py` | mirror 无变化不 sync；`fit` 拒变大、`shrink` 拒非缩小；receipt 进 observation；`messages` 全程不可变；旧三种 strategy 回归（`test_memory.py` 全绿） |
| `test_harness_selfctx.py` | 新 gene 默认值/校验；`genes_hash` 随新字段变化；旧 yaml 照常加载 |

## 11. 验收（Harbor A/B，小批量）

* 对照：`common-v0` vs `common-v1-selfctx`，同模型同任务（`terminal-bench` smoke 子集 ≥5 task）。
* 通过线：`score >= 对照 - 1σ` 且 `mean(view_chars/turn)` 下降或 `overflow 中断率` 下降；
  `verification evidence` grounding 通过率不降；`test_memory.py` 全绿。
* 不达标 → 关 strategy 默认值回退，mirror 代码留分支但不启用。

## 12. 实施顺序（文件级）

1. `src/agent_runtime/agent/context_mirror.py`（纯函数，无依赖，可先行）。
2. `src/agent_runtime/agent/context_budget.py`（纯函数 + `memory._message_chars` 口径复用）。
3. `src/agent_runtime/agent/turn_history.py:TurnHistory.replace_view`。
4. `src/agent_runtime/agent/observations.py`：mirror 标记跳过。
5. `src/agent_runtime/agent/loop.py`：render/read-back 接线 + trace 事件。
6. `src/agent_runtime/harness.py`：新 gene + 校验 + defaults。
7. `harnesses/common-v1-selfctx.yaml`（derive，不碰旧文件）。
8. 测试 + `docs/self-managed-context.md`（用户文档，spec 的精简版）。
9. Harbor smoke A/B。
