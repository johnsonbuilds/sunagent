"""Run a Harbor job and remove each task's docker image when its trial ends.

SWE-bench eval images are per-instance (~4-11GB each) and Harbor only
deletes containers (`environment.delete`), never images. A 100-task run
would accumulate ~500GB in the local docker cache. Each image is used by
exactly one trial, so it is safe to `docker rmi` it once that trial's
`result.json` carries `finished_at`.

Usage:
    uv run python scripts/run_with_image_gc.py \
        --dataset evaluation/swe_bench/dataset-smoke10 \
        --harness code-v6 --job-name my-run-01 -n 4

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


def read_finished_at(trial_dir: Path) -> str | None:
    try:
        payload = json.loads((trial_dir / "result.json").read_text())
    except (OSError, ValueError):
        return None
    return payload.get("finished_at")


def instance_id_of(trial_dir: Path) -> str | None:
    try:
        payload = json.loads((trial_dir / "result.json").read_text())
    except (OSError, ValueError):
        return None
    task_path = (payload.get("task_id") or {}).get("path", "")
    if task_path:
        return Path(task_path).name or None
    # Fallback: trial dir is "<instance_id>__<rand7>"; instance ids
    # themselves contain "__", so strip only the last segment.
    name = trial_dir.name
    return name.rsplit("__", 1)[0] if "__" in name else None


def image_of(dataset: Path, instance_id: str) -> str | None:
    try:
        text = (dataset / instance_id / "task.toml").read_text()
    except OSError:
        return None
    match = re.search(r'^docker_image\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None


def containers_referencing(image: str, only_running: bool) -> list[str]:
    flag = ["docker", "ps", "-q", "--filter", f"ancestor={image}"]
    if not only_running:
        flag.insert(2, "-a")
    proc = subprocess.run(flag, capture_output=True, text=True)
    return [c for c in proc.stdout.split() if c.strip()]


def image_in_use(image: str) -> bool:
    return bool(containers_referencing(image, only_running=True))


def remove_image(image: str) -> bool:
    # Drop stopped leftovers referencing the image (e.g. leaked envs from
    # deleted jobs); never touch running containers.
    if not image_in_use(image):
        for cid in containers_referencing(image, only_running=False):
            subprocess.run(["docker", "rm", "-f", cid],
                           capture_output=True, text=True)
    proc = subprocess.run(["docker", "rmi", image],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"[gc] rmi failed for {image}: "
              f"{(proc.stderr or proc.stdout).strip()[:200]}", flush=True)
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True,
                        help="Harbor dataset path, e.g. "
                             "evaluation/swe_bench/dataset-smoke10")
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
    cmd += ["-p", str(dataset),
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
            iid = instance_id_of(trial)
            image = image_of(dataset, iid) if iid else None
            if not image:
                print(f"[gc] done, no image found: {trial.name}", flush=True)
                continue
            if image_in_use(image):
                print(f"[gc] done, image busy, retry later: {image}",
                      flush=True)
                collected.discard(trial.name)
                continue
            ok = remove_image(image)
            print(f"[gc] {'removed' if ok else 'FAILED'}: {image} "
                  f"({trial.name}) free={disk_free_gb(Path('.')):.1f}GB",
                  flush=True)

    # Final sweep: harbor exited, collect anything left behind.
    for trial in sorted(p for p in job_dir.iterdir() if p.is_dir()):
        if trial.name in collected or read_finished_at(trial) is None:
            continue
        collected.add(trial.name)
        if args.no_gc:
            continue
        iid = instance_id_of(trial)
        image = image_of(dataset, iid) if iid else None
        if image and not image_in_use(image):
            ok = remove_image(image)
            print(f"[gc] final sweep {'removed' if ok else 'FAILED'}: "
                  f"{image}", flush=True)
    print(f"[gc] harbor exit={proc.returncode} "
          f"trials_collected={len(collected)} "
          f"disk free after: {disk_free_gb(Path('.')):.1f}GB", flush=True)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
