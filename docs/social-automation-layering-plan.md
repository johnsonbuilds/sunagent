# 社交平台自动化：分层实施方案（tool / skill / memory）

状态：方案待评审（2026-11-25）
目标场景：X（Twitter）与 LinkedIn 上的**找客户 → 读内容 → 互动（关注/点赞/评论/回复）→ 发帖**，
以及后续可能搬到 VPS 运行。

---

## 0. 结论摘要

三层分工，不三选一：

| 层 | 承载什么 | 为什么放这层 | 变更频率 |
|---|---|---|---|
| **tool**（Python） | CDP 原语、Chrome 生命周期、登录态、限速、审计日志、数据落盘 | 稳定、需要唯一实现、需要被测试保证 | 极低（CDP 协议级） |
| **skill**（`skills/<name>/SKILL.md`） | 站点选择器、发帖/存草稿/评论路径、验证方法、风控节奏、话术模板 | 易变，改一行 markdown 即可，不必改代码/重启/跑测试 | 高（站点改版） |
| **memory**（`workspace/memory/*.md`） | 账号 handle、用户偏好、一次性结论（"微信不可行"） | 长期有效、体量小 | 低 |

一句话判据：**"X 改版了要不要改代码？"** 要 → tool；不要 → skill。
**"这条结论半年后还成立吗？"** 成立 → memory；不成立 → skill。

第二个关键决定：**`browser` 的契约是 `steps=[...]` 批量，不是"一个 action 一次 LLM 往返"**。
实测发一条 GM 单 action 要 ~9 次往返，批量只要 1 次；"找 20 个客户并评论"从 ~100+ 次降到 ~5 次。
详见 §3.1。

现状问题（本次要解决的）：所有知识都在 `workspace/memory/browser_automation.md`，
而操作逻辑散落在 `.scripts/cdp.js` + 十几个一次性 `x_*.js` / `li_*.js` 里。
后果有两个：① 每次任务重写一遍脚本；② memory 里的选择器一旦过期，我**看不出它是错的**，
只会照着错的做。

---

## 1. 现状盘点（代码事实，非推测）

### 1.1 skill 机制已存在，但一个都没启用

- `src/agent_runtime/skills.py`：`resolve_skills(names, skills_dir)` 把名字解析成
  `skills/<name>/SKILL.md`；`_parse_skill_file` 读 front-matter 的 `name` / `version`；
  `MAX_SKILL_CHARS = 4000`，**超出直接抛 `HarnessError`（不是截断）**，报错信息本身
  就建议把细节挪到 `references/` 再引用。
- `render_skills_block()` 把正文包进 `<domain_playbooks>`，并声明优先级：
  harness policy > playbook；后出现的 playbook 覆盖先出现的。
- `src/agent_runtime/agent/turn_opening.py:41` 在**每轮首个请求**注入，并 emit
  `skills.loaded` 事件（含 `skills_trace_meta` 的 content hash）——所以"这次跑用的是哪版
  playbook"在 trace 里可查。
- `Skill.directory` 暴露 skill 目录，渲染成 `<playbook directory="...">` 属性，
  playbook 里可以引用同目录的附属文件（模型用 `read_file` 去读）——这是把长表格外置、
  绕开 4000 字符预算的官方口子。
- **`harnesses/*.yaml` 全部是 `skills: []`**，`skills/` 目录是空的。

### 1.2 tool 注册路径

- `src/agent_runtime/tools/spec.py`：`ToolSpec(name, description, parameters, handler)`，
  `handler` 必须是 `async`（`ToolRegistry.register` 会 `TypeError`）。
- `src/agent_runtime/tools/tools.py`：`builtin_tool_specs()` 聚合所有内置工具；
  `create_default_registry(enabled=[...])` 按 harness 的 `tools.enabled` 白名单过滤，
  未知名字直接 `ValueError`；`read_output` 恒在（观测 spill 的基础设施）。
- 加一个内置工具 = 新模块 + `*_spec()` 工厂 + `builtin_tool_specs()` 加一行
  + **更新 `tests/tools/test_registry.py::BUILTIN_TOOLS`**（否则该测试会红）。

### 1.3 已有可复用资产

- `.scripts/cdp.js`：Node 24 原生 WebSocket 的极简 CDP 客户端（`newPage` / `send` / `ev` / `waitFor`），
  已实测跑通 X 发帖与 LinkedIn 存草稿。
- `workspace/memory/browser_automation.md`：已记录两站的选择器与坑（含 video.js 假 dialog 陷阱）。
- 实测结论：Wayland 下 `agent-desktop` 的指针注入被策略拒绝（`POLICY_DENIED`），
  X 的 a11y ref 输入报 `AMBIGUOUS_TARGET` → **CDP 是唯一可行路径**。

---

## 2. 目标架构

```
        ┌─────────────────────────────────────────────┐
        │ skill: x-playbook / linkedin-playbook       │  易变：选择器、流程、风控、话术
        │ （注入 system prompt，<domain_playbooks>）   │
        └───────────────────┬─────────────────────────┘
                            │ 调用
        ┌───────────────────▼─────────────────────────┐
        │ tool: browser（单工具 + steps 批量）         │  稳定：CDP 原语 + 生命周期 + 限速
        │  steps=[ensure/open/dump/click/type/assert/  │
        │         wait/press/screenshot/close]         │
        │  一次调用跑完一整串，遇错即停                 │
        └───────────────────┬─────────────────────────┘
                            │
        ┌───────────────────▼─────────────────────────┐
        │ Chrome（自动化专用 profile，headless 或 headed）│
        └─────────────────────────────────────────────┘

        ┌─────────────────────────────────────────────┐
        │ tool: social_log  → workspace/social/*.jsonl │  数据层：去重、审计、打分
        └─────────────────────────────────────────────┘
```

关键设计原则：

1. **tool 里不出现任何站点选择器**。`browser` 只认 CSS selector 参数，不知道什么是"发帖按钮"。
2. **skill 里不出现任何 CDP 细节**。playbook 只说"点 `[data-testid=tweetButtonInline]`"，
   不说怎么发事件。
3. **写操作必须"先读后写"**：playbook 强制要求写之前 `assert` 校验目标（对方 handle / 原文），
   这是防"发错人"的唯一可靠手段。规则写在 skill 里，执行保障在 tool 的 `assert` action。
4. **一次 LLM 调用 = 一串步骤**：`browser(steps=[...])` 声明式批量，不是"一个 action 一次往返"。
   实测：发一条 GM 从 ~9 次往返降到 1 次。
5. **所有动作落审计日志**，事后可复盘"我到底点了什么"。

---

## 3. L1：`browser` tool 详细设计

### 3.1 为什么是"单工具 + steps 批量"而不是 12 个工具

- 12 个工具会挤占 schema 预算，且每个都要写"何时用我"的描述（`test_registry.py` 里
  `ToolRoutingGuidanceTests` 就在管这件事）。
- 这些 action 共享同一个会话状态（page_id、限速、超时），拆开就要靠全局变量传递。
- 单工具 + `action` 枚举，模型侧只需学一个工具。

**但"一次调用只做一个 action"是错的**，这是实测教训：

| 任务 | 单 action 契约 | 手写脚本实测 |
|---|---|---|
| 发一条 GM | ~9 次 LLM 往返 | 1 次（`x_post.js` 内部 17 处 CDP 调用点 + 40 次轮询） |
| 找 20 个客户并评论 | ~100+ 次 | ~5 次 |

所以契约是 **`browser(steps=[...])`**：一次 LLM 调用提交一串声明式步骤，
tool 内部串行执行、遇错即停、返回每步结果。单 action 退化成 1 元素数组。

代价：参数校验要自己写（`steps` 内每步的 `action` 与其它字段联合校验），
错误信息要指明"第几步失败"。可接受。

**为什么不是"让模型生成 JS 代码"**：`steps` 是声明式 JSON，没有注入面，
不需要 `allow_eval`；而生成 JS 会把站点知识藏进字符串，改版时无从下手（见 §3.7）。

### 3.2 文件与模块

```
src/agent_runtime/browser/__init__.py
src/agent_runtime/browser/session.py      # BrowserSession：进程管理 + CDP 连接 + 限速 + 审计
src/agent_runtime/browser/steps.py        # 步骤校验 + 串行执行器（run_steps）
src/agent_runtime/browser/cdp.py          # 极简 CDP 客户端（asyncio + websockets）
src/agent_runtime/browser/errors.py       # BrowserError + 错误码枚举
src/agent_runtime/tools/browser.py        # browser_spec() + async handler（薄壳）
tests/tools/test_browser.py               # 单测（fake transport，不连真 Chrome）
tests/browser/test_steps.py               # 步骤校验/遇错即停/结果对齐
tests/browser/test_session.py             # 生命周期/限速/审计单测
tests/integration/test_browser_live.py    # 真 Chrome 集成测试，SUN_CDP_LIVE=1 才跑
```

`tools/browser.py` 只做"参数 → `run_steps`"的映射，与 `files.py` 是 `Workspace` 薄壳
同一风格（见 `files.py` 模块 docstring 的自我定位）。

### 3.3 接口契约

```json
{"steps": [ {"action": "...", ...}, ... ],
 "page_id": "optional-default-page",
 "stop_on_error": true}
```

- `steps` 必填，1..50 个元素；超过 50 直接 `BAD_ARGS`，防止一次调用跑飞。
- 顺序执行，**遇错即停**（`stop_on_error` 默认 true）。
- 返回 `{ok, results:[...], failed_at, elapsed_ms}`；`results[i]` 与 `steps[i]` 一一对应，
  失败步之后不再执行，`results` 长度 = `failed_at + 1`。
- 每步可带自己的 `page_id` 覆盖顶层值。

单步 action 一览（`steps[i].action`）：

| action | 主要参数 | 返回 | 说明 |
|---|---|---|---|
| `status` | — | `{running, port, profile, headed, version, pages:[{id,url,title}]}` | 幂等，不启动进程 |
| `ensure` | `headed=false, profile?, port=9222` | 同 `status` | 未运行则启动；已运行则复用（单例） |
| `open` | `url, new_tab=false` | `{page_id, url, title}` | 导航并等 `document.readyState==='complete'` |
| `dump` | `selector?, max_chars=8000, items?` | `{text, matched, truncated}` 或 `{rows:[...]}` | **读的主力**，见 3.3.1 |
| `click` | `selector, expect_nav=false` | `{ok, url_after}` | 先 `wait` 可见再点；`expect_nav` 时等导航完成 |
| `type` | `selector, text, submit=false` | `{ok, value_after}` | 聚焦 → `Input.insertText`；`submit` 才按 Enter |
| `press` | `key, selector?` | `{ok}` | 单键（`Escape` / `Enter` / `Tab`） |
| `wait` | `selector, timeout_ms=15000` | `{found, elapsed_ms}` | 轮询，不抛异常，返回布尔 |
| `assert` | `selector, text_equals?, text_contains?, absent?` | `{ok, actual}` | **写前校验专用**，不满足则整串停，见 3.3.2 |
| `screenshot` | `path?` | `{path}` | 默认落 `.outputs/shot-<ts>.png`，走现有 spill 目录 |
| `eval` | `expression` | `{value}` | **默认禁用**，需 `allow_eval=true` 配置，见 3.7 |
| `cookies_export` | `path` | `{count, path}` | `Network.getAllCookies` → JSON（权限 600） |
| `cookies_import` | `path` | `{count}` | `Network.setCookies`，跨机迁移登录态用 |
| `close` | `page_id?` | `{closed}` | 无 `page_id` 则关整个浏览器 |

#### 3.3.0 典型调用：发一条 GM（1 次 LLM 往返）

```json
{"steps": [
  {"action": "ensure"},
  {"action": "open", "url": "https://x.com/home"},
  {"action": "wait", "selector": "[data-testid='tweetTextarea_0']"},
  {"action": "assert", "selector": "[data-testid='tweetTextarea_0']", "text_equals": ""},
  {"action": "type", "selector": "[data-testid='tweetTextarea_0']", "text": "GM"},
  {"action": "assert", "selector": "[data-testid='tweetTextarea_0']", "text_equals": "GM"},
  {"action": "click", "selector": "[data-testid='tweetButtonInline']"},
  {"action": "wait", "selector": "[data-testid='toast']", "timeout_ms": 20000},
  {"action": "dump", "selector": "[data-testid='toast']"}
]}
→ {"ok": true, "failed_at": null, "results": [ ... 9 项 ... ]}
```

注意第 4 步和第 6 步的 `assert`：**写前校验编辑器为空、写后校验内容完全一致**。
这是把"发错内容"这个不可逆错误挡在点击之前，也是 §2 原则 3 的落地形式。

#### 3.3.1 `dump` 的两种形态（这是"找客户"的关键）

**形态 A：整页/区域文本**

```json
{"action":"dump","selector":"main","max_chars":8000}
→ {"text":"...", "matched":1, "truncated":false}
```

**形态 B：结构化列表抽取**（搜索结果、时间线、评论列表）

```json
{"action":"dump",
 "items":{"container":"li.reusable-search__result-container",
          "fields":{"name":"span[aria-hidden=true]",
                    "headline":".entity-result__primary-subtitle",
                    "url":"a[href*='/in/']@href"}}}
→ {"rows":[{"name":"...","headline":"...","url":"..."}, ...], "count":10}
```

`@attr` 后缀表示取属性而非文本。这一条把"读 10 个搜索结果"从"塞 30KB HTML 进 context"
变成"塞 10 行 JSON"，是能不能做规模化的分水岭。

#### 3.3.2 `assert` 为什么必须是独立 action

`click`/`type` 的返回值只能告诉你"动作发出去了"，不能告诉你"目标对不对"。
社交场景里最贵的错误是**发错人**，而它只能靠"读回来比对"发现。

`assert` 的语义：

| 参数 | 含义 |
|---|---|
| `text_equals` | 元素 `innerText.trim()` 完全相等（用于校验编辑器内容） |
| `text_contains` | 包含子串（用于校验页面出现了对方 handle） |
| `absent: true` | 元素**不存在**才算通过（用于校验弹窗已关闭、草稿已清空） |

不满足 → 抛 `ASSERT_FAILED`，整串停，`failed_at` 指向该步，`results` 里带 `actual`。

playbook 里强制要求：**任何写操作（`click` 提交类按钮 / `type` 到别人页面）之前
必须有一条 `assert` 校验目标**。规则写进 skill，执行保障在 tool。

### 3.4 会话与登录态

- **自动化专用 profile**：`~/.local/share/sunagent/browser-profile`（可用
  `SUN_BROWSER_PROFILE` 覆盖）。**绝不碰用户日常 profile**——日常 Chrome 没开调试端口，
  且共用 profile 会互相锁。
- 首次使用：`ensure` 启动后由人手动登录一次（headed 模式），之后 cookie 常驻该 profile。
- 跨机迁移：`cookies_export` → 拷 JSON → 目标机 `cookies_import`。
  比搬 `Cookies` sqlite 干净（后者依赖 keyring 密钥，见 §9 风险）。
- 登录墙检测：`open` 后检查 `document.body.innerText` 是否命中
  `登录|Log in|Sign in|authwall` 且无主内容 → 返回 `LOGIN_REQUIRED`，让模型停下来问人，
  而不是继续瞎点。

### 3.5 进程生命周期

- **单例**：`BrowserSession` 是模块级懒加载单例，key = `(profile, port)`。
  `builtin_tool_specs()` 每次 run 都会调用，但 handler 内部才真正建连，未使用零成本。
- **启动参数**（默认 headless）：

```
chrome --headless=new \
       --user-data-dir=<profile> \
       --remote-debugging-port=<port> \
       --no-first-run --no-default-browser-check \
       --window-size=1400,900 about:blank
```

  `headed=true` 时去掉 `--headless=new`，加 `--window-position=-2400,-2400`（离屏，
  需要看时再挪回来）。headed 启动瞬间 GNOME 会给一次焦点，这是已知瑕疵。
- **健康检查**：`GET /json/version` 探活；进程存在但端口不通 → 杀掉重启（一次）。
- **收尾**：`close` 显式关；进程退出时 `atexit` 兜底 kill 自己启动的 pid（只 kill 自己启动的，
  不碰用户手动开的 Chrome）。
- **超时**：单步默认 30s，`wait` 单独 15s；CDP 单条命令 45s 硬超时；
  **整串 `steps` 总预算 180s**，超时则中断并返回已完成部分（`failed_at` 指向当前步）。

### 3.6 错误码

`BrowserError(code, message, suggestion, step_index)`，`code` 取值：

| code | 触发 | 模型该做什么 |
|---|---|---|
| `BROWSER_UNAVAILABLE` | 起不来 / 端口不通 | 检查 chrome 是否安装、端口占用 |
| `NAV_FAILED` | 导航超时 / DNS / 证书 | 换 URL 或重试一次 |
| `SELECTOR_NOT_FOUND` | `click`/`type` 目标不存在 | **先 `dump` 看页面真实结构**，别猜 |
| `ASSERT_FAILED` | `assert` 不满足 | **停手**，dump 看实际内容，报告给人 |
| `TIMEOUT` | 命令超时 | 重试一次，仍失败则报告 |
| `LOGIN_REQUIRED` | 命中登录墙 | 停下来问人，不要尝试登录 |
| `EVAL_DISABLED` | 未开 `allow_eval` | 用 `dump`/`click` 组合替代 |
| `BAD_ARGS` | action 与参数不匹配 / steps 超长 | 修正参数 |

`SELECTOR_NOT_FOUND` 的 `suggestion` 必须写"先 dump"，这是把"猜选择器"这个最常见的
失败模式直接掐掉。`ASSERT_FAILED` 的 `suggestion` 必须写"停手报告"，
因为它的语义就是"目标不对，继续做会出错"。

### 3.7 `eval` 的安全定位

`eval` 等于任意 JS 执行。但本 runtime 已有 `run_command`（任意 shell），
所以它**不构成新的权限边界**。仍然默认关闭，理由不是安全，而是**可维护性**：
一旦 playbook 依赖 `eval` 里的复杂 JS，站点改版时改的就是"藏在字符串里的代码"，
比改 CSS selector 难得多。默认关闭能逼着 playbook 用 selector 表达意图。

`steps` 契约进一步降低了 `eval` 的必要性：批量执行不再需要"写一段 JS 循环"，
声明式数组就够了。预期 `eval` 只在调试时临时开启。

### 3.8 依赖决策

Python 侧需要一个 WebSocket 客户端。三个选项：

| 方案 | 代价 | 评价 |
|---|---|---|
| **加 `websockets>=13` 依赖** | 一行 `uv add`，纯 Python | **推荐**，最省事、可测 |
| 自己写 asyncio WS 帧解析 | ~150 行 + 测试 | 项目定位是"小而可组合"，但这是重复造轮子 |
| 复用 `.scripts/cdp.js` 走 node 子进程 | 零新依赖，但多一层进程 + JSON 编解码 | 备选；好处是已验证，坏处是错误栈跨语言 |

推荐 `websockets`。若坚持零依赖，退到 node 子进程方案，`session.py` 里换 transport 即可
（见 §8 的 transport 抽象）。

### 3.9 代码骨架

```python
# src/agent_runtime/browser/steps.py
MAX_STEPS = 50

async def run_steps(session: "BrowserSession", steps: list[dict],
                    *, stop_on_error: bool = True) -> dict:
    """Execute steps in order; stop at the first failure and report where."""
    if not 1 <= len(steps) <= MAX_STEPS:
        raise BrowserError("BAD_ARGS", f"steps must be 1..{MAX_STEPS}, got {len(steps)}")
    results, failed_at = [], None
    started = time.monotonic()
    for i, step in enumerate(steps):
        try:
            results.append(await session.dispatch(step))
        except BrowserError as exc:
            exc.step_index = i
            results.append({"ok": False, "error": exc.as_dict()})
            failed_at = i
            if stop_on_error:
                break
    return {"ok": failed_at is None, "results": results,
            "failed_at": failed_at,
            "elapsed_ms": int((time.monotonic() - started) * 1000)}
```

```python
# src/agent_runtime/tools/browser.py
from functools import partial
from typing import Any
from agent_runtime.browser.session import BrowserSession
from agent_runtime.browser.steps import run_steps
from agent_runtime.tools.spec import ToolSpec

async def browser(steps: list[dict], *, session: BrowserSession,
                  page_id: str | None = None,
                  stop_on_error: bool = True) -> dict[str, Any]:
    """Run a batch of browser steps; all real work lives in BrowserSession."""
    return await run_steps(session, steps, stop_on_error=stop_on_error)

def browser_spec(session: BrowserSession) -> ToolSpec:
    return ToolSpec(
        "browser",
        "Drive a real Chrome over CDP by submitting a batch of declarative steps "
        "(open/dump/click/type/assert/wait/screenshot). Steps run in order and stop "
        "at the first failure, so one call can complete a whole flow. Site-specific "
        "selectors and step order live in the platform playbook, not here. Best "
        "suited for reading a page into structured rows and for replaying a known "
        "click/type sequence.",
        {"type": "object",
         "properties": {
             "steps": {"type": "array", "minItems": 1, "maxItems": 50,
                       "items": {"type": "object",
                                 "properties": {
                                     "action": {"type": "string", "enum": [...]},
                                     "selector": {"type": "string"},
                                     "text": {"type": "string"},
                                     "url": {"type": "string"},
                                     "items": {"type": "object"},
                                     "page_id": {"type": "string"},
                                     "timeout_ms": {"type": "integer", "default": 30000},
                                     "headed": {"type": "boolean", "default": False},
                                     "max_chars": {"type": "integer", "default": 8000},
                                 },
                                 "required": ["action"]}},
             "page_id": {"type": "string"},
             "stop_on_error": {"type": "boolean", "default": True},
         },
         "required": ["steps"]},
        partial(browser, session=session))
```

`builtin_tool_specs()` 里加一行 `browser_spec(BrowserSession.shared(runtime))`，
并更新 `tests/tools/test_registry.py::BUILTIN_TOOLS`。

### 3.10 限速与审计（放在 tool 层，不放 skill）

- **限速**：`BrowserSession` 维护 `per-domain last_action_ts`，同一域名两次写操作
  （`click`/`type`/`press`）最小间隔默认 3s，可配。超限则 `await` 补足，不报错。
  理由：这是**策略**，不是知识；放 skill 里我可能"忘了遵守"，放 tool 里是硬约束。
  **批量 `steps` 不绕过限速**——限速在 `dispatch` 内部，逐步骤生效。
- **审计**：每个 action 追加一行到 `workspace/social/actions.jsonl`：
  `{ts, action, selector, url, page_id, ok, error_code, step_index}`。**不记录 `text` 内容**
  （可能含私信内容），只记长度。事后可复盘"我点了什么、点在哪一页"。
- **批量调用的额外收益**：一次 `steps` 调用在审计里共享同一个 `batch_id`，
  复盘时能看出"这 9 步是同一次决策"。

## 4. L2：playbook skill 详细设计

### 4.1 目录与预算

```
skills/x-playbook/SKILL.md                    # 正文 < 3500 字符（硬限 4000，留余量）
skills/x-playbook/references/selectors.json   # 长选择器表，正文引用，模型按需 read_file
skills/x-playbook/references/templates.md     # 话术模板（GM / 评论 / 回复）
skills/linkedin-playbook/SKILL.md
skills/linkedin-playbook/references/selectors.json
skills/linkedin-playbook/references/templates.md
```

`references/` 这个目录名不是随便起的：`skills.py` 超限报错时就是这么建议的
（"move detail into `references` and reference it from SKILL.md"），跟着走即可。
正文写"选择器全表见 `references/selectors.json`"，模型用 `read_file` 取。
这样正文守得住预算，长表格又不丢。

### 4.2 `SKILL.md` 固定六节模板

```markdown
---
name: x-playbook
version: 2026-11-25
---
# X (Twitter) 操作手册

## 1. 身份与入口
账号 @johnsonbuilds；入口 https://x.com/home；登录墙特征：...

## 2. 读（找客户 / 看时间线）
- 搜索：`open https://x.com/search?q=<query>&f=user` → `dump items{...}`
- 时间线：`dump items{container:'article[data-testid="tweet"]', fields:{...}}`
- 判据：只读，可自由执行；单次 dump ≤ 20 条，翻页靠滚动 + 再 dump。

## 3. 写（发帖 / 评论 / 回复）
发帖：dump 校验编辑器为空 → type `[data-testid=tweetTextarea_0]` → 
      dump 校验 innerText 完全等于目标文案 → click `[data-testid=tweetButtonInline]` 一次
      → 校验编辑器清空 + toast "你的帖子已发送"
评论/回复：必须先 dump 目标推文，确认 handle 与原文，再 type + click。
**红线：任何写操作前必须 dump 校验目标；校验不通过就停。**

## 4. 验证（怎么算成功）
- 发帖：编辑器清空 + toast；再 open 个人页确认（注意置顶推文排第一，别误判）
- 评论：目标推文下出现自己的 handle

## 5. 风控与节奏
- 同一账号 10 分钟内写操作 ≤ 5 次；关注/点赞 ≤ 20 次/小时
- 新账号或异地登录后 24h 内只读不写
- 遇到验证码/checkpoint：立即停止，报告给人，不尝试绕过

## 6. 已知坑
- 个人页有缓存，验证要等 12s 或重进
- `article[data-testid="tweet"]` 里置顶帖带 `socialContext`，排序不是时间序
- 选择器失效时：先 dump 看真实 DOM，再改 `references/selectors.json`，并更新 version
```

LinkedIn 版同构，但第 3 节必须写死：

- 编辑器是 `.ProseMirror[contenteditable="true"]`，外层是 `<dialog>`，
  **不是** `div[role="dialog"]`（那是页面里 video.js 的隐藏弹层，会误导）
- 存草稿：`<dialog>` 内 `button[aria-label="Dismiss"]` → "Save this post as a draft?" →
  `Save as draft`（旁边是 `Discard`，别点错）
- **`Post` 按钮只存草稿时绝不点**；验证靠 feed 的 `Draft: <text>` 与个人页 `Drafts N`

### 4.3 失效自检

`scripts/check_playbooks.py`：读 `references/selectors.json`，对每个选择器在对应页面
`dump` 一次，报告"命中/未命中"。站点改版后跑一次就知道哪条过期了，
不用等任务失败才发现。

### 4.4 harness 接入

新建 `harnesses/social-v1.yaml`（`parent: general-v2`），**不改 `general-v2`**：

```yaml
id: social-v1
parent: general-v2
mutation: 'tools.enabled += browser, social_log; skills += x-playbook, linkedin-playbook'
reason: '社交平台任务需要浏览器原语与站点手册；通用 harness 不应携带站点知识'
tools:
  enabled: [run_command, read_file, write_file, edit_file, grep_search,
            find_files, list_dir, browser, social_log]
skills: [x-playbook, linkedin-playbook]
```

理由：站点知识只对社交任务有用，塞进通用 harness 是纯 context 浪费；
而且 `skills.loaded` 的 content hash 让"这次用的是哪版手册"可追溯。

---

## 5. L3：memory 边界

**留在 memory 的**（`workspace/memory/browser_automation.md` 瘦身后）：

- 账号 handle、profile 路径、端口约定
- 用户偏好：不想被抢焦点；LinkedIn 只存草稿不许点 Post；微信不可行（结论）
- 环境事实：Wayland 下 agent-desktop 指针注入被拒；无 tesseract/PIL
- 指向：详细流程见 `skills/*-playbook`，别在这里重复

**必须搬走的**：所有选择器、所有步骤序列、所有"坑"的细节 → 进 skill。
判据：**memory 里出现 `data-testid` 就是放错层了。**

---

## 6. L4：数据层（`social_log` tool）

没有它，"找客户"每次都要把结果重新塞进 context，且无法回答"这条我评论过没有"。

```
workspace/social/contacts.jsonl      # 人：platform, handle, name, headline, url, score, tags, first_seen, last_seen
workspace/social/interactions.jsonl  # 事：platform, kind(post|comment|reply|like|follow), target_url, target_handle, text_hash, ts, result
workspace/social/actions.jsonl       # 审计：browser tool 自动写
```

`social_log(action, ...)`：

| action | 参数 | 返回 |
|---|---|---|
| `append` | `file, record` | `{ok, id}` |
| `query` | `file, where?, limit=50` | `{rows}` |
| `seen` | `platform, key` | `{seen: bool, last_ts}` |

去重键：`contacts` 用 `platform+handle`；`interactions` 用 `platform+target_url+kind`。
`seen` 是"别重复评论同一个人"的唯一可靠依据。

实现：纯 Python `jsonl` 追加 + 线性扫描（量级 < 10 万行足够），不引 sqlite。
若以后要打分排序再换。

---

## 7. 里程碑与验收

| 里程碑 | 内容 | 验收标准 | 估时 |
|---|---|---|---|
| **M0** | `browser` tool 骨架：`steps` 执行器 + `ensure/open/dump/close/status` + 单测 | `test_browser.py` / `test_steps.py` 绿；`browser status` 能起停 Chrome；一次 `steps` 调用跑完 3 步 | 0.5d |
| **M1** | 写原语：`click/type/press/wait/assert/screenshot` + 限速 + 审计 + `batch_id` | 用**一次** `steps` 调用复现"发 GM → `assert` 校验 → 删除"全流程 | 0.5d |
| **M2** | 两个 playbook + `social-v1.yaml` + `check_playbooks.py` | `skills.loaded` 事件出现两个 playbook；自检全绿 | 0.5d |
| **M3** | `social_log` + 数据文件 | 同一 handle 二次 `seen` 返回 true | 0.5d |
| **M4** | **只读任务先跑**：X/LinkedIn 各搜 20 个目标 → 落盘 → 打分 | 产出 `contacts.jsonl` 40 行，零写操作 | 0.5d |
| **M5** | 写操作限速上线：评论/回复各 5 条试跑 | 无 checkpoint；`interactions.jsonl` 可复盘 | 0.5d |

**顺序不可颠倒**：M4 只读跑通再开写。理由：读的失败是"没找到人"，写的失败是"发错人/被封号"。

---

## 8. 测试策略

- **单测不连真 Chrome**：`BrowserSession` 依赖注入 `transport`（有 `send(method, params)`
  和 `evaluate(expr)` 两个方法）。测试用 `FakeTransport` 返回预置响应，覆盖：
  action 路由、参数联合校验、错误码映射、限速等待、审计写入、`dump items` 抽取逻辑。
- **`steps` 执行器单测**（`tests/browser/test_steps.py`）：`1..50` 边界、`results` 与 `steps` 一一对齐、
  遇错即停时 `failed_at` 正确且后续步骤未执行、`stop_on_error=false` 时继续跑、每步 `page_id` 覆盖生效。
- **集成测试**：`tests/integration/test_browser_live.py`，`SUN_CDP_LIVE=1` 才跑，
  默认 skip。内容：起 Chrome → 打开 `about:blank` → `dump` → `close`。
- **回归脚本**：`scripts/social_smoke.py` —— 发一条 `GM` → 校验出现 → 删除 → 校验消失。
  每次改 playbook 后跑一次，这是"手册没过期"的实证。
- **registry 测试**：加工具必须同步 `tests/tools/test_registry.py::BUILTIN_TOOLS`。

---

## 9. 风险与红线

1. **账号风险不对称**：X 是测试号，LinkedIn 是职业身份。LinkedIn 的写操作默认更保守
   （更慢的节奏、更少的量），且**任何 checkpoint 立即停**。
2. **绝不自动点 `Post`**：除非用户当轮明确说"发布"。存草稿是默认动作。
3. **cookie 文件权限 600**，`cookies_export` 落盘后提示用户；不写进 workspace 明文长期留存。
4. **VPS 迁移的真实障碍是 IP 不是登录态**：Contabo 是德国机房 IP，X/LinkedIn 都看
   IP 跳变 + ASN，LinkedIn 尤其激进（新国家 + datacenter ASN ≈ 盗号信号）。
   可行路径按优先级：① 本地 Chrome 常驻 + SSH 反向隧道（IP 与登录态都是本人）；
   ② VPS 上固定住宅出口 + 首次人工过验证；③ 直接搬 cookie 到裸机房 IP（不推荐）。
   详见后续独立文档。
5. **不做的事**：不破解验证码、不做批量关注/群发、不伪造指纹。
6. **`eval` 默认关闭**（理由见 §3.7）。
7. **`steps` 批量不等于"放手"**：一次调用最多 50 步，且**写操作步骤必须自带前置 `assert`**。批量只省往返，
   不降低校验要求；`stop_on_error` 默认 true，任何一步失败整串停，绝不"跳过继续"。

---

## 10. 待决策项（需要你拍板）

1. **依赖**：加 `websockets>=13`，还是走 node 子进程零依赖？（推荐前者）
2. **默认可见性**：`browser` 默认 headless，还是默认 headed（你能看着，代价是启动抢一次焦点）？
3. **数据层**：`jsonl` 够用，还是直接上 sqlite（为后续打分/排序）？
4. **写操作授权粒度**：每次写都要你确认，还是"当轮任务内授权、跨轮失效"？
   （我倾向后者：一轮任务里说"去评论这 5 个人"就授权这 5 次，下一轮重新授权。）
5. **`steps` 上限**：50 步够不够？（"找 20 个客户并逐个评论"可能超；备选是允许模型分 2–3 次调用，
   或把"对列表逐项执行"做成 `steps` 里的 `foreach` 语法。我倾向先卡 50，实测后再定。）
6. **`assert` 失败后的行为**：整串停（当前设计）还是"跳过该步继续"？我倾向整串停——写错人的代价远大于重跑。
