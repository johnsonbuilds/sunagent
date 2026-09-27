# eval100-code-v16-deepseek-v4-1 Analysis Report

* Job: `jobs/eval100-code-v16-ling-flash-6/` (dirname is a misnomer — model was
  `deepseek/deepseek-v4.1-flash` via OpenRouter, per operator statement + `.env`
  at run time; job artifacts do not record the model name, same gap as the v9 report)
* Result: 100/100 evaluated, mean **0.66**: 66×1.0, 34×0.0 (wall 10:45→15:34, ~4.8h)
* Baseline: ling-flash run3 on identical harness+code (`code-v16` + patch validation +
  factual tool catalog + chunked writes + containment/parent-check working-tree changes),
  45/99 (45.5%)
* Counting basis: same as previous reports.

## 1. Headline: +20.5pp from the model swap (66.0% vs 45.5%)

* Paired on 99 shared instances: 43×(1,1), 32×(0,0), **22 fixed, 2 broken**
  (django-12209, django-13568). The +22 list spans 8 repos (sympy×7 incl. a full
  30%→100% sweep, django×4, astropy×2, sklearn×2, mwaskom×2, plus xarray/psf/pylint/
  pytest/sphinx singles).
* Per-repo: sympy 30%→100%, mwaskom 0%→100%, sklearn 73%→91%, astropy/django/sphinx
  50%→70%, rest flat-or-up; matplotlib 12% both (model-independent wall),
  psf/pallets flat-high.
* django-12209 is now zero-edit-fail in **5 consecutive runs** (v15, v16, run2, run3,
  deepseek) across two models — task-level pathology, not a model gap. django-13568
  is the only genuine deepseek regression (1 trial).

## 2. Behavior profile: slower trials, correct tool use, near-zero format friction

* Durations UP sharply (pass mean 421s vs 255s; fail 630s vs 289s) while LLM steps are
  DOWN (40.9/50.4 vs 43.1/57.9): deepseek burns time in long tool executions (rerun
  excerpts show 44–57s suites), not in extra rounds. Cost implication: ~45% more wall
  per trial than ling for +20pp.
* Tool mix shifts: `read_file` −34% (686 vs 1044) with more passes — fewer,
  better-targeted reads; `write_file` 32 vs 17 (repro scripts); `find_files` flat-low
  (14) — unlike deepseek-v4-0731 on code-v9, this model does not lean on native search.
* `apply_patch` is ALIVE here: 35 calls, 29 succ / 6 err (**83%**) vs ling's ~0–40%.
  Format compliance is a model property; the parser improvements were necessary but
  not sufficient — ling never used them, deepseek does.
* `tool.error` total only 12 (vs ling 15–51): **zero** grep-alias errors, **zero**
  unknown-tool hallucinations, zero `--- a/`/unterminated patch errors.
  What remains: edit match errors ×4 (legit), `CHANGES*`/setup.cfg write CommandErrors
  ×5 (see §4), one parent-check firing (§4).
* Convergence discipline: no-edit 2 vs 8; hit80 12 vs 23; error-ends 1 vs 6;
  vpass 88 vs 70. First-edit medians slower (15/22 vs 12/19.5) — deepseek explores
  longer, then commits (only django-12209/sphinx-9229 never edited).

## 3. The pipe paradox, resolved: 85% piped, 1 rejection

* 718/847 pytest commands (85%) contain pipes — identical rate to ling — yet pipe-gate
  rejections are 1 vs ling's 35–38. Deepseek pipes for *exploration* but declares
  clean commands for *submission*: context-appropriate compliance, which no prompt
  template ever produced (2% adoption). This confirms the run2 verdict that prompt
  text cannot move this behavior class — and shows a stronger model routes around
  the gate correctly on its own.
* Remaining gate friction is substantive, not formal: `was never run` 24 (halved from
  49), rerun-caught *real* test failures 14 (up from 3–10, each with failing-test
  excerpts the model then iterates on), `budget exhausted` 11, `file-less` 3.
* Gate accuracy on this run: tp 58 / fp 30 / fn 8. False positives persist at the
  same absolute level as ling (27–29): narrow-but-green declarations remain the
  structural hole, independent of model strength.

## 4. First production firings of the new checks

* `write_file` parent check fired once (pytest-5787, `/tmp/repro/...`, trial failed on
  fix correctness): cost exactly 1 round, no loop — acceptable price, mechanism works.
* `CHANGES`/`ChangeLog`/`setup.cfg` writes failed 5× with `CommandError: exit code 1`
  (not E2BIG — small files; likely read-only/symlink targets in those repos).
  Pre-existing backend behavior, surfaced cleanly by the raise-unification; worth a
  look only if it recurs.
* **Containment-plan red flag**: deepseek touches `/tmp` as scratch in **62/100**
  trials (repro scripts, whl downloads; ling: 11). Setting `AGENT_RUNTIME_WORKSPACE`
  to a repo root without a `/tmp` exception would break the stronger model's
  legitimate workflow in a majority of trials. The pending root-containment change
  must scope to repo + tmp (or equivalent), not repo-only.

## 5. Conclusion / next step

* The §7 run2 map is confirmed empirically: harness work bought ~45%, the model swap
  bought the next ~20pp to 66%. Fix correctness (22 newly-fixed root causes, sympy
  sweep) dwarfs all plumbing effects — as predicted, the fp mass (30) needs a
  stronger fixer, not cleaner pipes.
* Harness backlog reprioritized by this run: (a) containment MUST handle `/tmp`
  before landing; (b) item 3 alias table is now moot for this model class (0 hits)
  but still pays for weaker ones — cheap, keep; (c) the remaining lever on the gate
  side is declared-files-⊇-edited-files (item 7) against the 30 fp, not more friction.
* Validation for next run: hold ≥60% with any model ≥ this class; `was never run`
  24→≤15 via the auto-attach suggestion (item 6); matplotlib wall (12% across all
  four runs and both models) broken out as its own investigation, not a harness item.
