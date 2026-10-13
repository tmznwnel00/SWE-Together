"""Pre-pull the SWE-Together task images so a trial never waits on (or dies during) a pull.

Resumable: images already present are skipped and `docker pull` keeps layers it has, so it is
safe to stop and rerun. `--max-minutes` stops starting new pulls after that long (in-flight ones
finish) and exits 3 while images remain, so a wrapper can rerun it in bounded chunks.

    metaharness bench swet pull [--workers 3] [--tasks a,b] [--max-minutes 25] [--force]
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed

from metaharness.config import HOME

SWET = HOME / "bench" / "SWE-Together"
_EST_GB = 2.8    # mean of the 6 images measured locally (1.0-4.1 GB); shared base layers make real use lower
_MARGIN_GB = 40


def images(tasks: list[str] | None = None) -> list[tuple[str, str]]:
    """(task, docker image) for the given tasks, default the canonical 109."""
    names = tasks or json.load(open(SWET / "canonical_full109.json"))["tasks"]
    return [(t, tomllib.load(open(SWET / "tasks" / t / "task.toml", "rb"))["environment"]["docker_image"])
            for t in names]


def _have(image: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0


def _pull(image: str) -> tuple[bool, str]:
    # benchmark images are amd64-only; pulling the same platform the trials run under
    p = subprocess.run(["docker", "pull", "--platform", "linux/amd64", "-q", image],
                       capture_output=True, text=True)
    return p.returncode == 0, (p.stderr or p.stdout).strip()[-300:]


def run(workers: int = 3, tasks: list[str] | None = None, max_minutes: float | None = None,
        force: bool = False) -> int:
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("docker daemon is not running")
        return 2
    plan = images(tasks)
    todo = [(t, img) for t, img in plan if not _have(img)]
    free_gb = shutil.disk_usage("/").free / 1e9
    need = len(todo) * _EST_GB
    print(f"{len(plan) - len(todo)}/{len(plan)} images already present; {len(todo)} to pull "
          f"(<= ~{need:.0f} GB at {_EST_GB} GB each); host disk free {free_gb:.0f} GB", flush=True)
    if not todo:
        return 0
    if free_gb < need + _MARGIN_GB and not force:
        print(f"not enough free disk (want ~{need + _MARGIN_GB:.0f} GB incl. {_MARGIN_GB} GB margin); "
              f"free some space or pass --force")
        return 2

    deadline = time.monotonic() + max_minutes * 60 if max_minutes else None
    t0 = time.monotonic()
    ok_n, not_started, fails = 0, 0, []

    def work(item):
        if deadline and time.monotonic() > deadline:
            return item, None, ""
        ok, msg = _pull(item[1])
        return item, ok, msg

    with ThreadPoolExecutor(workers) as ex:
        for fut in as_completed([ex.submit(work, it) for it in todo]):
            (task, _img), ok, msg = fut.result()
            if ok is None:
                not_started += 1
                continue
            if ok:
                ok_n += 1
            else:
                fails.append((task, msg))
            print(f"[{ok_n + len(fails)}/{len(todo)}] {'ok  ' if ok else 'FAIL'} {task}  "
                  f"({(time.monotonic() - t0) / 60:.1f} min)", flush=True)
    for task, msg in fails:
        print(f"FAILED {task}: {msg}")
    left = sum(1 for _t, img in plan if not _have(img))
    print(f"pulled {ok_n}, failed {len(fails)}, not started (time budget) {not_started}; "
          f"{left} of {len(plan)} images still missing; {(time.monotonic() - t0) / 60:.1f} min")
    if fails:
        return 1
    return 3 if left else 0
