# eval100-code-v16-ling-flash-3 Analysis Report

* Job: `jobs/eval100-code-v16-ling-flash-3/` (99/100 evaluated, mean **0.455**: 45×1.0, 54×0.0;
  1 errored trial excluded)
* Baseline: run2 (`jobs/eval100-code-v16-ling-flash-2/`, 100 evaluated, 43.0%)
* Code delta under test: `HarborWorkspace.write_file` chunked writing
  (committed `5f99aa3`, job started after it; ≤64KB encoded = byte-identical legacy
  single command, above = 32KB pieces + decode-and-move). Harness still `code-v16`.
* Counting basis: same as previous reports.

## 1. Headline: 43.0% → 45.5% (+2.5pp), best v16 run

* Paired on 99 shared instances: 49×(0,0), 37×(1,1), **8 fixed**
  (django-12209, requests-1724, xarray-6938, pylint-6386,
  sklearn-11578/14053/25102/25973),
  **5 broken** (django-14007, seaborn-3069, xarray-6744, sklearn-12973/13124).
* Movers are concentrated: sklearn 55%→73%, psf 71%→86%, pylint 11%→22%;
  astropy/django/matplotlib/pallets/pydata/pytest-dev/sphinx/sympy counts **exactly flat**;
  mwaskom 50%→0% (n=2; seaborn-3069 ping-pongs pass→fail→pass→fail across the four runs).
* Notable individual recoveries: xarray-6938 (the documented write-fail casualty in the
  run2 report) and sklearn-14053 (the unknown-tool trial) both fixed; django-12209
  (zero-edit in v15) fixed.

## 2. Infra fix verified in production: write-fail 10 trials → 0

* Zero `cannot write … Argument list too long` errors (run2: 10 trials; run1: 12 hidden).
  `apply_patch` errors 21→0 with usage collapsing 21→1 call (1/1 success) — the tool
  is effectively retired by the edit_file-default routing, and its former error mass
  did not migrate elsewhere.
* `tool.error` total 37→**15 (−60%)**; error-status trials 10→6; no-edit 11→8;
  memory summaries 97→76. Remaining errors: `output_mode`×5 + `-i`×2 (item 3 alias
  table, now the #1 category by itself), bare-`grep` hallucinations ×5 (variance:
  run2 had 0; the factual catalog still only repairs, never prevents),
  `bash_execute`×1, legit edit_file match errors ×2.
* Fail-group duration improved (370s→289s mean) while pass-group held (255s);
  first-edit medians flat (11/19.5). The gain came from removing friction, not from
  faster convergence — consistent with an infra fix.

## 3. Conclusion / next step

* The ceiling argument from the run2 report is confirmed: with large writes unblocked,
  the same harness+model went from 43.0% to 45.5% with zero new agent behavior.
  `apply_patch`'s 0% success in run2 is fully explained (9 infra + 12 correct
  rejections) — no parser over-blocking occurred.
* Next binding constraint is now unambiguous: grep argument aliases (7/15 remaining
  errors, item 3, already scoped) — then pipe normalization (item 1).
  Tool-error mass is small enough (15/100 trials) that further harness work should be
  weighed against model-side gains (29 prior false positives need a stronger fixer,
  not cleaner plumbing).
* Validation for next run: `output_mode`/`-i` errors 7→0, headline 45.5% held or better
  with the same concentrated-mover pattern (sklearn/psf/pylint) rather than broad drift.
