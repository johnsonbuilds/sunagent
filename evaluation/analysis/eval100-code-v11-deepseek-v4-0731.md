# eval100-code-v11-deepseek-v4-0731 Analysis Report

* Job: `jobs/eval100-code-v11-deepseek-v4-0731/` (100/100 completed, mean **0.48**: 48×1.0, 52×0.0)
* Baselines (same dataset, same 100 tasks):
  `jobs/eval100-code-v9-deepseek-v4-0731/` (40×1.0, mean 0.40 — same model, direct control);
  `jobs/eval100-code-v9/` (43×1.0, mean 0.43 — old model)
* Harness chain (one gene each, lineage clean): `code-v9 --(verification.mode off->return_contract)--> code-v10
  --(prompt.system empty->return-contract instruction)--> code-v11`
  (tools unchanged `[run_command, read_file, apply_patch, edit_file, write_file, grep_search, find_files]`,
  `max_iterations: 50`, `rerun_declared_command: false`)
* Trace check: `harness_id=code-v11` in 100/100 `agent-runtime.jsonl` headers.
* Counting basis: `tool.start` authoritative (`data.tool`); `verification.failed/passed` trace events;
  reward keyed by task prefix (`rsplit("__",1)[0]`).

## 1. Headline: +8pp vs same model (48 vs 40), largest single jump so far

* vs v9-deepseek: newly passed 14
  (django-12209/13568/14007, matplotlib-25332, xarray-4094, sklearn-12973/13124,
  sphinx-11510/9591, sympy-12419/15599/18199/18698/19495),
  newly failed 6 (django-11848/15128, psf-1142, xarray-3305, pytest-10051, sphinx-10449).
  McNemar b=14/c=6: χ²=3.2, p≈0.07 — suggestive, not yet 0.05-significant.
* 9 of the 10 v9-deepseek regressions (v9-pass→v9d-fail) are repaired in v11
  (all except pylint-6386); the price is 6 fresh regressions, 2 of them timeouts.
* vs v9 baseline (old model): +9/-4 (net +5): new astropy-13977, django-13568/15161,
  xarray-6744, pytest-5840, sklearn-12973, sphinx-11510, sympy-15599/18199;
  regress psf-1142, xarray-3305, pylint-6386, pytest-10051.
* Timeouts 9→12 (`AgentTimeoutError`: 14725/13449/12858/4075/15128/8898/6028/12209/13212/11848/7277/7080).
  Note django-12209 appears in BOTH pass and timeout lists: agent timed out at iter 30
  but edits persisted and the verifier passed.

## 2. Mechanism: the upfront ad worked, the nudge rarely fired

* Tool volume +13% (3725→4208): `run_command` 1994→2247 (+13%; pass-group mean 18.7→22.6,
  fail-group 20.8→22.3), `grep_search` 577→695, `read_file` 897→989.
  `find_files` flat (110→102), `edit_file` flat (107→109), `apply_patch` 30→40.
* `verification.failed`: 17 events in only 11 trials; 5 recovered to pass
  (astropy-14309, matplotlib-25332, sklearn-11578, sphinx-8120, sympy-19495 → 45% recovery).
  89 trials passed the gate on the first finish attempt — the model plans the 3-section
  return from round 1 (system-prompt advertisement), so the feared
  "always-collide-then-cosmetically-patch" loop did not materialize.
* Repair anatomy is "search→edit→run" completion, via the prompt not the nudge:
  sympy-18698 (0 edit→edit+patch, gate passed first try at iter 51),
  django-12209 (run 17→30 + 1 edit), sphinx-9591 (run 1→14 + 1 edit).
* Nudge-recovery example: matplotlib-25332 failed once on grounding, re-ran, passed.

## 3. Two new problems

**(a) The gate checks "ran + cited" but not "changed" — 2 regressions walked through open.**
psf-1142 (29→9 iters, 5 runs / 0 edits, gate accepted, tests fail),
pytest-10051 (28→8 iters, 1 run / 0 edits, accepted at iter 8).
Both quote real output and name a command they ran, so shape+grounding pass with zero
source edits. Zero-edit&fail barely moved (31→29). A real fix necessarily writes;
v12 candidate: require ≥1 `edit_file/apply_patch/write_file` in history, else reject
as missing `solution_description`.

**(b) Volume costs budget.** +13% tool calls → +3 timeouts (new: xarray-4075, psf-6028,
django-13212; plus django-11848, the v9d robustness win, lost to timeout).
50 iterations now bind; either reserve verify budget or accept the exchange rate.

## 4. Next step (one gene at a time)

* v12: grounding += edit-necessity (reject edit-less finishes). Expected: convert the
  psf-1142/pytest-10051 class from "gated but wrong" into "forced to attempt a patch";
  watch timeout count (extra forced edits cost rounds) and the 5/11 nudge-recovery rate.
* Deferred: `rerun_declared_command=true` (re-execute the declared command once) stays off
  until the edit gate proves out — no gold-test execution in v11 by design.
