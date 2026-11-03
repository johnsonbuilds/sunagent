# `agent_runtime` 代码架构评估与改进报告

对 `src/agent_runtime/` 下的全部 49 个 Python 源码模块（约 7,500 行代码）进行了全面架构与代码审查。整体而言，这是一个**架构边界清晰、工程纪律严明、设计哲学（如 Harness 基因化配置、双流事件模型、基于文件系统的无限上下文溢出）非常成熟**的 Agent Runtime。

以下从高内聚低耦合、可维护性、可扩展性、可观测性四个核心维度进行评估，并在最后给出 **Top 5 改进建议**。

---

## 一、 四维架构质量评估

### 1. 高内聚、低耦合性（评分：8.5 / 10）

**亮点：**
* **单向依赖树与协议解耦（Protocol-driven）：** 核心抽象通过 `typing.Protocol` 定义（如 `execution/base.py` 中的 `ShellExecutor` 与 `Workspace`，`tools/tools.py` 中的 `ToolExecutor`，`agent/chat.py` 中的 `ChatModel`）。底层 `execution`、`events`、`trace`、`harness/schema` 几乎零外部依赖，模块间不存在循环导入（Circular Imports）。
* **组合根（Composition Root）模式落地良好：** `agent/turn_factory.py` 与 `agent/turn_collaborators.py` 将 `AgentTurn` 所需的 11 个协作对象在入口处一次性装配注入，保证了 `workspace` 在文件工具与 `ObservationFormatter`（溢出归档器）之间的唯一性共享。
* **历史主动修复过倒置依赖：** 在 `execution/base.py:48` 中将搜索常量与 `matches_path` 下沉，消除了原先 `execution/harbor.py -> tools/search.py` 的反向依赖。

**不足（耦合痛点）：**
* **跨层具象依赖残留：**
  * `agent/observations.py:39` 为了拿一个常量 `OUTPUTS_DIR = ".outputs"`，直接导入了 `from agent_runtime.tools.code import OUTPUTS_DIR`（导致 `agent` 核心层反向耦合具体工具实现层 `tools.code`，且 `.outputs` 在 `execution/base.py:59` 中又硬编码了一份）。
  * `agent/turn_factory.py:33` 直接导入了具象实现 `from agent_runtime.providers.llm import OpenAICompatibleLLM` 来构造默认 `summarizer`，使纯编排层耦合了具体 Provider。
  * `tools/files.py`、`patch.py`、`search.py`、`shell.py`、`symbols.py` 全部硬编码了 `from agent_runtime.execution.local import LocalWorkspace` 作为参数缺省回退，使得具象工具实现与本地执行环境绑定。
* **`agent/context.py` 内聚度偏低：** 单文件达 864 行，同时承担了：消息/工具调用字符统计、锚点校准 Token 估算、工具结果折叠、`compact_observations` 策略、LLM 摘要 Prompt 构造、XML 解析与 Schema 校验、摘要器 LLM 懒加载与生命周期管理、策略路由等职责。

---

### 2. 可维护性（评分：8.5 / 10）

**亮点：**
* **不变量由结构保证（Invariants by Construction）：**
  * `TurnHistory`（`agent/turn_history.py`）将完整对话 `messages` 与请求视图 `view` 封装为同步追加结构，避免了在多处调用点手动维护两个列表的一致性。
  * `ToolCallOutcome`（`agent/tool_dispatch.py`）在进入历史记录前对未授信的 LLM 工具调用做 Schema 校验与 JSON 规范化，杜绝了畸形 JSON 污染会话上下文。
  * 不可变配置（`@dataclass(frozen=True)`）贯穿 `HarnessSpec`、`RunEvent`、`AgentEvent`、`SubmissionRules`。
* **注释重“Why”而非“What”：** 代码中的文档字符串深入解释了真实的线上/评测踩坑原因（如 `context.py` 中为何 `_summary_boundary` 只在产生新摘要时推进、`providers/llm.py` 中为何要剔除悬空 `tool_calls`）。

**不足：**
* **环境变量读取散落全局（25+ 处）：** `os.getenv` 散落在 `chat.py`、`llm.py`、`reasoning_echo.py`、`turn_factory.py`、`memory.py`、`guard.py`、`cli.py`、`webui.py` 等十几个模块中。更严重的是，`reasoning_echo_enabled()` 在 `turn_history.py:113,134` 和 `context.py:854` 中**不带参数调用**，导致即使上层向 `OpenAICompatibleLLM(model=..., base_url=...)` 显式传入了模型参数，预算估算层依然去读环境变量 `MODEL_ID` / `LLM_BASE_URL`，引发潜在的配置分裂（Split-Brain）。
* **渠道展示层与工具 Schema 演化脱节：** `channels/tool_summary.py:45-48` 中对 `apply_patch` 的摘要逻辑仍在读取旧版的 `arguments.get("patch")` 和 `'>>>>>>> REPLACE'` 标记，而实际上 `tools/patch.py` 早已重构为接收 `edits: list[{old_text, new_text}]`。

---

### 3. 可扩展性（评分：7.8 / 10）

**亮点：**
* **Harness 基因化演进机制：** 通过 YAML Manifest 的 `parent` 继承与 `genes_hash` 签名，支持以声明式数据驱动 Prompt、工具集、上下文策略、错误恢复与提交校验的演进。
* **Channel 插件化：** `EventEmitter` 支持任意实现了 `(AgentEvent) -> None` 的 Subscriber（如 `CLIRenderer`、`WebUIChannel`），未知事件类型自动忽略，具备良好的前向兼容性。

**不足：**
* **策略注册表存在“半开放”硬编码：**
  * `harness/schema.py` 虽然定义了 `TOOL_ERROR_STRATEGIES` 和 `BACKOFF_POLICIES` 字典，但 `context.strategy`（`agent/context.py:836`）和 `verification.mode`（`agent/turn_finisher.py:67`）仍是硬编码的 `if/elif` 分支，无法通过注册表扩展新策略。
* **多语言与远程环境扩展受限：**
  * `tools/symbols.py:100,131` 虽然设计了 `GRAMMARS = {".py": tree_sitter_python}` 字典，但 `find_symbol` 与 `find_references` 方法内部却硬编码了 `await self._sources(".py")` 以及 Python 专属的 AST 节点名 `"class_definition"` / `"function_definition"`，且文件缓存仅按 `size` 判断失效（同字节数修改会命中脏缓存）。
  * `memory.py:217` 强依赖本地文件系统路径 `Path(workspace.root)` 与 `os.walk`，无法通过 `Workspace` 协议在无本地挂载的远程容器环境（如 Harbor）中使用持久化记忆。

---

### 4. 可观测性（评分：9.0 / 10）

**亮点：**
* **双流分离设计（Dual-Stream Architecture）：**
  * **内部审计流（`RunTrace` / `RunEvent`）：** 面向离线评测与根因分析，JSONL 首行写入完整的 `HarnessSpec` 基因快照与 `genes_hash`，通过 `trace.span()` 记录每个迭代、LLM 调用、工具执行、上下文压缩的精确耗时（`duration_ms`）与状态。
  * **用户交互流（`EventEmitter` / `AgentEvent`）：** 面向 CLI 与 WebUI 实时渲染，解耦了底层 trace 细节。
* **评测闭环（`evaluation.py` + `lineage.py`）：** 实现了 `Harness -> Benchmark -> Trace -> Score` 的自动化归因与父子版本准确率对比（Delta）。

**不足：**
* **缺少 Token 消耗与成本的显式可观测事件：** `providers/llm.py` 虽然提取了 `usage: {prompt_tokens, completion_tokens}`，但 `agent/chat.py:139-144` 在收到响应后仅将其返回给 `loop.py` 用于校准 `_usage_anchor`，**既没有写入 `trace.span("llm")` 的 `span_meta`**，也没有在 `turn.end` / `agent.completed` 中汇总整轮对话的累计 Token 消耗、缓存命中或模型名称。
* **缺少 Trace Context 透传（Sub-call 盲区）：** 当 `context.py` 触发 `llm_summary` 摘要调用，或 `turn_finisher.py` 触发 `command_to_verify` 验证重跑时，没有 `parent_span_id` 关联，且摘要器的 Token 消耗与耗时未被纳入主 `RunTrace` 的 LLM 账本中。
* **`RunTrace` 内存无界增长：** `trace.py:39` 中 `self.events` 在整个运行周期内驻留内存且包含完整 `messages` / `content`，在超长多轮会话中存在内存膨胀风险。

---

## 二、 Top 5 改进建议（按优先级与架构收益排序）

### Top 1：消除隐式环境变量依赖，收敛为统一的 `RuntimeConfig` 并修复配置分裂
* **问题定位：**
  * `providers/llm.py`、`reasoning_echo.py`、`agent/chat.py`、`agent/turn_history.py`、`agent/context.py` 等 10 余个模块在运行时高频调用 `os.getenv()`。
  * `turn_history.py:113,134` 和 `context.py:854` 调用 `reasoning_echo_enabled()` 时未传入当前 `llm` 实例的 `model` 与 `base_url`，导致通过代码显式传参构造 `OpenAICompatibleLLM` 时，`TurnHistory` 与 `context` 仍按环境变量计算 `count_reasoning`，产生线级投影（Wire Projection）与预算估算不一致的 Bug。
* **改进方案：**
  1. 引入不可变的 `RuntimeConfig`（或 `EndpointConfig`），在组合根 `turn_factory.py` 启动时一次性解析环境变量与显式参数。
  2. 在 `ChatModel` 协议中暴露只读属性 `echo_reasoning: bool`（或 `model: str`），由 `TurnCollaborators` 注入给 `TurnHistory` 和 `context`，彻底消除深层业务逻辑中的 `os.getenv()` 调用。

### Top 2：补齐 LLM Token 账本与子调用（Summarizer / Verifier）的可观测性闭环
* **问题定位：**
  * `agent/chat.py:106-147` 的 `trace.span("llm", iteration)` 只记录了 `streaming`、`response` 文本和 `tool_call_count`，丢弃了 `response.get("usage")` 中的 `prompt_tokens` 和 `completion_tokens`。
  * `context.py` 中的 Summarizer LLM 调用以及整轮 `AgentTurn` 结束时的 `turn.end` 事件均无累计 Token 统计，导致离线分析 `agent-runtime.jsonl` 时无法核算单次任务的真实 Token 成本。
* **改进方案：**
  1. 在 `agent/chat.py` 的 `trace.span("llm")` 结束前，将 `usage`（`prompt_tokens`、`completion_tokens`）及 `model` 写入 `span_meta`。
  2. 在 `TurnHistory` 或 `RunTrace` 中维护 Turn 级别的 `TokenUsageAccumulator`（区分主模型调用与 Summarizer 辅助调用），并在 `turn.end` 和 `agent.completed` 事件中输出总计 Token 消耗。

### Top 3：拆分 `agent/context.py`（864 行），解耦 `turn_factory -> providers.llm` 与 `observations -> tools.code`
* **问题定位：**
  * `agent/context.py` 职责过载，且通过 `_DEFAULT_SUMMARIZER: ContextVar` 自行管理 LLM 客户端生命周期；同时 `agent/turn_factory.py:33` 反向导入了 `providers.llm.OpenAICompatibleLLM`。
  * `agent/observations.py:39` 从 `tools.code` 导入 `OUTPUTS_DIR`。
* **改进方案：**
  1. **拆分子模块：** 将 `agent/context.py` 拆分为 `agent/context/budget.py`（字符/Token 计量与锚点校准）、`agent/context/folding.py`（工具结果与历史折叠）、`agent/context/summarizer.py`（摘要 Prompt、XML 校验与执行）及 `agent/context/strategies.py`（策略注册表与路由）。
  2. **清理跨层依赖：** 将 `OUTPUTS_DIR = ".outputs"` 和 `SCRIPTS_DIR = ".scripts"` 下沉至 `execution/base.py`（与 `SKIP_DIRS` 同处一处）；将 Summarizer 的默认构造抽象为 `ChatModel` 工厂协议，移出 `agent/` 核心编排层。

### Top 4：将 `ContextStrategy` 与 `VerificationMode` 改造为可插拔的策略协议（Protocol Registry）
* **问题定位：**
  * `agent/context.py:836` (`apply_context_strategy`) 和 `agent/turn_finisher.py:67` (`TurnFinisher`) 使用硬编码的分支判断策略名称，新增上下文压缩算法或验证门控模式必须修改核心循环代码，违反开闭原则（OCP）。
  * `tools/tools.py:130-420` 中 12 个内置工具的 JSON Schema 定义全部集中在一个 472 行的文件中，每个工具实现文件（如 `files.py`、`patch.py`）与其 Schema 定义分离。
* **改进方案：**
  1. 参照 `TOOL_ERROR_STRATEGIES` 的设计，定义 `ContextStrategy(Protocol)` 和 `VerificationGate(Protocol)` 注册表，使 `context.strategy` 和 `verification.mode` 真正成为可独立注册扩展的组件。
  2. 将各工具的 `ToolSpec` 构造器就近下沉至各自的工具模块（`files.py`、`patch.py`、`search.py` 等），同时同步修复 `channels/tool_summary.py:45` 中 `apply_patch` 的过时参数摘要逻辑（改为读取 `arguments.get("edits")`）。

### Top 5：增强 `TreeSitterIndex` 缓存健壮性与 `memory.py` 的远程 Workspace 兼容性
* **问题定位：**
  * `tools/symbols.py:67` 中 `TreeSitterIndex` 仅用 `cached["size"] == size` 判断文件是否变更。当 Agent 使用 `edit_file` 修改了等长字符串（如将 `x == 1` 改为 `x != 1`，或修改等长变量名）时，文件 `size` 不变，缓存返回陈旧 AST。此外，符号提取硬编码了 `.py` 与 Python 节点类型。
  * `memory.py:217` 直接使用 `os.walk(Path(workspace.root) / "memory")`，绕过了 `Workspace` 抽象，导致远程/容器 Workspace 无法复用。
* **改进方案：**
  1. 在 `TreeSitterIndex` 缓存键中引入内容哈希（`hash(text)` 或 `(size, mtime/content_hash)`），并将每个语言的 AST 节点定义（如类/函数节点类型名称）封装进 `GRAMMARS` 语言配置结构中。
  2. 让 `memory.py` 优先通过 `Workspace.find_paths("memory/**")` 或 `Workspace.list_dir("memory")` 构建记忆树视图，消除对本地 `os.walk` 的强绑定，使本地与远程容器环境行为完全一致。
