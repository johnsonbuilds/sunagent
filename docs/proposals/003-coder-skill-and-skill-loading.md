# Proposal 003: Skill 加载机制 + coder skill（工具使用策略）

- **Status:** draft
- **Date:** 2026-09-10
- **Parent harness:** `code-v8`（`code-v7` + `grep_search,glob_files`）
- **Trigger:** `eval100-code-v7/v7-2` 分析：`run_command 75% + read_file 20%`，`edit_file/apply_patch` 合计仅 ~4%，`write_file 0`；`apply_patch` 失败模式稳定（`unified-diff path / unterminated block / unexpected arg`），靠改 tool description 无改善（`41% -> 44%`，噪声范围内）。

## 1. 目标

1. 把 `skills` 从 dormant gene（一直是 `[]`，`harness.py:196` 只做名字校验）变成可加载机制：harness 声明 `skills: [coder]`，运行时解析为文本并注入上下文。
2. 首个 skill 为 `coder`：针对当前工具集（`run_command/read_file/edit_file/apply_patch/write_file/grep_search/glob_files/read_output`）明确最佳 tool 使用策略，解决“模型知道有专用工具但仍用 `run_command` 仿真”的问题。
3. 可 A/B：`code-v9 = code-v8 + skills+=coder`，只变一个基因，重跑 `eval100` 对比。

## 2. 与 002 的分歧说明

002-D 主张“tool-selection 是 universal behavior，应进 `prompt.system` 而非 `skills`”。本方案不推翻该判断，而是基于成本做切分：

- `prompt.system`：放一句话宪法（`prefer dedicated tools over shell emulation`），常驻、极短。
- `skills/coder`：放可操作细则（什么时候 `grep` vs `rg`、什么情况 `edit_file` vs `apply_patch`、失败后怎么改），只在 coder 类任务加载、可版本化、可下线。

理由：细则太长不适合进常驻 prompt（每轮都烧 token）；skill 可按任务类型开关，`code-*` 与 `meta-*` 可复用不同 skill。

## 3. Skill 加载机制（最小实现）

- 目录：`skills/<name>/SKILL.md`（首个为 `skills/coder/SKILL.md`），纯 markdown，前置 `name/version` 头。
- 解析：`resolve_harness` 后加 `resolve_skills(spec) -> list[Skill]`；名字不在目录中则 `HarnessError`（与 `tools.enabled` 未知工具同级处理，见 `tools.py:370-371`）。
- 注入：拼接到 `prompt.system` 之后、任务指令之前，作为独立 `<skill name=coder>` 块；`trace` 的 `harness` 记录中追加 `skills_content_hash`（当前 `genes_hash` 只 hash 了 skill 名字列表，`harness.py:204-214`，内容变名字不变会导致溯源断裂，必须补）。
- 预算：单 skill 截断上限（如 4000 chars），超限截断并在 trace 打标；`skills: []` 时行为与现在完全一致（零回归路径）。

## 4. `coder` skill 内容草案（v1，只写策略，不写知识）

**Locate（先找再读）：**
- 找文件用 `glob_files`，不用 `ls/find` via `run_command`；`'*' 跨目录、bare name 全树匹配`，一次顶多次 `list_dir`。
- 找内容用 `grep_search`，不用 `grep/rg/cat|head` via `run_command`；返回 `(path,line,preview)`，`line` 直接喂 `read_file(offset=...)`；`truncated=true` 时收窄 pattern 或加 `include: '*.py'`，不要翻页 `rg`。
- `run_command` 只留给：跑测试/复现脚本、真正的 shell 语义（管道/git/build）。

**Read（分页读）：**
- `read_file` 长文件按 `truncated` 指示翻页；`.outputs/` 下的大输出用 `read_output` 翻页，**禁止重跑 tool 只为再看输出**。

**Edit（三选一，不混用）：**
- `edit_file`：默认选项。单点替换，`old_str` 必须唯一；`match_mode` 回报仅供确认；含 conflict-marker 文本也走它。
- `apply_patch`：仅当“多处替换必须一次原子提交（all-or-nothing）”时用；单 `patch` string 参数，`bare path + <<<<<<< SEARCH / ======= / >>>>>>> REPLACE` 独占一行；禁止 unified-diff（`--- a/...`）、禁止 markdown fence、禁止分多参数传；`unterminated` 报错时重发**完整** patch，不要只补 marker。
- `write_file`：仅新建文件或整文件重写；swe-bench 类单点修 bug 默认不用。

**Verify：**
- 改完即用 `run_command` 跑最小复现/相关单测；失败先读报错定位，再回 Locate，避免同参重跑（现有 rule 3 保持）。

## 5.  rollout

- `code-v9 ← code-v8`，mutation `skills+=coder`，其余基因不动；`MODEL_ID` 不动。
- 对比口径沿用 v7/v7-2：`pass` 翻转表 + McNemar、`run_command` 占比是否从 ~75% 下降、`grep/glob` uptake、iteration/token 成本；`±5%` 内判为无效。
- 若有效，再做强 LLM 对比（harness 固定在胜者上，只换 `MODEL_ID`，看 `pass/$`）。

## 6. 风险

- skill 文本每轮进上下文，token 开销常驻；v1 必须短（目标 <150 行），写成长篇规范必被模型忽略。
- 模型不服从天花板（002 已有结论：`agnes-2.5-flash` 级别的 compliance 问题 harness 修不完），skill 只抬下限。
- 内容与 tool description 重复时的优先级：以 skill 为准，tool description 保持现状不再精简（v7-2 教训：删 JSON 示例零收益）。
