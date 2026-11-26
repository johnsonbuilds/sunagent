# RuntimeConfig ↔ harness 职责边界 与 turn 组装层参数收敛（2026-11-26）

> 本文是 P0「RuntimeConfig 收口」之后的复盘问答记录。§4.3（删除 `tool_error_max_retries` 双源）已实施；其余条目仍是建议，供确认后单开一单实施。

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

用这条判据看现在的重叠，只有 4 处与 turn 行为直接相关（其余深层 env 读见 §4.2）：

| # | 重叠点 | 现状 | 判断 |
|---|---|---|---|
| 1 | `recovery.tool_error_max_retries`（harness）vs `RuntimeConfig.tool_error_max_retries`（env `AGENT_RUNTIME_TOOL_ERROR_MAX_RETRIES`） | **真正的双份同名字段**，env 覆盖 harness gene | 保留 gene 为唯一事实源；**删除 `RuntimeConfig` 字段与 env**（见 §4.3，已实施） |
| 2 | `retry_4xx`（runtime）vs `recovery.llm_errors.invalid_request.max_retries`（harness） | `llm_recovery.py:199-202` 里 `retry_4xx` 把 400/404/422 从 `invalid_request` 改判成 `provider_error`，**直接让 harness 声明的 invalid_request 策略失效** | 这是最隐蔽的越权。该保留（网关把瞬时错误报成 4xx 是端点事实），但必须文档化 + trace 里可见（已在 `_runtime_snapshot` 里，✅） |
| 3 | `reasoning_effort`（runtime，主模型）vs `context.summary_reasoning_effort`（harness，摘要模型） | 名字近似、语义不同域 | 保持现状即可；改名优先级低（见 §4.8） |
| 4 | `control.max_iterations`（harness）vs `create_agent_turn(max_iterations=...)`（入参） | 三层引用：协作对象、`TurnGuard`、`AgentTurn.max_iterations`（factory 只解析一次，不会漂移） | 清洁性调整，非正确性问题（见 §4.4） |

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
| `recovery.tool_error_max_retries` | harness gene | 连续相同工具错误熔断阈值（`turn_guard.py:55`） | **gene 唯一事实源** ✅（2026-11-26 已删双源） |
| summarizer 第二次尝试 | 无 | `context/summary.py:335` 的 `for attempt in (1, 2)` **硬编码**，连基因都没有 | 硬编码 ⚠️ |

建议（单开一单，别混进当前 phase）：

- `resolve_max_retries` 改名可在以后单独做；**但改名会改变 `genes_dict()` 的序列化键，从而改变所有 harness 的 `genes_hash`**，且仓库里 25+ 个 archived YAML 仍用旧键。若做，必须配套迁移/兼容策略，不能只在 loader 里"旧键报错"。
- **本次直接删除 `AGENT_RUNTIME_TOOL_ERROR_MAX_RETRIES` 与 `RuntimeConfig.tool_error_max_retries`**：要让 env "只补 gene 的空缺"在当前 `int = 0`（0 同时是合法"关闭"）下无法区分"未设置"与"显式关闭"；既然已决定 gene 为唯一事实源，就不再保留隐式的第二写入口。未来若确实需要调试覆盖，用显式命名（如 `AGENT_RUNTIME_TOOL_ERROR_OVERRIDE`）并在 trace 标注，不做双读。
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
3. （更晚，可选）`resolve_max_retries` 改名 + summarizer 重试基因化 —— 会动 harness YAML 与 lineage，建议单独一单，别和 2 混；**双源删除已在本单先做（§4.3）**。

---

## 4. 评审补正与实施决策（2026-11-26）

> 本节记录对第 1–3 节的复核：确认哪些结论成立、修正哪些逻辑，以及已实施/后续动作。

### 4.1 判据修订：harness gene ≠ 所有 A/B 变量

第 1 节的硬规矩

> 能进 `genes_hash`、能被 lineage 当实验变量 A/B、改动能归因到 benchmark 差异 → harness gene

与现状自相矛盾：`model`、`temperature`、`base_url` 都在 `RuntimeConfig`，但它们恰恰是 benchmark 最核心的 A/B 变量。按原文判据它们应该进 harness，显然不是设计意图。修订为：

- **harness gene** = 智能体策略（prompt / tools / control / context / recovery / skills），描述"agent 怎么做"。
- **RuntimeConfig** = 运行条件（endpoint / model / sampling / 超时 / 预算 / 路径 / 安全开关），描述"在哪里、以什么资源做"。
- benchmark 的 A/B 轴可以横跨两者：模型/温度是 runtime 轴，策略是 harness 轴；`genes_hash` 只标识后者。

**对 `max_reasoning_chars` 的连带影响**：把它留在 runtime 是对的，但"不会影响横向对比"只在 runtime 被固定时成立。目前 `evaluation.py:101-129` 只按 `harness_id`/`genes_hash` 分组，**不读 runtime snapshot**；trace 虽然通过 `run.config` 记了 runtime，evaluation 没有消费。若两次跑分别用 50k / 100k，`genes_hash` 相同，差异会被归因给 harness。后续应增加 runtime fingerprint（至少 `model`、`temperature`、`max_reasoning_chars`、`retry_4xx`、`stream*`），不一致时告警或分组。

### 4.2 "只有 4 处重叠"不完整

按本文自己的判据，至少还漏了这些深层/隐式 env 读：

| env | 位置 | 读取时机 |
|---|---|---|
| `AGENT_RUNTIME_PYTHON` | `tools/code.py:25` | 每次执行工具 |
| `AGENT_RUNTIME_HOME` | `storage.py:43` | 每次取路径 |
| `AGENT_RUNTIME_MODEL_CATALOG` | `model_catalog.py:109` | 启动期一次 |
| `AGENT_RUNTIME_REASONING_ECHO` | `reasoning_echo.py:49` | 无 runtime 时的兼容兜底 |

其中 `PYTHON` / `HOME` 与 `skills_dir` 同族（深层 per-call env 读），应纳入下一单排查范围；`MODEL_CATALOG` / `REASONING_ECHO` 见 §4.7 / §4.8。

### 4.3 `tool_error_max_retries`：删除双源（已实施）

**决策：保 gene 为唯一事实源，直接删除第二写入口。** 具体改动：

- 删除 `RuntimeConfig.tool_error_max_retries` 字段、`AGENT_RUNTIME_TOOL_ERROR_MAX_RETRIES` 解析、trace snapshot 字段、metrics env whitelist 条目。
- 删除 `resolve_max_retries()`；`TurnGuard` 直接读 `harness.recovery.tool_error_max_retries`，不再接受 `runtime=`。
- **不实现**"env 只补 gene 空缺"：`RecoveryGenome.tool_error_max_retries: int = 0` 中 `0` 同时是合法"关闭"，无法区分"未设置"与"显式关闭"。要支持补缺，gene 必须改成 `int | None`，那是另一套语义，本次不做。
- 未来若确实需要调试覆盖，使用显式命名（如 `AGENT_RUNTIME_TOOL_ERROR_OVERRIDE`）并在 trace 标注，不做隐式双读。
- **改名暂缓**：`tool_error_max_retries` → `same_error_limit` 会改变 `genes_dict()` 序列化键，从而改变所有 harness 的 `genes_hash`；且 25+ archived YAML 仍用旧键。若做，必须配套迁移/兼容策略，不能只让 loader 对旧键报错。

### 4.4 `max_iterations` "三层副本"表述过重

`resolved_max_iterations` 在 `turn_factory.py:84-86` 只解析一次，随后按值传给 `TurnGuard`、`TurnCollaborators`、`AgentTurn`。它们不是三个独立事实源，只是同一个不可变 int 的三份引用，**不会漂移**。这是整洁性问题，不是正确性问题。另外让 loop 改读 `self.guard.max_iterations` 会让 loop 依赖 guard 的策略状态，耦合方向不理想；优先级应放低。

### 4.5 删除 `runtime=None` 分支的测试影响被低估

除 `tests/agent/test_loop.py:1132-1136` 外，至少还有：

- `tests/agent/test_loop.py:1092,1103`：显式测试 `use_streaming()` 无参读 env。
- `tests/agent/test_context_gates.py:291,319,339,483`：`chat_with_recovery` / `consume_stream` 不传 runtime。
- `tests/agent/test_tool_call_extras.py:104`：`consume_stream` 不传 runtime。

所以"签名收紧为非 Optional、删兜底分支"必须附测试迁移清单，或保留 `runtime=None` 作为公开兼容入口（内部解析一次 env），否则会变成大范围测试改动。

### 4.6 `skills_dir` 迁移要保留 harness.source 回退

`default_skills_dir()` 的优先级是：`AGENT_RUNTIME_SKILLS_DIR` → `<harness.source 的上级>/../skills` → `cwd/skills`。改为 `RuntimeConfig.skills_dir` 后必须保持：

```
RuntimeConfig.skills_dir（显式/环境解析结果）
  > harness.source 推导目录
  > cwd/skills
```

否则会悄悄改变"harness 与 skills 是兄弟目录"的现有行为。

### 4.7 `model_catalog` 属于 scope creep

`AGENT_RUNTIME_MODEL_CATALOG` 是启动期的一次性查表路径，不是 per-turn endpoint/transport 配置；`webui/server.py` 的 `AgentRunner.__init__` 已读一次，不违反"组合根解析一次"。把它塞进 `RuntimeConfig` 会让 runtime 承担目录表职责，收益不明确。建议从 §3 第 1 项拆出，单独作为"消除启动期直接 env 读"的小重构。

### 4.8 `reasoning_effort` 改名优先级很低

`reasoning_effort`（主模型）与 `context.summary_reasoning_effort`（摘要模型）作用对象不同，不构成真正的双源。改名只是可读性收益，却要动 RuntimeConfig、trace、providers、测试和外部 env 名。写成 docstring 即可，不进近期单子。

### 4.9 修订后的下一单顺序

1. `RuntimeConfig.skills_dir` 落地（保留 harness.source 回退）；同时决定 `AGENT_RUNTIME_PYTHON` / `AGENT_RUNTIME_HOME` 是否一并收口。
2. evaluation 增加 runtime fingerprint（避免 runtime 变化被误归因给 harness）。
3. turn 组装层去平铺 + 测试迁移清单（或保留 `runtime=None` 兼容）。
4. （更晚，可选）`tool_error_max_retries` 改名、summarizer 重试基因化。
