"""Run a Harbor job and remove each task's docker image when its trial ends.

SWE-bench eval images are per-instance (~4-11GB each) and Harbor only
deletes containers (`environment.delete`), never images. A 100-task run
would accumulate ~500GB in the local docker cache. Each image is used by
exactly one trial, so it is safe to `docker rmi` it once that trial's
`result.json` carries `finished_at`.

Usage:
    # Local dataset directory (passed to harbor as -p):
    uv run python scripts/run_with_image_gc.py \
        --dataset evaluation/swe_bench/dataset-smoke10 \
        --harness code-v6 --job-name my-run-01 -n 4

    # Registry dataset (passed to harbor as -d, e.g. terminal-bench-2):
    uv run python scripts/run_with_image_gc.py \
        --dataset terminal-bench/terminal-bench-2 \
        --harness meta-v10 --job-name tb2-run -n 4

Only stdlib is used. Exit code mirrors the harbor process.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


def disk_free_gb(path: Path) -> float:
    total, used, free = shutil.disk_usage(path)
    return free / 1024**3

# Harbor's PACKAGE_CACHE_DIR: where -d registry datasets are unpacked,
# one directory per resolved content hash.
PACKAGE_CACHE = Path("~/.cache/harbor/tasks/packages").expanduser()


def read_result(trial_dir: Path) -> dict | None:
    try:
        return json.loads((trial_dir / "result.json").read_text())
    except (OSError, ValueError):
        return None


def read_finished_at(trial_dir: Path) -> str | None:
    payload = read_result(trial_dir)
    return payload.get("finished_at") if payload else None


def instance_id_of(trial_dir: Path, payload: dict | None = None) -> str | None:
    payload = payload or read_result(trial_dir)
    if not payload:
        return None
    task_path = (payload.get("task_id") or {}).get("path", "")
    if task_path:
        return Path(task_path).name or None
    # Fallback: trial dir is "<instance_id>__<rand7>"; instance ids
    # themselves contain "__", so strip only the last segment.
    name = trial_dir.name
    return name.rsplit("__", 1)[0] if "__" in name else None


def docker_image_of(task_toml: Path) -> str | None:
    try:
        text = task_toml.read_text()
    except OSError:
        return None
    match = re.search(r'^docker_image\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None


def image_of(trial_dir: Path, dataset: Path) -> str | None:
    payload = read_result(trial_dir)
    task_id = (payload or {}).get("task_id") or {}
    candidates: list[Path] = []
    # Local (or git) task ids carry the path to the unpacked task dir.
    task_path = task_id.get("path")
    if task_path:
        candidates.append(Path(task_path) / "task.toml")
    # Registry task ids ({org, name, ref: "sha256:<hash>"}) resolve to the
    # harbor package cache, which outlives the run.
    org, name = task_id.get("org"), task_id.get("name")
    if org and name:
        pkg_dir = PACKAGE_CACHE / org / name
        ref = task_id.get("ref") or ""
        if ref.startswith("sha256:"):
            candidates.append(pkg_dir / ref.removeprefix("sha256:")
                              / "task.toml")
        elif pkg_dir.is_dir():
            by_mtime = sorted(pkg_dir.glob("*/task.toml"),
                              key=lambda p: p.stat().st_mtime, reverse=True)
            candidates += by_mtime[:1]
    for candidate in candidates:
        image = docker_image_of(candidate)
        if image:
            return image
    # Legacy fallback: local dataset dir + instance id.
    iid = instance_id_of(trial_dir, payload)
    if iid:
        return docker_image_of(dataset / iid / "task.toml")
    return None


def containers_referencing(image: str, only_running: bool) -> list[str]:
    flag = ["docker", "ps", "-q", "--filter", f"ancestor={image}"]
    if not only_running:
        flag.insert(2, "-a")
    proc = subprocess.run(flag, capture_output=True, text=True)
    return [c for c in proc.stdout.split() if c.strip()]


def image_in_use(image: str) -> bool:
    return bool(containers_referencing(image, only_running=True))


def remove_image(image: str) -> str:
    """Remove an image; return 'removed', 'gone', or 'failed'."""
    # Drop stopped leftovers referencing the image (e.g. leaked envs from
    # deleted jobs); never touch running containers.
    if not image_in_use(image):
        for cid in containers_referencing(image, only_running=False):
            subprocess.run(["docker", "rm", "-f", cid],
                           capture_output=True, text=True)
    proc = subprocess.run(["docker", "rmi", image],
                          capture_output=True, text=True)
    if proc.returncode == 0:
        return "removed"
    err = (proc.stderr or proc.stdout).strip()[:200]
    if "No such image" in err:
        # Already collected by an earlier sweep of the same job dir.
        return "gone"
    print(f"[gc] rmi failed for {image}: {err}", flush=True)
    return "failed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True,
                        help="Harbor dataset: a local path (-p) or a "
                             "registry name like "
                             "'terminal-bench/terminal-bench-2' (-d)")
    parser.add_argument("--harness", default="code-v6",
                        help="AGENT_RUNTIME_HARNESS value (default: code-v6)")
    parser.add_argument("--job-name", default=None,
                        help="Harbor --job-name (default: let harbor decide, "
                             "job dir is auto-detected)")
    parser.add_argument("-n", "--n-concurrent", type=int, default=4)
    parser.add_argument("--poll-sec", type=int, default=30)
    parser.add_argument("--jobs-dir", default="jobs")
    parser.add_argument("--no-gc", action="store_true",
                        help="run without removing images")
    args, extra = parser.parse_known_args()

    dataset = Path(args.dataset)
    dataset_flag = "-p" if dataset.is_dir() else "-d"
    jobs_dir = Path(args.jobs_dir)
    env = dict(os.environ, AGENT_RUNTIME_HARNESS=args.harness)
    harbor_bin = shutil.which("harbor")
    if harbor_bin:
        cmd = [harbor_bin, "run"]
    elif shutil.which("uv"):
        # Same deps as the repo workflow when no harbor is on PATH.
        cmd = ["uv", "run", "harbor", "run"]
    else:
        cmd = [sys.executable, "-m", "harbor", "run"]
    cmd += [dataset_flag, str(dataset),
            "--agent", "agent_runtime.integrations.harbor:HarborAgent",
            "-n", str(args.n_concurrent)]
    if args.job_name:
        cmd += ["--job-name", args.job_name]
    cmd += extra

    print(f"[gc] starting: {' '.join(cmd)}", flush=True)
    print(f"[gc] disk free before: {disk_free_gb(Path('.')):.1f}GB",
          flush=True)
    before = {p.name: p.stat().st_mtime for p in jobs_dir.iterdir()
              if p.is_dir()} if jobs_dir.is_dir() else {}
    proc = subprocess.Popen(cmd, env=env)

    job_dir: Path | None = None
    deadline = time.time() + 600
    while job_dir is None and time.time() < deadline:
        if proc.poll() is not None:
            break
        current = [p for p in jobs_dir.iterdir() if p.is_dir()]
        fresh = [p for p in current
                 if before.get(p.name, 0) < p.stat().st_mtime]
        if args.job_name and (jobs_dir / args.job_name).is_dir():
            job_dir = jobs_dir / args.job_name
        elif fresh:
            job_dir = max(fresh, key=lambda p: p.stat().st_mtime)
        else:
            time.sleep(5)
    if job_dir is None:
        print("[gc] job dir never appeared; waiting for harbor to exit",
              flush=True)
        return proc.wait()

    print(f"[gc] tracking {job_dir} (gc={'off' if args.no_gc else 'on'})",
          flush=True)
    collected: set[str] = set()
    while proc.poll() is None:
        time.sleep(args.poll_sec)
        for trial in sorted(p for p in job_dir.iterdir() if p.is_dir()):
            if trial.name in collected or read_finished_at(trial) is None:
                continue
            collected.add(trial.name)
            if args.no_gc:
                print(f"[gc] done (kept): {trial.name}", flush=True)
                continue
            image = image_of(trial, dataset)
            if not image:
                print(f"[gc] done, no image found: {trial.name}", flush=True)
                continue
            if image_in_use(image):
                print(f"[gc] done, image busy, retry later: {image}",
                      flush=True)
                collected.discard(trial.name)
                continue
            status = remove_image(image)
            print(f"[gc] {status}: {image} "
                  f"({trial.name}) free={disk_free_gb(Path('.')):.1f}GB",
                  flush=True)

    # Final sweep: harbor exited, collect anything left behind.
    for trial in sorted(p for p in job_dir.iterdir() if p.is_dir()):
        if trial.name in collected or read_finished_at(trial) is None:
            continue
        collected.add(trial.name)
        if args.no_gc:
            continue
        image = image_of(trial, dataset)
        if image and not image_in_use(image):
            status = remove_image(image)
            print(f"[gc] final sweep {status}: "
                  f"{image}", flush=True)
    print(f"[gc] harbor exit={proc.returncode} "
          f"trials_collected={len(collected)} "
          f"disk free after: {disk_free_gb(Path('.')):.1f}GB", flush=True)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
