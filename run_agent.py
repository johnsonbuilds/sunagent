"""Run the real OpenAI-compatible agent loop from the command line."""

import argparse
import asyncio
import os
import readline
import sys
from pathlib import Path
from uuid import uuid4


SOURCE_DIR = Path(__file__).resolve().parent / "src"
sys.path.insert(0, str(SOURCE_DIR))

from dotenv import load_dotenv

from agent_runtime import storage
from agent_runtime.agent import Conversation, RunTrace, run_turn, use_streaming
from agent_runtime.channels import CLIRenderer
from agent_runtime.events import EventEmitter
from agent_runtime.harness import resolve_harness
from agent_runtime.providers import OpenAICompatibleLLM
from agent_runtime.tools import create_default_registry


async def _run() -> None:
    load_dotenv(override=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trace",
        metavar="PATH",
        help="append run events as JSONL to PATH",
    )
    parser.add_argument(
        "--harness",
        metavar="PATH_OR_ID",
        default=os.getenv("AGENT_RUNTIME_HARNESS"),
        help="harness manifest to run (file path, id under harnesses/, "
             "or omit for the default harness, common-v0)",
    )
    parser.add_argument("prompt", nargs="*", help="prompt to send; omit for interactive mode")
    args = parser.parse_args()

    harness = resolve_harness(args.harness)
    llm = OpenAICompatibleLLM()
    tools = create_default_registry(enabled=list(harness.tools.enabled))
    conversation = Conversation()
    # Traces are runtime state, so they land in the one place the
    # project persists things (see agent_runtime.storage).  An explicit
    # --trace still wins.
    trace = RunTrace(output_path=args.trace or storage.trace_path(f"run-{uuid4().hex[:12]}"),
                     harness=harness)
    events = EventEmitter(run_id=trace.run_id)
    renderer = CLIRenderer()
    events.subscribe(renderer)
    use_stream = use_streaming()
    prompt = " ".join(args.prompt).strip()

    try:
        while True:
            if not prompt:
                try:
                    prompt = input("> ").strip()
                except EOFError:
                    print()
                    break
            if prompt.lower() in {"exit", "quit"}:
                break
            if prompt:
                await run_turn(prompt, llm, tools, conversation=conversation,
                               harness=harness, stream=use_stream,
                               trace=trace, events=events)
            prompt = ""
    finally:
        renderer.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
