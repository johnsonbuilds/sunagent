# User-Facing Events & CLI Renderer — Usage Guide

## 1. Two Related But Distinct Streams

The runtime emits two streams while an agent runs. They serve different
consumers and should not be collapsed into one.

| Stream | Purpose | Consumers | Content |
|---|---|---|---|
| Internal Trace (`RunTrace`) | debugging, observability, benchmarking, failure analysis | JSONL files, `--trace`, Harbor context | compact, no model text: `llm.chunk`, `tool.start`, `tool.end` |
| User-facing Events (`EventEmitter`) | watching the run as it happens | CLI, Web UI, Telegram, Discord | streamed text, tool arguments, result summaries |

Internal trace stays compact and detailed. User-facing events carry what a
user needs to see the agent work through a task.

## 2. Why an Event Layer?

Without one, the runtime ends up coupled to every channel:

```python
if telegram:
    send_telegram(...)
if discord:
    send_discord(...)
if web:
    websocket.send(...)
```

With one, the runtime produces a single provider- and channel-neutral event
stream and knows nothing about frontends:

```
Agent Runtime
    ↓
EventEmitter
    ↓
Agent Event Stream
    ↓
CLI · Web UI · Telegram · Discord   (each is just an adapter)
```

A channel only converts events into its own presentation. The same design
idea as the provider-independent runtime: keep the core decoupled from any
specific consumer.

## 3. User-Facing Event Model

`call_id` is a top-level field, not part of `data`:

| Event | `data` | Meaning |
|---|---|---|
| `agent.started` | `message` | a new turn begins |
| `agent.completed` | `iterations`, `answer` | the turn finished with a final answer |
| `assistant.started` | — | an LLM round begins |
| `assistant.delta` | `content`, `reasoning` | streamed text / reasoning chunk |
| `assistant.completed` | `tool_calls` (names) | the model finished its message |
| `tool.started` | `tool`, `arguments` | a tool call was dispatched |
| `tool.completed` | `duration` + shell summary or `result` | the tool returned |
| `tool.failed` | `tool`, `error` | the tool raised / validation failed |
| `runtime.error` | `stage`, `error` | the run failed (e.g. LLM error) |

For shell-like results, `tool.completed` carries a summary
(`exit_code`, `stdout_tail`, `stderr_tail`, optional `error`) instead of raw
payloads, so channels show output without drowning in it.

```python
tool.started  →  {"tool": "run_command", "arguments": {"command": "pytest"}}
```

A channel can render this as `Running pytest`; the runtime never decides
what Telegram or a terminal should say.

## 4. EventEmitter API

```python
from agent_runtime.events import EventEmitter

emitter = EventEmitter(run_id="run-001")
```

### Synchronous subscribers (CLI-style)

```python
def on_event(event) -> None:
    print(event.event_type, event.data)

unsubscribe = emitter.subscribe(on_event)
```

`emit()` never blocks or raises because of a subscriber: a failing
subscriber is logged and does not break the run.

### Asynchronous consumers (WebSocket / Telegram style)

```python
async def consume() -> None:
    async for event in emitter.stream():
        await websocket.send_json(event.to_dict())

# externally, when the run is done:
emitter.close()
```

Events emitted between creating and iterating the stream are buffered, so an
adapter never misses the first events. The `close()` sentinel releases
consumers when the run ends.

### Wiring the runtime

```python
from agent_runtime.agent import run_turn

trace = RunTrace(run_id="run-001")          # internal trace, unchanged
events = EventEmitter(run_id=trace.run_id)  # user-facing events

answer = await run_turn(prompt, llm, tools, stream=True,
                        trace=trace, events=events)
```

`events` is optional: `run_turn` without it behaves exactly as before.

## 5. CLI Renderer

`CLIRenderer` is the reference adapter: it receives the same event stream as
any other channel and renders it incrementally.

```python
from agent_runtime.channels import CLIRenderer

events = EventEmitter()
events.subscribe(CLIRenderer())
```

Sample output:

```
▌ 先看一下测试情况                      ← reasoning delta, dim
Running the tests first.                ← content delta, streamed inline

● run_command                           ← tool.started, shown immediately
  $ echo "test_a ...F"; ...             ← shell arguments as a command
  │ test_a ...F                         ← stdout tail
  │ 1 failed                            ← stderr tail shown on failure
  ✗ exit 1 · 0.0s                       ← status + duration

42 个测试全部通过。                      ← final answer keeps streaming
```

Behavior:

- `assistant.delta` prints inline without waiting for the full response.
- `tool.started` prints the header and `$ command` at once, so the user sees
  what is running instead of waiting silently for a long command.
- stdout/stderr tails are limited to the last 5 lines; generic results to 3;
  lines longer than 100 chars are truncated.
- Colors are automatic on a TTY and disabled otherwise.
- Unknown event types are ignored, so a newer runtime still works with an
  older channel.
- Reasoning is shown dimmed unless disabled.

## 6. Running It

```bash
# setup
cp .env.example .env     # fill in LLM_API_KEY / LLM_BASE_URL / MODEL_ID

# one-shot
uv run python run_agent.py "运行 ls -la 并告诉我当前目录有什么"

# interactive REPL
uv run python run_agent.py

# also dump an internal trace
uv run python run_agent.py --trace runs/demo.jsonl "运行 pytest 看看测试状态"
```

`run_agent.py` subscribes the CLI renderer, shares one `run_id` between the
trace and the event emitter, and runs the LLM in streaming mode.

### Environment variables

| Variable | Default | Effect |
|---|---|---|
| `AGENT_RUNTIME_CLI_REASONING` | `1` | set to `0` to hide reasoning deltas |
| `AGENT_RUNTIME_STREAM_IDLE_TIMEOUT` | `120` | seconds before a silent stream fails |

## 7. Extending to Other Channels

A channel is anything that consumes `AgentEvent` via `subscribe()` or
`stream()`. It decides its own presentation:

- Web UI: forward events over a WebSocket and render deltas into a text buffer.
- Telegram: buffer `assistant.delta`, emit one chat message per
  `tool.started` / `tool.completed`, and use `agent.completed.answer` for the
  final summary.
- Discord: same pattern with a different presentation.

Future shell streaming (`stdout.delta`) can be added between `tool.started`
and `tool.completed` without changing the vocabulary — the event model is
already shaped for it.