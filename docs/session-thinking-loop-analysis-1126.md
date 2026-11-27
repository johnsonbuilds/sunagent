# 会话思维循环分析：sess-20261126-024212-b723c8

- Session ID: `sess-20261126-024212-b723c8`
- Title: 你看看 docs/architecture-review-1125.md里面| **P0** |…
- Model: `deepseek-v4.1-flash-1017`
- Harness: `coding-v2`
- Origin: webui
- 分析对象: `workspace/sessions/sess-20261126-024212-b723c8.json`（最后一次运行对应 trace 为 `workspace/traces/run-6100fb31cb26.jsonl`）

---

## 结论

**确定是陷入了思维循环，而不是信息不够。**

---

## 一、证据：信息早就足够，从第 11 轮后已完全进入空转循环

最后一条用户消息（`msg[769]`）只有 3 件事：① 给 `docs/architecture-review-1125.md` 加 P0 Run 生命周期不做注释；② 回答 Q1（`RuntimeConfig` vs `Harness` 边界、`max_reasoning_chars`、`resolve_max_retries` 及其他 retry 项）；③ 回答 Q2（`open_turn` / `create_agent_turn` / `TurnCollaborators` 透传 `runtime`）。

- **前 11 轮（`it=1..11`）是合理的查证**：开头因上下文超预算触发了一次 `llm_summary` 压缩（770 条历史压成 10 条），模型重新读了 `runtime_config.py`、`harness/schema.py`、`tool_error_budget.py`、`turn_collaborators.py`、`loop.py`、`turn_opening.py`、`turn_factory.py`、`llm_stream.py`、`llm_request.py`、`turn_guard.py`、`memory.py`、`skills.py`、`summary.py`、`harbor.py`、`docs/architecture-review-1125.md`。**到 `it=11` 为止，回答全部问题所需的事实已 100% 齐备**。
- **后 8 轮（`it=12..19`，直到被手动 cancel）是典型的原地打转**：

| 阶段 | 轮数 | Reasoning 字符 | 占比 | LLM 耗时 | Completion Tokens |
|---|---:|---:|---:|---:|---:|
| `it=1..11`（正常取证） | 11 | 19,607 | 22.5% | 56.9s | 6,874 |
| `it=12..19`（空转循环） | 8 | **67,582** | **77.5%** | **158.0s** | **17,960** |

循环特征极其明显：

1. **连续 10+ 次宣称"已经够了"，却次次在最后追加一个边缘 grep**：`it=6, 9, 10, 11, 12, 13, 15, 16, 17, 18, 19` 开头全是 `"Now I have enough / a good picture / understand..."`，但结尾总冒出 `"Let me check one more..."`，整轮 `edited_files: []`（从 `it=2` 就说要改 docs，直到 `it=19` 被取消一次都没改）。
2. **反复查同一批边缘信息**：`skills_dir`/`ENV_SKILLS_DIR` 在 `it=5, 7, 12, 18` 查了 4 遍；`stream=` / `webui/turn_context.py` 在 `it=13, 17, 18` 查了 3 遍；`resolve_max_retries` 在 `it=5, 13` 查了 2 遍；`schema.py` 读了 6 遍。
3. **结论在轮次间来回摇摆、每轮从零重写万字分析**：对 `max_reasoning_chars` 的建议在 `it=6` 写"belongs in harness `control`" → `it=12` 翻成"keep it in `RuntimeConfig`" → `it=18` 又翻回"belongs in `control`"。每一轮（`it=12` 16.2k、`it=13` 11.0k、`it=14` 8.7k、`it=15` 7.2k、`it=16` 10.2k、`it=17` 5.8k、`it=18` 8.5k）都在从头重推整套 Q1+Q2 提纲。

---

## 二、主要原因分析

### 1. 根本原因（架构级）：OpenRouter 路由下 `reasoning_content` 被剥离，导致 DeepSeek 思考模型每轮"跨工具失忆"

DeepSeek 思考模型是按 **interleaved thinking（同一轮多步 tool-call 之间回传 `reasoning_content`）** 训练的，因此它在调用工具时**习惯把 `assistant.content` 留空（本轮 18/18 次全为 `content=""`），把全部工作记忆、已得结论和行动计划写在 `reasoning_content` 里**。

但这次运行走的是 `model.json` 里的 OpenRouter 端点（`https://openrouter.ai/api/v1` + `deepseek/deepseek-v4.1-flash-1017`），触发了以下链路：

- `src/agent_runtime/reasoning_echo.py:40, 52-63` 将 `openrouter.ai` 硬编码进 `_GATEWAY_HOST_MARKERS`，`should_echo_reasoning()` 返回 **`False`**（该硬编码已在 §三 的修复中删除：实测网关并非不接收回放，详见 §三 的复核表）；
- `src/agent_runtime/providers/llm.py:290-296` 的 `_project_messages_for_wire()` 在 `echo=False` 时执行 `out.pop("reasoning_content", None)`；
- **Trace 的 `prompt_tokens` 直接实证了模型根本没看到上一轮的思考**：
  - `it=12` 生成了 **4,242 completion tokens**（16,183 字符思考），但 `it=13` 的 `prompt_tokens` 只增加了 `34,617 - 33,751 = 866`（刚好等于 2,510 字符的 tool output + tool_call 参数）；
  - `it=17` 生成了 **1,548 completion tokens**（5,832 字符思考），`it=18` 的 `prompt_tokens` 只增加了 `47,515 - 46,719 = 796`。

于是模型每轮醒来看到的上下文都是：**一长串空白的 `assistant(content="", tool_calls=[...]) -> tool(...)`**。它完全不知道自己上一轮已经写了 16,000 字分析、已经得出结论、已经决定改 docs；每轮只能**从零重读所有工具输出 → 从零重推整套 Q1/Q2 答案（耗掉 6k~16k 字符思考）→ 推到末尾顺手想到一个细枝末节 → 发一条 grep → 下一轮思考再次被清空 → 无限循环**。

**同仓 trace 对照实验验证（`workspace/traces/` 最近 30 次运行）**：

- `deepseek-v4.1-flash-1017` @ **OpenRouter（`echo=False`）**：高频复现完全相同的循环——本 session 的 Turn 1（`run-40da7f273c84`）60 轮 / 40 万字符思考 / **31 次** `"Now I have/good picture"`；Turn 4（`run-561558aff0a0`）91 轮 / 46.7 万字符 / **34 次**；另一次会话 `run-c6bc9693c28b` 直接打满 **101 轮上限** / 39.2 万字符 / **21 次**；本次 `run-6100fb31cb26` 19 轮被中断 / **10 次**。
- `main`（`deepseek-flash`）@ **`api.deepseek.com`（`echo=True`）**：最近 7 次运行（7~69 轮），**6/7 次出现 0 次**同类循环，唯一一次 69 轮长任务也仅出现 3 次，单轮 `max_rc` 显著更小。（注：原生直连的 `echo=True` 是**必须**的——带 `tools` 的请求不回传 `reasoning_content` 会被直接 400；这条对照只能说明"回放生效的端点循环更少"，不能反推网关不接收回放。）

### 2. 放大器（Prompt & 执行习惯层）

- **`harnesses/coding-v2.yaml` 原则 3（Do not speculate about unread code）**：强约束"未读代码绝不猜测"。在"失忆"叠加下，模型每轮从零起草回答时，只要脑中顺带提到一个文件/参数（如 `webui/turn_context.py` 是否传了 `skills_dir`、`run.sh` 有没有 `retry`），就会触发"先 grep 确认一下再答"，成为每次重启循环的导火索。
- **状态未外化**：模型既没有把 docs 修改先落地（把任务状态推进到外部文件），也没有在 `assistant.content` 里留 1~2 行阶段性结论，导致所有"已完成分析"只存在于下一轮就会被抹掉的 `reasoning_content` 中。

---

## 三、Top 1 改进建议

**Top 1：解除 `reasoning_echo.py` 对 OpenRouter 的一刀切禁用，让思考模型家族在网关上照常回传 `reasoning_content`。**

**为什么这是 Top 1（而不是只改 prompt）**：

只改 prompt（比如加"别过度查证"）治标不治本——只要 `echo=False` 持续抹掉模型 100% 的工作记忆，DeepSeek 思考模型每次 tool call 后仍会失忆并从零重推，只是从循环 30 轮变成循环 10 轮。把跨工具调用的思考链续上，才能直接消除"每轮从零重推 → 顺手再查一个细节"的根因（对 OpenRouter 上所有思考模型如 DeepSeek / MiMo / GLM 同样生效）。

**实测复核（2026-11-27，脚本直连两端点）**：

原建议里"OpenRouter 只用 `reasoning_details` 才行"的判断是错的，触发条件是**请求必须带 `tools`**。用同一形状（`assistant(reasoning + tool_calls) → tool`）只改一个变量：

| 端点 | `tools` | 回放字段 | prompt_tokens | 模型能否读到暗号 |
|---|---|---|---|---|
| OpenRouter | 带 | 无 | 373（基线） | 否 |
| OpenRouter | 带 | `reasoning_content` | **2177（+1804）** | **是** |
| OpenRouter | 带 | `reasoning_details` | 2177（+1804） | 是 |
| OpenRouter | 无 | `reasoning_content` | 116（**+0**） | 否 |
| DeepSeek 原生 | 带 | 无 | **HTTP 400**：`The reasoning_content in the thinking mode must be passed back to the API.` | — |
| DeepSeek 原生 | 带 | `reasoning_content` | 1495（+1131） | 是 |
| DeepSeek 原生 | 带 | `reasoning_details` | **HTTP 400** | — |

三条结论：

1. 与 DeepSeek 文档一致："请求带 `tools` 参数时，历史各轮的 `reasoning_content` 应回传并会被拼接进上下文；不带 `tools` 时即使回传也会被忽略。" agent loop 每个迭代都带 `tools`，所以回放在 loop 内有效。
2. 原 `reasoning_echo.py` 里"6400 字符回放涨 0 token"的测量，是在**不带 `tools`** 的形状下做的，因此得出了"网关丢弃 reasoning"的错误结论，进而把 `openrouter.ai` 硬编码进了 `_GATEWAY_HOST_MARKERS`。
3. `reasoning_details` 不是必需通道：OpenRouter 上它与 `reasoning_content` 等价；而 DeepSeek 原生端点**只认** `reasoning_content`（回 `reasoning_details` 直接 400）。因此修复应统一走 `reasoning_content`，而不是改成只回 `reasoning_details`。

**已落地的改法**：

1. `src/agent_runtime/reasoning_echo.py`：删除 `_GATEWAY_HOST_MARKERS` / `_endpoint_replays_reasoning()`，`auto` 只按思考模型家族（`deepseek/glm/mimo/kimi/...`）判定；`AGENT_RUNTIME_REASONING_ECHO=always|never` 仍可强制覆盖。`providers/llm.py` 与 context 预算层共用同一判定，因此预算记账同步变准（reasoning 真的上 wire 时才计费）。
2. `tests/providers/test_llm.py`：把两条"网关剥离"用例改为"网关上思考模型回放 / 非思考模型仍剥离"，并断言强制模式仍优先。
3. 全量测试通过（986 passed）。

**仍存在的风险（本次未改）**：

- 回放只是让模型记得自己的结论，不会自动让它停止探索；§二.2 的两个放大器（`coding-v2` 原则 3 触发的"再确认一次"、任务状态未外化到文件）仍在。预期循环显著变短，而不是必然归零。
- `echo=True` 后长思考任务更容易触发 `llm_summary` 压缩，属于记账变准确的副作用。
- 兜底策略（`TurnOpening` 的轻量工作记忆提示、`TurnGuard` 的收敛 nudge）暂未实现；只有在后续实测仍出现"大段 reasoning + 0 文件修改"的空转时才值得加。
