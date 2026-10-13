"""Subscription usage-limit detection for SWE-Together agent turns.

Only the CLI's own terminal/error events are inspected, never the whole
stdout: agent tool output routinely contains "429" or "rate limit" text.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_LIMIT_RE = re.compile(
    r"usage limit|hit your (?:\w+ )?limit|(?:session|weekly|daily|monthly) limit|limit reached"
    r"|rate.?limit|quota|too many requests|429",
    re.IGNORECASE,
)
# Claude Code: "You've hit your session limit · resets 5:40am (UTC)"
_CLOCK_RE = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:\(([^)]+)\))?", re.IGNORECASE)
_EPOCH_RE = re.compile(r"\|(\d{10})\b")
_IN_RE = re.compile(
    r"(?:try again|resets?) in\s+(?:(\d+)\s*days?)?[\s,]*(?:(\d+)\s*h(?:ours?)?)?[\s,]*"
    r"(?:(\d+)\s*m(?:in(?:utes?)?)?)?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class QuotaHit:
    harness: str
    message: str
    reset_epoch: float | None
    window: str | None = None      # "five_hour" | "seven_day" when Claude Code said which one (rate_limit_event)


def is_weekly(hit: QuotaHit, now: float | None = None, five_hour_max_s: float = 5.5 * 3600) -> bool:
    """The weekly window: Claude Code said `seven_day`; failing that, a reset further away than a 5-hour window can be."""
    if hit.window:
        return hit.window == "seven_day"
    now = time.time() if now is None else now
    return bool(hit.reset_epoch and hit.reset_epoch - now > five_hour_max_s)


def _terminal_messages(stdout: str, harness: str) -> list[str]:
    """Error messages of a turn that FAILED; [] for a turn that completed. codex logs
    `error` events ("Reconnecting... 2/5") inside turns that then succeed."""
    errors: list[str] = []
    plain: list[str] = []
    ok = failed = False
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            if line and ("error" in line.lower() or "limit" in line.lower()):
                plain.append(line)  # CLI error printed before/instead of the JSON stream
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        t = ev.get("type")
        if harness == "claude-code" and t == "result":
            if ev.get("is_error"):
                failed = True
                errors.append(str(ev.get("result") or ev.get("subtype") or ""))
            else:
                ok = True
        elif harness == "codex" and t == "turn.completed":
            ok = True
        elif harness == "codex" and t in ("error", "turn.failed"):
            failed = failed or t == "turn.failed"
            err = ev.get("error") if isinstance(ev.get("error"), dict) else {}
            errors.append(str(ev.get("message") or err.get("message") or ""))
    if failed:
        return errors
    if ok:
        return []
    return errors + plain


_TRANSIENT_RE = re.compile(
    r"reconnecting|routing discovery failed|stream disconnected|connection (reset|refused|closed)"
    r"|timed? ?out|internal server error|\b50[0234]\b|overloaded|service unavailable|econnreset",
    re.IGNORECASE,
)


def turn_error(stdout: str, harness: str) -> str | None:
    """First error message if the turn failed, else None."""
    msgs = [m for m in _terminal_messages(stdout or "", harness) if m]
    return msgs[-1][:300] if msgs else None


def detect_transient(stdout: str, harness: str) -> str | None:
    """A failed turn worth retrying as-is (backend hiccup), not a quota or input error."""
    for msg in _terminal_messages(stdout or "", harness):
        if _TRANSIENT_RE.search(msg) and not _LIMIT_RE.search(msg):
            return msg[:300]
    return None


def _parse_clock(msg: str, now: float) -> float | None:
    """Next occurrence of a wall-clock reset time such as "resets 5:40am (UTC)"."""
    m = _CLOCK_RE.search(msg)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    try:
        tz = ZoneInfo(m.group(4) or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        tz = timezone.utc
    cur = datetime.fromtimestamp(now, tz)
    target = cur.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= cur:
        target += timedelta(days=1)
    return target.timestamp()


def _parse_reset(msg: str, now: float) -> float | None:
    if m := _EPOCH_RE.search(msg):
        return float(m.group(1))
    if (m := _IN_RE.search(msg)) and any(m.groups()):
        d, h, mi = (int(x) if x else 0 for x in m.groups())
        return now + d * 86400 + h * 3600 + mi * 60
    return _parse_clock(msg, now)


def rejected_window(stdout: str) -> tuple[str, float] | None:
    """(window, reset epoch) of the `rate_limit_event` Claude Code emits when a limit REJECTS the request:
    {"rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour"|"seven_day", "resetsAt": <epoch>}}.
    The weekly window wins when both are rejected."""
    found: dict[str, float] = {}
    for line in (stdout or "").splitlines():
        if line.startswith("{") and "rate_limit_event" in line:
            try:
                info = json.loads(line).get("rate_limit_info") or {}
            except ValueError:
                continue
            if info.get("status") == "rejected" and info.get("rateLimitType") and info.get("resetsAt"):
                found[info["rateLimitType"]] = float(info["resetsAt"])
    for w in ("seven_day", "five_hour"):
        if w in found:
            return w, found[w]
    return next(iter(found.items()), None)


WEEKLY_FALLBACK_AT = 0.90      # weekly utilization (0-1) at which Claude turns move to OpenRouter, before the limit rejects


def weekly_utilization(stdout: str) -> tuple[float, float] | None:
    """(utilization 0-1, reset epoch) of the weekly window from the LAST `rate_limit_event` of a Claude Code stream, else None.
    Events carry `unifiedWindows.seven_day.utilization` for the whole account, whatever window the event is about."""
    found = None
    for line in (stdout or "").splitlines():
        if line.startswith("{") and "rate_limit_event" in line:
            try:
                w = ((json.loads(line).get("rate_limit_info") or {}).get("unifiedWindows") or {}).get("seven_day") or {}
            except ValueError:
                continue
            if w.get("utilization") is not None and w.get("resetsAt"):
                found = (float(w["utilization"]), float(w["resetsAt"]))
    return found


def detect_limit(stdout: str, harness: str, *, now: float | None = None) -> QuotaHit | None:
    for msg in _terminal_messages(stdout or "", harness):
        if _LIMIT_RE.search(msg):
            ev = rejected_window(stdout) if harness == "claude-code" else None
            reset = ev[1] if ev else _parse_reset(msg, time.time() if now is None else now)
            return QuotaHit(harness, msg[:500], reset, ev[0] if ev else None)
    return None


def wait_seconds(hit: QuotaHit, attempt: int, *, now: float | None = None) -> float:
    """Until the parsed reset (+60s slack), else backoff 5 -> 60 min."""
    now = time.time() if now is None else now
    if hit.reset_epoch and hit.reset_epoch > now:
        return min(hit.reset_epoch - now + 60, 6 * 3600)
    return min(300 * (2 ** attempt), 3600)


def probe_claude(timeout: float = 150.0) -> QuotaHit | None:
    """One minimal Claude Code call on the subscription: the QuotaHit if the limit is exhausted, None if usable.
    Uses the same top model the experiment uses, since limits can differ per model."""
    import os
    import subprocess

    from metaharness import secrets

    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")}
    env["CLAUDE_CODE_OAUTH_TOKEN"] = secrets.get("CLAUDE_CODE_OAUTH_TOKEN") or ""
    try:
        p = subprocess.run(["claude", "--verbose", "-p", "Reply with the single word OK.", "--model", "claude-opus-5-5",
                            "--effort", "low", "--output-format", "stream-json"],
                           capture_output=True, text=True, env=env, cwd="/tmp", timeout=timeout, stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError):
        return None          # an unreachable CLI is not a quota signal; the real turn will report its own error
    return detect_limit(p.stdout, "claude-code")
