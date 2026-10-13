"""~/.metaharness/.env loader. stdlib only. The only module that should read
raw values out of this file — callers get strings back, never persist or log
them beyond that (redact() at every storage/response boundary, same as
dash/creds.py and vcs/slack.py).
"""
from __future__ import annotations

from metaharness.config import HOME

_ENV_PATH = HOME / ".env"
_cache: dict[str, str] | None = None


def _load() -> dict[str, str]:
    global _cache
    if _cache is not None:
        return _cache
    out: dict[str, str] = {}
    try:
        for line in _ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    _cache = out
    return out


def get(name: str) -> str | None:
    return _load().get(name) or None


def has(name: str) -> bool:
    return bool(get(name))
