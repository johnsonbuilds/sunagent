# eval100-code-v8 Analysis Report

* Job: `jobs/eval100-code-v8/` (100/100 completed, mean **0.41**: 41×1.0, 59×0.0)
* Baselines: `jobs/eval100-code-v7/` (41×1.0, mean 0.41); `jobs/eval100-code-v7-2/` (44×1.0, mean 0.44, tool-describe-only change, judged noise)
* Harness difference is a single gene: `harnesses/code-v8.yaml = code-v7 + tools.enabled+=grep_search,glob_files`
  (`[run_command, read_file, apply_patch, edit_file, write_file]` → add `grep_search, glob_files`)
* Counting basis: `tool.start` authoritative; `llm.start` role=tool/user messages used only for error-kind dedup by `tool_call_id`
* Related code: `src/agent_runtime/tools/search.py:82-182`, specs `src/agent_runtime/tools/tools.py:221-263,344-345`

## 1. Headline: pass non-regression, efficiency win

* Net zero vs v7 (41 vs 41): newly passed 6
  (matplotlib-25332, requests-1724, sklearn-25102, sympy-12419/15599/18698),
  newly failed 6 (django-12858/13568, requests-1921, xarray-6599/6938, sklearn-13124).
  McNemar 0 — pure noise. vs v7-2 (44): +5/-8, also noise.
* Efficiency is the only real win: total calls 4103→3862 (-6%),
  `run_command` 3101→2793 (-10%, -308), mean llm rounds 40.6→36.6 (-10%), median 51→44.
  Trials using `run_command` 100→93, `read_file` 97→92 — 7 trials dropped shell entirely.
* `edit_file` back to v7 level (147→146, trials 77→74); `apply_patch` 21→16 calls,
  error rate flat (15/6 vs 12/4, ~71%→75% err, n too small).

## 2. Adoption: grep_search half-win, glob_files failure

* `grep_search`: 122 calls / 44 trials, 120 success + 2 error (1.6% err) — willing uptake and reliable
  (contrast `apply_patch` ~75% err). Pass among users 19/44=43% vs non-users 22/56=39% (+4pp, n.s., at least not toxic).
* `glob_files`: 3 calls / 3 trials — undiscovered. `ls/find` habit via shell persists.
* `write_file`: first-ever adoption, 1 call / 1 trial (requests-2931, passed). Single sample, no signal;
  zero-use on SWE-bench remains expected (no full-rewrite tasks).
* `read_output`: still 0.

## 3. Friction: model priors vs schema

* Hallucinated `grep` (3 trials: requests-1766, xarray-6599, pytest-5809): `unknown tool: grep`.
  Adding `grep_search` activated the short-name association without capturing it.
* Wrong kwargs from pretraining (2 trials): `grep_search() got an unexpected keyword argument 'output_mode'`
  (astropy-14995, repeated), `'type'` (django-14007). Model expects ripgrep-style schema,
  ours is `pattern/path/include/ignore_case/max_results`.
* History-inflation warning: repeating tool errors accumulate in `llm.start` messages;
  unique-`tool_call_id` dedup required (same lesson as v7-2 `malformed JSON` single-trial case).

## 4. Comparison notes

* v7-2 (tool-describe-only) already judged no-effect (41→44 noise, patch behavior 21→22 calls, 10→10 trials);
  v8 result (41) confirms the 41–44 band is the current plateau for `Ling-3.0-flash` on eval100.
* v8 did not convert the v7 "zero-write" (22/59) or "edited-but-failed" (37/59) categories into passes at scale;
  flips are symmetric, no repo-level pattern.

Conclusion: v8 `+grep_search/glob_files` non-regression holds with ~10% search-cost saving. Keep, don't expand.

## 5. Next steps (one gene at a time)

* Fix schema, don't add tools: alias or explicitly reject `output_mode/type` in `grep_search` description;
  add `grep` alias or "do not call it grep" line; give `glob_files` a basename example so it gets discovered.
* Then re-test stronger LLM on the winner harness (pass/$, not just pass), per plan.
