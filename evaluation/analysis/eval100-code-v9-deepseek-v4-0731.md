# eval100-code-v9-deepseek-v4-0731 Analysis Report

* Job: `jobs/eval100-code-v9-deepseek-v4-0731/` (100/100 completed, mean **0.40**: 40×1.0, 60×0.0)
* Baseline: `jobs/eval100-code-v9/` (100/100 completed, mean **0.43**: 43×1.0, 56×0.0 + 1 missing trial counted in n_trials=99)
  * Reference band: `eval100-code-v8` 0.41 (41/59), `eval100-code-v7-2` 0.44, `eval100-code-v7` 0.41
* Harness: identical — `harnesses/code-v9.yaml` (`code-v8 + glob_files->find_files` rename;
  tools `[run_command, read_file, apply_patch, edit_file, write_file, grep_search, find_files]`, `max_iterations: 50`, `verification.enabled: false`)
* Variable is model only (job-name `deepseek-v4-0731`; `config.json`/`lock.json` do not record model name —
  model switch inferred from name + behavior shift below, not from artifact field)
* Counting basis: `tool.start` authoritative (`data.tool`); `llm.start/end` only for iteration counts.
  `result.json` has `n_input/output_tokens: null`, so no token/$ comparison possible.

## 1. Headline: no performance gain (-3pp, noise)

* Net -3 (40 vs 43): newly passed 7
  (astropy-13977, django-11848/15128/15161, xarray-6744, pytest-5840, sphinx-10449),
  newly failed 10
  (django-12209/14007, matplotlib-25332, xarray-4094, pylint-6386,
  sklearn-13124, sphinx-9591, sympy-12419/18698/19495).
  `se≈4.9%` at n=100 — within noise. Same vs v8: +8/-9.
* Timeouts flat: deepseek 9×`AgentTimeoutError` vs v9 10×`AgentTimeout`+1×`VerifierTimeoutError`.
  Overlap on django-13449/12858/13568, pylint-8898, matplotlib-24149; deepseek adds
  sphinx-9591/django-14007/matplotlib-25332/25775, drops sklearn-25102/matplotlib-26342/26113/22719/django-11848.
* Only clear robustness win: `django-11848` — v9 baseline did 1 tool / 1 iter (instant abort),
  deepseek did 21 tools / 20 iters (`run9+grep5+find2+read4+edit1`) and passed.

## 2. Tool use: DID improve — in exactly the harness-intended direction

Total `tool.start` 3973→3725 (-6%), but composition flips:

| tool | v9 baseline (calls / trials using) | deepseek-v4 (calls / trials using) |
|---|---|---|
| `run_command` | 2812 / ~100 | 1994 / ~100 (−29%) |
| `grep_search` | 174 / 53 | 577 / 96 (+3.3×, 53%→96%) |
| `find_files` (`v9` rename of `glob_files`) | 2 / 1 | 110 / 58 (1%→58%) |
| `read_file` | 821 | 897 (+9%) |
| `edit_file` / `apply_patch` / `write_file` | 146 / 16 / 0 | 107 / 30 / 9 |
| `tool.error` | 19 (`apply_patch` 9, `grep_search` 6, `edit` 2, `grep` 2) | 8 (`edit` 4, `grep_search` 3, `apply_patch` 1) |

* v8/v9 intent was "native search replaces `rg/grep/find/ls` via shell; expect `run_command` share to drop"
  (`harnesses/code-v8.yaml`, `code-v9.yaml`). Baseline model never adopted it
  (`glob_files` 3/100 in v8, `find_files` 2/100 in v9). Deepseek is the first model to comply:
  grep coverage 53%→96%, find coverage 1%→58%, `run_command` −818 calls.
* Correctness also up: `tool.error` 19→8; hallucinated bare `grep` (2–3 trials in v8/v9) disappears (0 in deepseek).
* Pace: tools/trial mean 39.7→37.2, median 46→37; iters/trial mean 38.3→34.0;
  trials hitting the 50-iter ceiling 47→37. Less wall-hitting, not just fewer calls.

## 3. Why no score gain: search-rich, edit-poor

* Passing still requires a write in both jobs (zero-edit & pass = 0 in both).
  Zero-edit & fail rises 25→31 (+6) — the extra search did not convert to patches.
* `run_command` drops in *both* cohorts, so it is model style, not outcome:
  pass-group mean 27.0→18.7 (med 24→16), fail-group mean 28.6→20.8 (med 31→19).
  `grep_search` rises symmetrically (pass 1.6→5.6, fail 1.9→5.9). Static search substituted
  dynamic repro/pytest, not just `ls/grep` exploration.
* Regression anatomy (v9→deepseek per-task):
  * early-abandon, zero-edit: `django-12209` 50/51→24/17 iters, `matplotlib-25332` 51/51→18/13,
    `xarray-4094` 50→27, `sphinx-9591` 53→27 with only **1×`run_command`** (never tested);
  * searched-but-never-committed: `pylint-6386` 52→57 tools (`grep12+read34`, 0 edit vs baseline 2 edits),
    `sympy-12419/18698/19495` all ~50 iters, 0 edit;
  * `sklearn-13124` is the exception that searched *more* (25→45 tools) yet still flipped pass→fail.
* New-pass anatomy is the mirror: all 7 contain 1–3 edits with a full `grep+find+read+edit` chain,
  e.g. `sphinx-10449` (`find9+grep17+read21+edit1`), `astropy-13977` (62 tools, `edit3+patch1`).
  `django-15161` passed via brute force (77 tools, `run72`) — the only non-search win.

## 4. Conclusion / next step (one gene at a time)

* v9 `glob->find` rename is validated for discoverability *by this model* (1%→58%), but
  localization is no longer the bottleneck — edit-commit + edit→verify loop is.
* Do not chase another search tool. Enforce the micro-loop already proposed in v7-T2:
  `verification.enabled=true` or prompt/harness rule "every `edit_file/apply_patch` must be followed
  by a targeted `run_command` (repro/pytest) within N rounds; reject zero-edit submissions".
* Validation for next run: zero-edit-fail count 31→≤25, `run_command`-within-3-rounds-after-edit ratio,
  first-edit round median, with pass as primary and the 10-regression list as must-watch.
