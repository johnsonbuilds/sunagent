# eval100-code-v7 Analysis Report

* Job: `jobs/eval100-code-v7/` (100/100 completed, mean **0.41**: 41×1.0, 59×0.0)
* Baseline comparison: `jobs/eval100-code-v6-1/` (43×1.0 / 56×0.0 / 3 errors, mean ~0.43; `eval100-code-v6` only 19 completed, excluded from comparison)
* Harness difference is a single line: `harnesses/code-v7.yaml = code-v6 + tools.enabled+=write_file`
  (`[run_command, read_file, apply_patch, edit_file]` → add `write_file`)
* Tool prefer definitions: `src/agent_runtime/tools/tools.py:77-336`
* Counting basis: `tool.start` is authoritative (`llm.end.messages` compressed via `llm_summary` retains only ~70% of calls, used for intent sampling only)

## 1. 59 Zero-Score Tasks

By repo: matplotlib 8/8, mwaskom 2/2, pylint 8/9, django 7/10, sympy 7/10,
sphinx 6/10, pytest 6/11, astropy 5/10, pydata 4/11, sklearn 4/11, psf 2/7, pallets 0/1.

Two main categories:

**A. Never wrote to disk (22/59, zero edit/apply calls):**
astropy-13398/13977/14369, django-14725/15128, matplotlib-25775/26208,
seaborn-3187, requests-6028, pylint-4970/7080/8898, pytest-6197/7236,
sklearn-14087/25102, sphinx-11510/7985/9229, sympy-12419/14248/18698.

* 16 hit the budget ceiling (llm 51 rounds, ntools 42~55): "read-only loop".
  Example `sklearn-14087`: 36×run + 14×read, root cause fully identified
  (`self.multi_class` should be local `multi_class`), but submitted only a text answer citing "won't compile", not a single line changed.
* 6 stopped early (ntools<35): requests-6028, sklearn-25102, sphinx-11510/9229,
  sympy-18698, pylint-4970, early exit after failed localization.

**B. Edited but still failed (37/59):** edit_file 70 successes + 1 failure, apply_patch 15 calls with 8 failures;
22/37 also hit the 50-step ceiling (edited wrong → verification failed → no steps left to revert).
Sub-issues: apply_patch format misuse (matplotlib-24149 passed unified-diff,
sphinx-8595 3 times missing `>>>>>>> REPLACE`, xarray-6992 all 4 failed),
environment issues (astropy-13236 couldn't install erfa/numpy, xarray-3993/6992 30s timeout,
matplotlib-24149/8595 `OSError: Argument list too long: 'docker'`).
Zero-score group 38/59 hit ≥50 calls (pass group only 15/41), mean llm rounds 43.8 vs 35.9.

## 2. Full Tool Call Volume & Prefer Compliance

v7 total 4103 calls: `run_command 3101 (100/100) / read_file 834 (97/100) /`
`edit_file 147 (77/100) / apply_patch 21 (10/100) / write_file 0 / read_output 0`.
All 41 passing tasks contained edit calls (77 total, ~1.9/task) — writing to disk is a necessary condition for passing.

* `run_command` (shell repro/test): 75.6%. Visible subset ~50% is
  `grep/find/ls/git log` exploration, ~20% repro, ~18% pytest. **Compliant in name, substitute in practice:**
  harness didn't enable `grep_search/glob_files/find_symbol`, so exploration had to go through shell.
* `read_file` (paginated read): Compliant; 3 exceptions bypassed prefer:
  matplotlib-25775 (50 calls all run, used `sed -n` to read), requests-6028, sympy-18698.
* `edit_file` (single-point small edits): Compliant, the voted-for default.
* `apply_patch` (batch multi-file): Right scenario, wrong usage, 8 validation failures, fixable.
* `write_file` (v7 new, create/full rewrite): **0/100 zero adoption** — no regression but no benefit either.
* `read_output` (`.outputs/` overflow): 0/100, not triggered or replaced by re-runs.

## 3. Comparison with code-v6

* Net -2pt (41 vs 43), 12/100 flips, within noise range: regressions 7
  (django-12209/14007/15161, matplotlib-25332, requests-1724,
  sklearn-14087, sympy-12419), improvements 5
  (xarray-4094/6938, pytest-10051, sklearn-25973, sphinx-9591);
  both-pass 36, both-fail 49 (hardcore).
* Regression pattern consistent: v7 fewer edits, more shell
  (sklearn-14087 3→0 edit, requests-1724 4→1, django-15161 12→2,
  sympy-12419 read 15→4, run 34→46) — exploration got stuck, not write_file's fault.
* Total calls 4184→4103 (-2%), run flat, edit 193→147 (coverage 88→77 tasks).

Conclusion: v7 `+write_file` non-regression holds, but zero benefit.

## 4. Top 3 Improvements (by ROI, one gene at a time)

**T1. `tools.enabled += grep_search, glob_files` (planned for code-v8)**
Rationale: v7 ~half of run_command doing `grep/find/ls` exploration; 3 bypass-read,
7 regressions including sympy-12419 with plummeting reads — all due to inefficient search → budget burned → zero writes.
Expected: exploration steps halved, the most salvageable of the 22 "zero-write" tasks convert to edits.
Validation: rerun eval100, check run_command ratio, first-edit round median, zero-write count.

**T2. Enforce edit→verify micro-loop (planned for code-v9: `verification.enabled=true` or prompt "apply small patch then run targeted tests")**
Rationale: 38/59 zero-score tasks hit budget ceiling, pass group averages 8 fewer rounds; 37 "edited but failed" mostly wrong edits with no rollback;
sklearn-14087 class "analysis correct, no write" directly convertible.
Expected: move near-misses (edited but failed) and no-edit (analysis done, no edit) categories to both-pass.
Validation: first-edit round, ratio of pytest runs within 3 rounds after edit, narrowing of both-fail 49.

**T3. Fix `apply_patch` schema comprehension + give `write_file` a real role (planned for code-v10: prompt add minimal SEARCH/REPLACE few-shot, write_file limited to "create/full rewrite")**
Rationale: apply_patch 8 failures all format issues (passed diff, missing REPLACE, split args);
write_file 0/100 means schema self-description didn't change model habits.
Expected: apply failure rate → 0, multi-file tasks (django/sphinx series) success rate recovers; write_file either gets adoption or confirmed deletable.
Validation: apply error count, write_file adoption rate, multi-file task pass rate.
