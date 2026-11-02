# `src/agent_runtime` Review：可维护性 / 可扩展性 / 耦合 / 可读性

> 日期：2026-10-30
> 更新：2026-11-02，逐条标注修复状态（含 loop 拆解 / context 去耦）
> 范围：`src/agent_runtime/` 全量（`agent/`、`tools/`、`execution/`、`providers/`、根模块、`channels/`、`integrations/harbor.py`）
> 方法：静态审查 + `file:line` 证据抽查

状态图例：✅ 已修复　🟡 部分修复 / 已缓解　❌ 未修复　➖ 已不适用（代码结构已变）

总览（评审时状态）：**骨架是对的，局部已腐烂**。`Protocol + ToolSpec + 双流事件 + 薄 channels` 方向正确，但 `harness.py / agent/loop.py / context.py / tools.py` 四个大文件集中了约 60% 风险：上帝对象、私有跨模块调用、错误契约三套并存、安全 guard 仅防君子。

评审时评分（未随修复重算）：

| 维度 | 评分（10 分制） | 一句话 |
|---|---|---|
| 可维护性 | 5 | `harness 847 行 + loop 837 行 + context 776 行`，改一处动四层 |
| 可扩展性 | 6 | 新增 tool / LLM 重试易，新增 context 策略 / 校验规则难 |
| 耦合性 | 5 | 无顶层循环依赖，但靠懒导入 + `_私有函数`跨模块维持，脆弱 |
| 可读性 | 7 | 模块头文档 + 命名好，`Any / dict[str, Any]` + 魔法数 + 兼容别名拖后腿 |

## 0. 修复进展（2026-11-02）

已落地 commit：`8988387`（lineage 无损）、`4643f3b` / `3a2a549`（工具错误契约 + 断 harbor 倒依赖）、`5c16e4f` / `e5c5911`（turn 拆解）、`ba49613`（工具错误 / 公开边界）、`32d0eeb`（harness 拆分 + 共享 channel 摘要）、`622fc3a`（context 策略注册表）。

本轮（未提交）：`AgentTurn` 拆出 `turn_opening / turn_guard / tool_runner`，`loop.py` 288 行 / 5 方法；`context.py` 去掉 provider 懒导入与可变 summarizer 全局，改由 `TurnCollaborators` 注入；reasoning echo 策略下沉到中性 `reasoning_echo.py`。

| 原问题 | 状态 | 当前证据 |
|---|---|---|
| P0-1 有损序列化 | ✅ | `lineage.py:245`、`evaluation.py:188` |
| P0-2 错误契约 + 断倒依赖 | ✅ | `patch.py:53`、`execution/base.py:52`、`tool_dispatch.py` 公开 API |
| P0-3 拆大文件 + 抽共享 | 🟡 | harness / turn / channel 已拆；`loop.py` 509→288；`context.py` 仍 907 行，但已去懒导入 / 全局 summarizer |
| 最小修复清单 6 条 | 🟡 | 2 条完成 + 1 条部分 + 2 条未完成 + 1 条已不适用（详见 §5） |
| 安全类问题 | ❌ | guard / syntax_gate / harbor 写盘基本未动 |

## 1. 耦合：最严重的问题

1. ✅ **`AgentTurn` 成 God Object（已修复）**：
   - ✅ `loop.py` 从 509 行降到 288 行、5 个方法（`__init__ / run / _run / _request / _summary_turn`）；import 从 11 个模块降到 7 个。
   - ✅ 开场注入移到 `turn_opening.open_turn`（`turn_opening.py:26`）：system prompt / skills / memory 只在这里组装一次。
   - ✅ 横切策略移到 `TurnGuard`（`turn_guard.py:45`）：预算提醒、同工具同错熔断、上下文窗口刷新、拒绝观察各归其位。
   - ✅ 工具执行移到 `ToolRunner.dispatch`（`tool_runner.py:37`）：校验后的调用按序执行、渲染 observation、驱动 guard/finisher。
   - ✅ `__init__` 不再 `getattr(tools, "workspace")` 再造 `ObservationFormatter`。`workspace` 已是 `ToolExecutor` 协议成员（`tools/tools.py:17-31`），`turn_factory.py:58` 显式取 `tools.workspace` 并注入 formatter，"tools ↔ observations 隐式约定"变成显式契约。
   - ✅ `TurnFinisher` 接管 submit 校验 / rerun（`turn_finisher.py:116`）；`turn_factory` 成为组合根，`AgentTurn` 不再自己构造协作者。
   - ✅ `_run` 从 134 行降到 73 行；`_request` 63 行、`_summary_turn` 66 行。`TurnCollaborators` 仍是显式数据包，但每个协作者只负责一类策略，循环本身只做编排。
2. ✅ **私有函数公开用，封装破了（全部修复）**：
   - `tool_dispatch` 的下划线函数已全部转正：`tool_trace_metadata / tool_result_summary / rejected_observation_* / trace_tool_rejection`（`tool_dispatch.py:192-291`）。
   - `context._extract_summary` 已转正为 `extract_summary`（`context.py:116`）；reasoning 回放判断改为中性的 `reasoning_echo.should_echo_reasoning / reasoning_echo_enabled`，旧 `reasoning_replayed` 已删除。
   - `harbor.py` 已不再 import `tools.search`；`SKIP_DIRS / matches_path / MAX_FILE_BYTES` 下沉到 `execution/base.py:48-96`。
   - 全仓已无跨模块 `_private` 调用（`grep` 验证）。
3. ✅ **循环依赖靠懒导入续命（已修复）**：
   - ✅ `harness.py:699` 对 `evaluation / lineage` 的函数内导入，随 harness 拆包消失；`harness/`、`lineage.py`、`evaluation.py` 已无懒导入。
   - ✅ `context.py` 已无 `from agent_runtime.providers...`（`grep` 验证）；reasoning 回放策略下沉到中性 `reasoning_echo.py:66`，provider 与预算层共用 `should_echo_reasoning / reasoning_echo_enabled`，`reasoning_replayed` 兼容名已删。
   - ✅ summarizer 改为显式注入：`TurnCollaborators.summarizer`（`turn_collaborators.py:47`）→ `TurnHistory` → `apply_context_strategy`；默认实现是 `turn_factory._default_summarizer` 的 `lru_cache` 懒工厂（`turn_factory.py:34`），不再有模块级可变全局。
   - ✅ `set_summary_llm` 保留为 `ContextVar` 兼容 / 测试钩子（`context.py:584`），生产路径不依赖它。
4. ✅ **Channels 重复（已抽取）**：`channels/tool_summary.py` 新增 `describe_tool`，`cli.py:11` 与 `webui.py:33` 共用，原 `_format_arguments` vs `describe_tool` 的分叉已收敛。

## 2. 可维护性：大文件 + 错误契约分裂

- 🟡 **上帝对象**：✅ `harness.py:1-847` 已拆成 `harness/schema.py`(329) + `loader.py`(427) + `cli.py`(140)；❌ `evaluation.extract_record:82-180` 仍约 100 行做 5 件事。
- 🟡 **高复杂度函数**：
  - ✅ `loop._run:86`（约 73 行，原 134 行）— 请求、工具分发、摘要各自成方法 / 协作者。
  - ❌ `context.summarize_history:688`（约 202 行，原 181 行）— 仍偏长。
  - ❌ `context.compact_observations:285`（约 107 行）— 仍靠注释区分 3 pass。
  - 🟡 `chat._consume_stream` — 未见拆分。
- 🟡 **错误三套风格并存**：
  - ✅ `files.edit_file` 与 `patch.apply_patch` 已统一为 raise `ValueError`（`patch.py:53-94`）。
  - ✅ 环境失败统一为 `{"error": {...}}`，非法输入 raise（`execution/base.py:109-124` 已写入协议文档）。
  - ❌ `tool_dispatch.classify_tool_calls:149` 的 `except Exception` 仍无日志。
  - ❌ `context.py` + `loop.py` 的 `except Exception: fallback / False` 兜底仍在（`loop.py:216`、`context.py:833`）。
- 🟡 **重复 / 矛盾**：
  - ✅ `files.py:27-31` 的 `list_dir(parent)` 预检已删，写入统一交给 `Workspace.write_file`。
  - 🟡 `observations._format_shell_result:80` vs `tool_dispatch.tool_result_summary:262` 仍是两份渲染；现在注释承认是"模型面 / 事件面"，但实现仍重复。
  - 🟡 `loop._request` 已统一主循环的 `trace.span("llm") + chat_with_recovery`；`_summary_turn` 因无工具 / 不同兜底语义仍保留一份独立请求路径。
  - ✅ 根 `__init__.py:1-6` 已去重。
- ✅ **数据丢失级 bug（全部修复）**：
  - `lineage.manifest_text:245-262` 已用 `**spec.genes_dict()` 全量写盘；`derive:222-242` 不再丢基因。
  - `harness` 的 `rerun_declared_command` 字段已从 `VerificationGenome` 移除，不再"允许又丢弃"。
  - `evaluation._git_commit:188-196` 已传 `cwd=job_path`。

## 3. 可扩展性：好坏分明

- 🟡 **新增 tool 7/10**：`ToolSpec + handler` + `create_default_registry(enabled)` 仍在（`tools/tools.py:34,446`）。
  - ✅ `EDIT_TOOLS / test_target_pattern` 已收进 `VerificationGenome`（`harness/schema.py:221-226`），`SubmissionRules.from_genome` 注入（`verification.py:67`）。
  - ❌ `tools.py:393 _submit_result_spec()` 仍写死 `"submit_result"`，registry 不会按 `submit_tool` gene 改名注册。
  - ❌ 仍无 `register_one`；`builtin_tool_specs` 每次仍重建 spec 列表（但现在共享 workspace / executor）。
- ❌ **新增校验 6/10**：`verification.check_submission:282-335` 仍是 `if/elif` 串联，没有 `Validator / Rule` 注册表。
- ✅ **新增 context 策略 4/10 → 已解决**：`context.py:392-475` 新增 `ContextStrategyDefinition` + `CONTEXT_STRATEGY_DEFINITIONS` + `register_context_strategy`，`apply_context_strategy:477-509` 改为注册表分发；新增策略只需注册，不再改 `if/else`。
- ➖ **新增 LLM 重试 8/10**：设计仍是最佳，无需改动。
- 🟡 **memory / skills 是亮点**：
  - ✅ `memory.render_memory_block(workspace: Workspace)` 已用 Protocol（`memory.py:191`）。
  - ❌ `MAX_*_CHARS` 仍各自为政。

### `agent/` 子模块细化（当前共约 3545 行）

| 文件 | 行数 | 职责 | 评价 |
|---|---|---|---|
| `__init__.py` | 22 | 包门面 | ✅ 已清理重复导出 |
| `loop.py` | 288 | 主循环：request → 分发 → finish / summary | ✅ 原 837 行；现 5 个方法，策略已抽到 guard / tool_runner / opening |
| `context.py` | 907 | 请求视图裁剪 + 摘要 + 策略注册表 | 🟡 策略已可注册；已去懒导入 / 全局 summarizer，文件仍待拆 |
| `chat.py` | 314 | LLM 通信层：流式组装 + 按 `recovery.llm_errors` 重试 | 重试设计最佳 |
| `tool_dispatch.py` | 291 | 工具调用校验归一化 + observation 构造 | ✅ 私有 API 已转正 |
| `verification.py` | 341 | 回报契约门 + grounding | 🟡 规则仍无注册表；工具名/模式已 gene 化 |
| `observations.py` | 188 | 观察渲染 + 出生即归档 | 职责清晰；shell 渲染与 tool_dispatch 重复 |
| `turn_history.py` | 161 | 转内状态：`Conversation` + `TurnHistory` 投影 | ✅ 已改用 context 公开 API + 显式 summarizer |
| `tool_error_budget.py` | 79 | 同工具同错熔断 | 职责单一 |
| `turn_collaborators.py` | 55 | 一轮 turn 的协作者数据包 | ✅ 新增，显式装配 |
| `turn_factory.py` | 164 | 组合根：解析默认值、绑定 workspace、构造 finisher / guard / runner | ✅ 新增，消除 `getattr(tools,"workspace")` |
| `turn_failure.py` | 16 | 可回传的 turn 失败类型 | ✅ 新增 |
| `turn_finisher.py` | 348 | 提交 / rerun / 契约结果 | ✅ 新增，从 loop 抽出 |
| `turn_guard.py` | 194 | 预算提醒 / 错误熔断 / 上下文窗口刷新 / 拒绝观察 | ✅ 新增，从 loop 抽出 |
| `turn_opening.py` | 50 | system prompt / skills / memory 注入 | ✅ 新增，从 loop 抽出 |
| `tool_runner.py` | 127 | 校验后工具的执行、渲染与 guard 驱动 | ✅ 新增，从 loop 抽出 |

### `tools/` + `execution/` + `providers/` 细化

- ✅ `tools/tools.py:17-39` `ToolExecutor(Protocol)` 现在把 `workspace` 也写进契约；`ToolSpec（frozen dataclass）+ handler` 模式不变。
- ✅ `execution/base.py:31-96` `ShellExecutor / Workspace(Protocol)` + `paginate_lines` + 共享搜索常量；`HarborWorkspace` 的 server-side 搜索保持。
- ✅ `execution/base.py:13-29` `truncate_result(MAX_OUTPUT_CHARS=32000)` source-level 截断保留。
- ❌ `execution/__init__.py:3-6` 仍重导出 `Local + Harbor` 具体类。
- ✅ `providers/llm.py` 的 reasoning echo 策略下沉到中性 `reasoning_echo.py:66`；provider 与 `context` 共用同一判定，provider 内部保留 `_reasoning_echo_mode / _should_echo_reasoning` 历史别名。
- ❌ `Workspace` 契约（环境失败 `{"error"}` / 非法输入 `raise ValueError`）在 `harbor.py` 的路径 containment / 写盘路径上仍未完全一致（见 §4）。

### 根模块 + `channels/` + `integrations/` 细化

| 模块 | 职责 | 评价 |
|---|---|---|
| `events.py:45-64` `AgentEvent + EventEmitter` | 面向用户的展示流 | 清晰 |
| `trace.py:34-58` `RunTrace` | 内部可观测流，JSONL 落盘 | 清晰；`span:91-109` error/end 仍可能 double 计数 |
| `storage.py:1-72` | "写盘去哪"唯一答案 | ✅ 重构为 `runtime_home / sessions / traces`，原 `memory_dir` 撞名消失 |
| `memory.py:191-244` | `workspace/memory/` 只渲染树 | ✅ workspace 已用 Protocol |
| `skills.py:118-148` | `skills/<name>/SKILL.md` 解析 + 注入 | 清晰 |
| `reasoning_echo.py:66-89` | provider / 预算共享的 reasoning echo 策略 | ✅ 新增，消除跨层私有依赖 |
| `lineage.py:245-269` | 验证 / 树 / 派生 | ✅ `manifest_text` 无损 |
| `evaluation.py:82-208` | `jobs/ -> record.json` | 🟡 commit 已修；职责仍混杂、全 `dict[str, Any]` |
| `channels/cli.py`, `webui.py` | 纯渲染适配器 | ✅ 工具摘要已抽 `tool_summary.py` |
| `integrations/harbor.py:39-140` | Harbor `BaseAgent` 薄适配 | 🟡 仍混 harness 解析 / tool 注册 / answer 提取；`:54` 版本仍硬编码 `"0.1.0"` |

## 4. 可读性 + 安全

好的：`events:1-29`、`storage:1-26`、`memory:1-24` 头文档把"为什么这样分"讲清；`harness DEFAULT_*:138-165` 每个常量有行内经济意义注释；错误信息具体（`harness/schema.py`、`loader.py`）。

差的：

- ❌ `Any` 泛滥（`events.emit:95, trace.emit:49, evaluation` 全文件 `dict[str, Any]`）。🟡 `workspace` 的 Protocol 化已完成（`memory` / `tools`）。
- ❌ 兼容别名堆积：`harness/schema.py:115-122 MEMORY_STRATEGIES / CONTEXT_STRATEGIES`、`:153-154 MEMORY_BUDGET_UNITS / CONTEXT_BUDGET_UNITS`、`:204 MemoryGenome` 仍在，且仍无 `DeprecationWarning`。
- ❌ 魔法值散落：`cli.py:18-20 _STDOUT_LINES / _RESULT_LINES / _LINE_WIDTH`、`webui.py` id 格式、`harness/loader.py:182 sorted(fractions)` 等仍无注释。
- ✅ `VerificationGenome` 的 30 行版本演进史 docstring 已删，只留当前语义 + gene 说明（`harness/schema.py:233-250`）。
- ❌ `integrations/harbor.py:74-89 sync_context / record_event`：`_runtime_metadata` 仍写一次 `context.metadata`，闭包又 `dict()` 拷贝再写回，每事件仍 `event.to_dict()` 存 `last_event`，`context.metadata` 仍无界增长；`:54-55` `version()` 仍硬编码 `"0.1.0"`。
- ✅ `tools/code.py:130-198` 新增 Python AST footgun 检查（`subprocess / socket` 模块、`os.system / os.popen / sys.exit` 等调用），不再是 `execute_code(python: ...)` 零拦截。

安全（已诚实标注，但仍有洞）：

- 🟡 `execution/guard.py:1-9,38-43` 仍明确"fool-proofing, not security boundary"，态度正确；实现基本未动：
  - ❌ `guard.py:129-135` `_normalize: lower + 去引号 / 反斜杠`，`${IFS}`、`*` 通配、`base64 -d | sh` 变体仍可 trivially bypass。✅ `python -c` 这一条被 `code.py` 的 AST 检查部分覆盖，但 `run_command` 里的 `python -c` 仍不看 AST。
  - ❌ `_check_privilege:109-113` 仍只看首词 `sudo / su`，`env sudo ... / sudo -n` 漏网。
  - ❌ `guard.py:186` `rm -rf $VAR /`、`rm --recursive` 长选项带 `=`、多 target（`rm -rf a /` 只捕获首 target）仍可绕过；`guard.py:163-174` `workspace_root=None` 时相对路径仍全放行。
- ❌ `execution/local.py:81` 仍 `subprocess.run(..., shell=True)`；`HarborShellExecutor`（`harbor.py:60-80`）仍完全不查 guard（依赖容器隔离）。
- ❌ 路径 containment 不等强：`local.py:122-128` `resolve() + is_relative_to` 跟随 symlink（仍有 TOCTOU）；`harbor.py:122-130` 仍是纯 `posixpath.normpath` 词法 containment，容器内 `ln -s /etc/passwd ws/link; read_file(link)` 仍可穿透。
- ❌ `tools/syntax_gate.py:31-32` `subprocess.run(["bash", "-n"], timeout=5)` 仍同步阻塞，`edit_file / apply_patch` async 路径直接调用；`yaml.safe_load:29` 仍无大小上限；fail-open 仍无日志。
- ❌ `execution/harbor.py:177` `tmp = target + ".writetmp"` 仍非原子 + 固定后缀，并发同文件写仍会竞态丢失更新，无 lock（对比 `code.py:57 _SCRIPT_LOCK` 只保护 auto-naming）。

坏味道补充：

- ✅ 长函数 / 大类：`AgentTurn` 现为 5 个方法（`__init__ / run / _run / _request / _summary_turn`）；rerun 三 helper 在 `turn_finisher.py`，工具执行在 `tool_runner.py`，开场注入在 `turn_opening.py`，策略在 `turn_guard.py`。`loop._run` 已从 134 行降到 73 行。
- 🟡 全局状态：✅ `context.py` 的 `_summary_llm` 全局已删除，改为 `ContextVar` 兼容钩子 + `TurnCollaborators` 显式注入；❌ `chat.py:20,35,89` 每次调用仍读 `os.getenv`。
- 🟡 异常吞掉 / 过宽：❌ `tool_dispatch.py:149` 仍 `except Exception` 无日志；❌ `context.py` + `loop.py:108-112` 的 `except Exception: fallback / False` 仍在。
- 🟡 硬编码：✅ `_FILE_TARGET_RE` 已 gene 化；❌ `turn_guard.py` 的 budget 提醒文案、`context.py` 的 summarizer prompt 仍钉死模块内；`verification.py` 的阈值仍是模块常量（可注入但未 gene 化）。

## 5. 只改三处，收益最大（P0）

1. ✅ **修有损序列化**：`lineage.py:245 manifest_text` 已用 `genes_dict()` 全量 dump；`evaluation._git_commit` 已加 `cwd`。
2. ✅ **统一错误契约 + 断倒依赖**：`patch.py:53,94` 已对齐 `edit_file`（全 raise）；`SKIP_DIRS / matches_path / MAX_FILE_BYTES` 已下沉 `execution/base.py`；`harbor.py` 已删 `tools.search` 依赖；`tool_dispatch` 私有已转正。
3. 🟡 **拆大文件 + 抽共享**：✅ `harness` 已拆 schema / loader / cli；✅ `loop` 的 rerun helper 已移 `turn_finisher.py`，开场 / 策略 / 工具执行已移 `turn_opening / turn_guard / tool_runner`；✅ `cli / webui` 工具摘要已合一；✅ `EDIT_TOOLS / test_target_pattern` 已收进 genome；❌ `submit_result` 工具名仍写死在 registry；❌ `context.py` 未拆。

### 后续最小修复清单

1. ✅ `execution/base.py` 新增 `SKIP_DIRS / MAX_FILE_BYTES` 常量，`search.py` 与 `harbor.py` 同源 import。
2. ✅ `files.py:24-41` 父目录预检删掉，下沉到 `Workspace.write_file`。
3. ❌ `providers/__init__.py:1-5` 仍只导出 `OpenAICompatibleLLM`；`llm.py:389-394` 的 `OpenAILLM` 别名仍未在包级导出，也未删除。
4. 🟡 根 `__init__.py:1-6` 已去重（✅）；`verification.check_submission` 仍无 `list[Rule]` 注册表（❌）。
5. ❌ `symbols.py:68-80` 缓存仍只用 `size` 做 key、无 mtime / content-hash / 淘汰。
6. ➖ `storage.memory_dir` vs `memory.MEMORY_DIR`：`storage` 已重构为 sessions / traces，`memory_dir` 不再存在，撞名问题消失。

### 仍未修复的重点（按优先级）

1. ❌ 安全面：`guard.py` 的绕过路径、`local.py` 的 `shell=True`、`harbor.py` 的词法 containment、`syntax_gate` 的同步阻塞、`_write_chunked` 的非原子写。
2. 🟡 `context.py`：懒导入与模块全局 `_summary_llm` 已消除（907 行仍在，文件拆分与 `summarize_history` 202 行待续）。
3. ❌ registry / 规则可扩展性：`submit_result` 写死、`check_submission` 无规则注册表、`tools` 无 `register_one`。
4. ❌ 小项：`providers` 导出、`symbols` 缓存、`trace.span` double 计数、`_summary_turn` 仍有一份独立 LLM span、兼容别名无警告、`integrations/harbor.py` 版本与 metadata 增长。
