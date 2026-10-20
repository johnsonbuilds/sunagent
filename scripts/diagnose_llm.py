"""Isolate LLM API behavior from the agent runtime. No harbor involved."""

import asyncio
import os
import time

from dotenv import load_dotenv

load_dotenv()

TOOLS = [{
    "type": "function",
    "function": {
        "name": "run_command",
        "description": "Run a shell command in the environment",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
}]

ITER2_MESSAGES = [
    {"role": "user", "content": "List the files in /app."},
    {"role": "assistant", "content": "",
     "tool_calls": [{"id": "call-1", "type": "function",
                     "function": {"name": "run_command",
                                  "arguments": '{"command": "ls -la /app"}'}}]},
    {"role": "tool", "tool_call_id": "call-1",
     "content": "doomgeneric doomgeneric_mips\n"},
]


async def timed(name, factory, budget=75.0):
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(factory(), budget)
        print(f"[{name}] ok in {time.monotonic() - started:.1f}s: {result}", flush=True)
    except TimeoutError:
        print(f"[{name}] TIMEOUT after {time.monotonic() - started:.1f}s", flush=True)
    except Exception as exc:
        print(f"[{name}] ERROR after {time.monotonic() - started:.1f}s: "
              f"{type(exc).__name__}: {exc}", flush=True)


async def main():
    from openai import AsyncOpenAI
    client = AsyncOpenAI()
    model = os.getenv("MODEL_ID", "gpt-4o-mini")
    print(f"base_url={os.getenv('LLM_BASE_URL')} model={model}", flush=True)

    async def t1():
        r = await client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": "Reply with OK"}])
        return {"content": r.choices[0].message.content}

    async def t2():
        stream = await client.chat.completions.create(
            model=model, stream=True,
            messages=[{"role": "user",
                       "content": "What is 17*34? Think briefly, then answer."}])
        counts = {}
        first_at = {}
        started = time.monotonic()
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            fields = {k: v for k, v in {
                "content": delta.content,
                "reasoning_content": getattr(delta, "reasoning_content", None),
                "reasoning": getattr(delta, "reasoning", None),
            }.items() if v}
            for field in fields:
                counts[field] = counts.get(field, 0) + 1
                first_at.setdefault(field, round(time.monotonic() - started, 1))
        return {"delta_counts": counts, "first_seen_s": first_at}

    async def t3():
        r = await client.chat.completions.create(
            model=model, messages=ITER2_MESSAGES, tools=TOOLS)
        message = r.choices[0].message
        return {"content_len": len(message.content or ""),
                "tool_calls": [c.function.name for c in (message.tool_calls or [])]}

    async def t4():
        stream = await client.chat.completions.create(
            model=model, stream=True, tools=TOOLS,
            messages=[{"role": "user",
                       "content": "Think about the best way to inspect a MIPS binary, "
                                  "then run exactly one shell command."}])
        kinds = {}
        timeline = []
        started = time.monotonic()
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                kinds["content"] = kinds.get("content", 0) + 1
                timeline.append((round(time.monotonic() - started, 1), "content"))
            if getattr(delta, "reasoning_content", None) or \
                    getattr(delta, "reasoning", None):
                kinds["reasoning"] = kinds.get("reasoning", 0) + 1
                timeline.append((round(time.monotonic() - started, 1), "reasoning"))
            if delta.tool_calls:
                kinds["tool"] = kinds.get("tool", 0) + 1
                timeline.append((round(time.monotonic() - started, 1), "tool"))
        return {"chunk_kinds": kinds, "last_events": timeline[-5:]}

    await timed("1 non-stream simple", t1)
    await timed("2 stream simple (delta fields)", t2)
    await timed("3 non-stream tool-followup (iter-2 shape)", t3)
    await timed("4 stream with tools", t4)


asyncio.run(main())
