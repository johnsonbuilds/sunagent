# eval100-code-v16-ling-flash Analysis Report

* Job: `jobs/eval100-code-v16-ling-flash/` (97/100 evaluated, mean **0.43**: 43×1.0, 54×0.0;
  3 infra losses excluded from eval: 2×`EnvironmentStartTimeoutError` on matplotlib-24149/25775
  — no agent log — plus 1×`VerifierTimeoutError` on sphinx-7985)
* Baseline: `jobs/eval100-code-v15-ling-flash/` (100/100 completed, mean **0.45**: 45×1.0, 55×0.0)
* Harness delta (single `control` gene + skill text, both intended P0 from the v15 analysis):
  `harnesses/code-v16.yaml` (`parent: code-v15`,
  `control.budget_reminder.at_fractions [0.6,0.85]->[0.25,0.6,0.85]`, i.e. new nudge at step 20/80);
  `skills/coder/SKILL.md` (edit routing `apply_patch default`->`edit_file default`;
  appended pytest `> /tmp/pt.log 2>&1 && tail` template + no-pipe rule).
  Model fixed: `Ling-3.0-flash`. `result.json` has `n_input/output_tokens: null`, so no token/$ comparison.
* Counting basis: `tool.start` authoritative (`data.tool`); `llm.end` only for iteration counts;
  unique `run_command` strings deduped by tool_call id (raw `llm.end` replays full history).

## 1. Headline: flat score (-0.7pp evaluated, noise), changed composition

* Paired on 98 shared instances: 47×(0,0), 38×(1,1), **5 newly fixed**
  (astropy-8872, psf-1766/1921/2317, sympy-12419),
  **7 newly broken**
  (django-15128, seaborn-3069, requests-1142, pylint-6386,
  sklearn-12973/13439/14496), 1 infra-lost (sphinx-7985: v15 fail, v16 unevaluated).
  Net −2 on evaluated pairs; `se≈5%` at n≈100 — within noise, same verdict shape as the v9 report.
* Per-repo (paired): psf 29%→57%, astropy 40%→50%, sympy 30%→40%, sphinx 50%→56% up;
  sklearn 73%→45% (n=11), django 60%→50%, pylint 22%→11%, mwaskom 50%→0% (n=2) down;
  pydata/pallets/pytest-dev/matplotlib flat.

## 2. P0-3 early nudge (step-20 reminder): WORKS — the only clear win

* `budget.reminder@20` fired 94×; **43/94 (46%) had zero source edit at step 20** — the nudge hits the target population.
* `reminder@48 still-unedited` 17→9; trials hitting the 80-iter ceiling 30→20;
  `budget exhausted without submit` 29→21; `no_edit` trials 9→7.
* Pass group converges much faster: duration mean/med 309/296s→**236/210s (−24%)**,
  llm steps 51.6→42.2, tool calls 50.6→41.4, first-edit median 13→11.
* Side effect: fail-group first-edit median got *slower* (18.5→21) — the nudge accelerates
  trials that can converge; doomed explorations still drift (4 of the 7 regressions hit step 81).

## 3. P0-1 edit-routing reversal (`edit_file` default): PARTIAL

* `apply_patch` calls 39→32, errors 29→19, success rate 26%→**41%**;
  `edit_file` stays dominant (281→224 calls, ~97% success both jobs).
* Residual: 5× `--- a/`-prefix rejections persist in v16 — skill text reduces usage,
  does not fix the habit. Parser tolerance (accept `--- a/` prefix) is still open.

## 4. P0-2 pipe template: FAILED — 2% adoption, remove it

* `/tmp/pt.log` template appears in only **21/997 (2%)** of unique pytest commands;
  pipe rate 85%→87%; pipe-gate rejections 35→38; `was never run` 39→49; `-k` usage flat (253→257).
* The 5 newly-fixed trials still converged *through* pipe/file-less friction (e.g. requests-1921:
  4 gate rejections incl. pipe + file-less, then pass) — the gate burns rounds without blocking
  the capable, while the weak never adopt the template.
* Verdict: text-only compliance does not move this model. **Delete the template from the skill**
  (done post-analysis); pipe friction must move to the code layer
  (tool-side `| tail` auto-rewrite or gate auto-normalization). Kept the one-line no-pipe rule
  out as well — rejections prove it is not read either.

## 5. Gate semantics sharpened (intended + unintended)

* `gold-pass-without-gate` (fallback lucky submits) **11→0**: every v16 gold pass went through
  `verification.passed` (vpass events 61→72). The gate now vets everything.
* Cost: 3 of the 7 regressions (django-15128, seaborn-3069, requests-1142) are exactly v15's
  fallback lucky passes — blocked correctly, but the agent could not convert them into compliant
  submits and burned to step 81. Block-without-redirect loses winnable trials.
* `verification.rerun` failures now catch *real* test failures with detail
  (5× exit-1 with failing-test excerpts in v16 vs 1 opaque case in v15), e.g. sklearn-12973
  (`1 failed, 29 passed … AssertionError`) — true negatives the v15 gate would have waved through.
* False positives persist: gate-passed-but-gold-fail 27→29 (pylint-6386, sklearn-14496:
  clean submits, wrong patches). Narrow declared commands exit 0 without covering gold tests.

## 6. Conclusion / next step (one gene at a time)

* Keep: step-20 nudge (only proven win) + `edit_file`-default routing.
* Reverted: pytest pipe template (this file documents the 2% adoption evidence).
* Next harness gene (code-v17 candidate, code layer, not skill text):
  1) pipe auto-normalization at `run_command`/gate level;
  2) `apply_patch` `--- a/`-prefix tolerance + unterminated-block recovery hint;
  3) `grep_search` `-i`/`output_mode` alias tolerance (10 errors in v16, unchanged by any prompt).
* Validation for next run: pipe-gate rejections 38→~0 (by construction, not compliance),
  `hit80` 20→≤15, `was never run` 49→≤30, pass as primary with the 7-regression list as must-watch.
  sklearn-14496-class slow-trial variance (1304s single trial) stays a watch item for timeout policy,
  not agent quality.

## 7. Two dimensions: harness-fixable vs needs-stronger-model

*Rule of thumb used below: harness-fixable = the model shows it can do the task but trips on
interface/protocol (and prompt text demonstrably does not move it — 2% template adoption);
model-limited = the artifact itself is wrong despite the information being available
(wrong patch, wrong localization, no-edit drift).*

*Harness-fixable (≈ +3~6pp, cheap, deterministic):*
1) pipe compliance (38 rejections + `was never run` cascade, 87% pytest piped) — auto-normalize
   at tool/gate layer; 2) `apply_patch` `--- a/` + unterminated-block tolerance (19 errors);
3) `grep_search` `-i`/`output_mode` aliases (10 errors); 4) hallucinated-tool fuzzy match
   (`unknown tool: grep`×3); 5) `pip install` waste (**298** unique calls in v16, ~3/trial, worse
   than v15's 245) — pre-execution intercept returning "environment ready";
6) `was never run` (49) — auto-attach the exact history command into the contract nudge
   (redirect, not just block; this is where the 3 ex-lucky trials died);
7) narrow-command false positives (29) — gate-check declared files ⊇ edited files + neighbours
   (prompt already demands it, gate only checks "names a file").

*Needs-stronger-model (≈ +8~15pp ceiling, expensive):*
1) clean-submit wrong patches (the 29 fp, e.g. pylint-6386, sklearn-14496 — root-cause
   localization is a reasoning gap no exit-code gate can judge);
2) zero-edit drift (7 no-edit all-fail; fail-group first-edit median 21; 43/94 still unedited
   at step 20 — the nudge reminds, only judgment commits);
3) repo-complexity gradient (matplotlib 12–17%, pylint 11–22% vs pydata/sklearn 45–64%);
4) weak feedback loop (sklearn-12973: explicit rerun failure excerpt, still unfixed in 27 steps);
5) long-context contract following (`was never run`×49, plain-text submits×16).

*Ordering: harness first (it also de-noises the metric — v16 already removed 11 lucky passes,
so a stronger model swapped in now gets an honest, higher-ceiling benchmark), model second.
Rough map from 44%: harness → ~47–50%, stronger model on top → 55–60% band.

*Standing constraint for all harness work (general-agent goal): no task-specific logic in
runtime code — no repo names, no per-task branches, no benchmark-only string matches in
`src/agent_runtime/`. Audit 2026-09-24: code is clean (only pytest/testbed *examples* in
descriptions: `tools.py:394,399`, `verification.py:49,284`, `loop.py:73`). Borderline items
must stay config-driven: pipe/redirect normalization (generic shell semantics) and
edited-files-⊇-declared-files (history mechanism) are fine as code; `pip install`
interception must be a guard *policy* (`execution/guard.py` pattern, declared per-harness),
not `if "pip install" in cmd`; the pytest-shaped constants (`_FILE_TARGET_RE`,
`_TEST_RAN_RE`) are the next refactor target — promote to per-harness
`verification.test_file_pattern` / `test_ran_pattern` genes when touching that area.
