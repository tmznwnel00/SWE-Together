"""Bounded exec helper for the multi-turn wrappers.

Why this exists: a single `environment.exec()` can block indefinitely —
e.g. gpt-5.5 (reasoning) via OpenRouter occasionally enters an "I'm still
thinking…" stream that never emits a final answer, so the codex CLI waits
for the next token forever (observed in gpt55_codex_scout_v3 r2/r3, each
burned 3h25m wall-clock on one stuck exec before being killed manually).

The wrappers' multi-turn loop has a `_TRIAL_BUDGET_SEC` guard, but that
fires *between* turns — if a single exec never returns, the guard never
gets a chance to run.

All wrappers — including claude_code — now run turns through this, so a
slow/stuck turn is capped per-exec and cap-rescued (resume on next turn)
rather than killing the trial.

Default caps (shared by every harness):
  - PER_EXEC_CAP_SEC (1800) — per-turn single-call ceiling (30m).
  - TRIAL_BUDGET_SEC (5400) — total per-trial budget (90m), set equal to
    the E2B sandbox lifetime so the wrapper's own budget is the real
    ceiling and stops cleanly, instead of the sandbox dying mid-trial.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

import os

TRIAL_BUDGET_SEC = int(os.environ.get("TRIAL_BUDGET_SEC", "5400"))
PER_EXEC_CAP_SEC = int(os.environ.get("PER_EXEC_CAP_SEC", "1800"))


@dataclass
class _TimeoutResult:
    """ExecResult-shaped synthetic for the timeout case.

    Mirrors the attributes the wrappers read off harbor's exec result
    (`return_code`, `stdout`, `stderr`) so callers don't need a branch.
    """
    return_code: int = -1
    stdout: str = ""
    stderr: str = ""


async def exec_with_budget(environment, exec_input, *, start_time: float):
    """Run one ExecInput with the trial-budget + per-exec cap.

    Wraps the underlying `environment.exec()` in `asyncio.wait_for` so a
    hung sandbox call can't burn the whole trial.

    Returns ``(result, timed_out)``. On timeout, ``result`` is a synthetic
    ``_TimeoutResult`` with ``return_code=-1`` and a diagnostic stderr —
    callers can keep their existing ``result.stdout`` / ``result.stderr``
    / ``result.return_code`` paths and decide whether to ``break`` the
    multi-turn loop based on ``timed_out``.

    `set -o pipefail; ...` is prepended to the command (matching what
    every existing call-site already does).
    """
    elapsed = time.monotonic() - start_time
    remaining = max(TRIAL_BUDGET_SEC - elapsed, 1.0)
    cap_sec = int(min(remaining, PER_EXEC_CAP_SEC))

    try:
        result = await asyncio.wait_for(
            environment.exec(
                command=f"set -o pipefail; {exec_input.command}",
                cwd=exec_input.cwd,
                env=exec_input.env,
                timeout_sec=cap_sec,
            ),
            # +30s slack so e2b's own timeout_sec fires first if respected
            timeout=cap_sec + 30,
        )
        return result, False
    except asyncio.TimeoutError:
        msg = (
            f"[wrapper] exec capped at {cap_sec}s "
            f"(trial budget remaining {int(remaining)}s) — abandoning call"
        )
        log.warning(msg)
        return _TimeoutResult(stderr=msg), True
    except RuntimeError as exc:
        # [metaharness] Harbor's LOCAL DOCKER environment enforces `timeout_sec` itself and raises this, at cap_sec,
        # before the wait_for above (cap_sec + 30) can fire. Uncaught, it killed the whole trial instead of being
        # cap-rescued like on E2B. Treat it as the same timeout. The exec'd process keeps running inside the
        # container (only the docker client is terminated): the caller must stop it before the next turn.
        if not str(exc).startswith("Command timed out after"):
            raise
        msg = f"[wrapper] exec capped at {cap_sec}s by the docker environment (trial budget remaining {int(remaining)}s) — abandoning call"
        log.warning(msg)
        try:      # the CLI keeps running inside the container: stop it so the cap-rescue resume does not race it
            await asyncio.wait_for(environment.exec(command="pkill -f '[c]laude --verbose'; pkill -f '[c]odex exec'; true", timeout_sec=20), timeout=40)
        except Exception:
            pass
        return _TimeoutResult(stderr=msg), True


# ── [local-run] subscription quota wait ──────────────────────────────────────────────────────────────
# The stock wrappers have no answer to a subscription limit: the turn "succeeds" with a limit message and the
# simulator then talks to nothing. This waits for the reset and runs the same command again. Claude and Codex
# are separate: a Claude limit holds only Claude trials, a Codex limit only Codex trials. Time spent waiting is
# added back to the agent's start_time so it does not eat the 90-minute trial budget.
_NATIVE_QUOTA_MAX_TRIES = 40


def _native_harness(command: str) -> str | None:
    if "claude " in command and "--output-format" in command:
        return "claude-code"
    if "codex exec" in command:
        return "codex"
    return None


async def exec_with_quota_wait(agent, environment, exec_input):
    """exec_with_budget + wait-for-reset on a subscription limit. `agent._start_time` is shifted by the wait."""
    try:
        from metaharness.bench.swet_quota import detect_limit, wait_seconds
    except ImportError:          # Meta-Harness repo not on PYTHONPATH: stock behaviour
        return await exec_with_budget(environment, exec_input, start_time=agent._start_time)
    harness = _native_harness(exec_input.command)
    ctl = os.path.expanduser("~/.metaharness/bench/control")
    block = os.path.join(ctl, f"NATIVE_BLOCK_{harness}") if harness else None
    for attempt in range(_NATIVE_QUOTA_MAX_TRIES):
        if block and os.path.exists(block):          # another trial already hit this harness's limit
            try:
                until = float(open(block).read().strip())
            except (OSError, ValueError):
                until = 0.0
            if until > time.time():
                await _quota_sleep(agent, until - time.time(), harness, "waiting (shared)")
        result, timed_out = await exec_with_budget(environment, exec_input, start_time=agent._start_time)
        hit = None if timed_out or not harness else detect_limit(result.stdout or "", harness)
        if hit is None:
            return result, timed_out
        wait = wait_seconds(hit, attempt)
        os.makedirs(ctl, exist_ok=True)
        until = time.time() + wait
        try:
            prev = float(open(block).read().strip()) if os.path.exists(block) else 0.0
        except (OSError, ValueError):
            prev = 0.0
        open(block, "w").write(str(max(until, prev)))
        log.warning("%s subscription limit hit (%s): waiting %.0f min then running the turn again", harness, hit.window or "?", wait / 60)
        await _quota_sleep(agent, wait, harness, "limit hit")
    return result, timed_out


async def _quota_sleep(agent, seconds: float, harness: str, why: str) -> None:
    t0 = time.monotonic()
    await asyncio.sleep(max(seconds, 0))
    waited = time.monotonic() - t0
    agent._start_time += waited                  # waiting is not agent work: keep the trial budget intact
    total = getattr(agent, "_quota_wait_s", 0.0) + waited
    agent._quota_wait_s = total
    try:
        import json
        with open(os.path.join(agent.logs_dir, "quota_wait.json"), "w") as f:
            json.dump({"harness": harness, "total_wait_s": round(total), "last": why}, f)
    except OSError:
        pass
