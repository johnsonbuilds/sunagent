# eval100-code-v6 Analysis Report

* Job: `jobs/eval100-code-v6-1/` (100 tasks, 4 parallel, `code-v6` = code-v4 + max_iterations 50)
* Wall time: 2026-09-08 13:27 → 18:03 (4.6h)
* Dataset: `evaluation/swe_bench/eval-100.json` (smoke-10 + dev-50 + 40 new tasks),
  difficulty based on manifest human-estimated fix duration: Low `<15min` / Medium `15min-1h` / High `1-4h+`
* Trial median 7.5min, mean 10.6min, max 62.8min

## 1. Total Score: 43/99 = 0.43 (3 errored, counted separately)

3 errors unrelated to model: 2×`AgentTimeoutError` (pylint-4661/4970,
agent 3600s timeout), 1×`VerifierTimeoutError` (sphinx-7590, test suite exceeded 1800s).

## 2. Difficulty Stratification (monotonic, proxy empirically validated)

| Tier | Passed | Pass Rate |
|------|--------|-----------|
| Low (37 tasks) | 22 | 0.59 |
| Medium (43 tasks) | 19 | 0.44 |
| High (19 tasks) | 2 | 0.11 |

The 2 high-tier passes were both django (13449 smoke + 14007). This stratification can serve as an official difficulty label going forward.

## 3. smoke-10 Subset: 7/10, identical to historical best

Passed: astropy/django/pylint/psf/pytest/sklearn/sympy;
Failed: sphinx (f2p 0/2), matplotlib (f2p 1/1 + p2p 71/81, fixed target but broke regression),
xarray (f2p 0/1). Failure signatures consistent with the previous 5 smoke rounds, reproducible.

## 4. Verifier Health: Clean

* No insane totals (81/25470 class-bug zero recurrence).
* Exit distribution: `0:40 / 1:55 / 2:3 / 4:1`, all semantically normal.

## 5. Tool Usage & Circuit Breaker

* Call counts: `run_command 3109 / read_file 859 / edit_file 193 / apply_patch 23`
  (edit:patch ≈ 8:1, model votes with its feet for edit_file).
* Total 4184 tool calls across all runs, only 20 errors (0.5%):
  `unexpected_arg 8 / unterminated 3 / bad_header 3 / match ambiguity 2 / edit mismatch 4`.
* `loop.guard` triggered 3 times (matplotlib-26113, xarray-4094, sphinx-8595),
  all from consecutive apply_patch failures, `consecutive=3` exact trigger.

## 6. Failure Classification (56 fail, next-phase goldmine)

| Category | Count | Meaning |
|----------|-------|---------|
| Missed target, no regression | 24 | Pure capability gap (includes sphinx class with 0 edits) |
| Missed target + regression | 18 | — |
| **Target fixed, regression failed** | **10** | e.g. xarray-3993 (f2p 2/2, p2p 2374/2398, only 24 broken) |
| **Multi-test target partially achieved** | **4** | e.g. astropy-13977 (12/20), django-13212 (3/5) |

The latter 2 categories (14 near-misses) are the shortest path from 0.43→0.55.

## 7. max_iterations=50 Verdict: Zero False Positives

* Among 43 passes, the latest successful edit was at iteration 38 (median 14); 9 passes ran to iteration 51 which is verification tail.
* 46/100 hit the cap (51 = 50 iterations + summary round), of which 36 were fails; among fails, 12 had zero successful edits throughout — more iterations wouldn't help, the cap saves pure waste.

## 8. Image GC: Perfect Score

100 images pulled, only 1 remaining; disk 55GB → 80GB available; no false positives.

## 9. Next Steps (by ROI)

1. 14 near-misses → edit→test→fix micro-loop (see Q&A section below for details).
2. High-tier 2/19 → switch model or drop; stop tuning on smoke (overfitting risk).
3. sphinx total wipeout → standalone project (won't even write patches, unrelated to editor).
