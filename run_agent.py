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
from agent_runtime.agent import Conversation, RunTrace, run_turn
from agent_runtime.metrics import afinalize_run
from agent_runtime.runtime_config import RuntimeConfig
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
    parser.add_argument("--trace-chunks", choices=["on", "off"], default="off",
                        help="record every streamed llm.chunk (very verbose)")
    args = parser.parse_args()

    harness = resolve_harness(args.harness)
    runtime = RuntimeConfig.from_env()
    llm = OpenAICompatibleLLM(config=runtime)
    tools = create_default_registry(enabled=list(harness.tools.enabled))
    conversation = Conversation()
    renderer = CLIRenderer()
    use_stream = runtime.streaming
    prompt = " ".join(args.prompt).strip()

    async def run_one_turn(message: str) -> None:
        # Interactive mode is a sequence of independent runs: each turn
        # gets its own run_id, trace, and metrics record.
        run_id = f"run-{uuid4().hex[:12]}"
        trace = RunTrace(run_id=run_id,
                         output_path=args.trace or storage.trace_path(run_id),
                         harness=harness, retain_events="control",
                         origin="cli",
                         model=runtime.model,
                         trace_chunks=args.trace_chunks == "on")
        trace.record_config(
            runtime=runtime,
            model_params={"model": runtime.model,
                          "temperature": runtime.temperature,
                          "stream": use_stream,
                          "max_iterations": harness.control.max_iterations},
            tools=tools.schemas,
        )
        events = EventEmitter(run_id=trace.run_id)
        events.subscribe(renderer)
        status, answer, error = "error", "", None
        try:
            answer = await run_turn(message, llm, tools,
                                    conversation=conversation,
                                    harness=harness, stream=use_stream,
                                    trace=trace, events=events,
                                    runtime=runtime)
            # A turn that failed mid-run returns its error text instead of
            # raising, so the trace decides whether this was a success.
            agent_error = trace.agent_error()
            status = "error" if agent_error else "completed"
            if agent_error:
                error = {"stage": str(agent_error.data.get("stage")
                                      or "agent"),
                         "code": str(agent_error.data.get("error") or answer)}
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception as exc:
            error = {"stage": "runtime", "code": str(exc)}
            raise
        finally:
            await afinalize_run(trace, status=status, answer=answer,
                                error=error, origin="cli",
                                model=runtime.model)

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
                await run_one_turn(prompt)
            prompt = ""
    finally:
        renderer.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
