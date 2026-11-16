# Files 页面 — 工作区文件浏览与未提交变更

> 状态：v1 与 v1.1 均已实现（第 12 节）。
> 范围：`/files` 页面 + `webui/workspace.py` + 4 个只读 API。
> v1 非目标：写操作（编辑 / stage / commit / discard）、全局搜索、语法高亮、
> 图片与 PDF 预览、定时轮询。
>
> 实现记录（2026-11）：
> `webui/workspace.py`、`tests/webui/test_workspace.py`（41 项）、
> `webui/frontend/views/FilesPage.tsx` 与 `components/files/*`、`lib/useFiles.ts`、`lib/files.ts`。
> 两处与设计不同的实现细节：
> 1. `git status` 没有 `--relative`，改为在仓库根运行并把输出路径换算回 workspace；
> 2. `diff` / `--numstat` 使用 `-M20%`，让「重命名后又编辑」仍能配对成一条 rename diff
>    （默认 50% 会拆成删除 + 新增，丢失行数）。

## 1. 目标

- `/files` 页面顶部两个 tab：**文件**、**未提交变更**。
- 文件 tab：首屏只列工作区根目录一层；点目录才请求下一级；点文件在右侧预览文本。
- 变更 tab：树形展示未提交文件，按目录分组，**目录默认折叠**；点文件查看 unified diff。
- 与 agent 的文件工具看到同一个根：`AGENT_RUNTIME_WORKSPACE`（未配置则 `cwd`）。
- 保持现有架构：Next.js 静态导出 + FastAPI 托管 + Basic Auth，v1 前后端都不新增依赖
  （v1.1 的语法高亮会引入一个前端依赖，见第 12 节）。
- v1 **纯只读**：只读文件系统 + 只读 git 命令。

## 2. 决策记录

| 决策 | 结论 | 理由 |
|---|---|---|
| 浏览根 | `AGENT_RUNTIME_WORKSPACE`，未配置则 `cwd` | 与 `LocalWorkspace` / 文件工具完全一致，页面看到的就是 agent 能改的 |
| 读能力 | 只读，直读文件系统 | 不需要经过 agent 工具层；写操作涉及 index 锁、并发、审计，单独一期 |
| 文件树加载 | 懒加载，一次一层 | 仓库外还有 `jobs/`（21G）、`workspace/`（202M）等目录，全量扫描不可接受 |
| 变更展示 | 目录树，默认折叠 | 用户指定的交互；带「全部展开 / 折叠」补偿深路径 |
| 变更深度 | L2：列表 + 状态 + `+/-` + diff | 覆盖「看未提交改动」的核心需求 |
| 刷新 | 进入页面 + 切 tab + 手动刷新按钮 | 去掉轮询：数据变化低频且由用户驱动，避免后台反复 spawn git |
| 搜索 | v1 不做 | 懒加载树 + 全局搜索是两套数据模型，先不引入 |
| 依赖 | v1 不新增 npm / pip 依赖 | git 走子进程，diff 服务端解析，前端零依赖渲染；v1.1 另议（第 12.4 节） |
| 测试 | 新增 Python 单测；前端沿用 typecheck/build + 手动清单 | 项目当前没有前端测试框架，不为单页引入 |

## 3. 现状与约束

| 项 | 现状 |
|---|---|
| 前端 | Next 16 App Router + React 19，只有 next/react 两个依赖 |
| 构建 | `output: "export"` 静态导出，FastAPI 托管，没有 Node 常驻进程 |
| 路由 | `lib/routes.ts` + `app/(console)/[...slug]/page.tsx` 预生成；`/files` 已存在 |
| 页面注册 | `lib/views.tsx` 的 `Record<RouteId, ComponentType>` |
| 样式 | 单个 `app/globals.css`，CSS token + BEM 风格，没有现成 tab 组件 |
| 状态 | React Context（Theme / Toast / RuntimeConfig / Sessions / Board），页面私有状态留在 view |
| 后端 | `webui/server.py` 只有 sessions / board / config / restart / 静态托管，没有文件或 git API |
| 现成资产 | `src/agent_runtime/execution/local.py` 的 `LocalWorkspace` 有路径 containment 和 `read_file`/`list_dir` |
| 认证 | Basic Auth 中间件覆盖页面、API、SSE，新增端点自动受保护 |
| 规模 | git 跟踪 1238 个文件；排除 `.git/.venv/node_modules` 后磁盘约 4.3 万个文件 |

**为什么不用 `LocalWorkspace`**：它是面向模型工具的实现——错误以 dict 返回、没有 ignore 规则、
没有大小/二进制/敏感文件策略、root 不可注入。直接复用会把这些策略散到路由层。
本方案新建 `webui/workspace.py`，只重复约 4 行 root 解析（刻意与执行层解耦），
将来两边若要统一再抽公共 helper。

**为什么不用静态挂载**：`StaticFiles(directory=workspace)` 无法跑 git、无法过滤 ignore、
无法做大小与二进制策略，还会直接暴露 `.env` / `model.json`。

**为什么不用 SSE / 轮询**：git 状态和目录列表的变化是低频、用户驱动的。
路由切换会卸载并重挂 `FilesPage`（`ConsoleLayout` 常驻、page 替换），
「回到 files 页面」天然等于一次刷新；停留在页面时用刷新按钮。

## 4. 总体架构

```
FilesPage (client)
  ├─ 文件 tab
  │    ├─ GET /api/workspace/tree?path=src&ignored=0   → 单层条目
  │    └─ GET /api/workspace/file?path=src/a.py        → 文本预览
  └─ 变更 tab
       ├─ GET /api/workspace/changes                   → 扁平状态 entries
       └─ GET /api/workspace/diff?path=src/a.py        → 结构化 hunks

FastAPI (webui/server.py)
  └─ WorkspaceBrowser (webui/workspace.py)
       ├─ 路径 resolve + containment
       ├─ 忽略判定（git check-ignore，非 git 时内置兜底）
       ├─ 读文件（大小 / 二进制 / 截断 / 敏感文件）
       └─ git 子进程（status --porcelain=v2 / diff / numstat）

浏览根 = AGENT_RUNTIME_WORKSPACE 或 cwd
```

## 5. 后端设计

### 5.1 新模块 `webui/workspace.py`

```python
class WorkspaceBrowser:
    def __init__(self, root: Path | str | None = None) -> None:
        """root 显式传入时固定（测试用），否则每次请求从 env/cwd 解析。"""

    @property
    def root(self) -> Path: ...            # AGENT_RUNTIME_WORKSPACE 或 cwd，resolve 后
    @property
    def repo_root(self) -> Path | None:    # git rev-parse --show-toplevel；非 git 返回 None
    def list_dir(self, path: str, include_ignored: bool = False) -> dict: ...
    def read_file(self, path: str) -> dict: ...
    def changes(self) -> dict: ...
    def diff(self, path: str, old_path: str | None = None) -> dict: ...
```

内部辅助：

| 函数 | 说明 |
|---|---|
| `_resolve(path)` | `PurePosixPath` 拒绝绝对路径、`..`、NUL；`resolve()` 后必须 `is_relative_to(root)`；符号链接按真实路径判断，越界拒绝 |
| `_git(args, timeout)` | 固定参数数组、`-C <root>`、`--no-pager`、`GIT_OPTIONAL_LOCKS=0`、`LC_ALL=C`；不用 shell；status 超时 5s，diff 超时 10s |
| `_ignored_set(paths)` | 批量 `git -C <repo> check-ignore --stdin -z`；非 git 时用内置兜底 |
| `_parse_status(raw)` | porcelain v2 `-z` 解析 |
| `_parse_diff(text)` | unified diff → 结构化 hunks（带 old/new 行号） |
| `_decode_text(raw)` | 前 8KB 含 NUL → binary；UTF-8 严格解码失败 → binary |

### 5.2 根目录与路径

- `root`：`AGENT_RUNTIME_WORKSPACE` 优先，否则 `Path.cwd()`；`expanduser().resolve()`。
  显式传入的 `root`（测试）优先于环境变量。**每次请求现读**，改 env 不需要重启。
- API 路径统一为 **posix 相对路径**，`""` 表示根；URL 编码交给前端 `encodeURIComponent`。
- `repo_root`：`git rev-parse --show-toplevel`。workspace 是仓库子目录时也必须工作：
  git 命令统一在仓库根运行（`status` 的 porcelain 输出锚定仓库根，且它没有
  `--relative` 选项），再按 workspace 前缀过滤并换算成「相对 workspace root」。
- 非 git 目录：`changes()` 返回 `is_git:false`，不使用 5xx。

### 5.3 忽略与敏感文件

**忽略判定**（`ignored=0` 时隐藏）：

1. 主判定：`git check-ignore`，永久对齐 `.gitignore`（含子目录规则）。
2. 永久硬跳过：`.git/`、`.venv/`、`node_modules/`、`__pycache__/`、`.next/`。
   防止性能事故，也避免 `.git/config` 之类的内容被读到。
3. 兜底名单（始终生效）：上述 + `workspace/`、`jobs/`、`.outputs/`、`.scripts/`、`out/`、`dist/`。
   这些运行时目录即使有文件被 git 跟踪（本仓库的旧 `jobs/` 就是这样），默认也不列；
   「显示忽略文件」时可以展开。
4. `ignored=1` 时显示被忽略项，标记 `ignored:true` 并置灰；`hidden_ignored` 返回当前层隐藏的数量。

**敏感文件**（默认已被 gitignore 隐藏；开启显示后仍禁止读取）：

| 模式 | 处理 |
|---|---|
| `.env`、`.env.*` | 树里 `protected:true`；`file` 返回 403 |
| `model.json` | 同上（含真实 API key） |
| `*.pem`、`*.key`、`*.p12` | 同上 |
| `id_rsa*`、`.netrc`、`credentials*` | 同上 |

**读取限制**：

| 项 | 上限 | 越界行为 |
|---|---|---|
| 单目录条目 | 1000 | `truncated:true` |
| 预览原始读取 | 2 MiB | 截断到 512 KiB，并裁到行边界 |
| 预览行数 | 5000 | `truncated:true` |
| diff 原始文本 | 2 MiB | `truncated:true` |
| diff 行数 | 4000 | `truncated:true` |
| changes 条目 | 5000 | `truncated:true` |

### 5.4 git 命令

**状态**：

```
git -C <repo_root> status --porcelain=v2 --branch -z --untracked-files=all
```

（`status` 没有 `--relative`；先按仓库根取路径，再换算到 workspace。）

- X = index 状态，Y = worktree 状态 → `staged` / `unstaged` 两个布尔，可同时为真。
- 汇总单个 `status`，优先级：
  `conflicted > untracked > renamed > added > deleted > modified > typechange > copied`。
- `# branch.head` / `# branch.ab +N -M` → `branch` / `ahead` / `behind`；detached 时 `branch` 为短 hash。
- 重命名行（`2 R. ...`）带旧路径 → `old_path`。
- `--untracked-files=all` 把未跟踪目录展开成文件，否则树里只有一个目录名、无法看 diff。

**增删行数**：

```
git -C <repo_root> diff HEAD --numstat -z -M20%
```

- `-z` 下重命名格式为 `add\tdel\t\0old\0new\0`，单独解析。
- 未跟踪文件由服务端数行（上限 2 MiB），`additions=行数`、`deletions=0`。
- numstat 为 `-\t-` 或 diff 含 `Binary files ... differ` → `binary:true`，统计为 `null`。

**diff**：

```
git -C <repo_root> diff HEAD --no-color --no-ext-diff -M20% -U3 -- <repo_path> [<repo_old_path>]
```

- 传 `old_path` 才能在 rename 时正确配对。
- 未跟踪文件：直接读文件合成 `@@ -0,0 +1,N @@` 的全 add hunk，与普通 diff 共用解析器和渲染器。
- 已删除文件：`git diff HEAD -- <path>` 天然给出全 del。
- 冲突文件：容忍组合 diff 中的未知行，归为 `note`，UI 提示「冲突中，diff 仅供参考」。
- 二进制：`binary:true`、`hunks:[]`。
- v1 只展示「工作区 vs HEAD」的合并 diff；staged / unstaged 拆分留到后续。

**diff 解析器**：识别 `diff --git`、`---` / `+++`、`@@ -a,b +c,d @@`、`+` / `-` / 空格、
`\ No newline at end of file`；逐行维护 old/new 行号；metadata 不进入响应，控制 payload。

### 5.5 API 契约

所有端点位于 Basic Auth 之后，响应 `Cache-Control: no-store`；
错误统一 `{"detail": "..."}`（前端现有 `api()` 直接消费）。

#### `GET /api/workspace/tree`

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `path` | string | `""` | workspace 相对目录，`""` 为根 |
| `ignored` | bool | `false` | 是否显示被忽略项 |

```json
{
  "root": "/root/sunagent",
  "repo_root": "/root/sunagent",
  "is_git": true,
  "path": "src",
  "entries": [
    {
      "name": "agent_runtime",
      "path": "src/agent_runtime",
      "type": "dir",
      "size": null,
      "mtime": 1763000000.0,
      "ignored": false,
      "protected": false
    },
    {
      "name": "main.py",
      "path": "src/main.py",
      "type": "file",
      "size": 812,
      "mtime": 1763000000.0,
      "ignored": false,
      "protected": false
    }
  ],
  "hidden_ignored": 2,
  "truncated": false
}
```

- 排序：目录优先，再按 `name.lower()`。
- `type`：`dir | file | symlink`；目录的 `size` 为 `null`。
- `hidden_ignored` 只统计当前层。

#### `GET /api/workspace/file?path=...`

```json
{
  "path": "README.md",
  "size": 9503,
  "mtime": 1763000000.0,
  "binary": false,
  "truncated": false,
  "lines": 320,
  "encoding": "utf-8",
  "content": "# SunAgent\n..."
}
```

- `binary:true` → `content:null`、`lines:null`。
- 403 敏感文件；404 不存在；400 传入目录或路径非法。

#### `GET /api/workspace/changes`

```json
{
  "root": "/root/sunagent",
  "repo_root": "/root/sunagent",
  "is_git": true,
  "git_error": null,
  "branch": "main",
  "head": "4a0be6f",
  "upstream": null,
  "ahead": 0,
  "behind": 0,
  "entries": [
    {
      "path": "src/a.py",
      "old_path": null,
      "status": "modified",
      "staged": true,
      "unstaged": true,
      "binary": false,
      "additions": 3,
      "deletions": 1,
      "protected": false
    }
  ],
  "counts": {
    "total": 1,
    "modified": 1,
    "added": 0,
    "deleted": 0,
    "renamed": 0,
    "untracked": 0,
    "conflicted": 0
  },
  "truncated": false
}
```

`status` 枚举：`modified | added | deleted | renamed | copied | typechange | untracked | conflicted`。

#### `GET /api/workspace/diff?path=...&old_path=...`

```json
{
  "path": "src/a.py",
  "old_path": null,
  "status": "modified",
  "binary": false,
  "truncated": false,
  "hunks": [
    {
      "header": "@@ -10,6 +10,8 @@",
      "old_start": 10,
      "old_count": 6,
      "new_start": 10,
      "new_count": 8,
      "lines": [
        { "kind": "ctx", "old": 10, "new": 10, "text": "def run():" },
        { "kind": "add", "old": null, "new": 11, "text": "    # new" },
        { "kind": "del", "old": 11, "new": null, "text": "    # old" },
        { "kind": "note", "old": null, "new": null, "text": "\\ No newline at end of file" }
      ]
    }
  ]
}
```

`kind` 枚举：`ctx | add | del | note`。

v1 固定 3 行上下文。v1.1 会追加向后兼容的 `context` / `full` 参数用于「展开完整文件」，
响应形状不变，详见第 12.3 节。

### 5.6 错误码

| 场景 | 状态码 |
|---|---|
| 路径越界 / 参数非法（文件当目录、目录当文件） | 400 |
| 敏感文件 | 403 |
| 文件或目录不存在 | 404 |
| 非 git 仓库 | 200 + `is_git:false`，不是错误 |
| git 不存在 / 超时 / 非零退出 | 500，`detail` 带截断的 stderr |
| 二进制 / 超大 / 截断 | 200 + 标志位 |

### 5.7 路由与依赖注入（`webui/server.py`）

```python
def create_app(store=None, board=None, *, workspace: WorkspaceBrowser | None = None,
               enable_executor: bool = True) -> FastAPI:
    browser = workspace if workspace is not None else WorkspaceBrowser()
    app.state.workspace = browser
    ...
```

四个端点注册在 board 端点之后、静态 catch-all 之前。
测试通过 `create_app(..., workspace=WorkspaceBrowser(tmp))` 指向临时 git 仓库，
不触碰真实工作区；既有测试调用不受影响（默认参数）。

## 6. 前端设计

### 6.1 页面结构

```
<div class="files-page">
  <header class="files-head">
    <div class="files-tabs" role="tablist">
      [文件]  [变更 3]
    </div>
    <div class="files-actions">
      <span class="files-root">sunagent</span>
      <button 刷新 />
      <toggle 显示忽略文件 />        ← 仅文件 tab
    </div>
  </header>

  <div class="files-body">           ← flex:1; min-height:0
    <aside class="files-tree-pane">  文件树 / 变更树 </aside>
    <section class="files-view-pane"> 预览 / diff / 空态 </section>
  </div>
</div>
```

- 桌面端左树右预览；窄屏（`useShell().narrow`）选中文件后右侧变为整页抽屉，带返回按钮。
- `AppShell` 给 files 路由加 `main-files`（对齐现有 `.main-board { overflow:hidden }`），
  避免出现双层滚动条。
- tab 只在页面内部，不改 `TitleBar`。

### 6.2 URL 与历史

| URL | 含义 |
|---|---|
| `/files` | 默认「文件」tab，根目录，无选中 |
| `/files?tab=files&path=src` | 文件 tab，选中目录或文件 |
| `/files?tab=changes&path=src/a.py` | 变更 tab，打开该文件的 diff |

- 不传 `tab` 默认 `files`。
- 切 tab 用 `history.pushState`（后退可回上一个 tab）。
- 选中/打开文件或目录用 `history.replaceState`（避免后退键在几十个文件里倒退，和聊天 `?session=` 策略一致）。
- **不使用 `useSearchParams`**：静态导出 + prerender 有 Suspense 限制。
  照聊天页的做法读 `window.location.search` 并监听 `popstate`。
- **不使用 `router.replace` / `router.push` 写页内状态**：静态导出下 Next
  会先请求新 URL 的 RSC payload（`/files.txt?_rsc=…`）再重渲染；一旦该请求
  失败（旧页面 build id 不匹配、静态托管不提供 `.txt`、移动浏览器中断），
  Next 会退化成整页导航 —— 组件重挂、文件树从空开始重新拉根目录，表现为
  点击目录时整棵树闪成只有「Loading…」。直接写 history 不触发任何导航，
  页面本身已经按 `popstate` 恢复状态。

### 6.3 刷新契约（无轮询）

| 触发 | 行为 |
|---|---|
| 进入 / 重新回到 Files 页面（组件挂载） | 按 URL 加载当前 tab 数据；另外静默拉一次 changes，仅用于 tab 的变更计数徽标 |
| 切到「文件」tab | 使用已缓存的树，不重新请求（保留展开状态与滚动位置） |
| 切到「变更」tab | 拉一次 changes；若当前有打开的 diff，同步重拉 diff |
| 点刷新按钮 | 文件 tab：并发重拉所有已展开目录 + 当前选中文件；变更 tab：重拉 changes + 当前 diff。刷新时不清空已有内容，只显示轻量 loading |
| 展开目录 / 打开文件 | 按需请求，不做额外刷新 |
| 定时器 / 窗口 focus / run.completed | 不做 |

已知且可接受的行为：

- 停留在文件 tab 时 agent 改了文件，页面不会自动更新，需要点刷新。
- 变更徽标在进入页面时是准的；之后切到变更 tab 或点刷新才更新。
- 在 `/files` 上重复点击同一个导航项不会重挂，也不会刷新 —— 用刷新按钮。

### 6.4 文件 tab 交互

- 首次：`GET tree?path=""`，只渲染根目录。
- 点目录：首次点击拉取 `tree?path=<dir>`，写入缓存并展开；再次点击折叠（不重复请求）。
- 点文件：选中 → 右侧拉 `file`；同时 `history.replaceState` 更新 `?path=`。
- 目录行：chevron + folder 图标；文件行：file 图标 + 人类可读大小。
- `protected` 文件显示锁图标；点击只显示提示，不发内容请求。
- 空目录显示「空目录」；加载中显示骨架行；失败显示行内「重试」。
- 深链 `?path=src/a.py`：启动时逐级展开祖先目录（串行拉取），再选中文件。
- 「显示忽略文件」切换：清空树缓存，按新参数重拉根目录；同名目录保留展开状态。
- 刷新：重拉所有已展开层级 + 当前文件；已消失的目录收起并提示。

### 6.5 变更 tab 交互

- `changes` 返回扁平 entries，前端 `buildChangeTree()` 按路径分组。
- **默认全部目录折叠**：只渲染根级目录 + 根级文件；目录行右侧显示子树内变更文件数。
- 点目录展开/折叠；点文件在右侧拉 diff。
- 文件行：状态字母徽标（M / A / D / R / ? / U）+ `+3 −1` + 文件名；
  重命名显示 `old → new`。
- 顶部摘要：分支名、`ahead/behind`、变更总数、最后刷新时间；
  非 git 时显示「当前工作区不是 git 仓库」。
- 空态：「工作区干净，没有未提交的变更」。
- 提供「全部展开 / 全部折叠」按钮，补偿深路径下默认折叠的查找成本。
- diff 面板头部：路径、状态、staged/unstaged 标签、重命名的旧路径；
  「在文件 tab 中打开」按钮（删除文件不显示）。
- diff 正文：`ctx/add/del` 三色行 + 双列行号；二进制、截断、冲突分别给提示。
  默认 unified；split 与「展开完整文件」在 v1.1（第 12.3 节）。
- 变更 tab 的 `?path=` 表示当前打开的 diff 文件；从深链进入时自动展开祖先目录。

### 6.6 变更树构建

```ts
interface ChangeNode {
  name: string;
  path: string;              // "" 表示虚拟根
  type: "dir" | "file";
  count: number;             // 子树内变更文件数
  entry?: ChangeEntry;
  children: Map<string, ChangeNode>;
}
```

- 根节点收集所有 entries 的祖先目录链；目录默认不进入 `expanded`。
- 目录在前、按名称；文件按名称。
- 虚拟根不渲染，直接渲染根级 children。

### 6.7 数据 hooks（`lib/useFiles.ts`）

```ts
useFileTree({ includeIgnored }) => {
  dirs: Record<string, { entries: DirEntry[]; loading: boolean; error: string | null }>;
  expanded: Set<string>;
  loadDir(path: string): Promise<void>;   // 幂等 + 去重 + AbortController
  toggleDir(path: string): void;
  expandTo(path: string): Promise<void>;   // 深链：逐级加载祖先
  refreshVisible(): Promise<void>;
  reset(): void;
}

useFilePreview(path: string | null) => { data, loading, error, reload() }

useChanges() => { data, loading, error, refresh() }

useFileDiff(path: string | null, oldPath: string | null) =>
  { data, loading, error, reload() }
```

- 每个请求带 `AbortController` + 递增序号，旧响应不得覆盖新状态。
- 不订阅 `SessionsProvider` 的 `focusSeq` / `boardSyncSeq`，不注册定时器与可见性监听。
- 选中文件时重新拉一次内容；`mtime/size` 与上次不一致时提示
  「文件已在磁盘上变更，点击刷新」。

### 6.8 组件拆分

| 文件 | 职责 | 约行数 |
|---|---|---|
| `views/FilesPage.tsx` | 页面骨架、tab、URL 同步、布局、选中态编排 | 160 |
| `components/files/FilesTabs.tsx` | tablist + 变更计数徽标 | 50 |
| `components/files/FilesToolbar.tsx` | 根路径、刷新、显隐忽略、全展开/折叠 | 90 |
| `components/files/FileTree.tsx` | 懒加载树、递归行、键盘焦点 | 220 |
| `components/files/TreeRow.tsx` | 通用行（缩进、chevron、图标、名称、元信息） | 80 |
| `components/files/FilePreview.tsx` | 文件头 + 行号文本 + 二进制/截断/受保护态 | 140 |
| `components/files/ChangesTree.tsx` | 变更树（默认折叠）、摘要、空态 | 180 |
| `components/files/DiffView.tsx` | hunk 渲染、双行号、三色、提示态 | 170 |
| `components/files/StatusBadge.tsx` | M/A/D/R/?/U 徽标 | 40 |
| `lib/files.ts` | `buildChangeTree`、`dirnameChain`、`formatSize`、`statusLabel`、路径工具 | 130 |
| `lib/useFiles.ts` | 4 个 hook | 260 |
| `lib/types.ts` | 新增 `DirEntry / WorkspaceTree / WorkspaceFile / ChangeEntry / ChangesResponse / DiffHunk` | +40 |
| `lib/i18n.tsx` | 约 28 个中英文 key | +60 |
| `components/Icons.tsx` | `ChevronIcon`、`FolderIcon`、`FolderOpenIcon`、`FileIcon`、`GitBranchIcon`、`LockIcon` | +80 |
| `app/globals.css` | `.files-*` 一节，复用现有 token | +260 |
| `components/shell/AppShell.tsx` | files 路由加 `main-files` | +2 |

### 6.9 样式要点

- tab：segmented pill，复用 `--surface-subtle` / `--border` / `--radius-pill`；
  active 用 `--text` + 反色背景；变更徽标用 `--accent`。
- 树行高 26px、等宽字体、hover 背景；缩进 14px/层；选中行左侧 2px accent 竖条。
- diff：add 用低饱和绿、del 用低饱和红、行号列用 `--text-dim`；
  **只使用 token，不硬编码颜色**，dark / light 两套主题都要验证。
- 预览 `<pre>` 保留换行、`white-space: pre`、`overflow:auto`、`tab-size: 4`；
  v1 不做语法高亮（v1.1 见第 12.1 节）；语言图标与状态着色见第 12.2 节。
- 窄屏：`.files-body` 单列，选中后隐藏树 pane，预览头部出现返回按钮。

### 6.10 i18n key 草案

`filesTabFiles` / `filesTabChanges` / `filesRefresh` / `filesShowIgnored` /
`filesHideIgnored` / `filesHiddenIgnored` / `filesEmptyDir` / `filesEmptyFile` /
`filesProtected` / `filesBinary` / `filesTruncated` / `filesCopyPath` /
`filesCopied` / `filesOpenInFiles` / `filesNotGitRepo` / `filesNoChanges` /
`filesBranch` / `filesAheadBehind` / `filesStaged` / `filesUnstaged` /
`filesExpandAll` / `filesCollapseAll` / `filesConflictDiff` / `filesDiffBinary` /
`filesLoadFailed` / `filesRetry` / `filesChangedOnDisk` / `filesBack` /
`filesWorkspaceRoot` / `statusModified` … `statusConflicted`

## 7. 改动清单

**新增**

- `webui/workspace.py`
- `tests/webui/test_workspace.py`
- `webui/frontend/lib/useFiles.ts`
- `webui/frontend/lib/files.ts`
- `webui/frontend/components/files/*.tsx`（8 个）
- 本文档

**修改**

- `webui/server.py`：`create_app` 注入 + 4 个路由
- `webui/frontend/views/FilesPage.tsx`：占位 → 真实页面
- `webui/frontend/lib/types.ts`、`lib/i18n.tsx`、`components/Icons.tsx`
- `webui/frontend/components/shell/AppShell.tsx`：`main-files`
- `webui/frontend/app/globals.css`
- `docs/webui.md`：端点表 + 新增「Workspace browsing」小节
- 实现完成后在 `README.md` 的文档索引里补一行链接（可选）

**不动**

- 路由表（`/files` 已存在）、Provider 层级、Board / Sessions 逻辑、依赖清单。

**v1.1 追加**（不影响上面的 v1 清单）：`lib/highlight.ts`、`lib/fileIcons.ts`、
`package.json` 增加 `highlight.js`、`DiffView` 的 split 配对与展开、`workspace.py` 的
`context` / `full` 参数，详见第 12 节。

## 8. 边界与取舍

| 情况 | 处理 |
|---|---|
| `jobs/` 21G、`workspace/` 202M | 被 gitignore → 默认隐藏；开启显示后仍靠懒加载，不递归扫描 |
| `.env` / `model.json` | 默认隐藏 + 读取 403 + 锁图标 |
| 符号链接 | 显示 `symlink`，不跟随；真实路径越界即拒绝 |
| 同一文件 staged + unstaged | 徽标显示汇总；v1 只给「工作区 vs HEAD」合并 diff |
| 未跟踪大文件 | diff 全 add，超过上限截断并提示 |
| 非 UTF-8 文件 | UTF-8 解码失败按二进制处理（v1 不做 GBK 探测） |
| 文件查看时被改写 | `mtime/size` 比对 + 提示，不自动覆盖 |
| 变更数量巨大 | 5000 条截断 + 提示 |
| workspace 是仓库子目录 | git 加 `--relative`，统一为 workspace 相对路径；测试覆盖 |
| 无 Node 常驻 | 静态导出 + 新 JSON API，部署方式不变 |

**v1 明确不做**：全局搜索、语法高亮、图片/PDF 预览、编辑保存、stage / commit / discard、
虚拟滚动、SSE、轮询。其中语法高亮、语言图标与状态着色、split 视图与展开完整文件
已排入 v1.1，见第 12 节；其余仍不做。

## 9. 测试计划

### 9.1 后端 `tests/webui/test_workspace.py`

`TemporaryDirectory` + `git init` 构造仓库；`git` 不存在时 `skipUnless`。

1. tree：单层、目录优先排序、空目录、隐藏忽略项计数、`ignored=1`、
   条目上限截断、符号链接指向外部时拒绝、`.git` 强制隐藏。
2. file：普通 UTF-8、CRLF 保留、空文件、二进制（含 NUL）、超长截断、
   受保护文件 403、目录 400、越界 400、不存在 404。
3. changes：`M`（staged / unstaged / 两者）、`A`、`D`、`R`（带 `old_path`）、
   冲突、未跟踪目录展开、`+/-` 统计、branch / ahead / behind、非 git、
   **workspace 为仓库子目录**时的路径换算。
4. diff：修改（行号与 hunk 头）、未跟踪全 add、删除全 del、重命名带 `old_path`、
   二进制、无末尾换行、超长截断。
5. API：注入临时 root 后 4 个端点的状态码与 JSON 形状；
   `create_app` 既有测试不受影响。

### 9.2 前端

项目没有前端测试框架，沿用：

- `npm run typecheck && npm run build`。
- 手动清单：
  - 首屏只 1 层；展开 / 折叠 / 刷新；深链 `?path=`；tab 切换与浏览器后退。
  - 忽略开关；受保护文件；二进制；大文件截断；空目录。
  - 变更树默认折叠、计数、全部展开、重命名、删除、未跟踪；
    diff 行号与增删色正确。
  - 无变更空态、非 git 空态、git 失败重试。
  - 刷新按钮能更新已展开目录与当前 diff。
  - 窄屏抽屉与返回；dark / light 两套主题；中英文切换。
  - 文件被外部修改后的提示。

## 10. 分期实施

| 阶段 | 内容 | 验收 | 估时 |
|---|---|---|---|
| P1 后端 | `WorkspaceBrowser` + tree/file 端点 + 单测 | `pytest tests/webui/test_workspace.py` 全绿；curl 能列目录、读文件、拒绝越界 | 3–4h |
| P2 文件 tab | tab 骨架、URL、懒加载树、预览、空 / 错态、响应式、`main-files` | 首屏只 1 层；点目录加载下一层；刷新与深链正常；`typecheck + build` 通过 | 4–5h |
| P3 变更 tab | changes/diff 端点 + 变更树 + diff 渲染 + 刷新契约 | 默认折叠树正确；M/A/D/R/?? 显示正确；diff 行号正确；空态正确 | 4–5h |
| P4 收尾 | 敏感文件 / 截断 / 二进制 / 冲突提示、i18n、双主题、窄屏、文档 | 第 9.2 节清单全过；`docs/webui.md` 更新 | 2–3h |

合计约 2 个工作日。每个阶段结束先跑 `pytest` / `typecheck` / `build` 再进入下一阶段。
v1 验收通过后再排 v1.1 的三项增强（第 12 节），不占用上述工期。

## 11. 验收清单

- 直接访问 `/files`、刷新、从聊天 / 看板切回，页面都能正确加载。
- 文件 tab 首屏不扫描子目录；`jobs/`、`workspace/` 不会出现在默认视图。
- 目录展开 / 折叠、忽略开关、刷新、深链、tab 切换与后退行为符合第 6.3 节。
- 文本预览、二进制提示、截断提示、受保护文件提示。
- 变更 tab 默认折叠且计数正确；重命名、删除、未跟踪、冲突都有正确展示。
- diff 行号、hunk 头、增删色正确；二进制与截断有提示。
- 非 git 工作区、git 命令失败都有可读提示，不白屏。
- 窄屏、dark / light、中英文均正常。
- v1 无新增运行时依赖；静态导出与 FastAPI 托管方式不变。

## 12. 下一阶段实现（v1.1）

v1（第 4–11 节）保持不变。下面三项在其上增量实现，各自独立可交付、可回滚；
除语法高亮外不引入新依赖，API 只做向后兼容的扩展。

**实现记录（2026-11）**：三项均已完成。
`highlight.js@11.12` 是唯一新增依赖；diff 增加 `context` / `full` 参数；
`DiffView` 支持 unified/split 与展开完整文件；文件树增加了语言标签、
git 状态着色和目录计数。下面各节保留原始设计，并在末尾补充实现差异。

### 12.1 语法高亮

**目标**：文件预览与 diff 正文按语言着色；高亮不可用时自动退回纯文本，不影响阅读。

**依赖**
- 新增 `highlight.js`（单依赖、无 WASM、浏览器端运行）。
  不选 Shiki：需要 WASM 与主题资源，对静态导出和包体都更重。

**实现**
- 新增 `lib/highlight.ts`：
  - 扩展名 → 语言注册表，首批只覆盖：`py ts tsx js jsx json yaml yml toml md sh bash css html sql rs go java diff`；
  - 每种语言用动态 `import("highlight.js/lib/languages/...")` 按需加载，未打开过的语言不进首屏；
  - 导出 `highlightCode(code, lang): string`（HTML）和 `highlightLine(line, lang): string`（diff 逐行复用同一实例）；
  - 模块级 LRU：键为 `path + mtime + lang`（预览）和 `lineHash + lang`（diff），上限 20 条 / 单条 512 KB。
- `FilePreview`：高亮成功时用 `dangerouslySetInnerHTML` 注入 highlight.js 输出；输入先做 HTML 转义，
  不接受文件内容里的原始标签。未知语言、加载失败、超限时走现在的 `<pre>` 路径。
- `DiffView`：`ctx/add/del` 每行单独高亮（先转义再高亮），行首的 `+/-` 与行号列不参与着色。
- 颜色：新增 `--syntax-keyword` / `--syntax-string` / `--syntax-number` / `--syntax-comment` /
  `--syntax-function` / `--syntax-type` / `--syntax-variable` / `--syntax-punctuation` 共 8 个 token，
  dark / light 各一套；高亮器只输出 class，不引入第三方主题 CSS。

**边界**
- 预览超过 256 KB 或 2000 行时跳过高亮，直接纯文本。
- diff 只高亮当前渲染的 hunk；切换文件时 abort 未完成的语言模块加载。
- 任何高亮异常都静默降级为纯文本，不显示错误提示。

**验收**
- Python / TypeScript / JSON / YAML 四类文件在文件预览和 diff 中颜色正确。
- 未知扩展名、二进制、截断文件仍是纯文本。
- 首次打开某语言有短暂的模块加载，但正文立即以纯文本可见，不闪白。
- dark / light 切换后无残留颜色；构建产物按语言分 chunk，首屏不含语言包。

**实现差异**：
- 预览对整份文件高亮一次，因此保留独立行号栏并用一个 `<pre>` 渲染高亮 HTML；
  diff 仍逐行高亮（同一行文本走缓存）。
- 首屏只会预取 highlight.js core（约 62 KB）；各语言文法仍是按需 chunk。
- 增加单行 4000 字符上限，压缩过的单行文件直接回退纯文本。

### 12.2 语言图标与 git 状态着色

**目标**：文件树一眼区分文件类型与改动状态；不新增请求，复用页面已有的 `changes` 数据。

**语言图标**
- 新增 `lib/fileIcons.ts`：扩展名 / 特殊文件名（`Dockerfile`、`Makefile`、`.gitignore` 等）→ `{ icon, color }`，
  约 20 组，其余落到通用 `FileIcon`。目录沿用 `FolderIcon` / `FolderOpenIcon`。
- 图标用 `Icons.tsx` 的内联 SVG（零依赖），统一 16×16 viewBox；颜色走现有 token，不硬编码。
- `TreeRow` 增加 `icon` / `iconColor` 两个可选 props，默认值保持现有通用图标。

**git 状态着色**
- 页面挂载时已经静默拉一次 `changes` 供 tab 徽标使用；把结果按 `path` 建成 `Map<string, ChangeEntry>`，
  文件 tab 的树行直接查表：
  - 文件名颜色：`modified` → `--warning`；`added` / `untracked` → `--success`；
    `deleted` → `--danger`；`renamed` / `copied` → `--accent`；`conflicted` → `--danger` + 加粗；
  - 行尾紧凑徽标 `M / A / D / R / ? / U`；目录行显示子树内变更文件数。
- 变更 tab：同一套颜色规则，但每行都带徽标；目录行按子树内最高优先级着色
  （`conflicted > deleted > modified > added > renamed > untracked`）。
- 颜色只作用于文件名与徽标，不影响 hover 背景、选中竖条、图标颜色和缩进对齐。
- 刷新按钮、切 tab、拉取新 changes 后重新 join；不新增网络请求。

**边界**
- `protected` 文件用锁图标覆盖状态色；受保护文件仍不可预览。
- 开启「显示忽略文件」后被忽略的条目降低透明度，不显示状态色。
- 符号链接保留自身图标，不按目标文件类型推断。

**验收**
- 同一文件在文件 tab 与变更 tab 的颜色、徽标一致。
- 重命名显示 `old → new` 时两段都着色且不串色；删除、未跟踪、冲突的优先级正确。
- 工作区干净时没有任何着色或徽标。
- dark / light 下文字对比度可读；无变更图标错位或行高变化。

**实现差异**：
- 图标采用 2–3 个字母的彩色语言标签（`PY` / `TS` / `{}` / `MD` …），
  不是每种语言一套 SVG；15px 下更易读，也避免引入图标资产。
- 文件 tab 的目录状态与计数来自 `lib/files.ts` 的 `indexChanges()`，
  不额外请求 changes。
- protected 文件不显示状态色，锁图标优先。

### 12.3 split / unified 切换与展开完整文件

**服务端**
- `GET /api/workspace/diff` 增加两个向后兼容的参数：
  - `context`：int，默认 3，范围 0–2000，透传为 `git diff -U<n>`；
  - `full`：bool，默认 false；为 true 时用足够大的 context 生成 hunk，仍走现有解析器。
- 响应结构与第 5.5 节一致，只增加 `context` 回显字段，前端不需要新的解析器。
- 上限保护：生成的 patch 超过 2 MB 或行数超过 20000 时返回 `truncated:true`，保留已解析部分。
- 二进制、删除、重命名、未跟踪的判定逻辑不变。

**前端**
- `DiffView` 顶部加 segmented 切换 `Unified | Split`，选择写入 `localStorage`（键 `sunagent.diffStyle`），
  默认 unified；从变更 tab 深链进入时按存储值渲染。
- split 布局：把 hunk 里的 `del` / `add` 序列配对成左右两列（连续删除与新增按顺序对齐，不足补空行），
  `ctx` 行同时出现在两侧，左右各一套行号列。配对算法放 `lib/files.ts`，纯函数、可单测。
- 每个 hunk 末尾以及首尾被截断处显示「展开完整文件」：
  - 首次点击请求 `diff?path=...&full=true`，替换当前数据并缓存（键 `path + mtime`）；
  - 展开后按钮变为「收起」，切回 3 行上下文直接用初始数据，不再发请求；
  - 加载中保留现有行，按钮显示轻量 loading；失败恢复原状并给行内重试。
- diff 头部显示上下文提示（如「仅显示改动附近 3 行」），让用户知道可以展开。
- 文件 tab 的预览本来就是全文，不受这一项影响。

**边界**
- 重命名 + 修改、纯删除、未跟踪文件：展开后分别表现为全 del、全 add、全 ctx + add，颜色规则不变。
- 超限文件：「展开完整文件」禁用，提示「文件过大，仅显示前 N 行」。
- 冲突文件维持 v1 的提示，不在本阶段做冲突合并视图。

**验收**
- 同一 diff 在 unified / split 下行号与内容一一对应，滚动不错行。
- 展开后能看到未改动的完整文件；收起后不再发请求；切文件、刷新后模式保持。
- 重命名、删除、未跟踪三种 diff 在两种布局下都正确。
- `truncated` 时有明确提示且按钮禁用。

**实现差异**：
- `context` 接受 0–2000；`full=true` 内部使用 4000 行上下文，
  再受既有的 2 MB / 4000 行上限约束。
- split 配对是 `lib/files.ts` 的纯函数 `toSplitRows()`：删除与新增按下标配对，
  短的一侧补空格。
- 展开结果缓存在 hook 中，收起不发请求；切文件或刷新时回到 3 行上下文。
- split/unified 偏好存 `localStorage` 的 `sunagent.diffStyle`，默认 unified。

### 12.4 依赖、风险与实施顺序

| 项 | 依赖 | 主要风险 | 缓解 |
|---|---|---|---|
| 语法高亮 | `highlight.js` | 包体增大、首次加载慢 | 语言动态 import、LRU、大文件跳过高亮 |
| 语言图标 | 无（内联 SVG） | 图标风格不统一 | 统一 16×16、只用 token 上色、控制在 25 个以内 |
| 状态着色 | 无 | 与现有状态色冲突 | 只复用 `--success/--warning/--danger/--accent` |
| split / 展开 | 无 | 配对错误、全量 patch 过大 | 纯函数配对 + 单测；2 MB / 20000 行上限 |

**实施顺序**：12.2（零依赖、见效快）→ 12.3（API 小改 + 渲染）→ 12.1（引入依赖，最后做）。
每项结束跑 `npm run typecheck`、`npm run build`；12.3 额外跑 `pytest tests/webui`。

**统一验收**：第 11 节 v1 清单重跑不回归；新增交互按第 9.2 节的方式手动走查；
构建产物中高亮语言按需分 chunk，首屏体积不因本阶段明显上涨。
