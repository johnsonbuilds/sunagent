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

## 1. Two Pieces

| Piece | Location | Responsibility |
|---|---|---|
| `WebUIChannel` | `src/agent_runtime/channels/webui.py` | Translate `AgentEvent` into JSON frames. Knows nothing about HTTP. |
| `webui/` | `webui/` (repo root) | FastAPI app, session store, static assets. Knows nothing about the event vocabulary. |

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
`workspace/traces/<run_id>.jsonl`.

A session that never received a message is never written to disk and
never listed: the browser keeps it in memory (there has to be
somewhere to type), and it earns a file the moment the first message
arrives.  Otherwise every "new chat" click would leave a stub behind
and the sidebar would fill up with blank conversations.  See
[storage.md](storage.md) for the full map of what the project writes.

## 4. HTTP API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | the single page app |
| `GET` | `/api/health` | liveness probe |
| `GET` | `/api/config` | model, harness, and enabled tools |
| `GET` | `/api/sessions` | list sessions, newest first |
| `POST` | `/api/sessions` | create a session |
| `GET` | `/api/sessions/{id}` | one session with its transcript |
| `PATCH` | `/api/sessions/{id}` | rename a session |
| `DELETE` | `/api/sessions/{id}` | delete a session |
| `POST` | `/api/sessions/{id}/messages` | send a message, start a turn |
| `POST` | `/api/sessions/{id}/cancel` | stop the running turn |
| `GET` | `/api/sessions/{id}/events` | SSE stream of frames |

The event stream replays the recent frames of a session before switching to
live delivery, and honours `Last-Event-ID`, so a browser that reconnects
resumes where it left off instead of starting from a blank transcript.

## 5. Running It

```bash
# dependencies (FastAPI + uvicorn ship with the runtime)
uv sync

# start the server
uv run webui --port 8000
```

Then open <http://127.0.0.1:8000>.

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
| `AGENT_RUNTIME_HOME` | `workspace` | the root of all runtime state (sessions, traces, summaries) |
| `AGENT_RUNTIME_WEBUI_AUTH_USER` | `admin` | the user name the web UI asks for |
| `AGENT_RUNTIME_WEBUI_AUTH_PASSWORD` | *(required)* | the password; the web UI refuses to start without it |

### Access control

The web UI runs an agent with shell and file tools, so it is never
served open — not even on localhost. The server refuses to start until
a password is configured in `.env`:

```bash
# .env
AGENT_RUNTIME_WEBUI_AUTH_USER=admin
AGENT_RUNTIME_WEBUI_AUTH_PASSWORD=<a long random string>
```

Every request — the page, the API, and the event stream — then requires
HTTP basic authentication. The browser asks for the user name and the
password once, caches them for the origin, and sends them with every
later request. The client code does not know that authentication
exists.

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
turns are cancelled at the same time and their sessions are saved.

## 6. The Frontend

`webui/static/` holds a dependency-free client: `index.html`, `style.css`,
`app.js`, and `markdown.js` (a small Markdown renderer). There is no build
step and no CDN — the page works offline and can be read top to bottom.

The layout follows the same three-part frame as a desktop console app: a
title bar, a conversation sidebar, and the transcript with a composer. Tool
calls and thinking blocks are deliberately quiet — they are transcript
metadata, not the main event — and expand on click.
