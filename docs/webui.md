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
under `harnesses/archived/` never appear in the picker.

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
| `POST` | `/api/sessions/{id}/cancel` | stop the running turn |
| `GET` | `/api/sessions/{id}/events` | SSE stream of frames |
| `GET` | `/api/board` | the task board snapshot |
| `POST` | `/api/board/tasks` | create a task (optional `model` / `harness`) |
| `PATCH` | `/api/board/tasks/{id}` | edit or move a task, or change its `model` / `harness` |
| `DELETE` | `/api/board/tasks/{id}` | delete a task (optionally its conversation) |
| `POST` | `/api/board/tasks/{id}/session` | create or reuse the task's conversation |
| `PATCH` | `/api/board/executor` | pause or resume serial task execution |

The event stream replays the recent frames of a session before switching to
live delivery, and honours `Last-Event-ID`, so a browser that reconnects
resumes where it left off instead of starting from a blank transcript.

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
* Only tasks explicitly queued after this feature existed are run. Old
  `todo` cards without `queued_at` stay untouched until moved or edited.
* A successful turn moves the card to `review`; the agent never moves it
  to `done`. A failed or cancelled run also lands in `review` with the
  error shown on the card, and the worker continues with the next task.
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
board task interrupted this way goes back to `todo` and resumes on the
next start.

## 7. The Frontend

`webui/frontend/` is a Next.js App Router project written in TypeScript.
`app/page.tsx` owns the client state (view, sessions, frame upserts,
composer, board dialogs, theme, settings), and the visual pieces live in
`components/` (`TitleBar`, `Sidebar`, `BoardView`, `TaskCard`,
`TaskEditor`, `Transcript`, `Composer`, `SettingsPanel`). Board data and
mutations live in `lib/useBoard.ts`; `lib/board.ts` holds the shared column
and priority vocabulary. `app/globals.css` carries the design tokens and
every component style; `lib/markdown.ts` is the same small Markdown
renderer as before, ported to a module.

The layout follows the same three-part frame as a desktop console app: a
title bar, a sidebar with a view rail, and either the transcript with a
composer (Chat) or the five-column board. Tool calls and thinking blocks
are deliberately quiet — they are transcript metadata, not the main event
— and expand on click.

`npm run build` emits a static export to `webui/frontend/out/`. FastAPI
mounts `/_next` from that directory and serves `index.html` at `/`; if the
export is missing, the page says how to build it. `npm run dev` instead
runs the regular Next.js server and rewrites `/api` to
`http://127.0.0.1:8000`.
