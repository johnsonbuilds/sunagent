# `src/agent_runtime` Review：可维护性 / 可扩展性 / 耦合 / 可读性

> 日期：2026-10-30
> 范围：`src/agent_runtime/` 全量（`agent/`、`tools/`、`execution/`、`providers/`、根模块、`channels/`、`integrations/harbor.py`）
> 方法：静态审查 + `file:line` 证据抽查

总览：**骨架是对的，局部已腐烂**。`Protocol + ToolSpec + 双流事件 + 薄 channels` 方向正确，但 `harness.py / agent/loop.py / context.py / tools.py` 四个大文件集中了约 60% 风险：上帝对象、私有跨模块调用、错误契约三套并存、安全 guard 仅防君子。

| 维度 | 评分（10 分制） | 一句话 |
|---|---|---|
| 可维护性 | 5 | `harness 847 行 + loop 837 行 + context 776 行`，改一处动四层 |
| 可扩展性 | 6 | 新增 tool / LLM 重试易，新增 context 策略 / 校验规则难 |
| 耦合性 | 5 | 无顶层循环依赖，但靠懒导入 + `_私有函数`跨模块维持，脆弱 |
| 可读性 | 7 | 模块头文档 + 命名好，`Any / dict[str, Any]` + 魔法数 + 兼容别名拖后腿 |

## 1. 耦合：最严重的问题

1. **`AgentTurn` 成 God Object**：`agent/loop.py:16-62` 一次 import 11 个模块，`__init__:156-190` 还 `getattr(tools, "workspace")` 取 workspace 再造 `ObservationFormatter`，tools ↔ observations 隐式约定。
2. **私有函数公开用，封装破了**：
   - `loop.py:28-30` 用 `tool_dispatch._rejected_*` 等 8 个下划线函数；
   - `turn_history.py:30-35` 用 `context._extract_summary / _reasoning_replayed`；
   - `chat.py:11` 用 `tool_dispatch._tool_trace_metadata`；
   - `execution/harbor.py:221` 反向依赖 `tools.search._matches_path / SKIP_DIRS`。底层依赖上层，重构必破，应下沉到 `execution/base.py` 或转正为公开 API。
3. **循环依赖靠懒导入续命**：`context.py:105,540` 函数内 `from providers.llm import`，`harness.py:699` 函数内 `from evaluation, lineage import`。能跑但预算口径 / 依赖关系分裂两处。
4. **Channels 是模范**：`channels/cli.py:12, webui.py:33` 只依赖 `events.AgentEvent`，不碰 trace / harness / tools。但两通道 `cli._format_arguments:200-233` vs `webui.describe_tool:238-271` 几乎复制，已分叉，应抽 `tool_summary.py`。

## 2. 可维护性：大文件 + 错误契约分裂

- **上帝对象**：`harness.py:1-847` 同时做 7 个 Genome dataclass（`177-297`）+ 校验（`396-613`）+ 加载（`644-692`）+ 5 子命令 CLI（`695-830`）。`evaluation.extract_record:82-180` 约 100 行做 5 件事。建议拆 `harness/schema.py + loader.py + cli.py`。
- **高复杂度函数**：
  - `loop._run:252（约 133 行）`
  - `context.summarize_history:581（约 181 行）`
  - `context.compact_observations:264（约 104 行，3 pass 靠注释区分）`
  - `chat._consume_stream:120（约 102 行）`
  - 加分支只能加 `if`。
- **错误三套风格并存**：`files.edit_file:90-104` return dict，`patch.apply_patch:73-97` raise `ValueError`，`search.grep_search:122` fast-path 吞成 dict。loop 必须同时处理两种。`tool_dispatch.py:147` `except Exception` 转 rejection 无日志；`context.py:702 + loop.py:208` `except Exception: fallback / False` 掩盖 summarizer 根因。
- **重复 / 矛盾**：
  - `files.py:24-41` `list_dir(parent)` 预检 vs `local.py:153 / mkdir(parents=True)` + `harbor.py:152 / mkdir -p` 自动建，Harbor 下多一次 RTT 且语义打架；
  - `observations._format_shell_result:81` vs `tool_dispatch._tool_result_summary:262` 同一 shell 两种渲染；
  - `loop.py:544` vs `285` `trace.span("llm") + chat_with_recovery` 近复制；
  - `__init__.py:5,7` 重复 import。
- **数据丢失级 bug**：
  - `lineage.manifest_text:255-258` 只写 control 2 字段 + context 1 字段，`derive:222-242` 写盘即丢基因，下一代静默回默认。
  - `harness._verification_genome:586` 允许 `rerun_declared_command` 又丢弃，静默漂移。
  - `evaluation._git_commit:188` 未传 `cwd=job_path`，取错 commit。

## 3. 可扩展性：好坏分明

- **新增 tool 7/10，易**：`tools/tools.py:34 ToolSpec + handler` + `440 create_default_registry(enabled)` 白名单思想好。但 `loop.py:69 SUBMIT_TOOL` 写死、`verification.py:34 EDIT_TOOLS` 写死三种编辑工具、`builtin_tool_specs:417` 每次新建 executor 不共享、无 `register_one`，加写类工具要同步改三处。
- **新增校验 6/10，中**：`verification.check_submission:236` 返回 `{field:reason}` 结构好，但无 `Validator` 注册表，加规则只能续 `elif`；`loop._validate_submission:647` 把纯校验和“执行一次 run_command”绑死。
- **新增 context 策略 4/10，难**：`context.apply_context_strategy:397` 字符串 `if / else raise` 分发，无注册表；加 knob 要动 genome + 分发 + `turn_history:134` anchor 三层。
- **新增 LLM 重试 8/10，最佳**：`chat.chat_with_recovery:252` 按 `harness.recovery.llm_errors` 字典驱动，加类别只需 provider 抛带 `llm_category` 异常即可。
- **memory / skills 是亮点**：`memory.render_memory_block:183` 纯渲染 + fail-open，不读内容只 hash `path:size`；`skills:118` 解析注入清晰。只是 `workspace: Any` 该换 `Protocol{root: Path}`，`MAX_*_CHARS` 各自为政。

### `agent/` 子模块细化（共 2936 行）

| 文件 | 行数 | 职责 | 评价 |
|---|---|---|---|
| `__init__.py` | 11 | 包门面 | 有重复导出，需清理 |
| `loop.py` | 837 | 主循环：`AgentTurn` 编排 LLM → 校验 → 执行 → 校验提交 → summary；另含 rerun 判定 3 helper | 风险集中区，God Object |
| `context.py` | 776 | 请求视图裁剪：`compact_observations` 确定性折叠 + `summarize_history` LLM 摘要 + `apply_context_strategy` 适配器 | 风险集中区，待拆分 |
| `chat.py` | 314 | LLM 通信层：流式组装 `_consume_stream` + 按 `recovery.llm_errors` 重试 `chat_with_recovery` | 重试设计最佳 |
| `tool_dispatch.py` | 283 | 工具调用校验归一化：`validate / classify / canonical` + observation 消息构造 | 相对最健康 |
| `verification.py` | 292 | 回报契约门：`check_submission / contract_nudge / render_submission` + grounding | 需规则注册表 |
| `observations.py` | 189 | 观察渲染 + 出生即归档：`format_tool_result / OutputSpill / ObservationFormatter` | 职责清晰 |
| `turn_history.py` | 155 | 转内状态：`Conversation` 全量 + `TurnHistory{view, boundary, usage_anchor}` 投影 | 投影语义隐晦但正确 |
| `tool_error_budget.py` | 79 | 同工具同错熔断：`SameToolErrorBudget + error_signature / resolve_max_retries` | 职责单一 |

### `tools/` + `execution/` + `providers/` 细化

- `tools/tools.py:34-39` `ToolSpec（frozen dataclass）+ handler` 模式清晰；`22-31` `ToolExecutor(Protocol)` 使 agent loop 只依赖协议，可 mock / 替换。
- `execution/base.py:31-86` `ShellExecutor / Workspace(Protocol)` + `paginate_lines` 中心化分页语义好；`HarborWorkspace:210-416` server-side `rg / grep / find` 单 exec，`search.py:114-125,189-198` `getattr(ws, "search_contents")` 优雅降级，开闭原则成立。
- `execution/base.py:15-28` `truncate_result(MAX_OUTPUT_CHARS=32000)` source-level 截断防 OOM。
- 问题：`execution/__init__.py:3-6` 重导出 `Local + Harbor` 具体类，后续加 `DockerWorkspace` 要改散点，不如只暴露 `base + factory`；`Workspace` 契约中“环境失败返回 `{"error": ...}`，非法输入 `raise ValueError`”（`base.py:44-49`）在各 tool 落实不一致。

### 根模块 + `channels/` + `integrations/` 细化

| 模块 | 职责 | 评价 |
|---|---|---|
| `events.py:45-64` `AgentEvent + EventEmitter` | 面向用户的展示流 | 清晰，文档头 `1-29` 把 `trace vs events` 双流讲透 |
| `trace.py:34-58` `RunTrace` | 内部可观测流，JSONL 落盘 | 清晰；`span:88-109` error/end 可能 double 计数 |
| `storage.py:42-72` | “写盘去哪”唯一答案 | 清晰；但 `memory_dir` 与 `memory.MEMORY_DIR` 命名撞车 |
| `memory.py:183-217` | `workspace/memory/` 只渲染树，不读内容 | 单一、可扩展性好 |
| `skills.py:118-148` | `skills/<name>/SKILL.md` 解析 + 注入 | 清晰；6 处函数内导入 `HarnessError` 本可顶层导入 |
| `lineage.py:33-242` | 验证 / 树 / 派生 | 清晰；`manifest_text` 有损是 P0 |
| `evaluation.py:82-208` | `jobs/ -> record.json` | 职责混杂，全 `dict[str, Any]` 无类型 |
| `channels/cli.py`, `webui.py` | 纯渲染适配器 | 解耦好，通道间重复待抽取 |
| `integrations/harbor.py:39-116` | Harbor `BaseAgent` 薄适配 | 基本薄，但 `run:60-116` 混 harness 解析 / tool 注册 / answer 提取 |

## 4. 可读性 + 安全

好的：`events:1-29`、`storage:1-26`、`memory:1-24` 头文档把“为什么这样分”讲清；`harness DEFAULT_*:138-165` 每个常量有行内经济意义注释；错误信息具体（`harness:428,432,490`）。

差的：

- `Any` 泛滥（`events.emit:95, trace.emit:49, memory:183, evaluation` 全文件 `dict[str, Any]`）。`workspace` 应定义 `Protocol { root: Path }`。
- 兼容别名堆积：`harness.py:124-131 MEMORY_STRATEGIES / CONTEXT_STRATEGIES`、`162-163 MEMORY_BUDGET_UNITS / CONTEXT_BUDGET_UNITS`、`221 MemoryGenome`、`302-304 HarnessSpec.memory property`、`523-525 _memory_genome` 透传 wrapper。建议集中一处 `deprecated_aliases` 并打 `DeprecationWarning`。
- 魔法值散落：`cli.py:18-20 _STDOUT_LINES / _RESULT_LINES / _LINE_WIDTH`、`webui.py:216-221` id 格式、`harness.py:466 sorted(fractions)` 隐式排序无注释。
- `VerificationGenome:236-265` 30 行 docstring 讲 code-v10～v13 演进史，属于 git log 内容，会随版本腐烂；应只留当前语义 + 指到 ADR。
- `harbor.py:74-82 sync_context / record_event`：`_runtime_metadata:119-133` 已写一次 `context.metadata`，闭包又包一层 `dict()` 拷贝再写回，每事件一次 `event.to_dict()` 存 `last_event`（`81`），`context.metadata` 无界增长。`version:54-55` 硬编码 `"0.1.0"` 与包版本脱节。

安全（已诚实标注，但仍有洞）：

- `execution/guard.py:1-9,38-43` 明确“fool-proofing, not security boundary”，态度正确。但实现是纯文本正则：
  - `guard.py:129-135` `_normalize: lower + 去引号 / 反斜杠`，`${IFS}`、`*` 通配、`base64 -d | sh` 变体、`python -c` 可 trivially bypass。`_check_privilege:109-113` 只看首词 `sudo / su`，`env sudo ... / sudo -n` 漏网。
  - `guard.py:186` `rm -rf $VAR /`、`rm --recursive` 长选项带 `=`、多 target（`rm -rf a /` 只捕获首 target `target:\S+`）均可绕过；`guard.py:163-174` `workspace_root=None` 时相对路径全放行。
- `execution/local.py:75-82` `subprocess.run(..., shell=True)` + 无 guard 的 `HarborShellExecutor:52-65` 根本不查 guard（guard 只在 `LocalShellExecutor.execute:54`）。`tools/code.py:167-197` `_guard_decision` 对 `bash` 逐行 `guard.check`、对 `python / r / node` 直接放行，意味着 `execute_code(python: open("/etc/shadow").read())` 零拦截。
- 路径 containment 不等强：`local.py:122-128` `resolve() + is_relative_to` 跟随 symlink，能防 symlink 逃逸（但有 TOCTOU）；`harbor.py:112-120` 纯 `posixpath.normpath` 词法 containment，容器内 `ln -s /etc/passwd ws/link; read_file(link)` 即穿透。
- `tools/syntax_gate.py:31-32` `subprocess.run(["bash", "-n"], timeout=5)` 同步阻塞，`edit_file / apply_patch` async 路径直接调用，大并发下堵 event loop；`yaml.safe_load:29` 无大小上限；fail-open（`36` return None）无日志，静默放行。
- `execution/harbor.py:147-186` `_write_chunked:169` `tmp = target + ".writetmp"` 非原子 + 固定后缀，并发同文件写竞态丢失更新，无 lock（对比 `code.py:57 _SCRIPT_LOCK` 只有 auto-naming 有锁）。

坏味道补充：

- 长函数 / 大类：`loop.py:149 AgentTurn` 13 个方法 + `run / _run / _summary_turn / _dispatch_tools / _validate_submission / _rerun_declared_command`；`loop.py:91-142` rerun 三 helper 实为“验证执行子域”，应独立文件。
- 全局状态：`context.py:478 _summary_llm` + `481 set_summary_llm` 模块全局可变；`chat.py:19,28` 每次调用读 `os.getenv`，行为不可从签名看出，难单测。
- 异常吞掉 / 过宽：`tool_dispatch.py:147-150 classify_tool_calls: except Exception`；`context.py:702-724` + `loop.py:208-218` `except Exception: return fallback / False`。
- 硬编码：`loop.py:71,76 _RERUN_OUTPUT_CHARS / _RERUN_TIMEOUT`、`loop.py:601-606` budget 提醒文案、`context.py:44-91` summarizer prompt 钉死模块内、`verification.py:20-25 MIN_FIELD_CHARS / QUOTE_CHARS / TOKEN_OVERLAP`、`207 _FILE_TARGET_RE` 只认 `py|sh`。

## 5. 只改三处，收益最大（P0）

1. **修有损序列化**：`lineage.py:245 manifest_text` 用 `genes_dict()` 全量 dump + `evaluation._git_commit` 加 `cwd`，否则派生链静默退化。
2. **统一错误契约 + 断倒依赖**：`patch.py:73,94` 对齐 `edit_file`（全 return 或全抛二选一）；`SKIP_DIRS / _matches_path` 下沉 `execution/base.py`，删 `harbor -> tools.search`；`tool_dispatch` 私有转正，`loop / chat / turn_history` 不再跨模块调下划线。
3. **拆大文件 + 抽共享**：`harness` 拆 schema / loader / cli，`loop:91` rerun helper 移 `verification_rerun.py`，`cli / webui` 工具摘要合一，`EDIT_TOOLS / _FILE_TARGET_RE / SUBMIT_TOOL` 收进 genome 消灭硬编码。

### 后续最小修复清单

1. `execution/base.py` 新增 `SKIP_DIRS / MAX_FILE_BYTES` 常量，`search.py` 与 `harbor.py` 同源 import。
2. `files.py:24-41` 父目录预检删掉或下沉到 `Workspace.write_file` 原子实现。
3. `providers/__init__.py:1-5` 补 `OpenAILLM` 导出（`llm.py:391,394 __all__` 有别名但包 init 只导出 `OpenAICompatibleLLM`），或删别名。
4. `__init__.py:5-7` 去重导入；`verification.check_submission` 拆成 `list[Rule]` 注册表。
5. `symbols.py:59-82` 缓存只用 `size` 做 key、无失效 / 淘汰，大仓重复 parse 开销大，需补 mtime / content-hash。
6. `storage.memory_dir` vs `memory.MEMORY_DIR` 改名其一（如 `summary_dir` / `persistent_memory_dir`）。
