# Web UI — Usage Guide

The web UI is a **channel**. It consumes exactly the same user-facing
agent events as the CLI and decides on its own how to present them. The
runtime knows nothing about HTTP, SSE, sessions, or the browser.

```
agent_runtime.events.EventEmitter
        ↓  AgentEvent stream
agent_runtime.channels.webui.WebUIChannel      ← frames, no I/O
        ↓  JSON frames
webui.sessions.FrameBus                        ← fan-out per session
        ↓  SSE
browser
```

The web UI has two pieces:

* a **Next.js front end** in `webui/frontend/` (App Router, TypeScript,
  React), and
* a **FastAPI host** in `webui/` that owns the sessions and serves the
  front end's static export.

In production the front end is exported with `output: "export"` and
served by FastAPI, so the page, the API, and the SSE stream share one
origin (and one basic-auth challenge) with no Node server in the middle.

## 1. Two Pieces

| Piece | Location | Responsibility |
|---|---|---|
| `WebUIChannel` | `src/agent_runtime/channels/webui.py` | Translate `AgentEvent` into JSON frames. Knows nothing about HTTP. |
| `webui/` | `webui/` (repo root) | FastAPI app, session and board stores, serial board executor, and static-export hosting. Knows nothing about the event vocabulary. |
| `webui/frontend/` | `webui/frontend/` | Next.js client: title bar, sidebar, transcript, composer, and task board. Knows nothing about the runtime. |

The split matters: the channel is reusable by any transport (SSE today,
WebSocket or a Telegram bridge tomorrow), and the server can be rewritten
without touching the runtime.

## 2. The Frame Vocabulary

Frames are plain JSON objects with a `type` discriminator and a monotonic
`seq` (added by the frame bus, used for resumable streams).

| Frame | Payload | Meaning |
|---|---|---|
| `run.started` | `run_id`, `message` | a turn began |
| `delta` | `id`, `channel`, `text` | streamed text; `channel` is `assistant` or `reasoning` |
| `tool.started` | `id`, `call_id`, `tool`, `summary`, `arguments` | a tool call was dispatched |
| `tool.completed` | `id`, `tool`, `status`, `duration`, `output`, `exit_code` | the tool returned |
| `tool.failed` | `id`, `tool`, `status`, `error` | the tool raised |
| `segment` | `segment` | a finished transcript block (the persistable form) |
| `run.completed` | `answer`, `iterations` | the turn finished |
| `run.error` | `stage`, `error` | the run failed |

Every block carries an `id`. The browser **upserts** blocks by that id, so
a live run and a replayed transcript render through the same code path —
there is no "live mode" and "history mode" to keep in sync.

`segment` frames are the authoritative form of a finished block. The server
appends them to the session transcript, which is what makes the conversation
survive a reload and a server restart.

## 3. Sessions

A session owns two things:

* the **transcript** — user bubbles, assistant prose, thinking blocks, and
  tool cards, in order;
* the **conversation** — the `agent_runtime.agent.Conversation` handed to
  `run_turn`, which is what keeps the agent's own context alive between
  turns.

Both are persisted as one JSON document per session under
`workspace/sessions/` (override with `AGENT_RUNTIME_WEBUI_SESSIONS_DIR`).
The runtime trace of each turn is written next to it, under
`workspace/traces/<run_id>.jsonl`. The task board is a separate single
document at `workspace/board.json` (see section 5).

A session that never received a message is never written to disk and
never listed: the browser keeps it in memory (there has to be
somewhere to type), and it earns a file the moment the first message
arrives.  Otherwise every "new chat" click would leave a stub behind
and the sidebar would fill up with blank conversations.  See
[storage.md](storage.md) for the full map of what the project writes.

### Branching from an answer

Each assistant bubble carries a branch button next to its copy button:
`POST /api/sessions/{id}/branch` with the block's `segment_id` copies the
session up to that answer into a **new, independent** session (same model,
same harness, same history prefix including the system prompt). The parent
is not touched, and the two conversations have no link afterwards.

Only the last block of a turn can start a branch: cutting in the middle
would keep an assistant message whose tool calls never got their results.
The copy's history ends on that answer with its `tool_calls` dropped, so the
next message is a legal sequence. The block/message pairing is verified by
text, because the transcript and the `messages` list have no shared id; a
session whose two sides drifted apart (an interrupted run) is refused with
`400` rather than cut at a guess.

### Model and harness per session

Each session stores a catalog id (`model`) and a harness id (`harness`),
not endpoint settings, so the session file never contains a secret.

* The **model** can be switched at any time. It is read when the next
  turn starts, so a turn that is already running keeps using the model it
  started with.
* The **harness** can only be picked while the session is still empty —
  before its first message. It shapes the system prompt and the tool set,
  so changing it later would not retrofit the conversation. A `PATCH`
  that tries to change a locked harness returns `409`.

At the start of every turn the two ids are resolved into an immutable
`webui.turn_context.TurnSnapshot` (provider, harness, tools, runtime),
which the turn uses for its whole lifetime.

### `model.json`

The model catalog is a JSON file (default `./model.json`, overridable with
`AGENT_RUNTIME_MODEL_CATALOG`). Start from `model.json.example`:

```json
{
  "default": "main",
  "models": [
    {
      "id": "main",
      "label": "DeepSeek Chat",
      "base_url": "https://api.deepseek.com/v1",
      "api_key": "${DEEPSEEK_API_KEY}",
      "model_id": "deepseek-chat"
    }
  ]
}
```

`id` is what the session stores and the picker shows; `model_id` is sent
to the provider. `api_key` may be a literal secret or an `${ENV_VAR}` /
`env:ENV_VAR` reference, expanded at startup. Without a `model.json` the
runtime builds a single-entry catalog from `MODEL_ID`, `LLM_BASE_URL` and
`LLM_API_KEY`.

Only ids and labels cross the HTTP boundary: `/api/config` never returns
`base_url` or `api_key`.

Harness choices come from `harnesses/*.yaml` — a flat scan, so manifests
under `harnesses/archived/` never appear in the picker.  A manifest marked
`internal: true` (the diagnosis harness `evaluation-v1`) also stays out of
`/api/config`, and naming it when creating a session or a board task is
refused with a 400; it still resolves by id, which is how a diagnosis
starts one.

## 4. HTTP API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | the single page app |
| `GET` | `/api/health` | liveness probe |
| `GET` | `/api/config` | default model/harness, catalogs, and enabled tools |
| `GET` | `/api/sessions` | list sessions, newest first |
| `POST` | `/api/sessions` | create a session (optional `model` / `harness`) |
| `GET` | `/api/sessions/{id}` | one session with its transcript and selection |
| `PATCH` | `/api/sessions/{id}` | rename, switch `model`, or pick a `harness` |
| `DELETE` | `/api/sessions/{id}` | delete a session |
| `POST` | `/api/sessions/{id}/messages` | send a message, start a turn |
| `POST` | `/api/sessions/{id}/branch` | copy the conversation up to one answer into a new session |
| `POST` | `/api/sessions/{id}/cancel` | stop the running turn |
| `GET` | `/api/sessions/{id}/events` | SSE stream of frames |
| `GET` | `/api/board` | the task board snapshot |
| `POST` | `/api/board/tasks` | create a task (optional `model` / `harness`) |
| `GET` | `/api/board/tasks/{id}` | one task with its comment and run history |
| `PATCH` | `/api/board/tasks/{id}` | edit or move a task, or change its `model` / `harness` |
| `DELETE` | `/api/board/tasks/{id}` | delete a task (optionally its conversation) |
| `POST` | `/api/board/tasks/{id}/comments` | leave feedback and re-queue the task |
| `POST` | `/api/board/tasks/{id}/session` | create or reuse the task's conversation |
| `PATCH` | `/api/board/executor` | pause or resume serial task execution |
| `GET` | `/api/evaluation/overview` | run-level KPIs/trends (`range`, `origin`, `harness`, `model`) |
| `GET` | `/api/evaluation/board-overview` | TSR / first pass / rework / funnel / artifacts (`range`) |
| `GET` | `/api/evaluation/benchmarks` | Harbor benchmark records (own view) |
| `GET` | `/api/evaluation/tasks` | task rows (`range`, `status`, `harness`, `model`, `origin`, `q`, `sort`, `order`, `page`, `page_size`) |
| `GET` | `/api/evaluation/tasks/{id}` | lifecycle, runs with metrics, evidence, diagnoses |
| `GET` | `/api/evaluation/runs/{id}` | `run.metrics` plus a bounded trace digest |
| `GET` | `/api/evaluation/diagnoses` | diagnosis sessions linked through `Session.target` (`task_id`), each with its frozen report |
| `POST` | `/api/evaluation/diagnose` | start a diagnosis for `{target, force?}`; returns `session_id` / `bundle_hash` (or `duplicate` + `existing_session_id`) |
| `POST` | `/api/evaluation/refresh` | rebuild the in-memory evaluation index |
| `GET` | `/api/workspace/tree` | one directory level of the workspace (`path`, `ignored`) |
| `GET` | `/api/workspace/file` | text preview of one file (`path`) |
| `GET` | `/api/workspace/changes` | uncommitted changes from `git status` |
| `GET` | `/api/workspace/diff` | one file's diff against `HEAD` (`path`, optional `old_path`, `context`, `full`) |

The event stream replays the recent frames of a session before switching to
live delivery, and honours `Last-Event-ID`, so a browser that reconnects
resumes where it left off instead of starting from a blank transcript.

### Workspace browsing

`/files` is read-only. Its API browses the same root the agent uses
(`AGENT_RUNTIME_WORKSPACE`, otherwise the server's working directory):

* **Tree** requests are one level at a time; the browser never scans
  recursively. Entries report `type`, `size`, `mtime`, `ignored` and
  `protected`.
* **Ignored** entries come from `git check-ignore`, plus a fixed list of
  runtime/output directories (`workspace/`, `jobs/`, `.outputs/`, …) that
  stays hidden even when tracked. `.git`, `.venv`, `node_modules`,
  `__pycache__` and `.next` are never listed. `?ignored=true` reveals the
  rest, greyed out.
* **Protected** names (`.env*`, `model.json`, `*.pem`, `*.key`, `*.p12`,
  `id_rsa*`, `.netrc`, `credentials*`) appear in the tree with a lock but
  return `403` from both `file` and `diff`.
* **Limits**: 1000 entries per directory, 5000 preview lines, 512 KiB of an
  oversized preview, 2 MiB of diff text, 4000 diff lines, 5000 change
  entries. Truncation is a flag in the response, never an error.
* **Diff context**: `context` accepts 0–2000 lines (default 3); `full=true`
  asks for the whole previewable file and uses the 4000-line budget.
  The preview highlights known languages with lazily loaded
  `highlight.js` grammars and can switch between unified and split
  layouts (the choice is stored locally).
* **Paths** are POSIX paths relative to the root; escapes, absolute paths,
  hard-skipped directories and symlinks are rejected.
* **Changes** use `git status --porcelain=v2 --branch -z
  --untracked-files=all`; a workspace that is a subdirectory of a
  repository is handled with repository-relative git calls and local
  paths in the response.
* **Refresh** is explicit: on page entry, on switching to the Changes tab,
  and on the refresh button. There is no polling, watcher or SSE.

The client keeps one directory listing per expanded path, so collapsing
and re-expanding a directory does not fetch it again. The full design is
in [`docs/files-page.md`](files-page.md).

## 5. Task Board

The left rail has three destinations: **Chat**, **Board**, and **Settings**.
Settings opens its panel over whichever view is active; opening Chat or
Board switches the main area back to that view.

A board card is an independent **task**, not a conversation. That lets a
task exist before any agent work starts. Each task has:

* a required title and description, plus free-form tags,
* one of five fixed columns — `backlog`, `todo`, `doing`, `review`, `done`,
* a priority — `low`, `normal`, `high`, `urgent`,
* an optional `model` and `harness` for the conversation it will run,
* a history of comments and per-run final responses,
* an optional linked conversation (`1 task ↔ 0 or 1 session`).

The description is also the execution prompt: when the serial worker
starts a task, it sends the description unchanged as the first user
message of the task's conversation.

The model and harness selects use the same catalog and harness list as the
composer. Leaving either on **Service default** stores `null` and applies
whatever `AGENT_RUNTIME_MODEL` / `AGENT_RUNTIME_HARNESS` resolve to. A task
choice is applied when its conversation is first created, and editing the
card updates an already linked conversation: the model always takes effect
on the next turn, while the harness can only change before the conversation
has any history. Editing the harness of a locked conversation is rejected
with `409` and leaves both the card and the conversation as they were.

### Serial auto-execution

`todo` is the execution queue. Moving or creating a card in `todo` sets a
`queued_at` marker, and a single server-side worker drains that queue one
task at a time:

```text
backlog ──(human)──> todo ──(worker)──> doing ──(completed)──> review
                         ^                 │
                         └──(failed)───────┘  review + last_error
```

* Order is `urgent > high > normal > low`, then the card's position in
  the Todo column. A running task is never preempted by a new urgent one.
* The first run always includes the card description, together with any
  comments that were waiting before it started. Comments left on a later
  card become the next run's user message; several waiting comments are
  sent together as one message.
* A run interrupted by a restart keeps its conversation and resumes from
  where it stopped instead of resending the description. If the turn had
  not started yet, the next run sends the description normally.
* A recovered task returns to the front of its own priority band in Todo;
  it never jumps ahead of a higher-priority card.
* Leaving a comment returns the card to `todo`. If it is already running,
  the current turn is not interrupted: the comment waits and the card
  returns to `todo` when that run finishes.
* Only tasks explicitly queued after this feature existed are run. Old
  `todo` cards without `queued_at` stay untouched until moved or edited.
* A successful turn moves the card to `review`; the agent never moves it
  to `done`. A failed or cancelled run also lands in `review` with the
  error shown on the card, and the worker continues with the next task.
  Each finished run records its final answer in the card history; tool
  calls and intermediate reasoning are never copied.
* Moving a `doing` card out of that column cancels its run; the human's
  status wins. Deleting a task or its conversation also cancels the run.
* The worker reuses the card's linked conversation, creating one lazily
  with the task title if needed. Re-running a card from `review` reuses
  the same conversation.
* The board header has a pause/resume switch. Pausing stops new claims
  but does not kill the current task.
* Only board execution is serial. Manual chat turns remain independent;
  because they share the same workspace, avoid editing the same files by
  hand while a board task is running.
* On startup any executor-owned `doing` card is returned to `todo` and
  resumes. The board also records `run_id`, `started_at`, `finished_at`,
  and `last_error` for diagnostics.

The worker runs in-process (no Celery/Redis) and is started from the
FastAPI lifespan. A task's run uses the same session/turn machinery as a
manually sent message, so the conversation transcript, SSE stream and
trace file are identical.

### Editing and filtering

Clicking a card opens its **task details**: the description, the comment
and run timeline, and a comment box. Each comment is shown as the user
message that queued it, followed by the final response of the run it
triggered. Only final responses are stored, so the card never shows
thinking blocks or tool activity. The pencil button opens the edit form.
The detail area is a 65vh scroll box that jumps to the bottom once per
opening, so the newest activity is visible without scrolling; later
background refreshes leave the reader's scroll position alone.

Sending a comment stores it and re-queues the card, then closes the
dialog. It is delivered as the next user message when the worker claims
the card, not immediately, so the same text cannot be executed twice. If
the card is currently running, the comment waits for that run to finish
and is sent by the follow-up run.

Drag a card to reorder it within a column or move it to another one. The
card's **move to** select does the same thing without a pointer, for touch
screens and keyboard use. The sidebar filter hides cards by title,
description, or tag, and **Hide done** removes finished cards from view.
Actions that depend on a hidden card are appended rather than guessed, so
filtering never corrupts the stored order.

The Todo column keeps its manual order; a `#N` badge shows the actual
execution queue position derived from priority.

### Storage

The board is one JSON document at `workspace/board.json` (under
`AGENT_RUNTIME_HOME`). Mutations return the complete normalized snapshot —
columns renumbered from zero — and the browser replaces its state with it.
The polled snapshot carries only `comment_count` and `run_count`; the full
comment and run history is loaded on demand from the task detail endpoint.
Writes go through a temporary file and `os.replace`, so a crash leaves
either the old board or the new one. A corrupt file is copied to
`board.json.corrupt-<timestamp>` and the board starts empty.

Deleting a card keeps its conversation by default; `DELETE` accepts
`?delete_session=true` when the caller really wants both gone. Deleting a
conversation from the Chat sidebar unlinks it from every card. On startup
the store also drops links whose conversation no longer exists.

The board polls only while the Board view is visible (every five seconds,
plus on window focus), and refreshes after a run finishes, after a message
is sent, and after a conversation is deleted.

## 6. Running It

```bash
# build the Next.js front end (first run also installs dependencies)
cd webui/frontend
npm install
npm run build
cd ../..

# start the FastAPI host
uv run webui --port 8000
```

`./run.sh start` does the frontend build for you (only when the sources
changed) and then starts the server. Then open
<http://127.0.0.1:8000>.

For front-end development, run the API on port 8000 and the Next.js dev
server on port 3000; the dev server proxies `/api` to FastAPI:

```bash
uv run webui --port 8000          # API + session host
cd webui/frontend && npm run dev  # http://127.0.0.1:3000
```

The web UI **is** the agent host: the FastAPI process holds the LLM, the
tools, and the harness, and every message runs `run_turn` as a background
task inside it. There is no second process to start and no separate agent
to keep in sync — `uv run agent` (the CLI channel) is an alternative
front end, not a prerequisite. The two share the same `.env`
(`LLM_API_KEY`, `LLM_BASE_URL`, `MODEL_ID`) and the same
`AGENT_RUNTIME_HARNESS`, but each keeps its own conversations.

### Environment variables

| Variable | Default | Effect |
|---|---|---|
| `AGENT_RUNTIME_WEBUI_HOST` | `127.0.0.1` | interface to bind |
| `AGENT_RUNTIME_WEBUI_PORT` | `8000` | port to bind |
| `AGENT_RUNTIME_WEBUI_REASONING` | `1` | set to `0` to hide thinking blocks |
| `AGENT_RUNTIME_WEBUI_SESSIONS_DIR` | `workspace/sessions` | where sessions are stored |
| `AGENT_RUNTIME_HOME` | `workspace` | the root of all runtime state (sessions, traces, summaries, board) |
| `AGENT_RUNTIME_WEBUI_AUTH_USER` | *(unset)* | the user name; setting it turns authentication on |
| `AGENT_RUNTIME_WEBUI_AUTH_PASSWORD` | *(unset)* | the password that goes with the user name |

### Access control

The web UI runs an agent with shell and file tools, so anyone who can
reach the port can drive it. Set a user name and a password in `.env`
and every request — the page, the API, and the event stream — requires
HTTP basic authentication:

```bash
# .env
AGENT_RUNTIME_WEBUI_AUTH_USER=admin
AGENT_RUNTIME_WEBUI_AUTH_PASSWORD=<a long random string>
```

The user name is the switch: without `AGENT_RUNTIME_WEBUI_AUTH_USER`
the web UI is open, whatever else is configured, and the launcher says
so when it starts. The browser asks for the user name and the password
once, caches them for the origin, and sends them with every later
request. The client code does not know that authentication exists.

Basic authentication is only base64, not encryption. Keep the server on
localhost and reach it through an SSH tunnel
(`ssh -L 8000:127.0.0.1:8000 <host>`), or put it behind a reverse
proxy that terminates TLS, before anyone else can reach the port.

Generate a password with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

### Stopping the server

`Ctrl+C` returns immediately, even with browser tabs still connected.
Server-sent events never end on their own, and uvicorn waits for open
connections before it finishes shutting down, so the application closes
every stream itself when the process is asked to stop — and then hands
the signal on so the normal graceful shutdown still happens. Running
turns are cancelled at the same time and their sessions are saved; a
board task interrupted this way goes back to `todo` and resumes its
conversation on the next start.

## 7. The Frontend

`webui/frontend/` is a Next.js App Router project written in TypeScript.
The route table in `lib/routes.ts` is the single source of truth for
destinations (`/`, `/board`, `/files`, `/evaluation`, `/schedules`,
`/settings`); `app/(console)/[...slug]/page.tsx` statically generates every
non-root path and `lib/views.tsx` maps each route id to its view. Adding a
page means adding one route row, one view, and one entry in the registry.

The console frame lives in `components/shell/AppShell.tsx` and is rendered
by `app/(console)/layout.tsx`, so page switches do not rebuild the shell.
Client state is layered in providers under `lib/stores/`: theme, toast,
runtime config, sessions/SSE, and the board snapshot plus its dialogs.
Sessions and board data stay alive while the user visits another page;
board polling runs at 5s on `/board` and backs off to 25s elsewhere.

Chat selection is URL state (`/?session=<id>`) rewritten with
`router.replace`, so refresh and deep links open the same conversation.
Chat state lives in `components/Transcript.tsx` and `Composer.tsx`; board
components are `BoardView`, `TaskCard`, `TaskDetail` and `TaskEditor`, with
data and mutations in `lib/useBoard.ts` and `lib/board.ts`. Settings is a
full page under `views/SettingsPage.tsx`. `app/globals.css` carries the
design tokens and every component style; `lib/markdown.ts` is the same
small Markdown renderer as before.

The layout follows the same three-part frame as a desktop console app: a
title bar, a sidebar with a view rail, and the selected view. Tool calls
and thinking blocks are deliberately quiet — they are transcript
metadata, not the main event — and expand on click.

`npm run build` emits a static export to `webui/frontend/out/`. FastAPI
mounts `/_next` from that directory, serves the generated `.html` file for
each path (with a `404.html` fallback), and keeps `/api` and `/_next`
ahead of the catch-all. If the export is missing, the page says how to
build it. `npm run dev` instead runs the regular Next.js server and
rewrites `/api` to `http://127.0.0.1:8000`.

See `docs/webui-frontend.md` for the architecture, state layering, and the
checklist to follow when adding a page.

### The evaluation page

`/evaluation` (`views/EvaluationPage.tsx`) reads the three evaluation views
through `lib/useEvaluation.ts` and keeps them apart: run-level KPIs come from
`/api/evaluation/overview`, board outcome (TSR, funnel, rework, VCR) from
`/api/evaluation/board-overview`, and Harbor records from
`/api/evaluation/benchmarks`. The filter bar sets `range`, `origin`,
`harness` and `model`; `harness` / `model` options are derived from the
response itself, so they only ever list values that actually occur.

Eight tabs: overview (funnel, acceptance and clean-run trends, rework
reasons, top offenders, data quality), validity (artifact evidence:
judged / claimed / unmeasured tasks, VCR, hallucinated completions, slices),
process (tool error kinds, iterations histogram, errors by stage), efficiency
(token mix, latency split, tokens by day, duration histogram, TTFT and queue
wait), reliability (error / cancel / retry / summary-invalid plus compaction
counts), tasks (table with sorting, paging, filtering), benchmarks (Harbor
records and the latest accuracy per harness), diagnosis (the history of
diagnosis conversations linked through `Session.target`).

Every rate renders `—` when its denominator is empty: the page never shows
`0%` for "nothing to judge". The task table drills into
`TaskDetailDrawer.tsx`, which shows the lifecycle from
`board_events.jsonl`, each run's metrics and evidence state, and the run's
trace digest on demand (`/api/evaluation/runs/{id}`); "open conversation"
goes through `/api/board/tasks/{id}/session`, so it reuses or creates the
task's session exactly like the board does. Charts (`TrendChart`,
`BarList`, `StackedBars`, `EvaluationFunnel`, `Histogram`) are hand-rolled
SVG/CSS — the page adds no chart dependency — and `app/styles/evaluation.css`
carries their styles, collapsing to one column below 860px.

A session opened while its turn is still running starts with `busy` taken
from `GET /api/sessions/{id}`, so a reload during a run shows Stop instead
of a composer that would reject the next message.
