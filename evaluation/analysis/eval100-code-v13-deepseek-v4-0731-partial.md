# eval100-code-v13-deepseek-v4-0731 Analysis Report (PARTIAL)

> **PARTIAL — job manually terminated after 83 judged trials.**
> `jobs/eval100-code-v13-deepseek-v4-0731/`: 87 trial dirs scraped,
> 83 with verifier reward (40×1.0, 43×0.0, mean **0.482**),
> 4×`CancelledError` (no reward, killed by termination),
> 11×`AgentTimeoutError`. All subset comparisons below use the 83 judged
> trials only and are therefore noisier than full-eval deltas.

* Harness chain: `code-v12 --(verification.mode return_contract->task_result
  + tools.enabled +submit_result + prompt.system rewritten + finish-violation
  fuse + rerun replays matched call with cwd/600s + prompt declares suite
  per-repo, exact-as-run, concise output)--> code-v13`
  (tools `[run_command, read_file, apply_patch, edit_file, write_file,
  grep_search, find_files, submit_result]`, `max_iterations: 50`,
  `finish_violation_limit: 3`, `rerun_declared_command: true`)
* Trace check: `harness_id=code-v13` in 87/87 `agent-runtime.jsonl` headers.
* Counting basis: same as v11/v12 reports — `tool.start` authoritative
  (`data.tool`); `verification.*` trace events; reward keyed by task prefix
  (`rsplit("__",1)[0]`); agent wall time from trial `result.json`
  `agent_execution`.

## 1. Headline: 40/83 (48.2%), flat vs v11/v12 within noise

* Overlap vs v11 (83 tasks): v11pass=42, v13pass=40.
  Newly passed 7 (django-14725, seaborn-3187, **psf-1142**, xarray-6599,
  **pytest-10051**, sklearn-25102, sphinx-7985);
  newly failed 9 (django-12209/13568, psf-1766, sklearn-12973,
  sphinx-11510, sympy-15599/18199/18698/19495).
  McNemar b=7/c=9: noise.
* Overlap vs v12 (83 tasks): v12pass=44, v13pass=40.
  Newly passed 3 (django-14725, sklearn-14087, sphinx-8120);
  newly failed 7 (django-12858, psf-1766, xarray-3305, pylint-4661/6386,
  sklearn-12973, sphinx-10449).
  McNemar b=3/c=7: noise.
* Both design targets **psf-1142 and pytest-10051 still pass** under v13 —
  no regression on the edit-necessity cases.
* Cost: agent_exec median **30.3 min** (v12 full-eval median 24.6),
  mean 33.0, 8 trials pinned at the 60.0 min trial cap, 11 timeouts.
  Per-trial split: LLM **7.9 min** vs tools **22.9 min** (~1:3).

## 2. Mechanism: the gate is quiet and precise; the budget is the bottleneck

* Gate activity collapsed vs v12: `verification.failed` **51 events in 47
  trials** (v12: 179 in 66); `verification.passed` 23; fuse `aborted` **0**;
  plain-text finish nudges **1** across all 87 trials — the model submits
  via the tool immediately. Edit-gap events **0** (edits happen upfront);
  zero-edit trials 19/87, **0 passed** (unchanged invariant: a real fix
  necessarily writes).
* Rerun precision fixed: **28 reruns, 23 ok / 5 fail**, exit codes
  `{0: 23, 1: 5}` — **zero exit-2** (v12: 133 exit-2 from prose-wrapped
  declarations). cd-prefixed declarations (`cd /testbed && …`) dominate and
  exact-match; typed params killed the dirty-string class. `--tb=short`
  appears in declarations (concise-output hint adopted).
* Tool volume down in count, up in duration: 48 tools/trial (v12: 61),
  `run_command` 28/trial (v12: 41); durations median 0.6s / p90 23s with
  **140 runs ≥240s** — "fewer but longer". The full-suite mandate made each
  verify cycle minutes-long (fix → full suite → fix → full suite).

## 3. Three problems

**(a) Matcher program-rule misfires on shell-prefixed commands (6/28 reruns).**
Declared and history commands both start with `cd`/`source`, so the
program token is always `cd`/`source` and a 2-token overlap
(testbed+astropy…) is trivially satisfied. The rerun then executes the
WRONG history command:

* astropy-14369: declared `pytest …test_format.py…`, reran
  `git log --oneline -3 && python -c print(version)` → exit 1 (false reject)
* seaborn-3187: declared `pytest tests/_core/test_plot.py`, reran a
  `python -c import matplotlib…` probe → exit 1 (false reject)
* sphinx-8595: declared `pytest …`, reran a heredoc-python edit script →
  exit 1 (false reject)
* sklearn-12973: declared `pytest sklearn/linea…`, reran `grep -n …` →
  exit 0 (false ACCEPT — trial still failed the verifier)
* sklearn-14053: declared `pytest sklearn/tree`, reran a different pytest
  invocation (env-activate variant) → exit 0 (accept on wrong command)
* sphinx-7985: declared `pytest tests/test_bu…`, reran version-check +
  `pip download` → exit 0 (false ACCEPT — trial passed the verifier)

2 false rejects cost iterations; 3 false accepts weaken the guarantee.
The `--tb=short` adoption does not help here — this is matching logic.

**(b) 25/40 passes never touched the gate.**
25 passing trials have zero `verification.passed` events; their terminal
event is `iter 51: budget exhausted without a valid submit_result call`.
`max_iter` p50=51, 50 trials ≥45 iterations. The model works diligently to
budget (fixes land), but long full-suite runs leave no rounds for a closing
submit. The gate shaped behavior (edits upfront, submit-first-try) but
decided only ~15/40 outcomes. 11 timeouts + 8 trials at the 60-min cap are
the same phenomenon at the extreme.

**(c) Prompt micro-edits split.**
cd-prefix declarations: worked (exact-match rate high, half the credit for
zero exit-2). Concise-output hint: adopted (`--tb=short` visible) but saved
no time — the bottleneck is execution, not verbosity. Repeated full suites
(xarray-6721/6744 ~1300s×2 each, pylint-8898 ×2) dominate; single monsters
(django-15128 3239s, sklearn-14087 2420s) exceed any sane per-command cap.

## 4. Next step (fix approach PENDING — not decided)

* Matcher misfire: candidate fix is stripping leading shell-setup segments
  (`cd`/`source`/`export`/`conda … &&`) before program extraction so the
  real program is compared. **Held: owner explicitly deferred the approach
  decision ("不打算这么修").**
* Budget structure: with 25/40 gate-bypassed passes, gate accuracy matters
  less than reserving closing budget (e.g. forbid `run_command` in the last
  N iterations, via prompt or control gene). Also pending decision, and a
  lineage change — keep separate from any matcher fix.
* Long single runs (3000s+): whether to cap is a third, separate question.
* Cherry-pick audit (prompt-assigned fullness): pass-trial declaration
  shapes + evidence-count distribution still to be tabulated from these
  traces once the matcher question is settled.
