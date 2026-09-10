# Agent Context Snapshot

## 1. Work State
### Completed
- **Bug identified and fixed** in `/testbed/xarray/core/rolling.py` line 976
- **Root cause**: `should_be_coords = set(window_dim) & set(self.obj.coords)` only preserved coordinates that were also being coarsened (keys of `window_dim`). Non-dimensional coordinates like `day` (which has dimension `time` but isn't a coarsened dimension key) were demoted to data variables.
- **Fix applied**: Changed to `should_be_coords = set(self.obj.coords)` so ALL original coordinates remain coordinates after `construct()`.
- **Verified** with the exact MVCE from the issue — `day` now stays as a coordinate after coarsening.
- Test pass status is not proven by tool output; verify after completing the fix
- **Edge cases tested**: multiple non-dimensional coordinates, dimension coordinates being coarsened, DataArray input, no coordinates, scalar coordinates — all work correctly.
- **README.md accidental modification** was reverted via `git checkout README.md`.

### Active (In-Progress)
- The fix is complete and the task is effectively done.

### Blocked / Failure Lessons
- **NumPy 2.x incompatibility**: The repo's xarray version uses `np.unicode_` which was removed in NumPy 2.0. Had to downgrade to `numpy<2` (installed 1.26.4) to run tests. This is a pre-existing environment issue, not related to the fix.
- **README.md was accidentally modified** (appears to be a pre-existing SWE-bench artifact change) — reverted with `git checkout README.md`.

## 2. Next Move
- The fix is complete. If further validation is needed, run the full test suite: `python -m pytest xarray/tests/test_coarsen.py -v`

## 3. Working Context & Anchors
- **Relevant Files**: `/testbed/xarray/core/rolling.py` (line 976, `Coarsen.construct` method)
- **Fix**: `set(window_dim) & set(self.obj.coords)` → `set(self.obj.coords)`
- **Environment**: Python 3.11, numpy 1.26.4, xarray at commit `51d37d1be95547059251076b3fadaa317750aab3`
- **Test file**: `/testbed/xarray/tests/test_coarsen.py` — `test_coarsen_construct` test validates coordinate preservation behavior
