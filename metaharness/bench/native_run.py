"""Stock SWE-Together agents on the user's subscriptions, in the fresh clone at <repo>/SWE-Together.

    python3 -m metaharness.bench.native_run run   --combo claude|codex --tag nat_claude [--tasks a,b] [--workers 3]
    python3 -m metaharness.bench.native_run judge --tag nat_claude [--workers 3]

claude = `--agent-type claude-code` + claude-opus-5-5 (CLAUDE_CODE_OAUTH_TOKEN, subscription)
codex  = `--agent-type codex` + gpt-6-luna (host ~/.codex/auth.json, subscription)
Simulator and tagger: Gemini 3.1 Pro through OpenRouter. Judge: Claude Opus 4.6 on the Claude subscription.
Both CLIs are installed at their latest release, effort high (see SWE-Together/.env); a subscription limit makes the
turn wait for the reset (src/user_agent/exec_helpers.py::exec_with_quota_wait) instead of failing.
It reuses swet_cli's environment (CA bundle for the corporate proxy, OpenRouter key, docker judge) pointed at the clone.
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from metaharness.bench import swet_cli

import os

from metaharness import secrets

# Cloud sessions have no ~/.metaharness/.env: take the two secrets from the environment instead. SWT_OAUTH_TOKEN is not
# called CLAUDE_CODE_OAUTH_TOKEN on purpose: that name would also switch the cloud session's own `claude` to the subscription.
for _env, _name in (("SWT_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"), ("OPENROUTER_API_KEY", "GEMINI_OPENROUTER_API_KEY")):
    if os.environ.get(_env) and not secrets.get(_name):
        secrets._load()[_name] = os.environ[_env]

_ROOT = Path(__file__).resolve().parents[2]
CLONE = _ROOT if (_ROOT / "src" / "run_eval.py").exists() else _ROOT / "SWE-Together"   # the fork carries these files at its root
swet_cli.SWET = CLONE          # swet_cli resolves SWET at call time

COMBOS = {
    "claude": ("claude-code", "anthropic/claude-opus-5-5"),
    "codex": ("codex", "openai/gpt-6-luna"),
}


def run(a: argparse.Namespace) -> int:
    agent, model = COMBOS[a.combo]
    (CLONE / "results" / a.tag).mkdir(parents=True, exist_ok=True)
    if not swet_cli._sim_reachable():
        print("aborting: fix the network/TLS path to the simulator model first (VPN? CA bundle?)")
        return 2
    env = swet_cli._env(a.tag)
    if a.combo == "claude":
        # run_eval's key resolver wants a key for the model; an OAuth token (sk-ant-oat01-…) there makes build_agent_env
        # send it as CLAUDE_CODE_OAUTH_TOKEN (subscription billing), never as an API key.
        env["ANTHROPIC_API_KEY"] = env["CLAUDE_CODE_OAUTH_TOKEN"]
    else:
        env["OPENAI_API_KEY"] = "subscription-via-host-auth"      # placeholder: auth.json from CODEX_USE_HOST_AUTH is used
    cmd = [str(CLONE / ".venv" / "bin" / "python"), "src/run_eval.py", "--model", model, "--user-model", swet_cli._GEMINI,
           "--agent-type", agent, "--env-type", "docker", "--tag", a.tag, "--trials-dir", f"trials/{a.tag}",
           "--workers", str(a.workers), "--agent-timeout", "172800", "--skip-existing",
           *(["--tasks", a.tasks] if a.tasks else []), *(["--dry-run"] if a.dry_run else [])]
    return subprocess.call(cmd, cwd=CLONE, env=env)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="native_run")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--combo", choices=COMBOS, required=True)
    r.add_argument("--tag", required=True)
    r.add_argument("--tasks")
    r.add_argument("--workers", type=int, default=3)
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(func=run)
    j = sub.add_parser("judge")
    j.add_argument("--tag", required=True)
    j.add_argument("--workers", type=int, default=3)
    j.set_defaults(func=lambda a: swet_cli._judge(argparse.Namespace(tag=a.tag, workers=a.workers, openrouter=False)))
    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
