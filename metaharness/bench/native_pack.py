"""Move finished trials between a cloud session and the local machine.

    python3 -m metaharness.bench.native_pack pack   --tag nat_claude [--out cloud_results/nat_claude]   # cloud
    python3 -m metaharness.bench.native_pack unpack --tag nat_claude [--src cloud_results/nat_claude]   # local

pack: one <trial>.tar.gz per trial that has a result.json (files above MAX_FILE_MB are left out and listed in
MANIFEST.json). unpack: extracts trials that are not already under SWE-Together/trials/<tag>/. Whether a trial counts as
completed is decided afterwards by the benchmark's own `--skip-existing` rule; unfinished ones are archived by it.
"""
from __future__ import annotations

import argparse
import json
import tarfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
CLONE = _ROOT if (_ROOT / "src" / "run_eval.py").exists() else _ROOT / "SWE-Together"   # the fork carries these files at its root
MAX_FILE_MB = 30


def pack(tag: str, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    manifest, n = json.loads((out / "MANIFEST.json").read_text()) if (out / "MANIFEST.json").exists() else {}, 0
    for d in sorted((CLONE / "trials" / tag).glob("*__*")):
        if not (d.is_dir() and not d.name.startswith("_") and (d / "result.json").exists()):
            continue
        skipped = []
        with tarfile.open(out / f"{d.name}.tar.gz", "w:gz") as tf:
            for f in sorted(d.rglob("*")):
                if f.is_file() and not f.is_symlink():
                    if f.stat().st_size > MAX_FILE_MB * 1024 * 1024:
                        skipped.append(str(f.relative_to(d)))
                        continue
                    tf.add(f, arcname=f"{d.name}/{f.relative_to(d)}")
        manifest[d.name] = {"skipped_large_files": skipped}
        n += 1
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=1))
    print(f"packed {n} trials into {out} ({sum(f.stat().st_size for f in out.glob('*.tar.gz')) / 1e6:.0f} MB)")
    return 0


def unpack(tag: str, src: Path) -> int:
    dest = CLONE / "trials" / tag
    dest.mkdir(parents=True, exist_ok=True)
    got = 0
    for t in sorted(src.glob("*.tar.gz")):
        if (dest / t.name[: -len(".tar.gz")]).exists():
            continue
        with tarfile.open(t) as tf:
            tf.extractall(dest, filter="data")
        got += 1
    print(f"unpacked {got} new trials into {dest}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["pack", "unpack"])
    ap.add_argument("--tag", required=True)
    ap.add_argument("--out", "--src", dest="path")
    a = ap.parse_args()
    path = Path(a.path) if a.path else Path("cloud_results") / a.tag
    return pack(a.tag, path) if a.cmd == "pack" else unpack(a.tag, path)


if __name__ == "__main__":
    raise SystemExit(main())
