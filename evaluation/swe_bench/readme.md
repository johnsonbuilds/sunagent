# SWE-bench Evaluation

## Overview

Code repair evaluation based on the SWE-bench Verified dataset. Harbor automatically runs `tests/test.sh` for verification after the agent phase completes. reward=1 indicates resolved (F2P all pass + P2P all pass).

## Quick Start

### 1. Pull Docker Images (first time)

```bash
for id in $(python3 -c "import json;print(' '.join(json.load(open('evaluation/swe_bench/smoke-10.json'))['instance_ids']))"); do
  docker pull swebench/sweb.eval.x86_64.$(echo $id | sed 's/__/_1776_/'):latest
done
```

### 2. Run a Single Task (testing)

```bash
# Run a single task (using code-v1 harness, includes file editing tools)
uv run harbor run -p evaluation/swe_bench/dataset-smoke10/astropy__astropy-14309 \
  --agent agent_runtime.integrations.harbor:HarborAgent \
  --config harnesses/code-v1.yaml -y

# View results
cat jobs/<job-id>/result.json
cat jobs/<job-id>/<task-id>/verifier/rewards.json
```

### 3. Run All Tasks

```bash
# code-v1 arm (includes run_command, read_file, apply_patch)
AGENT_RUNTIME_HARNESS=code-v1 uv run harbor run \
  -p evaluation/swe_bench/dataset-smoke10 \
  --agent agent_runtime.integrations.harbor:HarborAgent

# meta-v12 arm (more tools)
AGENT_RUNTIME_HARNESS=meta-v12 uv run harbor run \
  -p evaluation/swe_bench/dataset-smoke10 \
  --agent agent_runtime.integrations.harbor:HarborAgent
```

### 4. Limit Task Count

```bash
# Run only the first 3 tasks
uv run harbor run -p evaluation/swe_bench/dataset-smoke10 \
  --agent agent_runtime.integrations.harbor:HarborAgent \
  --config harnesses/code-v1.yaml -l 3
```

### 5. Recommended Workflow: Single Task First, Then Full Run (100 tasks)

Always run a single trial before a large batch to confirm the verifier can score properly (there was an instance where all tasks scored 0:
`test.sh` was incompatible with old pytest, scoring parsing silently failed). Use `-i` to filter for single tasks:

```bash
# Single task verification (use a new job-name each time; reusing an old name with changed params will raise lock.json FileExistsError)
uv run python scripts/run_with_image_gc.py \
  --dataset evaluation/swe_bench/dataset-eval100 \
  --harness code-v6 --job-name verify-fix2 -n 1 \
  -i astropy__astropy-7336

# Confirm rewards.json shows tests actually executed (fail_to_pass_total > 0 and has passes), then run full batch
uv run python scripts/run_with_image_gc.py \
  --dataset evaluation/swe_bench/dataset-eval100 \
  --harness code-v6 --job-name eval100-code-v6 -n 4 \
  --environment-build-timeout-multiplier 3
```

Notes:

* `scripts/run_with_image_gc.py` is a superset of `harbor run` (`-p/--agent/-n/--job-name`
  and harbor passthrough params forwarded as-is, `--harness` injects `AGENT_RUNTIME_HARNESS`).
  It runs `docker rmi` on the task's dedicated image after its `result.json` shows `finished_at` —
  SWE images are packaged per instance (~5GB average), Harbor only removes containers not images,
  and 100 tasks without cleanup would accumulate ~500GB and fill the disk.
* `--environment-build-timeout-multiplier 3`: large images pulled under high parallelism may exceed the default
  600s environment build timeout, which previously caused `EnvironmentStartTimeoutError`.
* `--job-name` determines the `jobs/<job-name>/` directory; `result.json` for overall score,
  `<trial>/verifier/rewards.json` for per-task results, `<trial>/agent/agent-runtime.jsonl`
  for full traces (including `loop.guard` circuit breaker events).

## Task List (smoke-10)

| Instance ID | Repo |
|-------------|------|
| astropy__astropy-14309 | astropy/astropy |
| django__django-13449 | django/django |
| sphinx-doc__sphinx-11510 | sphinx-doc/sphinx |
| pylint-dev__pylint-6903 | pylint-dev/pylint |
| psf__requests-1921 | psf/requests |
| matplotlib__matplotlib-26342 | matplotlib/matplotlib |
| pytest-dev__pytest-5809 | pytest-dev/pytest |
| scikit-learn__scikit-learn-14053 | scikit-learn/scikit-learn |
| pydata__xarray-6938 | pydata/xarray |
| sympy__sympy-19495 | sympy/sympy |

## Directory Structure

```
evaluation/swe_bench/
├── readme.md              # This file
├── smoke-10.json          # 10 task ID list
├── dev-50.json            # 50 development tasks
├── build_swe_dataset.py   # Dataset build script
├── select_swe_tasks.py    # Task selection script
└── dataset-smoke10/       # Task directory
    └── <task-id>/
        ├── task.toml          # Task config (Docker image, timeout)
        ├── instruction.md     # Task description (GitHub issue)
        ├── solution/solve.sh  # Reference solution (apply gold patch)
        ├── tests/
        │   ├── test.sh        # Verification script
        │   ├── test_patch.diff
        │   └── gold.patch
        └── environment/README.md
```

## Harness Selection

SWE-bench tasks require file editing tools to fix code. The following harnesses are recommended:

| Harness | Tools | Use Case |
|---------|-------|----------|
| `baseline-v0` | run_command | Read-only analysis |
| `code-v1` | run_command, read_file, apply_patch | **Recommended** - Code repair |
| `files-v1` | run_command, write_file, read_file, list_dir | File operations |
| `meta-v1` | run_command, write_file, read_file, list_dir, edit_file, apply_patch, grep_search, glob_files | Full toolset |

**Run command examples:**
```bash
# Using environment variable
AGENT_RUNTIME_HARNESS=code-v1 uv run harbor run ...

# Or using --config parameter
uv run harbor run ... --config harnesses/code-v1.yaml
```

## Notes

### Docker Images

- Image name format: `swebench/sweb.eval.x86_64.<repo_id>:latest`
- Docker Hub uses `_1776_` in place of `__` (e.g., `astropy_1776_astropy-14309`)
- Each image contains the specific commit and dependency environment for the corresponding repo
- Images include a `testbed` conda environment (Python + pytest + dependencies)

### Verification Mechanism

- After the agent completes, Harbor automatically runs `tests/test.sh`
- The script first runs `export PATH=/opt/miniconda3/envs/testbed/bin:$PATH` to use the correct Python environment
- Verification result written to `/logs/verifier/reward.txt` (1=resolved, 0=unresolved)
- Detailed results in `/logs/verifier/rewards.json`

### Timeout Settings

- Agent timeout: 3600s (1 hour)
- Verifier timeout: 1800s (30 minutes)
- Environment build timeout: 600s (10 minutes)

### FAQ

**Q: Image pull returns 404?**
A: Check that the image name uses the `_1776_` format, or rebuild with `build_swe_dataset.py --image-tag <tag>`.

**Q: Test reports `No module named pytest`?**
A: Ensure `test.sh` contains `export PATH=/opt/miniconda3/envs/testbed/bin:$PATH`.

**Q: numpy compatibility error?**
A: Use the `testbed` conda environment (numpy 1.25.2), not the base environment.

### Regenerating the Dataset

```bash
uv run python evaluation/swe_bench/build_swe_dataset.py \
  --list evaluation/swe_bench/smoke-10.json \
  --out evaluation/swe_bench/dataset-smoke10 \
  --parquet <path-to-parquet> \
  --image-tag latest
```

Requires `pyarrow` to read the Verified parquet file.
