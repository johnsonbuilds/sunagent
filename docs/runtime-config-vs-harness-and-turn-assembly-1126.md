# RuntimeConfig ↔ harness 职责边界 与 turn 组装层参数收敛（2026-11-26）

> 本文是 P0「RuntimeConfig 收口」之后的复盘问答记录。**尚未动代码**，只记录结论与建议，供确认后单开一单实施。

---

## 0. 先回答 memory 语义

| 入口 | memory |
|---|---|
| Harbor (`integrations/harbor.py:56`) | **始终关闭**：`replace(RuntimeConfig.from_env(), memory_enabled=False)`，即使环境里 `AGENT_RUNTIME_MEMORY=1` 也无效 |
| `RuntimeConfig` 字段默认值 | `memory_enabled: bool = False`（`runtime_config.py:81`） |
| `.env` | 没有 `AGENT_RUNTIME_MEMORY`（`.env.example` 里是注释行） |
| `run.sh webui` | **显式** `AGENT_RUNTIME_MEMORY=1 nohup uv run webui ...`（`run.sh:166,169`） |
| 直接 `uv run webui` / `run_agent.py` | **关闭**（没人设变量） |

所以准确说法是：**Harbor 强制关；其它入口"默认关"，只有经 `run.sh` 启动的 webui 才是开**。如果你想让"非 Harbor 一律开"，得把 `RuntimeConfig.memory_enabled` 默认改 True 或写进 `.env`，而不是保持现在的 `_env_truthy(..., False)`。

---

## 1. RuntimeConfig ↔ harness 职责

### 判据（建议当成硬规矩写进 docs）

> **能进 `genes_hash`、能被 lineage 当实验变量 A/B、改动能归因到 benchmark 差异 → harness gene。**
> **描述"这台机器 / 这个端点 / 这次部署"的事实与安全刹车、改了不该影响 harness 横向对比 → RuntimeConfig。**

用这条判据看现在的重叠，只有 4 处真有问题（其余都是干净分工）：

| # | 重叠点 | 现状 | 判断 |
|---|---|---|---|
| 1 | `recovery.tool_error_max_retries`（harness）vs `RuntimeConfig.tool_error_max_retries`（env `AGENT_RUNTIME_TOOL_ERROR_MAX_RETRIES`） | **真正的双份同名字段**，env 覆盖 harness gene | 保留 gene 为唯一事实源；`RuntimeConfig` 那个只是调试阀，应**改名**表明它是 override（见下） |
| 2 | `retry_4xx`（runtime）vs `recovery.llm_errors.invalid_request.max_retries`（harness） | `llm_recovery.py:234` 里 `retry_4xx` 把 400/404/422 从 `invalid_request` 改判成 `provider_error`，**直接让 harness 声明的 invalid_request 策略失效** | 这是最隐蔽的越权。该保留（网关把瞬时错误报成 4xx 是端点事实），但必须文档化 + trace 里可见（已在 `_runtime_snapshot` 里，✅） |
| 3 | `reasoning_effort`（runtime，主模型）vs `context.summary_reasoning_effort`（harness，摘要模型） | 名字近似、语义不同域 | 保持现状，但建议 `RuntimeConfig` 侧改名 `model_reasoning_effort` 或至少在 docstring 写"主模型 vs 摘要模型" |
| 4 | `control.max_iterations`（harness）vs `create_agent_turn(max_iterations=...)`（入参） | 三层副本：协作对象、`TurnGuard`、`AgentTurn.max_iterations` | 见第 2 节，应压到"只在 factory 解析一次" |

### `max_reasoning_chars` 该不该进 `control`？

**建议不进**，三个理由：

1. 它是**特定模型失效模式的对症药**：`llm_stream.py:41` 的注释写明是防"思考链无限螺旋、永不产出 content/tool_call"（ARS 类任务上观察到）。这是"这个端点+这个模型有这毛病"，跟 `stream_idle_timeout` 同一族，而不是你想在 harness 家族里演化的行为策略。
2. 进度条一进 `control` 就会进 `genes_hash`：任何一次 `harness derive` 顺手改它，都会在 `evaluation.py` 的 lineage 对比里表现为"harness 变好/变差"，**给归因添噪声**。
3. 现在默认 50k，你从没按 harness 调过它；提前搬过去属于 over-engineering。

如果你**真的**想跑"截断 CoT 到 50k 是否能提升 pass rate"这个 ablation，那时再加 `control.max_reasoning_chars: int | None = None`（`None` = 用 runtime），做成**单向**的"harness 优先、runtime 兜底"，而不是两边都能写同一语义。

### `resolve_max_retries` 到底管什么 + 命名坑

它管的是 **`recovery.tool_error_max_retries`：连续"同样的工具错误"容忍几次后熔断早停**（`tool_error_budget.py:61-88`：按 `(tool, error_signature)` 计数，任何成功或换错归零，`0` = 关闭该护栏，退回"一直试到 `max_iterations` 用完"）。所以 **`retry` 这个词是错的**——它不是"重试 N 次"，而是"容忍 N 次相同失败"。现在 "retry" 在仓库里有 **3 套完全不同、且互不相通的东西**：

| 名字 | 谁管 | 语义 | 事实源 |
|---|---|---|---|
| `recovery.llm_errors.<category>.max_retries` + `backoff`/`base_delay` | harness | 单次 LLM 调用的重放次数与退避（`llm_request.py:296`） | 纯 harness ✅ |
| `recovery.tool_error_max_retries` | harness gene + `RuntimeConfig` env override | 连续相同工具错误熔断阈值（`turn_guard.py:65`） | **双源** ⚠️ |
| summarizer 第二次尝试 | 无 | `context/summary.py:390` 的 `for attempt in (1, 2)` **硬编码**，连基因都没有 | 硬编码 ⚠️ |

建议（单开一单，别混进当前 phase）：

- `resolve_max_retries` → `resolve_same_error_limit(harness_value, *, runtime)`；字段 `tool_error_max_retries` → `same_error_limit` / `tool_error_limit`，harness YAML 里同改（loader 只认新键，旧键报明确错误，别做双读）。
- 保留 env 名 `AGENT_RUNTIME_TOOL_ERROR_MAX_RETRIES` 一个版本做过渡，但**它只能覆盖 harness gene 的空缺或调试**，不能是"两个字段各写一半"。
- summarizer 的那次重试要么提成 `context.summary_retries: int = 1` gene，要么在注释里明确"这是实现细节、不进基因"——现在是第三种：悄悄硬编码。

---

## 2. `open_turn` / `create_agent_turn` / `TurnCollaborators` 的平铺参数

### 为什么不能马上 `open_turn(runtime=...)`

**因为 `skills_dir` 还没进 `RuntimeConfig`**。`loop.py:100-106` 现在把 `memory_enabled`/`memory_dir` 从 runtime 拆出来传，而 `skills_dir` 是 `create_agent_turn` 的独立入参，最终落到 `skills.py:58 default_skills_dir()` → **每轮现读 `AGENT_RUNTIME_SKILLS_DIR`**。webui 从不传 `skills_dir`（grep 无命中），所以它每轮都在读环境——这正是当前 phase 遗留的 P1 尾巴（review doc 已记）。

顺序必须是：**先 `RuntimeConfig.skills_dir` 落地 → 再谈把 runtime 整体下传**，否则 `open_turn(runtime=...)` 里还是会有个 `skills_dir` 平级参数，"清爽"只做了一半。

### 目标形状（建议的分层）

```
入口（run_agent / webui / harbor）
  → RuntimeConfig.from_env(...)  +  resolve_harness(...)     ← 组合根，只有这里碰环境
  → create_agent_turn(msg, llm, tools, *, runtime, harness, stream_override=None, max_iterations=None)
       负责：所有"派生值"解析一次（max_iterations、stream、skills/memory 全部来自 runtime）
  → TurnCollaborators = 纯对象图 {llm, tools, workspace, harness, conversation, trace, events,
                                 observations, guard, tool_runner, summarizer} + runtime + stream
  → AgentTurn：runtime 非 None（factory 已保证），删掉 4 处 `if self.runtime is not None else None`
```

三条具体建议：

1. **`TurnCollaborators` 去掉派生字段**。`max_iterations` / `skills_dir` 都不该是它的属性：
   - `max_iterations`：`TurnGuard` 里已有一份（`turn_guard.py:60`），`loop.py:115,205` 改读 `self.guard.max_iterations` 即可，副本归零。
   - `skills_dir`：进 `RuntimeConfig` 后由 `open_turn` 自己取。

   剩下真正"跨 turn 可变、且不由 runtime/harness 决定"的只有 `stream`（测试/CLI 要强制关流），保留它作为**唯一显式 override**。
2. **`open_turn(conversation, harness, workspace, trace, runtime)`**：内部 `runtime.memory_enabled/memory_dir/skills_dir` 直取，`runtime=None` 只在测试直接调用时兜底一次。这样 loop 里那 6 行三元表达式变 1 行。
3. **`AgentTurn.runtime` 改为非 Optional**（`RuntimeConfig | None` → `RuntimeConfig`）。已核实 `AgentTurn(...)` 全仓只在 `turn_factory.py:109` 构造，且 `turn_factory.py:74` 已保证非 None；`TurnCollaborators` 也只在 factory 里被构造 → 这个收窄是安全的，能删掉 `loop.py:103-106`、`llm_request.py` 那条 `runtime or RuntimeConfig.from_env()` 的测试兜底分支（`use_streaming`/`_reasoning_budget_chars`/`_retry_4xx_enabled` 的 `runtime is None` 分支可一并删掉，签名收紧为非 Optional）。

**一个需要拍板的点**：`run_turn(..., stream: bool = False)` 的默认值。生产入口都是 `runtime.streaming`（webui 传 `snapshot.stream`，harbor 传 `use_stream`），但几十个测试靠"不传 = 非流式"吃饭。若把默认改成 `None → runtime.streaming`，`from_env()` 默认 streaming=True 会**让一批测试悄悄变流式**。建议：**本次不动 `stream` 默认值**，只在 `create_agent_turn` 里加 `stream: bool | None = None`（`None` = 用 `runtime.streaming`），`run_turn` 保持 `stream=False`，等单独一单做测试 sweep。

---

## 3. 建议的下一单范围（按收益/风险排序，推荐一单只做 2）

1. `RuntimeConfig.skills_dir` 落地（含 `AGENT_RUNTIME_SKILLS_DIR` 一次解析）+ 删 `model_catalog` 启动期的直接 env 读 —— 是 2 的前置，独立可交付。
2. **turn 组装层去平铺**：`open_turn(runtime=...)`、`TurnCollaborators` 去 `max_iterations`/`skills_dir`、`AgentTurn.runtime` 非 Optional、连带删 `runtime is None` 兜底分支。改动集中在 4 个文件 + 单测（`tests/agent/test_loop.py:1132-1136` 那条断言要看）。
3. （更晚）`resolve_max_retries` 改名 + summarizer 重试基因化 —— 会动 harness YAML 与 lineage，建议单独一单，别和 2 混。
