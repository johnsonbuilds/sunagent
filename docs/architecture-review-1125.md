### 总体判断(2026-11-25)

当前 `src/agent_runtime/`（64 个文件、13,186 LOC）的**底座质量不错**：核心循环（`agent/loop.py`）、会话视图不变量（`agent/turn_history.py`）、协议解耦（`ChatModel` / `ToolExecutor` / `Workspace` / `ShellExecutor`）和双流可观测性（`RunTrace` + `EventEmitter`）都比较扎实，没有盲目堆框架或过度抽象。

但随着功能从 ~7.5k LOC 扩张到 13k+ LOC，当时最突出的问题**不是缺抽象，而是三类结构性腐化**：
1. **半成品迁移带来的“双轨制”**：`RuntimeConfig`、`register_context_strategy`、遗留别名都处于“新机制已建，旧路径未清”的状态，增加了维护与排查成本。
   - **进展（2026-11-27 复核）**：`RuntimeConfig` ✅ 已收口；`register_context_strategy` “假开放” ❌ 仍开放；遗留别名 ❌ 仍待清。
2. **局部“引力井”膨胀**：核心循环本身很简单，但复杂度正往 `agent/chat.py`（769 行）和 `execution/harbor.py`（421 行）聚集；同时离线评测/诊断代码（`diagnosis.py` 814 行、`evaluation.py` 356 行、`lineage.py`）直接混在 `agent_runtime` 顶层。
   - **进展（2026-11-27 复核）**：`chat.py` ✅ 已三拆（`llm_request`/`llm_stream`/`llm_recovery`）；`harbor.py` 与评测/诊断混放 ❌ 依旧。
3. **跨入口生命周期样板重复**：CLI、WebUI、Harbor 三个入口各自手写 `record_config -> run_turn -> agent_error -> afinalize_run`，已经出现实际漂移 Bug。
   - **决策（2026-11-26）**：✅ 不做统一（漂移 Bug 已各自修掉），详见 §5 P0 行与文末注。

下面按你关注的五个维度展开，所有结论均基于当时（2026-11-25）源码与 AST 依赖扫描；**逐项最新状态以 2026-11-27 复核标注为准**。

---

### 一、 核心循环与执行层的“简单与稳定”评估

#### 1. 核心循环（`agent/loop.py`，261 LOC）——整体健康，警惕周边膨胀
- **做得好的地方**：`AgentTurn._run()` 控制流非常线性、易读：开场注入 → 迭代上限检查 → 刷新请求视图 → `chat_with_recovery` → 有工具则 `ToolRunner` 执行、无工具则结束。`TurnHistory` 把完整历史 `messages` 与请求视图 `view` 同步封装，从结构上消灭了状态不同步风险。
- **稳定性隐患**：
  - ~~**`agent/chat.py`（769 LOC，全库第二大文件）承担了 4 类正交职责**~~ **✅ 已解决（2026-11-25）**：当时它承担 4 类正交职责（SSE 流式拼接 / 失败分类与 nudge / 遥测计费 / 重试与超窗压缩），任何一项改动都要动到核心重试循环所在文件。现已三拆为 `agent/llm_request.py`（291 行，主流程）+ `agent/llm_stream.py`（227 行，流式拼接）+ `agent/llm_recovery.py`（247 行，分类与恢复），`chat.py` 已不存在，边界与原诊断的四象限一一对应。
  - **退出路径上的事件发射分散**（❌ 仍开放，2026-11-27 复核）：`agent.completed`（面向 UI）分散在 **3 个文件/分支**（`agent/loop.py:_run`、`agent/loop.py:_summary_turn`、`agent/turn_guard.py:check_tool_error`），而 `agent.end`（面向 trace）统一在 `loop.py` 的 `trace.span("agent")` 里。`TurnGuard` 作为保护逻辑却直接承担了“发射最终完成事件”的职责，一旦新增终止条件极易漏发其中一条流。
  - **`TurnCollaborators` 已达 12 个字段**（`agent/turn_collaborators.py`）：尚在可接受边界，但继续往里塞参数会让装配层变脆；应避免再把细碎策略拆成更多微对象注入。

#### 2. 执行层（`execution/`）——`local.py` 干净，但 `base.py` 与 `harbor.py` 正在偏离“纯执行原语”
- **做得好的地方**：`LocalShellExecutor` 和 `LocalWorkspace`（`execution/local.py`, 181 LOC）边界清晰、代码克制，路径越界检查（`resolve().is_relative_to`）和 CRLF 字节保真都很扎实。
- **边界侵蚀问题**：
  - **工具层常量下沉污染了执行层契约**（❌ 仍开放，2026-11-27 复核）：`execution/base.py` 里塞了 `DEFAULT_GREP_RESULTS`、`MATCHES_PER_FILE`、`PREVIEW_CHARS`、`DEFAULT_FIND_RESULTS`、`MAX_FILE_BYTES`、`matches_path()`。这本来是 `tools/search.py` 的业务规则，只因为 `HarborWorkspace` 实现了 `search_contents` / `find_paths` 为了避免 `execution -> tools` 循环依赖，被迫把搜索策略塞进了执行底座。
  - **`execution/harbor.py`（现 417 LOC）混杂了“容器传输原语”与“远端搜索脚本引擎”**（🔧 部分解决，2026-11-27 复核）：大约一半代码是在拼装 `rg` → `rg --pcre2` → `grep -rn` 的 shell 管道和输出解析（仍在，未拆 `harbor_search.py`）。但隐式耦合已修：`Workspace` Protocol 现已显式声明 `search_contents` / `find_paths`（默认返回 `None` 表示无快速路径），`tools/search.py` 的 `getattr(ws, ...)` 探测已成为带默认实现之上的防御性检查，不再是无契约的鸭子类型。
  - **执行安全语义不一致 + 小漂移**（❌ 仍开放，2026-11-27 复核）：
    - `LocalShellExecutor` 有 `CommandGuard`，而 `HarborShellExecutor` 没有（即便容器有隔离，策略是否豁免应是显式决策而非遗漏）。
    - `execution/guard.py` 定义了 `CommandGuard.blocked_result()`，但 `execution/local.py:55-63` 自己手拼了返回 dict，没调该方法，容易造成字段漂移。

---

### 二、 模块化、高内聚与低耦合

AST 全量依赖扫描结果暴露出几个值得关注的架构边界问题：

1. ~~**存在真实的模块循环依赖**~~ **✅ 已彻底解决（2026-11-27，经独立复核验证）**：
   当时的环：
   ```
   agent_runtime/__init__.py -> agent_runtime.trace -> agent_runtime.metrics -> agent_runtime (via `from agent_runtime import storage`)
   agent_runtime.trace <-> agent_runtime.metrics
   ```
   两个根因及对应解法：
   - 根因①`__init__.py` 在包根 re-export `RunTrace`，任何 `from agent_runtime import storage` 都先拉起 `trace -> metrics` → **解法**：`__init__.py` 改为 PEP 562 惰性 `__getattr__` 导出（`TYPE_CHECKING` 块保类型），包根初始化零重量级 import；
   - 根因②`trace.py` 与 `metrics.py` 互相强绑定（`RunTrace` 驱动 `RunStats`，`finalize_run` 深度操作 `RunTrace`）→ **解法**：共享契约下沉两个零环叶子模块——`run_records.py`（`RunMetrics`/`RunConfig`/常量，只依赖 stdlib）与 `trace_stats.py`（`RunStats`，只依赖 `run_records`）；`metrics.py` 改为面向 `TraceLike(Protocol)` 的依赖倒置，不再触碰 `RunTrace` 私有属性。
   最终依赖图（已是 DAG）：
   ```
   __init__ ──(惰性)──► trace ──► run_records (纯 stdlib 叶子)
                   │                ▲
                   └──► trace_stats ┘
   metrics ──► run_records + trace_stats + storage
   ```
   防回潮：`tests/test_import_graph.py` 6 条子进程导入冒烟 + 叶子纯度断言全绿；另经 AST 环检测、多入口 × 正反导入顺序独立进程冒烟复核，`trace <-> metrics` 运行时耦合为零。

2. **运行时核心 vs. 离线分析/评测系统混在一个顶层命名空间**（❌ 仍开放，2026-11-27 复核）：
   - `src/agent_runtime/diagnosis.py`（814 LOC，全包最大文件）、`evaluation.py`（356 LOC）、`lineage.py` 与 `trace.py`、`events.py` 平级放在顶层。
   - 实际上它们属于“消费 trace/metrics 的离线评测与自诊断系统”，与“执行一个 agent turn 的 runtime”演化速率完全不同。放在一起会让 `agent_runtime` 的公开表面越来越重。

3. **工具元信息的隐式硬编码耦合**（❌ 仍开放，2026-11-27 复核）：
   - `agent/tool_dispatch.py` 硬编码 `_EDIT_TOOLS = {"write_file", "edit_file", "apply_patch"}` 来统计 `edited_files`；
   - Shell 结果的鸭子类型判断（`"exit_code" in result and "stdout" in result`）在 `agent/observations.py` 和 `agent/tool_dispatch.py` 两处重复。
   - 新增一个写文件类工具或修改工具返回结构时，必须记着同步改 `agent/` 内部代码，破坏了“工具自描述、核心层不感知具体工具名”的原则。

---

### 三、 可扩展性与配置一致性（兼查过度设计）

#### 1. 两个典型的“半迁移”阻断点
- **`RuntimeConfig` 已收口（P0 完成，2026-11-26）**：
  `runtime_config.py` 的契约现在是“组合根一次解析，深层显式注入”。除 `runtime_config.py` 自身外，`agent_runtime` 里剩下的 `os.getenv` 可归为两类：
  - **可接受的边缘/一次性读取**：`integrations/harbor.py`（日志流开关）、`channels/{cli,webui}.py`（渲染开关）、`storage.py`（`AGENT_RUNTIME_HOME`，按调用读取以便测试改环境）、`tools/code.py`（`AGENT_RUNTIME_PYTHON`）、`trace.py`（`ENV_WHITELIST` 快照——记录而非读取行为）；`reasoning_echo.py` 的两个无参入口是兼容壳，`RuntimeConfig.from_env()` 只在解析时调它们一次并冻结结果。
  - **仍待收尾（P1，不在 P0 范围内）**：`model_catalog.py:110` 的 `AGENT_RUNTIME_MODEL_CATALOG` 在 WebUI 启动时读一次；它是启动期查表路径，不属于 per-turn runtime wiring。`AGENT_RUNTIME_SKILLS_DIR` 已于 2026-11-26 收口到 `RuntimeConfig.skills_dir`（默认 `./skills`，RuntimeConfig 为唯一事实源，不再有 harness-relative 回退）。

  本项已固定的一条硬语义：`resolve_max_retries(harness_value, runtime=...)` 在注入 `runtime` 时**永远不再读环境**——`tool_error_max_retries is None` 表示“沿用 harness gene”，而不是“回退到 env”。此前它会在 override 为 `None` 时二次 `RuntimeConfig.from_env()`，导致 `AGENT_RUNTIME_TOOL_ERROR_MAX_RETRIES` 能覆盖已注入的配置（即“一次解析、显式注入”契约被破坏）；`TurnGuard` 现在只传 `runtime=`，不再自行拆字段。**2026-11-26 后续**：双源已彻底删除——`RuntimeConfig.tool_error_max_retries` / `AGENT_RUNTIME_TOOL_ERROR_MAX_RETRIES` / `resolve_max_retries()` 均移除，gene 是唯一事实源（见 `docs/runtime-config-vs-harness-and-turn-assembly-1126.md` §4.3）。
- **Context 策略注册表是“假开放”**（❌ 仍开放，2026-11-27 复核；`view.py` 的特判现有注释说明其 char-metered 语义，但 loader 校验静态 `CONTEXT_STRATEGIES` 元组、绕过注册表的本质未变）：
  - `agent/context/strategies.py` 提供了 `register_context_strategy()` 用来按 OCP 扩展新策略；
  - 但 `harness/loader.py:216` 校验的仍是 `harness/schema.py:130` 里的静态元组 `CONTEXT_STRATEGIES`（注册了新策略 YAML 根本加载不过去）；
  - 且 `agent/context/view.py:135` 对 `fallback_strategy == "compact_observations"` 写了特殊 `if` 分支绕过 handler，等于注册表机制并没有真正闭环。

#### 2. 过度设计检查（哪里**不要**加抽象）
- **不要引入中间件链 / EventBus / DI 容器**：当前 `AgentTurn` + `TurnCollaborators` 的显式组合方式对 Python 代码非常友好，静态可读性和调试体验远优于框架化抽象。
- **不要把 `AgentTurn` 继续拆细**：`loop.py` 已经 261 行，职责够纯；继续拆只会增加跳转成本。
- **清理而不是包装历史遗留**（❌ 仍开放，2026-11-27 复核）：`MemoryGenome`（`schema.py:210` 别名）、`HarnessSpec.memory`（legacy 键兼容属性）、`VerificationGenome`（明文标注 no-op）、`OpenAILLM` 别名（`providers/llm.py:409`）都还在；无人发射的 `guard.block`/`policy.block` 分支已随重构挪到 `trace_stats.py:87`（补了注释说明来源，但兼容噪音本身未清）。`apply_memory_strategy` 已消失。适合按兼容窗口逐步删掉，而不是再加适配层。

---

### 四、 可观测性与可维护性

**强项**：
- 双流分离（`RunTrace` 结构化落盘 vs. `EventEmitter` 实时流式推送）非常清晰；
- `RunTrace` 后台队列写入 + 仅丢弃 `llm.chunk` + 取消路径同步补写（cancel-path flush），工程细节很成熟；
- 溢出即落盘（`.outputs/obs-*.txt`）+ `RunStats` 在 `retain_events="control"` 下实时累计指标，保证了长程运行的内存有界与指标完整；
- `genes_hash` + `run.config` 快照让实验具备很强的可复现性。

**明显短板**：
- **Run 生命周期样板在 3 个入口重复，且已产生实际 Bug**：
  `run_agent.py`、`integrations/harbor.py`、`webui/server.py` 都在重复“建 `RunTrace` → `record_config` → `run_turn` → 查 `trace.agent_error()` 决定 `status`/`failure` → `finally: afinalize_run`”。
  实际已经出现漂移：`integrations/harbor.py:109` 写了 `harness.control.temperature if hasattr(harness.control, "temperature") else None`，但 `ControlGenome` 根本没有 `temperature` 字段（它在 `RuntimeConfig` 上），导致 Harbor 录出来的 `run.config.model_params.temperature` 永远是 `None`。
- **指标解析依赖约定而非显式契约**（❌ 仍开放，2026-11-27 复核；随重构移到 `trace_stats.py:RunStats.observe()`）：既监听已废弃的 `"guard.block"` / `"policy.block"`，又通过 `tool.end` 里的 `error_kind == "command_blocked"` 兜底计数；并且仍需把账本汇总后的 `prompt_tokens` 减去 `summarizer_prompt_tokens` 还原主模型用量（一旦上游变更口径就会悄悄算错）。

---

### 五、 精简、务实的演进建议（按优先级）

原则：**只动高收益边界，每项改进都以“减少重复、收口单一事实源”为目标，不引入重型抽象。**

| 优先级 | 改动项 | 涉及文件 | 做法（最简方案） |
| :--- | :--- | :--- | :--- |
| **P0** | ~~收口 Run 生命周期包装~~ **不做（2026-11-26 决策）** | 新增轻量 helper（如 `agent/session.py` 或 `run_session` 函数），简化 `run_agent.py`、`integrations/harbor.py`、`webui/server.py` | 提供一个约 50 行的 `execute_run(...)` async context manager / helper，统一 `record_config`、状态判定（`completed/error/cancelled`）、异常捕获与 `afinalize_run`。顺手修复 Harbor 的 `temperature` 漏记，且把 `memory` 开关改为显式参数传入，消除 `harbor.py` 修改 `os.environ` 的副作用。<br><br>**决策：不做。** 未来主力入口只有 webui；CLI（`run_agent.py`）可能弃用，Harbor（`integrations/harbor.py`）只用于偶尔验证 benchmark 是否回落，三者不值得为一个共享 helper 付耦合成本。保留各自入口的独立 run 收尾逻辑。<br>**已核实**：`harbor.py` 改 `os.environ` 的副作用早已消除（现为 `replace(RuntimeConfig.from_env(), memory_enabled=False)`，见 `src/agent_runtime/integrations/harbor.py:56`）；`temperature` 漏记也已修（现读 `self._runtime.temperature`，见同文件 `record_config`）；`harness.control.temperature` 幻影字段已不存在。若哪天要退役 CLI，再顺手删 `run_agent.py` 即可，无需先合并生命周期。 |
| **P0** | **彻底完成 `RuntimeConfig` 收口 — ✅ 已完成（2026-11-26）** | `agent/llm_request.py`, `providers/llm.py`, `agent/tool_error_budget.py`, `execution/{local,harbor,guard}.py` | 删除 `llm_request.py` / `llm_stream.py` 与 `llm.py` 里重复的 `os.getenv` 回退函数（测试若不传 `RuntimeConfig`，由最外层默认 `RuntimeConfig.from_env()` 一次即可）；把 `workspace_root`、`command_guard_mode`、`memory_enabled`、`memory_dir`、`tool_error_max_retries`、`harness` 纳入配置，避免深层隐式读环境。收口后深层的 `runtime=None` 只意味着“没有配置可传”，**任何已注入的 `RuntimeConfig` 都不再被环境二次覆盖**。**其后修正（2026-11-26）**：`tool_error_max_retries` 不属于 RuntimeConfig——env 覆盖会破坏 harness gene 的唯一事实源，已连同 `AGENT_RUNTIME_TOOL_ERROR_MAX_RETRIES` / `resolve_max_retries()` 一并删除。 |
| **P1** | **拆分 `agent/chat.py`（保持 `loop.py` 不动）— ✅ 已完成（2026-11-25）** | `agent/chat.py` → `agent/llm_request.py` + `agent/llm_stream.py` + `agent/llm_recovery.py` | 只沿已有清晰边界三刀切：`llm_stream.py`（纯流式 chunk 合并与超时/截断检测）、`llm_recovery.py`（错误分类、`RecoveryPlan`、nudge 逻辑），`llm_request.py` 只保留调用+重试主流程（`chat_with_recovery`）并从两个 facet 模块再导出，`loop.py` 与各入口的依赖面零改动。三个文件统一 `llm_` 前缀，与 `turn_*` / `tool_*` 命名惯例一致。 |
| **P1** | ~~**打断 `trace <-> metrics` 与根包循环依赖**~~ **✅ 已完成（2026-11-27，独立复核通过）** | `src/agent_runtime/__init__.py`, `trace.py`, `metrics.py`, 新增 `run_records.py` / `trace_stats.py` | 落地优于原方案：1) `__init__.py` 改 PEP 562 惰性 `__getattr__`（根因①消除）；2) 共享契约下沉两个叶子模块 `run_records.py`（纯 stdlib）+ `trace_stats.py`（`RunStats`），`trace.py` / `metrics.py` 单向依赖叶子，互相不再 import；3) `metrics.py` 面向 `TraceLike(Protocol)` 依赖倒置，`finalize_run` 只走公共接口。验证：`tests/test_import_graph.py` 6 条（独立子进程、含叶子纯度断言）+ AST 环检测 + 多入口正反导入顺序冒烟全绿。 |
| **P1** | **统一 `agent.completed` 发射出口** | `agent/loop.py`, `agent/turn_guard.py` | `TurnGuard` 只返回“是否早停 + 停止文案”，不再直接调 `events.emit("agent.completed", ...)`；所有自然结束、超步总结、熔断早停统一在 `AgentTurn._run` 单一出口发射 `agent.completed`，与 `trace.span("agent")` 严格 1:1 对齐。 |
| **P2** | **理顺 `execution` 与 `tools` 边界** | `execution/base.py`, `execution/harbor.py`, `execution/local.py`, `tools/search.py`, `tools/spec.py` | 1) `LocalShellExecutor` 复用 `CommandGuard.blocked_result()`；2) 若保留远端快速搜索，在 `execution/base.py` 明确一个可选小 Protocol（`FastSearchWorkspace`）并把 shell 搜索脚本独立到 `execution/harbor_search.py`，把纯搜索默认参数放回 `tools/search.py`；3) 在 `ToolSpec` 上增加 1~2 个轻量元信息（如 `mutates_files: bool = False`），替代 `tool_dispatch.py` 里硬编码的 `_EDIT_TOOLS`。 |
| **P2** | **理清 `agent_runtime` 与评测/诊断子系统的边界** | `diagnosis.py`, `evaluation.py`, `lineage.py` | 将离线评测与自诊断模块归拢到 `agent_runtime/eval/`（或独立子包），并修齐 `harness/loader.py` 与 `register_context_strategy()` 的策略校验单一事实源（直接校验注册表而非另一份静态 tuple）。 |

---

### 建议保留不动的部分（已经足够好）
- `agent/loop.py`、`agent/turn_history.py`、`agent/tool_runner.py`、`agent/observations.py`：抽象粒度恰到好处，继续保持简单稳定。
- `execution/local.py` 与 `ShellExecutor` / `Workspace` 基础契约：保持极简，不要往 `Workspace` 主协议里塞过多高级操作。
- `harness` YAML 基因化 + `genes_hash` + `EventEmitter` 订阅模型：扩展性已经很够用，只需修补策略校验的单一事实源即可。

### 状态总览（2026-11-27 复核后）

| 项 | 状态 |
| :--- | :--- |
| P0 `RuntimeConfig` 收口 | ✅ 完成（2026-11-26） |
| P0 Run 生命周期统一 | ✅ 决策不做（2026-11-26，漂移 Bug 已各自修掉） |
| P1 `chat.py` 三拆 | ✅ 完成（2026-11-25） |
| P1 `trace <-> metrics` 与根包循环依赖 | ✅ 完成（2026-11-27，含防回潮测试） |
| P1 `agent.completed` 发射出口统一 | ❌ 未动（`turn_guard.py:122` + `loop.py` 3 处仍分散） |
| P2 `execution` ↔ `tools` 边界 | 🔧 部分（`Workspace` 已声明快速搜索协议；常量下沉、`harbor_search` 拆分、`blocked_result` 复用、`_EDIT_TOOLS` 均未动） |
| P2 评测/诊断归拢 `eval/` | ❌ 未动（`diagnosis.py`/`evaluation.py`/`lineage.py` 仍在顶层） |
| P2 Context 策略注册表闭环 | ❌ 未动（loader 仍校验静态 `CONTEXT_STRATEGIES`） |
| 清理遗留别名/死分支 | ❌ 未动（`OpenAILLM`/`MemoryGenome`/`VerificationGenome`/`HarnessSpec.memory`/`guard.block` 兼容分支） |

如果你想继续落地，剩下的候选按收益排序是：**P1 统一 `agent.completed` 发射出口**（退出路径正确性，防漏发）、**P2 Context 策略注册表闭环**（假开放挡扩展）、**P2 `execution` ↔ `tools` 边界收尾**。告诉我从哪一项开始，我可以直接给出具体改动方案并带测试验证落地。

> 注（2026-11-26）：**“收口 Run 生命周期包装” 已决定不做** —— 未来主力入口是 webui，CLI 可能弃用、Harbor 仅用于偶尔的 benchmark 回归验证，不值得为三个入口抽共享生命周期。
>
> 注（2026-11-27）：本次复核逐项对照了当前源码（含 `agent/completed` 发射点、`Workspace` 协议、`CONTEXT_STRATEGIES` 校验、遗留别名、`_EDIT_TOOLS` 等 grep 实证）并全量跑过 `tests/test_import_graph.py`；新增的 ✅/🔧/❌ 标注均以实测为准，原始诊断文字保留作历史记录。
