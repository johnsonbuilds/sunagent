# SunAgent WebUI 任务看板 — 完整方案

## 1. 目标与边界

**目标**：在 WebUI 左侧导航增加一个一级“看板”视图，用独立的“任务卡片”管理要交给 agent 做的事；支持拖拽改状态、新建/编辑/删除任务，并能把任务关联到一个对话，直接进入聊天继续执行。

**本期范围**

- 单一看板，固定 5 列
- 任务 CRUD、拖拽排序、跨列移动
- 任务关联一个 WebUI 会话，可从卡片打开对话
- 卡片显示关联会话的运行状态
- 中英文文案、明暗主题、窄屏适配
- 后端持久化、接口测试、前端 typecheck/build

**明确不做（放到后续）**

- 多看板、多人协作、权限
- 自定义列、自定义字段
- 截止时间、负责人、附件
- agent 通过工具直接读写看板
- 全局 SSE、运行统计图表
- 自动状态流转（本期状态完全由人维护）

---

## 2. 核心决策

| 决策 | 方案 | 理由 |
|---|---|---|
| 卡片对象 | 独立 `Task`，不是会话本身 | 任务可以先存在、后执行；不受“空会话不落盘”约束 |
| 任务与对话 | **1 个任务 ↔ 0 或 1 个会话**，懒创建 | 模型简单；首次打开对话时才创建 session |
| 状态 | 人工维护：`backlog / todo / doing / review / done` | agent 跑完不等于人验收通过，`review` 有实际意义 |
| 运行状态 | `busy` 只作为卡片上的“运行中”指示 | 不把运行时状态当列，避免列自动跳动 |
| 排序 | 每列内 `position` 整数，服务端归一化 | 拖拽后由服务端返回完整快照，前端不用自己算 |
| 持久化 | 单文件 `workspace/board.json` | 单用户本地工具，最简单；原子写即可 |
| 入口 | 左侧导航一级视图 | 符合已确认的交互 |
| 删除任务 | 默认只删任务，保留对话 | 避免误删有价值的会话 |
| 拖拽 | 原生 HTML5 DnD + 卡片“移动到…”菜单兜底 | 不引新依赖；菜单覆盖触屏和无障碍 |
| 实时性 | 看板可见时 5 秒轮询 + 运行结束主动刷新 | 本期不需要全局事件流 |

---

## 3. 数据模型

### 3.1 Task

```json
{
  "id": "task-9f2c1a7b3d4e",
  "title": "修复登录接口的超时问题",
  "description": "复现步骤、相关日志、验收标准……",
  "status": "todo",
  "priority": "high",
  "tags": ["backend", "bug"],
  "position": 0,
  "session_id": "sess-20261113-084621",
  "created_at": 1794556358.48,
  "updated_at": 1794556401.12
}
```

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | string | `task-<hex12>` | 服务端生成，前端不可改 |
| `title` | string | 1–200 字符，去首尾空白 | 必填 |
| `description` | string | 0–20,000 字符 | 纯文本，后续可加 Markdown 预览 |
| `status` | enum | 5 个固定值 | 列归属 |
| `priority` | enum | `low / normal / high / urgent` | 默认 `normal` |
| `tags` | string[] | 最多 20 个，每个 ≤32 字符 | 默认空数组 |
| `position` | int | 当前列内 0..n-1 | 服务端维护，不接受任意值 |
| `session_id` | string \| null | 指向存在的 session | 懒创建 |
| `created_at` | number | epoch 秒 | 与现有 session 一致 |
| `updated_at` | number | epoch 秒 | 每次变更更新 |

> 如果想把 MVP 再压小，可以去掉 `priority` 和 `tags`，其他设计不受影响。我建议保留，因为它们的 UI 和存储成本很低。

### 3.2 状态列

| id | 中文 | 含义 |
|---|---|---|
| `backlog` | 待整理 | 想法/暂不安排 |
| `todo` | 待办 | 已准备执行 |
| `doing` | 进行中 | 正在处理 |
| `review` | 待验收 | agent 已产出，等人确认 |
| `done` | 已完成 | 人确认完成 |

状态顺序由服务端返回，前端只负责翻译标签，避免两端顺序不一致。

### 3.3 派生字段

接口返回的任务会额外带一个 `session` 摘要，**不落盘**：

```json
{
  "session": {
    "id": "sess-...",
    "title": "修复登录接口的超时问题",
    "busy": true,
    "updated_at": 1794556401.12
  }
}
```

- 会话不存在或已被删除 → `session: null`
- 不返回 `segments` / `messages`，避免看板接口变重

---

## 4. 持久化设计

### 4.1 文件位置

在 `src/agent_runtime/storage.py` 增加：

```python
def board_path() -> Path:
    return runtime_home() / "board.json"
```

即默认 `workspace/board.json`，与 `sessions/`、`traces/` 同级。

### 4.2 文件结构

```json
{
  "version": 1,
  "updated_at": 1794556401.12,
  "tasks": [ ... ]
}
```

**为什么单文件**

- 单用户、单进程，任务量通常几十到几百
- 排序、跨列移动需要整体一致性，单文件最简单
- 原子替换即可避免半写状态
- 便于备份、查看、手工修复

**代价**

- 多标签页同时编辑是 last-write-wins
- 没有按任务的版本历史
- 任务量特别大时需要拆分（本期不做）

### 4.3 原子写与并发

- 写入临时文件 `board.json.tmp`，`os.replace` 替换正式文件
- `BoardStore` 内部用 `threading.RLock` 保护读改写
- 所有变更先构造新的任务列表，**写盘成功后再替换内存状态**；写失败时内存保持旧值，接口返回 500
- 与现有 `SessionStore` 一样，文件很小，同步写可以接受

### 4.4 加载与容错

- 文件不存在 → 空看板，不创建文件
- JSON 损坏 → 备份为 `board.json.corrupt-<timestamp>`，记录 warning，然后以空看板启动
- 启动时执行 `board.reconcile(sessions)`：清理指向不存在会话的 `session_id`，避免卡片一直显示“对话已删除”

---

## 5. 后端设计

### 5.1 新增/修改文件

| 文件 | 动作 | 内容 |
|---|---|---|
| `src/agent_runtime/storage.py` | 修改 | 增加 `board_path()` |
| `webui/board.py` | 新增 | `BoardTask`、`BoardStore`、校验、排序、持久化 |
| `webui/server.py` | 修改 | 请求模型、`/api/board` 路由、会话删除联动 |
| `tests/webui/test_board.py` | 新增 | Store 与 API 测试 |
| `docs/webui.md` | 修改 | 增加看板章节 |

`webui/sessions.py` **不需要改**，看板是独立模块。

### 5.2 BoardStore 职责

```python
class BoardStore:
    def load(self) -> None
    def snapshot(self, sessions: SessionStore) -> dict
    def create(self, title, *, description="", status="backlog",
               priority="normal", tags=None) -> dict
    def update(self, task_id, patch: dict) -> dict
    def delete(self, task_id) -> bool
    def move(self, task_id, status: str, position: int | None) -> dict
    def ensure_session(self, task_id, sessions, *, model, harness) -> tuple[str, bool]
    def unlink_session(self, session_id: str) -> None
    def reconcile(self, sessions: SessionStore) -> None
```

内部辅助：

- `_normalize_positions(status)`：把某列任务重新编号为 `0..n-1`
- `_validate_status` / `_validate_priority` / `_validate_tags`
- `_read` / `_write_atomic`
- 自定义异常：`BoardValidationError`、`BoardNotFoundError`、`BoardStorageError`

### 5.3 HTTP API

统一前缀 `/api/board`。

#### `GET /api/board`

返回完整快照：

```json
{
  "version": 1,
  "statuses": ["backlog", "todo", "doing", "review", "done"],
  "updated_at": 1794556401.12,
  "tasks": [ ... ]
}
```

`tasks` 按 `status` + `position` 排好序；每项带派生 `session` 摘要。

#### `POST /api/board/tasks`

请求：

```json
{
  "title": "修复登录接口的超时问题",
  "description": "",
  "status": "backlog",
  "priority": "normal",
  "tags": ["backend"]
}
```

- 只有 `title` 必填
- `status` 默认 `backlog`；从某列“+”创建时前端传该列 id
- 返回 `201` + 新快照

#### `PATCH /api/board/tasks/{id}`

请求为部分字段：

```json
{
  "title": "新标题",
  "description": "更新描述",
  "status": "doing",
  "position": 2,
  "priority": "high",
  "tags": ["bug"]
}
```

移动规则：

- 只传 `status`：移动到目标列末尾
- 只传 `position`：在当前列内移动
- 同时传：插入目标列的指定位置
- 服务端对受影响列重新编号，返回完整快照

#### `DELETE /api/board/tasks/{id}?delete_session=false`

- 默认只删除任务，保留关联会话
- `delete_session=true` 时，先删除会话再删任务
- 返回：

```json
{
  "board": { ... },
  "deleted_session": false
}
```

> 本期前端默认传 `false`；批量删除/联动删除留给后续。

#### `POST /api/board/tasks/{id}/session`

按需创建或复用会话：

- 任务已有存活会话 → 直接返回
- 任务无会话 → 用任务标题创建 session，默认 model/harness，写回 `session_id`
- 任务有 `session_id` 但会话已不存在 → 创建新会话并重新关联

响应：

```json
{
  "board": { ... },
  "session_id": "sess-...",
  "created": true
}
```

#### 错误码

| 场景 | 状态码 |
|---|---|
| 参数不合法 | `400` |
| 任务不存在 | `404` |
| 写盘失败 | `500`，日志记录细节，响应不暴露路径 |
| 删除会话时看板写失败 | 会话删除仍成功，记录 warning |

### 5.4 服务端集成

在 `create_app` 中：

```python
sessions.load()
board = board_store or BoardStore()
board.load()
board.reconcile(sessions)
runner = AgentRunner()
```

- `app.state.board = board`
- `create_app(store=None, board=None)` 增加可选 `board` 参数，方便测试注入临时目录
- `DELETE /api/sessions/{id}` 成功后调用 `board.unlink_session(id)`
- `/api/health` 可选增加 `"tasks": len(board.list_all())`，便于观察

### 5.5 请求模型

用 Pydantic 定义：

- `CreateTaskRequest`
- `PatchTaskRequest`
- 描述/标题长度用 `Field` 限制
- 状态/优先级在 Store 层再校验，保证 CLI/测试直接调用也不会写坏数据

---

## 6. 前端设计

### 6.1 导航模型

当前 `PanelId` 只控制侧栏内容。新增：

```ts
export type AppView = "chat" | "board";
export type NavId = "chat" | "board" | "settings";
```

`IconRail` 的导航项改为：

| 导航 | 行为 |
|---|---|
| Chat | `view="chat"`，侧栏显示 `sessions` |
| Board | `view="board"`，侧栏显示 `board` 筛选面板 |
| Settings | 只切换侧栏到 `settings`，不改变主区域 |

这样可以保持“左侧导航一级视图”，同时不破坏现有 Settings 的行为。

### 6.2 页面结构

`page.tsx` 保持组合根角色：

```tsx
const [view, setView] = useState<AppView>("chat");
const board = useBoard({ enabled: view === "board" });
```

主区域条件渲染：

```tsx
{view === "chat" ? (
  <>
    <Transcript ... />
    <Composer ... />
  </>
) : (
  <BoardView ... />
)}
```

- 切换视图时 **不卸载 SSE 连接**，聊天状态和草稿保留
- `TitleBar` 标题：聊天显示会话标题，看板显示“任务看板”
- `TitleBar` 的新建按钮：聊天 → 新建对话；看板 → 新建任务
- 从看板打开对话时：`setView("chat")` + `selectSession(sessionId)`

### 6.3 新增组件

| 文件 | 职责 |
|---|---|
| `components/BoardView.tsx` | 看板头部、列、拖拽容器、空状态 |
| `components/TaskCard.tsx` | 单张任务卡、运行指示、快捷操作 |
| `components/TaskEditor.tsx` | 新建/编辑弹窗，复用 `Modal` |
| `components/BoardPanel.tsx` | 侧栏：搜索、隐藏已完成、新建任务、列计数 |
| `lib/useBoard.ts` | 数据获取、轮询、乐观更新、CRUD |
| `components/Icons.tsx` | 增加 `BoardIcon` 等 |

### 6.4 看板布局

```
┌─────────────────────────────────────────────────────────────┐
│ 任务看板    [搜索] [隐藏已完成]              [+ 新建任务]    │
├──────────┬──────────┬──────────┬──────────┬─────────────────┤
│ 待整理 3 │ 待办 2   │ 进行中 1 │ 待验收 0 │ 已完成 8        │
│ ┌──────┐ │ ┌──────┐ │ ┌──────┐ │          │ ┌──────┐        │
│ │ card │ │ │ card │ │ │ card │ │          │ │ card │        │
│ └──────┘ │ └──────┘ │ └──────┘ │          │ └──────┘        │
│          │          │          │          │                 │
└──────────┴──────────┴──────────┴──────────┴─────────────────┘
```

- 列横向排列，窄屏横向滚动
- 列头：名称、数量、`+` 按钮（在该列新建）
- 列体：卡片列表 + 空列占位
- 卡片拖拽时显示插入位置指示线
- 整个看板高度撑满主区域，列内独立滚动

### 6.5 任务卡内容

- 标题（最多两行，超出省略）
- 描述预览（一行，可选）
- 优先级徽标（颜色）
- 标签（最多显示 3 个，其余 `+N`）
- 关联会话状态：
  - 未关联：灰色“未开始”
  - 已关联且空闲：对话图标 + 相对时间
  - 已关联且 `busy`：旋转指示 + “运行中”
- 悬停操作：编辑、打开对话、删除
- 点击卡片主体：打开 `TaskEditor` 详情/编辑弹窗
- 卡片上的“打开对话”按钮：直接进入聊天

### 6.6 拖拽方案

**桌面端**：原生 HTML5 DnD

- 卡片 `draggable`
- `dragstart` 写入 task id
- 列监听 `dragover` / `drop`
- 根据鼠标位置计算插入 index
- 放下后乐观更新本地顺序，再 `PATCH /api/board/tasks/{id}`
- 服务端返回完整快照后替换本地状态
- 失败时回滚并 toast

**触屏/无障碍兜底**：卡片菜单提供“移动到 → 待办/进行中/待验收/已完成”

- 不引入 `@dnd-kit`
- 如果后续明确需要移动端顺滑拖拽，再评估 `@dnd-kit/core` + `@dnd-kit/sortable`

### 6.7 新建/编辑弹窗

复用现有 `Modal`：

- 标题（必填，自动聚焦）
- 描述（textarea）
- 状态（select）
- 优先级（select 或分段按钮）
- 标签（逗号分隔输入，保存时去重/trim）
- 关联会话（只读展示 + “打开对话”）
- 底部：取消、保存；编辑态额外提供删除

交互细节：

- 保存中禁用按钮
- 标题为空时行内提示，不发请求
- 删除使用 `ConfirmDialog`，文案明确“只删除任务，对话会保留”
- 关闭弹窗不做未保存拦截，保持轻量

### 6.8 状态管理与乐观更新

`useBoard` 负责：

```ts
{
  snapshot,
  loading,
  error,
  refresh,
  createTask,
  updateTask,
  deleteTask,
  moveTask,
  ensureSession,
  filter,
  setFilter,
  hideDone,
  setHideDone
}
```

策略：

- `GET /api/board` 成功后整体替换 `snapshot`
- 所有变更接口返回完整快照，前端直接替换，避免本地排序逻辑和服务端不一致
- 拖拽先本地乐观移动，失败回滚
- 有变更请求在飞行中时暂停轮询覆盖，避免“拖到一半被轮询打回去”
- 轮询发现 `updated_at` 未变则跳过 `setState`

### 6.9 轮询与刷新

- 只在 `view === "board"` 时启用
- 每 5 秒 `GET /api/board`
- `document.hidden` 时暂停，`visibilitychange` / `window.focus` 时立即刷新
- 聊天页收到 `run.completed` / `run.error` 时，如果看板已有数据，调用 `board.refresh()`
- 发送消息后也主动刷新一次，让卡片尽快显示“运行中”

### 6.10 与聊天/会话的集成

**打开任务对话**

1. 任务有 `session_id` 且会话存在 → 直接 `selectSession(session_id)`
2. 没有 → `POST /api/board/tasks/{id}/session`
3. 服务端创建 session：
   - 标题 = 任务标题（截断到 200）
   - model/harness 用服务默认值
   - 写回 `session_id`
4. 前端 `refreshSessions()` + `selectSession()` + `setView("chat")`

**会话创建后的规则**

- 会话标题只在创建时从任务复制一次，之后互不同步
- 用户可以在聊天里切换 model；harness 仍遵守现有规则：首条消息前可选，之后锁定
- 任务卡片的“运行中”来自 session 的 `busy`，不是任务自身状态

**删除会话**

- 侧栏删除会话后，服务端 `board.unlink_session(session_id)`
- 任务保留，卡片显示“未关联对话”
- 再次点击“打开对话”会创建新会话

**删除任务**

- 默认只删任务，保留会话
- 如果用户同时想删会话，本期让用户去侧栏删；后续再加复选框

### 6.11 侧栏 BoardPanel

看板视图下，侧栏显示：

- 搜索框（按标题/描述/标签过滤）
- “隐藏已完成”开关
- 新建任务按钮
- 各列数量小计

筛选状态放在 `useBoard` 或 `page.tsx`，由 `BoardPanel` 和 `BoardView` 共享。

### 6.12 i18n

所有文案进 `lib/i18n.tsx`，新增键至少包括：

- `boardTitle`、`boardNewTask`、`boardSearch`
- `boardColumnBacklog/Todo/Doing/Review/Done`
- `boardPriorityLow/Normal/High/Urgent`
- `boardTags`、`boardDescription`、`boardTitleField`
- `boardOpenConversation`、`boardCreateConversation`
- `boardUnlinked`、`boardRunning`、`boardLastActive`
- `boardDeleteTask`、`boardDeleteTaskConfirm`
- `boardMoveTo`、`boardHideDone`
- `boardEmpty`、`boardEmptyColumn`
- `boardSaveFailed`、`boardLoadFailed`

`MessageKey` 由 `en` 推导，新增英文键后 TypeScript 会强制中文补全。

### 6.13 样式

在 `app/globals.css` 增加：

- `.board` / `.board-head` / `.board-toolbar`
- `.board-columns` / `.board-column` / `.board-column-head` / `.board-cards`
- `.task-card` / `.task-card.dragging` / `.task-card.drop-target`
- `.task-meta` / `.task-priority-*` / `.task-tags`
- `.board-empty`
- 响应式：`@media (max-width: 860px)` 下列宽缩小、侧栏浮层保持现有行为

全部复用现有设计 token：`--bg`、`--surface`、`--surface-subtle`、`--border`、`--accent`、`--success`、`--warning`、`--error`、`--radius-*`。

---

## 7. 关键流程

### 7.1 新建任务

1. 点击“新建任务”或某列 `+`
2. 打开 `TaskEditor`，状态预填
3. `POST /api/board/tasks`
4. 返回快照，卡片出现在对应列
5. 刷新后仍在

### 7.2 拖拽

1. 拖起卡片，列显示插入指示
2. 放下 → 本地乐观更新
3. `PATCH { status, position }`
4. 服务端归一化并返回快照
5. 前端替换；失败则回滚 + toast

### 7.3 打开对话

1. 点击卡片“打开对话”
2. 无 session → 创建并关联
3. 切换到聊天视图并选中该会话
4. 用户发送消息，卡片在 5 秒内显示“运行中”
5. 运行结束后卡片恢复空闲，用户手动拖到“待验收/已完成”

### 7.4 删除联动

- 删任务：默认保留会话
- 删会话：任务保留，`session_id` 置空
- 服务重启：看板加载后清理失效链接

### 7.5 多标签页

- 本期 last-write-wins
- 轮询会让另一个标签页在 5 秒内看到变化
- 不做 ETag/冲突提示，避免过度设计

---

## 8. 边界与异常

| 场景 | 处理 |
|---|---|
| 标题为空/超长 | 400，前端行内提示 |
| 描述/标签超限 | 400，前端提示 |
| 非法 status/priority | 400 |
| 任务不存在 | 404，前端刷新看板 |
| 拖拽时任务已被另一标签页删除 | PATCH 404 → 回滚 + 刷新 |
| 关联会话被删除 | 启动/接口返回时 `session: null`，卡片显示“未关联” |
| 看板文件损坏 | 备份 `.corrupt-*`，空看板启动，日志 warning |
| 写盘失败 | 500，内存保持旧状态，前端 toast |
| 会话创建后未发消息 | 会话本身不落盘（现有规则），任务仍保留；重启后链接被清理 |
| agent 读到/写坏 `board.json` | 加载时校验 + 损坏备份；本期不做路径隔离 |
| 大量任务 | 本期不做虚拟滚动；几百张卡片以内可接受 |

---

## 9. 测试与验收

### 9.1 后端测试 `tests/webui/test_board.py`

- 创建任务并持久化，重新加载后还在
- 更新标题/描述/状态/优先级/标签
- 同列移动、跨列移动、插入指定位置
- 移动后 position 连续且无重复
- 删除任务
- 非法标题/状态/优先级/标签被拒绝
- 损坏文件 → 空看板 + `.corrupt-*` 备份
- `unlink_session` 清理链接
- `reconcile` 清理不存在的 session
- 写盘失败时内存不脏（可用 mock 模拟）
- 可选：`TestClient` API 测试（`tests/webui/test_auth.py` 已有 TestClient 用法）

### 9.2 前端验证

- `npm run typecheck`
- `npm run build`
- 手动检查：
  - 新建/编辑/删除任务
  - 同列/跨列拖拽，空列放置
  - 刷新页面和重启服务后顺序保持
  - 打开对话、创建会话、运行中指示
  - 删除会话后任务变为未关联
  - 中英文切换、明暗主题
  - 窄屏布局、触屏“移动到”菜单
  - 聊天视图无回归

### 9.3 验收标准

- [ ] 看板是一级导航，布局与现有 UI 一致
- [ ] 任务可创建、编辑、删除、拖拽，重启后状态和顺序不丢
- [ ] 任务可关联会话，能从卡片进入聊天
- [ ] 卡片能反映关联会话的运行状态
- [ ] 删除会话后任务不丢，链接自动清理
- [ ] 所有文案支持 en/zh
- [ ] 不影响现有聊天、SSE、认证、重启功能
- [ ] 后端测试通过，前端 typecheck/build 通过

---

## 10. 实施顺序

### 阶段 1：后端基础

- `storage.board_path()`
- `webui/board.py`：数据模型、校验、排序、原子读写
- `tests/webui/test_board.py`
- 先不接 HTTP，保证 Store 行为正确

### 阶段 2：HTTP API

- `webui/server.py`：请求模型、5 个路由
- 会话删除联动、启动 reconcile
- API 测试

### 阶段 3：前端基础

- `types.ts`、`useBoard.ts`
- `IconRail` / `Sidebar` / `page.tsx` 导航与视图切换
- `BoardView` 只读展示 + 列布局

### 阶段 4：交互

- `TaskEditor` 新建/编辑/删除
- `TaskCard` 操作
- 拖拽 + 菜单兜底
- 筛选、隐藏已完成

### 阶段 5：集成与收尾

- 打开对话、创建会话、运行状态刷新
- i18n、CSS、主题、窄屏
- 更新 `docs/webui.md`
- 全量 typecheck/build + 手动 QA

---

## 11. 风险与取舍

| 风险 | 处理 |
|---|---|
| 看板和会话两套标题/状态造成混淆 | 明确职责：任务管“做什么、到哪一步”，会话管“聊天记录”；标题只复制一次 |
| 单文件写入冲突 | 单用户假设 + 内存锁 + 原子写；后续多用户再换 SQLite |
| 原生 DnD 触屏不可用 | 卡片菜单“移动到”兜底；确认需要顺滑触屏再上 dnd-kit |
| 轮询带来延迟 | 5 秒可接受；运行结束主动刷新；后续可加全局 SSE |
| 字段膨胀 | 本期固定 5 列、固定字段；自定义列/字段不做 |
| agent 文件工具能访问 board.json | 与现有 sessions 文件同等对待；加载校验 + 损坏备份，后续可加路径保护 |
| 删除会话导致任务链接失效 | 服务端主动 unlink + 启动 reconcile |

---

## 12. 需要你确认的默认项

我按以下默认值设计，如果有不同意见可以直接改：

1. **固定 5 列**：待整理 / 待办 / 进行中 / 待验收 / 已完成
2. **保留 `priority` 和 `tags`**（想更简可以去掉）
3. **1 任务 ↔ 1 会话**，首次打开对话时懒创建
4. **删除任务默认保留会话**
5. **单看板**，存 `workspace/board.json`
6. **原生拖拽 + “移动到”菜单兜底**，不引入 dnd-kit
7. **不做自动状态流转**：运行开始/结束只改卡片指示，不改列
