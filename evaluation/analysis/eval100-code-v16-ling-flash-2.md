# eval100-code-v16-ling-flash-2 Analysis Report

* Job: `jobs/eval100-code-v16-ling-flash-2/` (100/100 evaluated, mean **0.43**: 43×1.0, 57×0.0;
  zero infra losses this round)
* Baseline: `jobs/eval100-code-v16-ling-flash/` run1 (97 evaluated, mean **0.443**: 43×1.0, 54×0.0)
* Code delta under test (committed as `17d2fd2`, job started after it; harness stays `code-v16`,
  skill = template-removed version): item 2 (`patch.py`: unified-diff sniff, path legality
  precheck, read/write-failure raise unification, create-requires-existing-parent) and item 4
  (`tool_dispatch.py`: unknown-tool factual catalog, no did-you-mean).
* Counting basis: same as v16 report (`tool.start` authoritative, unique run_commands deduped).

## 1. Headline: flat (43.0% vs 44.3%), symmetric churn

* Paired on 98 shared instances: 48×(0,0), 37×(1,1), **6 fixed**
  (seaborn-3069, requests-1142, sklearn-12973/13124/13439/14496),
  **6 broken** (xarray-6599/6938, sklearn-11578/14053/25973, sympy-12419). Net 0 —
  run-to-run noise dominates at n=100; no systematic headline gain or loss from the changes.
* Per-repo: psf 57%→71%, sklearn 45%→55%, mwaskom 0%→50% up;
  pydata 64%→45%, sympy 40%→30% down; rest flat.
* Drift signals slightly worse: no-edit 7→11, hit80 20→24, error-status 5→10,
  `grep_search` +28% (491→628) with `run_command` flat — more searching, less committing.

## 2. Item 2 verdict: precise errors correct fast; successes moved to edit_file

* `apply_patch` attempts 32→21, errors 19→21, successes 13→**0**. All 21 rejections audit
  clean (no over-blocking): 9× infra write-fail (see §4), 4× unified-diff (correct),
  3× absolute-path (correct), 4× unterminated block (correct), 1× no-pair (correct).
  `edit_file` successes absorbed the load (216→239, errors halved 8→4).
* Unified-diff sniff fired in 4 trials; xarray-6938 shows textbook 1-round correction
  (it41 precise error → it42 correct SEARCH/REPLACE format). Final outcomes of those trials
  were then decided elsewhere (infra write-fail + gate friction), not by the parser.
* Parent-dir creation check fired **0** times in production (the one grep hit was issue
  text, not our error) — the silent-creation pit did not occur this round, so that half
  is covered by unit tests only.
* `test_patch.py` 9→15 cases; `tests/agent+tools+harness+execution` 383 passed.

## 3. Item 4 verdict: works as designed, immediately exposed item 3

* Hallucinated tool names 6 trials→1 (sklearn-14053: whole shell line as tool name).
  The factual catalog routed it to `grep_search` in exactly 1 round — no misdirection.
  The very next call hit `output_mode` (item 3, not yet implemented): precise evidence
  that the alias table is now the binding constraint on this path.
* `grep` bare-name hallucinations 3→0.

## 4. Big find (pre-existing infra bug, surfaced by change C): Harbor large-write failure

* `HarborWorkspace.write_file` embeds base64 content as shell argv
  (`printf %s <blob>`, `execution/harbor.py:129-134`); files past the exec arg limit fail
  with `OSError: [Errno 7] Argument list too long: 'docker'`.
* Run2: 10 trials hit it (all reward-mixed: 3 pass / 7 fail — recovery possible but burns
  rounds). Run1 had the identical failure in 12 trials, **hidden**: old code returned
  `{"error"}` as a success observation, so it never appeared in `tool.error` counts.
  Change C (return→raise) is what made it visible — observability win, not a regression.
* xarray-6938 is the documented casualty: correct patch blocked by write-fail at it42,
  then 30+ rounds of gate friction to an error end.
* Fix direction (not implemented, needs approval): stdin/chunked write in
  `HarborWorkspace.write_file` (split base64, append with `>>`), backend-agnostic,
  zero task knowledge. Expected effect: removes a ~10%-of-trials friction source that
  no prompt or gate change can address; also un-blocks `apply_patch` success rate
  (9 of its 21 run2 errors are this bug, not the model).

## 5. Conclusion / next step

* Item 2 + item 4 behave exactly per the "generic validation, precise hints" principle:
  no over-blocking observed, 1-round corrections observed, zero misdirection observed.
  Headline flat because (a) the binding constraints moved downstream (infra writes,
  gate friction, fix correctness), and (b) n=100 noise (±5pp) swallows single-digit gains.
* Recommended order: infra write fix first (biggest measured friction, ~10 trials/round),
  then item 3 alias table (already the observed next blocker on the corrected path),
  then item 1 pipe normalization. Items 5/6/7 unchanged pending approval.
* Validation for next run: write-fail trials 10→0, `apply_patch` success rate back to >0
  with rejections still auditing clean, `output_mode` errors 7→0, pass as primary.
