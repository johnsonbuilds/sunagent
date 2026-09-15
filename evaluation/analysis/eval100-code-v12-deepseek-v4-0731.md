# eval100-code-v12-deepseek-v4-0731 Analysis Report

## 0. Verdict

* Score 48→50 (+2pp), McNemar b=11/c=9 (χ²=0.05), far from significant —
  essentially noise.
* edit-necessity **met expectations**: both named targets (psf-1142,
  pytest-10051) flipped from "zero edits, gate passed, verifier failed" to
  "real edits made, pass"; zero-edit trials 29→23; 7 explicit edit-gap
  rejections, zero false positives.
* rerun **mechanism works but precision is broken**: 140 of 171 re-executions
  failed, ~135 of them with exit-2 shell parse errors — free-text extraction
  grabs trailing prose (backticks, explanatory sentences) and reruns it
  verbatim, instead of catching genuine verify failures. Only 5 exit-1
  failures on clean commands were the real signal.
* Unacceptable cost: `run_command` +82%, total tool calls +46%, trials with
  `max_iter≥45` 40→62, fail-group run mean 22.3→53.8, plus a
  "more-explanation → dirtier-extraction" vicious cycle.
* Overall: **partially meets expectations; fix rerun before keeping it**.
  v13 fixes command extraction only (see §4); the edit gate stays.

* Job: `jobs/eval100-code-v12-deepseek-v4-0731/` (100/100 completed, mean **0.50**;
  control `jobs/eval100-code-v11-deepseek-v4-0731/` mean 0.48)
* Harness chain: `code-v11 --(verification.rerun_declared_command false->true +
  prompt.system advertises edit-necessity/rerun)--> code-v12`
  (tools unchanged `[run_command, read_file, apply_patch, edit_file, write_file,
  grep_search, find_files]`, `max_iterations: 50`)
* Trace check: `harness_id=code-v12` in 100/100 `agent-runtime.jsonl` headers.
* Counting basis: same as the v11 report — `tool.start` authoritative
  (`data.tool`); `verification.failed/passed/rerun` trace events; reward keyed
  by task prefix (`rsplit("__",1)[0]`).

## 1. Headline: 48→50 (+2pp), but essentially noise

* Newly passed 11 (django-12858, seaborn-3187, **psf-1142**, xarray-3305/6599,
  pylint-4661/6386, **pytest-10051**, sklearn-25102, sphinx-10449/7985),
  newly failed 9 (django-12209/13568, sklearn-14087, sphinx-11510/8120,
  sympy-15599/18199/18698/19495).
  McNemar b=11/c=9: χ²=0.05 (3.84 = p0.05) — far from significant.
* The good news is both design-target cases flipped, and both flipped by
  adding real edits (see §3); the bad news is the cost (see §2).

## 2. Mechanism: rerun works, but 96% of rejections are false positives

* `verification.rerun` fired 171 times: only 31 successes, **140 failures** —
  **133 with exit 2, 6 with exit 1**. Sampling the command strings shows
  nearly all failures are shell parse errors
  (`unexpected EOF while looking for matching '`'`), with the root cause
  located: **free-text `command_to_verify` extraction grabs trailing prose**
  (800-char window including markdown backticks and explanatory sentences
  like "this command was just re-run…"), and rerun executes that blob verbatim
  (e.g. `…test_quantity_ufuncs.py -q\`` plus whole follow-on sentences).
  Only 5 failures on clean commands with exit 1 — the actual "unworthy verify
  command" signal rerun was built to catch.
* Worse, a **vicious cycle**: rerun rejection → the model restates with more
  explanation ("exit 2 is a quoting artifact, the command itself succeeds") →
  dirtier extraction → another rejection. Clearly visible over 3–4 rounds in
  the astropy-13977 and psf-1142 traces.
* Cost: `run_command` 2247→**4083 (+82%)**, total tool calls 4208→6135 (+46%);
  trials with `max_iter≥45` 40→62; fail-group run mean 22.3→**53.8**
  (pass group 22.6→27.9 — even passing trials got more expensive).
  Fail-reason mix shifted from v11's `{shape:36, unrun-cmd:5}` to
  `{rerun:140, shape:78, unrun-cmd:11, edit-gap:7}` — gate activity
  (failed events 17→179, trials with rejections 11→66, failed→later-passed
  1→36) is almost entirely rerun-noise churn, with productive repair a
  minority.
* Timeouts 12→10 (net -2) with set churn: 5 newly timed out
  (django-13568, matplotlib-26208, pylint-4970, sympy-16597/19495, of which
  13568/19495 were v11 passes) vs 7 escaped
  (11848, 13449, 15128, 6028, 4075, 7080, 7277).
  13568/19495 "burned the whole budget without ever finishing"
  (zero verification events in v12), plausibly rerun-driven iteration burn;
  the remaining regressions (sympy-15599/18199/18698, sklearn-14087,
  sphinx-11510/8120) mostly died on the summary turn (shape/edit-gap) or show
  no gate events at all — more like noise plus budget pressure. Note the gate
  never changes the score directly (the score depends only on the verifier's
  verdict on repo state); it only steers model behavior.

## 3. The two target cases: edit-necessity delivered

* **psf-1142**: v11 (5 runs / 0 edits / iter 9, gate passed, verifier failed) →
  v12 (33 runs / 3 edits / iter 50, reward 1.0).
  Three rerun rejections along the way (all exit-2 dirty strings, plus one
  unrun-cmd), then a genuine rewrite of `prepare_content_length()` plus a
  runnable verify script passed.
* **pytest-10051**: v11 (1 run / 0 edits / iter 8) →
  v12 (19 runs / 4 edits / iter 28, reward 1.0); same 3×exit-2 pattern, then a
  cleanly declared command with a rerun exit-0 accept on the 4th attempt.
* Globally: 7 explicit edit-gap rejections (13398, 23476, 25775, seaborn-3069,
  sphinx-11510/8120, sympy-14248), all ending at reward 0 —
  the edit gate alone doesn't convert to passes; it forces edits, and passing
  still requires a correct fix. Zero-edit trials 29→23, and **zero-edit never
  passed the verifier in either job**, confirming the "a real fix necessarily
  writes" premise.

## 4. Next step (v13: fix command extraction only)

1. **Fix extraction, two options**: (a) extract only from fenced code blocks /
   JSON values / the first line, stripping backticks and trailing prose;
   (b) cleaner — rerun never executes the declared string but the history
   command grounding already matched (`_command_grounded`'s hit), using the
   declared string for matching only. Either zeroes out the exit-2 noise class
   and leaves exit-1 as the true signal.
2. **Keep the edit gate**: cheap and proven (both named targets converted,
   7 rejections with zero false positives).
3. Re-run eval100 after the fix; acceptance metrics: exit-1 share of rerun
   failures, number of rerun-triggered trials, nudge-recovery rate back near
   v11 levels, fail-group run mean back down.
4. Housekeeping: `evaluation/records/` still has no v12 record (only up to
   v11); suggest `harness record` to add one before the report.
