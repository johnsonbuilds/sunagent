# SunAgent WebUI 前端架构 — 路由、状态与迁移方案

> 状态：设计稿，待评审后实施。
> 目标不是“把 SPA 改成多页面”，而是让 URL、状态和页面边界清晰，
> 新增文件视图、可观测性、定时任务等页面时不需要继续往一个组件里堆状态。

## 1. 目标

- **URL 是页面位置的唯一事实来源**：刷新、深链、前进/后退都可用。
- **常驻 Shell**：标题栏、导航、侧栏不随页面切换重建。
- **状态分层**：服务端数据和跨路由状态放进 Provider，页面只保留自己的临时 UI 状态。
- **新增页面成本固定**：改路由表 + 写一个 view，导航/标题/侧栏自动生效。
- **不引入 Node 运行时**：继续 `output: "export"` + FastAPI 静态托管 + Basic Auth。
- **聊天流不中断**：切到看板/文件页再回来，SSE 和会话状态仍在。

## 2. 非目标

- 不做 SSR / SEO / 公开页面。
- 不引入 react-router、zustand、SWR/React Query（当前规模不需要，后续可局部替换）。
- 不做按会话 id 的路径路由（`/chat/{id}`）；子状态统一用查询参数。
- 本期不把任务详情/编辑弹窗升级成独立路由。
- 本期不做按路由的代码分割；等文件树/图表库真正进来再按需加 `next/dynamic`。

## 3. 当前问题

`webui/frontend/app/page.tsx` 是唯一路由，也是组合根：

- `view` / `activePanel` 是内存状态，没有 URL、深链和前进后退。
- 会话列表、聊天记录、SSE、看板快照、弹窗、重启轮询、主题全部在同一个
  865 行组件里；新增页面只能继续加 `useState` 和条件渲染。
- `useBoard` 只在看板视图启用，离开看板就停止轮询。
- `app/page.tsx` 卸载时 SSE 断开；未来任何跨页面跳转都会丢聊天状态。

## 4. 架构总览

```
URL (pathname + query)
        │
        ▼
Next App Router（catch-all + generateStaticParams）
        │
        ▼
(console)/layout.tsx  →  ConsoleLayout（常驻）
        │
        ├── Providers: Theme / Toast / RuntimeConfig / Sessions / Board
        │
        └── AppShell: TitleBar + IconRail + Sidebar + <children/>
                │
                └── ConsoleView: 按路由表渲染 ChatView / BoardPage / ...
```

和之前 Vue 项目的对应关系：

| Vue | 这里 |
|---|---|
| `routes` 数组 | `lib/routes.ts` |
| `<router-view>` | `ConsoleView` + `lib/views.tsx` |
| layout 组件 | `(console)/layout.tsx` + `AppShell` |
| Pinia store | 各 Provider |
| `<keep-alive>` | `SessionsProvider` 持有 SSE 和会话状态 |

## 5. 目录结构

```
webui/frontend/
  app/
    layout.tsx                 # html / i18n / theme（保持现状）
    (console)/
      layout.tsx               # 挂 ConsoleLayout，跨页常驻
      page.tsx                 # "/" → <ConsoleView path="/" />
      [...slug]/page.tsx       # 其余静态路径 → <ConsoleView path=... />
    not-found.tsx              # 客户端 404（P3）
  components/
    ConsoleLayout.tsx          # Providers + AppShell 组合
    ConsoleView.tsx            # 路由表 → 视图
    shell/
      AppShell.tsx             # 标题栏 + 导航 + 侧栏 + 主区域
      TitleBar.tsx             # 从 page.tsx 迁入
      IconRail.tsx             # 从 state 改为 pathname 驱动
      Sidebar.tsx              # panel 由路由表决定
      panels.tsx               # PanelId → 组件映射
    evaluation/                # 评测页图表与抽屉（无第三方图表库）
      EvaluationKpis.tsx       # KPI 带：ratioCard 支持 invert（越低越好）
      EvaluationFunnel.tsx
      BarList.tsx              # value 可为 null，渲染 —
      StackedBars.tsx
      Histogram.tsx
      TrendChart.tsx
      TaskTable.tsx            # 列与 SORTABLE 一一对应
      TaskDetailDrawer.tsx     # Esc 关闭
      RunTimeline.tsx
      DiagnosisPanel.tsx
      Section.tsx
    ...
  views/
    ChatView.tsx
    BoardPage.tsx
    SettingsPage.tsx
    FilesPage.tsx              # P3 占位
    EvaluationPage.tsx         # 评测页（/evaluation）
    SchedulesPage.tsx          # P3 占位
  lib/
    routes.ts                  # 纯数据路由表（server-safe）
    views.tsx                  # RouteId → view + useChrome
    stores/
      theme.tsx
      toast.tsx
      runtime-config.tsx
      sessions.tsx
      board.tsx
    useRestart.ts
    useEvaluation.ts           # 评测页数据：KPI 立即刷新、任务列表独立防抖
```

## 6. 路由设计

### 6.1 路由表

路由表只放纯数据，**不 import 任何组件**，这样 `generateStaticParams`
可以在服务端安全使用：

```ts
// lib/routes.ts
import type { MessageKey } from "@/lib/i18n";

export type RouteId =
  | "chat" | "board" | "files" | "evaluation" | "schedules" | "settings";

export type NavIconId =
  | "chat" | "board" | "files" | "evaluation" | "schedules" | "settings";

export type PanelId = "sessions" | "board" | null;

export interface RouteDef {
  id: RouteId;
  path: string;
  nav?: { labelKey: MessageKey; icon: NavIconId };
  panel: PanelId;
  titleKey?: MessageKey;
}

export const ROUTES: RouteDef[] = [
  { id: "chat", path: "/", panel: "sessions",
    nav: { labelKey: "navChat", icon: "chat" } },
  { id: "board", path: "/board", panel: "board", titleKey: "boardTitle",
    nav: { labelKey: "navBoard", icon: "board" } },
  // 占位页先不放侧栏面板；真实页面接入时再补 PanelId 和面板组件。
  { id: "files", path: "/files", panel: null, titleKey: "navFiles",
    nav: { labelKey: "navFiles", icon: "files" } },
  { id: "evaluation", path: "/evaluation", panel: null,
    titleKey: "navEvaluation",
    nav: { labelKey: "navEvaluation", icon: "evaluation" } },
  { id: "schedules", path: "/schedules", panel: null,
    titleKey: "navSchedules",
    nav: { labelKey: "navSchedules", icon: "schedules" } },
  { id: "settings", path: "/settings", panel: null, titleKey: "settings",
    nav: { labelKey: "settings", icon: "settings" } },
];

export function matchRoute(path: string): RouteDef | null {
  return ROUTES.find((route) => route.path === path) ?? null;
}
```

图标是 `NavIconId` 字符串，由 `IconRail` 映射到具体组件，避免路由表
把 client 组件带进服务端构建。

### 6.2 catch-all + 静态生成

所有页面路径在构建期已知，因此用**一个 catch-all 页面**承接除 `/`
以外的路由：

```tsx
// app/(console)/[...slug]/page.tsx
import { notFound } from "next/navigation";
import ConsoleView from "@/components/ConsoleView";
import { matchRoute, ROUTES } from "@/lib/routes";

export const dynamicParams = false;

export function generateStaticParams() {
  return ROUTES
    .filter((route) => route.path !== "/")
    .map((route) => ({ slug: route.path.slice(1).split("/") }));
}

export default async function Page(
  { params }: { params: Promise<{ slug: string[] }> },
) {
  const { slug } = await params;
  const path = `/${slug.join("/")}`;
  if (!matchRoute(path)) notFound();
  return <ConsoleView path={path} />;
}
```

`app/(console)/page.tsx` 只服务 `/`：

```tsx
import ConsoleView from "@/components/ConsoleView";

export default function Page() {
  return <ConsoleView path="/" />;
}
```

效果：

- 每个路径生成自己的 HTML（`out/board.html`、`out/files.html` 等），
  刷新和深链首屏正确，不会先闪聊天界面。
- 页面切换直接用 `next/link` / `next/navigation`，前进后退、预取、
  新标签页由 Next 负责。
- 未知路径天然 404，不需要 SPA fallback 把 404 吞成 200。
- 新增页面只改 `ROUTES`，不需要新增路由文件。

### 6.3 URL 与查询参数

页面路径固定，选中态用查询参数：

| 页面 | URL |
|---|---|
| 聊天 | `/?session=<session_id>` |
| 看板 | `/board`，未来 `?task=<task_id>` |
| 文件 | `/files?path=<相对路径>` |
| 评测 | `/evaluation`（筛选与 Tab 是页内状态，不进 URL） |
| 定时任务 | `/schedules?task=<id>` |
| 设置 | `/settings` |

约定：

- 页面切换用 `router.push`，保留历史。
- 切换会话用 `router.replace`，避免后退键在几十个会话里倒退。
- 聊天导航链接带上当前 `?session=`，保证回到聊天时还是同一个会话。
- 刷新时没有 `session` 参数 → 选最新会话；没有会话 → 新建。
- 深链指向已删除的会话/文件 → toast + 回到默认视图。

**P2 需要一个 30 分钟的 spike**：在静态导出下验证
`<Link>` 跨页导航和仅改 query 的 `router.replace`。若 query 导航
触发多余请求或页面重挂，退化为 `lib/useQueryParam.ts`
（History API + `popstate`，约 50 行），页面路径导航仍交给 Next。

### 6.4 静态托管与 fallback

`next build` 产出 `out/index.html`、`out/board.html`、`out/board.txt`
等文件。FastAPI 的 `server.py` catch-all 增加两步：

1. 精确文件（保留现有逻辑，`_next`、`.txt`、图标都走这里）。
2. 无扩展名路径依次尝试 `<path>.html`、`<path>/index.html`。
3. 仍找不到 → 返回 `404.html`（带 404 状态码）。

`/api/*` 和 `/_next` 在 catch-all 之前注册，不受影响。这样普通
404 不会被伪装成 200，也不需要全站 SPA fallback。

### 6.5 导航与标题

- `IconRail` 的 active 由 `usePathname()` 匹配路由表得出。
- `AppShell` 监听 `Ctrl/Cmd+K`，触发当前页面的主操作
  （聊天：新会话；看板：新建任务）。
- `document.title` 在 `AppShell` 里按路由标题更新。

## 7. 状态分层

### 7.1 Provider 清单

```
ThemeProvider
  ToastProvider
    RuntimeConfigProvider          # /api/config，启动加载一次
      SessionsProvider             # 会话列表 + 当前会话 + SSE
        BoardProvider              # 看板快照 + 轮询 + 弹窗状态
          AppShell
```

| Provider | 职责 | 生命周期 |
|---|---|---|
| `ThemeProvider` | `data-theme`、localStorage | 应用级 |
| `ToastProvider` | toast 文案 / 类型（info · success · error）、计时器、渲染 toast（popover，位于浏览器 top layer，盖住 modal 弹框） | 应用级 |
| `RuntimeConfigProvider` | models / harnesses / defaults | 启动一次 |
| `SessionsProvider` | 会话列表、active session、segments、busy、SSE、模型/harness | 跨路由常驻 |
| `BoardProvider` | snapshot、filter、hideDone、mutation、editor/detail 弹窗状态 | 跨路由常驻 |

### 7.2 拆分规则

- **高频状态单独 context**：SSE 的 `segments`、`busy` 放进
  `ChatContext`；会话列表、标题放进 `SessionsListContext`。
  否则每个 token 都会让 Sidebar 跟着重渲染。
- Provider 只暴露“数据 + action”，不暴露裸 setter。
- 网络请求统一走 `lib/api.ts`。
- 页面私有状态（滚动、拖拽）留在 view 内；
  跨页面要保留的才进 Provider。
- Provider 不 import view 组件；`lib/views.tsx` 是唯一的
  RouteId → view 映射处，避免循环依赖。

### 7.3 聊天状态与 SSE

- `activeId` 由 `?session=` 决定；Provider 内存中的 `activeId`
  在跨页面导航时保留。
- SSE 连接在 `SessionsProvider` 的 effect 中建立，按 `activeId`
  重建；`seq` 去重、`Last-Event-ID` 重连逻辑从 `page.tsx` 原样迁移。
- `ChatView` 卸载不影响流；回到聊天时从 store 重新渲染。
- 切换会话时清空 segments、重置 pinned、重新拉详情、重连 SSE。
- 输入框草稿按会话存在 `SessionsProvider` 里，并镜像到
  `localStorage` 的 `sunagent-draft:<sessionId>`：切页、刷新后
  回来还在，发送或点清空图标就删掉。
- 会话增删改后刷新列表；`run.completed` 时刷新列表并递增
  `runCompletedSeq`，供看板做即时刷新。

### 7.4 看板轮询

按约定**不在看板页也保持轮询**，采用分级频率：

| 条件 | 间隔 |
|---|---|
| `pathname === "/board"` 且页面可见 | 5s |
| 其他页面且页面可见 | 25s |
| `document.hidden` | 暂停 |
| 窗口重新聚焦 / 页面重新可见 | 立即刷新一次 |
| 进入 `/board` | 立即刷新一次 |
| `runCompletedSeq` 变化 | 立即刷新一次 |

保留现有 `pending` / `epoch` 防抖和 `JSON.stringify` 比较，避免
旧轮询覆盖乐观更新。任务数量增长后再考虑后端 revision/ETag。

### 7.5 页面 chrome

```ts
interface Chrome {
  title: string;
  status?: StatusState;
  primary?: { labelKey: MessageKey; onClick: () => void };
}
```

`lib/views.tsx` 的注册项可以带 `useChrome`：

```tsx
const VIEWS: Record<RouteId, {
  View: ComponentType;
  useChrome?: () => Chrome;
}> = {
  chat:  { View: ChatView,  useChrome: useChatChrome },
  board: { View: BoardPage, useChrome: useBoardChrome },
  files: { View: FilesPage },
  // ...
};
```

- 聊天：标题取会话标题，状态取 busy/error，主操作是新会话。
- 看板：静态标题，主操作是新建任务。
- 其余页面：路由表 `titleKey`，无主操作。
- 看板的新建任务/详情/删除弹窗状态放 `BoardProvider`，
  这样标题栏按钮和 `Ctrl+K` 能直接触发。

## 8. 新增页面流程

以后加“文件视图”这类页面：

1. `lib/routes.ts` 加一行（id、path、nav、panel、titleKey）。
2. `views/FilesPage.tsx` 写页面；需要标题/主按钮就导出 `useChrome`。
3. `lib/views.tsx` 注册 view。
4. 需要侧栏就写 `components/shell/panels/FilesPanel.tsx`，
   在 `panels.tsx` 注册。
5. `i18n.tsx` 加文案 key。
6. 数据请求写 `lib/stores/files.tsx` 或 `lib/hooks/useFiles.ts`。
7. `npm run build` 自动生成对应 HTML。

## 9. 迁移计划

每一步都保持 `npm run typecheck` 和 `npm run build` 通过。

### P1 状态分层 + Shell 抽离（0.5–1 天）

行为不变，只搬代码：

- 新建 `lib/stores/theme.tsx`、`toast.tsx`、`runtime-config.tsx`、
  `sessions.tsx`、`board.tsx`，把 `page.tsx` 的状态和 action 迁进去。
- 新建 `components/shell/AppShell.tsx`，把 TitleBar / Sidebar /
  侧栏开合 / 窄屏判断 / 快捷键 / toast 容器放进去。
- `SessionsPanel`、`BoardPanel`、`SettingsPanel` 改为直接消费 store，
  不再从 `page.tsx` 透传 props。
- 看板的 editor/detail/delete 弹窗状态进 `BoardProvider`。
- `page.tsx` 暂时仍用 `view` 状态切换聊天/看板，便于对照回归。

验收：功能与现在一致；聊天流、看板拖拽、重启、主题、i18n 正常。

### P2 路由落地（0.5–1 天）

- 新增 `lib/routes.ts`、`lib/views.tsx`、`components/ConsoleView.tsx`、
  `components/ConsoleLayout.tsx`、`app/(console)/` 下的 layout 和两个 page。
- 抽出 `views/ChatView.tsx`、`views/BoardPage.tsx`、`views/SettingsPage.tsx`。
- `IconRail` / `Sidebar` 改为路由表驱动；导航用 `next/link`；
  `selectSession` 改为 `router.replace("/?session=...")`。
- 删除旧的 `view` / `activePanel` state，避免 URL 和内存状态双真相。
- `server.py` 增加 `.html` / `index.html` fallback。
- 先跑 6.3 的静态导出 spike，确认 query 导航行为。

验收：直接打开/刷新每个路径正常；前进后退正常；聊天中切到看板
再回来，消息和后台流都在；`Ctrl+K` 按页面生效；404 正常。

### P3 占位页与收尾（2–4 小时）

- `FilesPage`、`ObservabilityPage`、`SchedulesPage` 占位：
  空状态 + 说明文案；真实数据 hook 随各功能接入时再加。
- `app/not-found.tsx` + FastAPI `404.html` fallback。
- 文档更新：`docs/webui.md` 第 7 节、`docs/board.md` 6.1/6.2
  改为指向本文。
- 可选：按 view 加 `next/dynamic`，控制首屏 bundle。

之后每个真实页面（含后端 API）预计 2–4 小时前端部分。

## 10. 手动回归清单

前端没有自动化测试，迁移后按下面清单走一遍：

**聊天**

- 启动无会话 → 自动新建；有会话 → 选中最新。
- 发消息、流式输出、reasoning、工具块展开、停止、错误提示。
- SSE 断线重连；刷新后 transcript 回放。
- 切换模型/harness；首条消息后 harness 锁定。
- 新建、重命名、删除会话；删除当前会话后的跳转。

**看板**

- 加载、筛选、隐藏 done、暂停/恢复自动执行。
- 列内/跨列拖拽；拖拽后刷新顺序保持。
- 新建/编辑/删除任务；评论并重新排队。
- 任务详情、打开关联对话；删除会话后任务解绑。
- 在聊天页停留时看板轮询仍在跑；进入看板立即刷新。

**路由**

- 直接访问 `/`、`/board`、`/settings`、三个占位页。
- 刷新、前进、后退、新标签页打开。
- 未知路径 404。
- 深链到已删除会话/文件。
- 切换页面后聊天草稿、会话、流状态符合预期。

**其他**

- 主题切换、中英文切换、窄屏侧栏遮罩。
- `Ctrl+K` 在聊天/看板的行为。
- Basic Auth 下直接访问子路径。
- 重启服务后页面恢复、URL 保留。

## 11. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| Context 高频更新 | 每个 token 触发大面积重渲染 | 拆分 `ChatContext` / `SessionsListContext`，面板 memo |
| 所有 view 进同一个 chunk | 首屏随页面增多变大 | 先接受；需要时 `next/dynamic` 按 view 懒加载 |
| 静态导出下的 query 导航 | 可能多余请求或页面重挂 | P2 先 spike；必要时换 `useQueryParam` |
| 常驻看板轮询 | 多标签页/任务多时浪费 CPU | 5s/25s 分级、隐藏暂停、后续加 revision |
| 无前端测试 | 迁移回归靠人工 | 分阶段提交 + typecheck/build + 第 10 节清单 |
| 文档漂移 | 后续维护者按旧结构理解 | P3 更新 webui.md / board.md，本文为入口 |
| View 注册与路由表不一致 | 路由存在但没有 view | `views.tsx` 用 `Record<RouteId, ...>`，缺项直接类型报错 |

## 12. 决策记录

| 决策 | 选择 | 理由 |
|---|---|---|
| 路由形态 | 固定 path + query 子状态 | 静态导出下路径可预生成，不需要手写 router |
| 路由实现 | catch-all + `generateStaticParams` | 一个入口生成全部静态页，新增页面不改路由文件 |
| 聊天状态 | 进 `SessionsProvider` | 跨页保活 SSE，ChatView 可安全卸载 |
| 看板轮询 | 常驻 + 5s/25s 分级 | 满足“离开看板仍更新”，同时控制开销 |
| 设置页 | 独立 `/settings` | 一个导航项对应一个 URL |
| 状态库 | React Context | 当前规模不引第三方；边界清晰后可局部替换 |
| 子状态编码 | 查询参数 | 避免运行时动态路由和路径匹配器 |
| 首期新页面 | 三个占位页 | 先跑通导航/深链，再逐页接后端 |
