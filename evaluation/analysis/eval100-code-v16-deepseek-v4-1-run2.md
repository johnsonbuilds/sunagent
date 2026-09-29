# eval100-code-v16-deepseek-v4-1-run2 Analysis Report

* Job: `jobs/eval100-code-v16-deepseek-v4.1/` (100/100 evaluated, wall
  2026-09-28 15:23→19:45 UTC, ~4.4h)
* Result: mean **0.69**: 69×1.0, 31×0.0
* Model: `deepseek/deepseek-v4.1-flash` — **same model as the -6 run**
  (`jobs/eval100-code-v16-ling-flash-6/`, whose dirname is a misnomer; see
  `eval100-code-v16-deepseek-v4-1.md`). Prior chat analysis that labeled -6
  "ling-flash" was wrong; the 0.66→0.69 delta is same-model.
* Harness lineage identical: `code-v16`, genes `99aea98bc27bef4b` in both runs.
  Working-tree delta (only): skill `+Search routing` / `+de-narrowed verify`
  (skills hash `270b605895dcf217`→`f14bde0e829de480`), single-exec repo-context
  probe live in 100/100 trials, `apply_patch` write-refusal message already
  reverted to the old text before this run.
* Counting basis: same as previous reports (tool calls deduped by
  `tool_call_id`; submit counts via verification events, not `tool.start`).

## 1. Headline: +3pp is noise (McNemar)

Paired on the same 100 instances:

|              | ds-run2 pass | ds-run2 fail |
|--------------|--------------|--------------|
| -6 pass      | 63           | **3 regressed** |
| -6 fail      | **6 improved** | 28           |

* Discordant pairs = 9, McNemar χ² (continuity-corrected) = **0.44**
  (bar 3.84, p≈0.5 — coin-flip level). Two-sample z ≈ 0.45. Same conclusion.
* Decision line for n=100: treat any mean drift < ~0.08 as noise; significance
  needs roughly net +8 with few regressions (e.g. a 9:1 split, χ²≈4.9).
* The 6 improved instances (django-12209/13212, xarray-6599, pylint-4970,
  sphinx-11510/9229 — incl. two former zero-edit stuck cases) are *compatible*
  with the skill changes helping, but n=9 is compatible with anything: hypothesis
  generator, not evidence. Regressions (pylint-6386, sympy-17630/18199) show no
  shared pattern.

## 2. Mechanism readout: one flat, one directional, one broken

| Metric (-6 → run2) | Values | Verdict |
|---|---|---|
| shell-grep share | 699/3528 (19.8%) → 648/3335 (19.4%) | **No effect.** Skill routing text does not move interactive search behavior — same lesson as the pipe ban (obeyed in declarations only). |
| `git log` calls | 256 (2.56/trial) → 229 (2.29/trial) | No effect. |
| which/version/pip probes | 165 → 175 | No effect. |
| declared `-k` narrowing | 12/42 → 3/42 submits | Directional improvement, **weak evidence** (submit counting via `llm.start` undercounts; keep as hypothesis). |
| `tool.error` total | 12 → 3 (1× ChangeLog write-refusal under the old message, 2× edit-match) | Continued healthy; refusal-with-old-text still occurs. |
| pipe-mask rejections | 1 → 6 | Uptick, small N — watch one more run. |
| never-run / budget-exhausted / file-less | 24/11/3 → 17/8/3 | Directional, all inside noise. |
| zero-edit trials | 2 → 1 | Flat. |
| iters mean/med, tools mean | 44.1/40.0, 48.4 → 42.1/37.5, 47.2 | Flat. |

## 3. Probe post-mortem: live mechanism, wrong content (reverted)

* The probe fired in 100/100 trials, but its pytest section reported the **base**
  env interpreter: `/opt/miniconda3/bin/python: No module named pytest` —
  while agents actually test with `/opt/miniconda3/envs/testbed/bin/python`.
  Net effect: a misleading toolchain fact. Consistent with probes not dropping:
  the model kept probing the real environment itself.
* Decision: reverted in full (`4ee829c` — probe code + its tests). Lesson: a
  one-exec probe is only viable if it resolves the *effective* toolchain
  (testbed env), not `PATH` python. Not re-attempted until that is designed.

## 4. Failure structure unchanged: 30/31 are fix-quality

* 30× `verification.passed` + official 0.0 (held-out F2P added by test patch at
  grading; agents ran the F2P file green without the new test). Same structural
  blindness as the -6 run — no tool-call intervention addresses it.
* 1× process failure (sympy-17630, 1 rejection). Budget-exhausted class shrank
  3→0 on this run (noise-range, not claimed).
* The `-k`-narrowed subset persists inside the 30; the skill de-narrowing line
  is the only live countermeasure, still unproven (§2).

## 5. Change log of this cycle (for the record)

* `d934cb0`: probe + skill routing + skill de-narrow + write-refusal message.
* `5c63d03` (owner): reverted the write-refusal message.
* `4ee829c`: reverted probe code + probe tests (post-run analysis §3).
* Retained: skill search-routing + de-narrowed-verify lines (zero cost,
  unproven — kept, not credited).

## 6. Standing rules adopted from this run

* Every future comparison reports the McNemar table; χ²>3.84 before celebrating.
* Do not credit mechanism deltas inside noise (this retracts the run2-report-era
  reading of never-run/budget/`-k` drops as "improvements").
* Search-bypass stays deprioritized (costs tokens, not score) unless a
  non-text mechanism (soft runtime hint) is proposed with a powered test.
