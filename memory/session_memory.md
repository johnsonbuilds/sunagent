# Agent Context Snapshot

## 1. Work State
### Completed
- Reproduced the bug: `SequentialFeatureSelector` with `cv=splits` (iterable from `LeaveOneGroupOut().split()`) raises `IndexError: list index out of range` in `_aggregate_score_dicts` because the generator is consumed on the first `cross_val_score` call, leaving empty splits for subsequent calls.
- Root cause identified: `cross_val_score` calls `check_cv(cv, ...)` internally, which wraps iterables in `_CVIterableWrapper(cv)` doing `list(cv)`. When `cv` is a generator, it's exhausted after the first call.
- Fix implemented in `/testbed/sklearn/feature_selection/_sequential.py`:
  1. Added `check_cv` import from `sklearn.model_selection`
  2. Added `is_classifier` import from `sklearn.base`
  3. In `fit()`: added `cv = check_cv(self.cv, y, classifier=is_classifier(self.estimator))` to materialize the cv object once
  4. Changed `_get_best_new_feature_score` signature to accept `cv` parameter instead of using `self.cv`
  5. Updated call site in `fit()` loop to pass `cv` to `_get_best_new_feature_score`

### Active (In-Progress)
- The fix has NOT been tested yet — need to verify it resolves the bug and doesn't break existing tests.

### Blocked / Failure Lessons
- The build environment requires the `testbed` conda env (`source /opt/miniconda3/bin/activate testbed`) with Python 3.9 and pre-built sklearn 1.3.dev0. Using base Python 3.11 fails because the `.so` files are for Python 3.9.

## 2. Next Move
- Test the fix by running the reproduction script from the issue
- Run the existing test suite for sequential feature selection: `pytest sklearn/feature_selection/tests/test_sequential.py`
- Verify no regressions

## 3. Working Context & Anchors
- **Relevant Files**: `/testbed/sklearn/feature_selection/_sequential.py` (modified)
- **Environment**: conda env `testbed` at `/opt/miniconda3/envs/testbed`, Python 3.9.21, sklearn 1.3.dev0
- **Key imports added**: `check_cv` from `sklearn.model_selection`, `is_classifier` from `sklearn.base`
- **Key method changed**: `_get_best_new_feature_score(self, estimator, X, y, current_mask, cv)` — now takes `cv` as explicit parameter
- **Key change in `fit()`**: `cv = check_cv(self.cv, y, classifier=is_classifier(self.estimator))` called once before the feature selection loop