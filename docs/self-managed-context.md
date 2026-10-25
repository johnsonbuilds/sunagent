# Self-Managed Context — Usage Guide

`memory.strategy: self_managed_file` lets the model compress its own request
view by editing a workspace mirror file, under a usage-based budget gate.
The full conversation (`messages`) is never rewritten — only the request
projection (`view`) is swapped — so audit, UI and verification grounding
keep working. Default-off: existing harnesses are unaffected.

## Harness

Derive, don't edit in place:

```bash
uv run python -m agent_runtime.harness derive my-v1 --from common-v1-selfctx \
  --mutation "..." --reason "..."
```

`harnesses/common-v1-selfctx.yaml` is the MVP reference (derived from
`common-v0`): `memory.strategy=self_managed_file`, 60k-char budget with a
12k reserve, mirror at `.live_ctx/LIVE_CTX_MAIN.md`, `fit` edit gate.

## How it works

1. Each turn renders `view[protect:]` to the mirror file (header lines are
   metadata; `## turn <k> [role]` headers must stay byte-identical).
2. The model edits turn bodies with `edit_file` / `apply_patch` /
   `execute_code` (python): replace stale regions with short notes.
   - An emptied body drops that turn.
   - A `tool` turn becomes a plain archived note unless its body starts
     with `[KEEP-PAIRING]` and its id matches a live tool call.
3. After each tool call the harness reads the mirror back:
   - `applied` → `view` swapped, `context.mirror_sync` traced.
   - `rejected` (grew past the limit / broke pairing / no change) → a
     one-line receipt joins the observation stream, `view` unchanged.
4. The budget gate nudges at 25/50/75% of the budget, urgently near the
   wall (sized by recent tool outputs), rolls back newest turns with a
   pinned ledger on overflow, and grants one final turn before stopping —
   never silent truncation.

## Prompt contract

The harness prompt (`CONTEXT FILE` section) tells the model the mirror
path, the bodies-only rule, the batched-edit preference, the
`[KEEP-PAIRING]` semantics, and the waterline readout.

## Observability

- `trace.emit("context.mirror_sync", {before_chars, after_chars, turns})`
- `trace.emit("budget.nudge" | "budget.rollback", ...)`
- The UI renders `messages` (uncompressed record); mirror receipts are
  tagged `metadata.source=mirror` and skipped by legacy compaction.

## Details

Full executable contract (mirror format, state machine, gene schema, test
matrix): `docs/self-managed-context-spec.md`.
