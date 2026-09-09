# Agent Context Snapshot

## 1. Work State
### Completed
- Identified the root cause of the bug: `SequentialFeatureSelector._get_best_new_feature_score` calls `cross_val_score` multiple times with `cv=self.cv`. When `self.cv` is an iterator/generator (e.g., from `LeaveOneGroupOut().split()`), it gets consumed on the first call. Subsequent calls receive an exhausted iterator, `check_cv` wraps it in `_CVIterableWrapper` which does `list(cv)` on the exhausted iterator → empty list → `cross_val_score` returns empty array → `_aggregate_score_dicts` fails with `IndexError: list index out of range` on `scores[0]`.
- The build of scikit-learn from source succeeded after installing compatible dependencies (`numpy<2`, `scipy<1.12`, `cython<3`).
- The `_CVIterableWrapper` in `sklearn/model_selection/_split.py` (line 2353) converts iterables to lists via `self.cv = list(cv)` in `__init__`, which is the point of failure when the iterator is already exhausted.

### Active (In-Progress)
- Need to implement the fix in `sklearn/feature_selection/_sequential.py`
- The fix should ensure `self.cv` is converted to a reusable form before being used multiple times in `_get_best_new_feature_score`

### Blocked / Failure Lessons
- Building scikit-learn from source with incompatible numpy/cython versions fails with Cython compilation errors (e.g., `PyArray_Descr` has no member named `subarray`). Must use `numpy<2`, `scipy<1.12`, `cython<3` for this codebase version.
- Build is very slow (~5+ minutes); use `-j1` flag and be patient.

## 2. Next Move
- Edit `sklearn/feature_selection/_sequential.py` to fix the bug. The fix should convert `self.cv` to a reusable list/object in `fit()` before it's used multiple times in `_get_best_new_feature_score`. The cleanest approach: call `check_cv` once in `fit()` and store the result, or convert `self.cv` to a list if it's an iterable without a `split` method. Then in `_get_best_new_feature_score`, pass the stored reusable cv object instead of `self.cv` directly.
- After making the fix, run the relevant tests to verify.

## 3. Working Context & Anchors
- **Relevant Files**: `/testbed/sklearn/feature_selection/_sequential.py` (main fix target), `/testbed/sklearn/model_selection/_split.py` (contains `_CVIterableWrapper` and `check_cv`), `/testbed/sklearn/model_selection/_validation.py` (contains `cross_val_score` and `cross_validate`)
- **Key method**: `SequentialFeatureSelector._get_best_new_feature_score` (line ~293) calls `cross_val_score` with `cv=self.cv` in a loop over features
- **Key method**: `SequentialFeatureSelector.fit` (line ~200) should be modified to materialize `self.cv` before it's used
- **Environment**: Python 3.11, sklearn source at `/testbed`, built successfully with `numpy==1.26.4`, `scipy==1.11.4`, `cython==0.29.37`
- **Test file**: `/testbed/sklearn/feature_selection/tests/test_sequential.py`
