"""`metaharness bench swet {run,judge,report,simcompare}` — SWE-Together with
per-turn routing, subscription auth by default. Runs inside the SWE-Together clone's
own venv (Harbor lives there); see swet_patches/ for the patch it needs."""
from __future__ import annotations

import json
import os
import subprocess
import time
import sys
from pathlib import Path

from metaharness import secrets
from metaharness.bench import pricing, swet_pull
from metaharness.config import HOME

SWET = HOME / "bench" / "SWE-Together"
REPO = Path(__file__).resolve().parents[2]
_CA_OVERLAY = HOME / "bench" / "swet-ca.yaml"
_CA_BUNDLE = HOME / "bench" / "ca-bundle.pem"
_CA_IN = "/etc/ssl/certs/metaharness-ca-bundle.pem"
_STRIP = ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
          "OPENROUTER_API_KEY", "OPENAI_API_KEY")


_GEMINI = "openrouter/google/gemini-3.1-pro-preview"   # simulator + tagger (leaderboard's choice)


def _env(tag: str | None = None, lock: bool = False, kv_hint: bool = False, api_cap: float | None = None,
         claude_api: float | None = None, claude_always: bool = False, judge_openrouter: bool | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP}
    if judge_openrouter:
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    else:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = secrets.get("CLAUDE_CODE_OAUTH_TOKEN") or ""
    if judge_openrouter is not None:
        # Only this judge subprocess changes backend; trials and later queue stages keep their defaults.
        env["SWT_JUDGE_BACKEND"] = "openrouter" if judge_openrouter else "native"
        env["SWT_JUDGE_MODEL"] = "claude-opus-4.6"
        env.pop("JUDGE_VIA_OR", None)
        env.pop("JUDGE_VIA_CODEX", None)
    # Gemini key: host-side only (simulator in the harbor process, tagger subprocess). The agent
    # container env is built separately and never receives it.
    env["OPENROUTER_API_KEY"] = secrets.get("GEMINI_OPENROUTER_API_KEY") or ""
    if _CA_BUNDLE.exists():  # corporate TLS inspection (VPN) re-signs openrouter.ai: host-side clients must trust its CA
        env["SSL_CERT_FILE"] = env["REQUESTS_CA_BUNDLE"] = env["CURL_CA_BUNDLE"] = str(_CA_BUNDLE)
    env["MH_SIM_BACKEND"] = "litellm"
    if tag:
        env["MH_LEDGER"] = str(SWET / "results" / tag / "ledger.jsonl")
    if tag and api_cap is not None:
        env["MH_API_CAP_USD"] = str(api_cap)
        env["MH_API_SPEND_DIR"] = str(SWET / "results" / tag / "api_spend")
    if tag and claude_api is not None:
        env["MH_CLAUDE_API_CAP_USD"] = str(claude_api)
        # one queue = one budget: `swet queue` points every stage's run at the same directory
        env["MH_CLAUDE_API_SPEND_DIR"] = os.environ.get("MH_CLAUDE_API_SHARED_DIR") or str(SWET / "results" / tag / "claude_api_spend")
        if claude_always:
            env["MH_FORCE_CLAUDE_API"] = "1"
    if lock:
        env["MH_LOCK_HARNESS"] = "1"
    if kv_hint:
        env["MH_KV_HINT"] = "1"
    env["PYTHONPATH"] = f"src:{REPO}"
    if _CA_OVERLAY.exists():
        env["HARBOR_EXTRA_COMPOSE"] = str(_CA_OVERLAY)
    if _CA_BUNDLE.exists():
        env["JUDGE_DOCKER_ARGS"] = (f"-v {_CA_BUNDLE}:{_CA_IN}:ro -e SSL_CERT_FILE={_CA_IN} "
                                    f"-e CURL_CA_BUNDLE={_CA_IN} -e NODE_EXTRA_CA_CERTS={_CA_IN}")
    env["JUDGE_SANDBOX"] = "docker"
    return env


def _py(*args: str, tag: str | None = None, lock: bool = False, kv_hint: bool = False, api_cap: float | None = None,
        claude_api: float | None = None, claude_always: bool = False, judge_openrouter: bool | None = None) -> int:
    return subprocess.call([str(SWET / ".venv" / "bin" / "python"), *args], cwd=SWET,
                           env=_env(tag, lock, kv_hint, api_cap, claude_api, claude_always, judge_openrouter))


def start_api_spend(tag: str, harness: str = "codex") -> float:
    """Fresh accounting for this run: baseline = API spend of everything already under trials/<tag> (archived trials
    included, they were paid for), per-trial files of earlier runs cleared. Returns the baseline in USD."""
    from metaharness.bench import swet_usage
    from metaharness.bench.swet_control import api_spend_total, record_api_spend

    shared = os.environ.get("MH_CLAUDE_API_SHARED_DIR") if harness == "claude" else None
    if shared:      # a queue-wide budget keeps counting across its stages and rounds: nothing to reset, no baseline to add
        return api_spend_total(Path(shared))
    spend_dir = SWET / "results" / tag / ("api_spend" if harness == "codex" else "claude_api_spend")
    for f in spend_dir.glob("*.json") if spend_dir.exists() else []:
        f.unlink()
    base = 0.0
    for agent in (SWET / "trials" / tag).rglob("agent"):
        if (agent / "routing.jsonl").exists():
            base += swet_usage.summarize(swet_usage.extract(agent))["api_billed_codex" if harness == "codex" else "api_billed_claude"]
    record_api_spend(spend_dir, "baseline", base)
    return base


_PENDING = """
import json
from pathlib import Path
import run_eval as R
tasks = json.load(open("canonical_full109.json"))["tasks"]
print("PENDING=" + json.dumps([t for t in tasks if not R.is_task_completed(t, Path("trials/%s"))]))
"""


def pending(tag: str) -> list[str]:
    """Canonical tasks with no counted-complete trial under trials/<tag> (what `run --skip-existing` would run)."""
    env = {**_env(), "PYTHONPATH": f"src:external/harbor/src:{REPO}"}
    p = subprocess.run([str(SWET / ".venv" / "bin" / "python"), "-c", _PENDING % tag], cwd=SWET, env=env,
                       capture_output=True, text=True)
    line = next((x for x in p.stdout.splitlines() if x.startswith("PENDING=")), None)
    if line is None:
        raise RuntimeError(f"could not list pending tasks: {(p.stderr or p.stdout)[-400:]}")
    return json.loads(line[len("PENDING="):])


_PING = """
import asyncio, sys
sys.path.insert(0, "external/harbor/src")
from harbor.llms.lite_llm import LiteLLM
r = asyncio.run(LiteLLM(model_name="%s", temperature=0.0).call("Reply with the single word OK."))
print("OK" if r.content else "EMPTY")
"""


def _sim_reachable() -> bool:
    """One real simulator-model call through the exact client/env the trials use. Without this, a blocked
    network silently turns every simulator turn into a no-op and trials end early with junk results."""
    p = subprocess.run([str(SWET / ".venv" / "bin" / "python"), "-c", _PING % _GEMINI], cwd=SWET, env=_env(),
                       capture_output=True, text=True, timeout=120)
    ok = p.returncode == 0 and "OK" in p.stdout
    print("simulator model reachable:", "yes" if ok else f"NO\n{(p.stderr or p.stdout)[-600:]}")
    return ok


def _prepare_claude(claude_api: float | None, always: bool = False) -> None:
    """Start of `swet run`: with the one-off API fallback on, a closed WEEKLY window means "run on OpenRouter until it
    resets" instead of waiting (a closed 5-hour window still waits); without it (the normal policy) wait, and drop any API state an earlier run left behind."""
    from metaharness.bench.swet_control import clear_claude_api, clear_claude_block, set_claude_api
    from metaharness.bench.swet_quota import is_weekly, probe_claude

    if claude_api is None:
        clear_claude_api()
        _wait_for_claude()
        return
    clear_claude_block()
    if always:      # Claude never touches the subscription in this run: nothing to probe or wait for
        clear_claude_api()
        print(f"Claude runs through OpenRouter for this whole run (cap ${claude_api:g}); the subscription is not used", flush=True)
        return
    hit = probe_claude()
    if hit is None:
        clear_claude_api()
        print("claude limit: usable (API fallback armed for this run)", flush=True)
    elif hit.reset_epoch and is_weekly(hit):
        set_claude_api(hit.reset_epoch + 60)
        print(f"claude WEEKLY limit exhausted ({hit.message[:60]}): running on OpenRouter until "
              f"{time.strftime('%m-%d %H:%M', time.localtime(hit.reset_epoch))}, spend cap ${claude_api:g}", flush=True)
    else:
        _wait_for_claude()          # the 5-hour window (or an unknown reset): wait as usual


def _wait_for_claude() -> None:
    """The experiment only means something while Claude is usable: wait here, before any container starts."""
    from metaharness.bench.swet_control import clear_claude_block
    from metaharness.bench.swet_quota import probe_claude, wait_seconds

    clear_claude_block()      # a block left by an earlier run may be stale (limits get reset by hand)
    while (hit := probe_claude()) is not None:
        wait = wait_seconds(hit, 0)
        print(f"claude limit exhausted ({hit.message[:80]}); waiting {wait / 60:.0f} min "
              f"(re-checking every 10 min)", flush=True)
        time.sleep(min(wait, 600))
    print("claude limit: usable", flush=True)


def _run(a) -> int:
    """Price-check first (list-price math is only as good as the table), then launch the trials."""
    out = SWET / "results" / a.tag
    out.mkdir(parents=True, exist_ok=True)
    chk = pricing.check_against_openrouter()
    (out / "pricing_check.json").write_text(json.dumps({**chk, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}, indent=2))
    print(f"price table vs OpenRouter: {chk['checked']} models checked, {len(chk['mismatches'])} mismatches"
          + (f", error: {chk['error']}" if chk["error"] else ""))
    for m in chk["mismatches"]:
        print("  PRICE MISMATCH:", m)
    if chk["mismatches"] and not a.ignore_price_mismatch:
        print("fix metaharness/bench/pricing.py (or pass --ignore-price-mismatch) before running")
        return 2
    cap = a.claude_api if a.claude_api is not None else a.claude_api_fallback
    always = a.claude_api is not None
    from metaharness.config import load as load_config
    backend = load_config().router.backend
    if backend != "llm" and os.environ.get("MH_ALLOW_ROUTER_BACKEND") != backend:
        print(f"aborting: [router] backend is {backend!r} in ~/.metaharness/config.toml, but these experiments use the 'llm' router "
              f"(the config is re-read on every routing call, so another session's change would switch the router mid-run). "
              f"Set it back to \"llm\", or export MH_ALLOW_ROUTER_BACKEND={backend} to run with it on purpose.")
        return 2
    _prepare_claude(cap, always)
    if not _sim_reachable():
        print("aborting: fix the network/TLS path to the simulator model first (VPN? CA bundle?)")
        return 2
    if cap is not None:
        cb = start_api_spend(a.tag, "claude")
        scope = "this whole queue" if os.environ.get("MH_CLAUDE_API_SHARED_DIR") else a.tag
        print(f"ONE-OFF Claude on OpenRouter ({'always' if always else 'only after the weekly limit'}), cap ${cap:g} for {scope} "
              f"(already spent there: ${cb:.2f}); once the cap is spent Claude goes back to the subscription rules", flush=True)
    base = start_api_spend(a.tag)
    print(f"GPT API spend of {a.tag} so far: ${base:.2f}; cap ${a.api_cap:g} (Codex turns wait for the subscription beyond it)", flush=True)
    return _py(
        "src/run_eval.py", "--model", "anthropic/claude-opus-5-5",
        "--user-model", _GEMINI, "--agent-type", "metaharness",
        "--env-type", "docker", "--tag", a.tag, "--trials-dir", f"trials/{a.tag}",
        "--workers", str(a.workers), "--agent-timeout", "172800", "--skip-existing",
        *(["--tasks", a.tasks] if a.tasks else []), tag=a.tag, lock=a.lock_harness, kv_hint=a.kv_hint, api_cap=a.api_cap, claude_api=cap, claude_always=always)


def clear_failed_verdicts(tag: str) -> int:
    """judge_verdict.json files are never re-made once they exist, so a judge that died on a Claude limit, a patch-apply
    timeout, a failed Claude install or a failed sandbox would stay unscored forever. Delete exactly those so the next
    `judge` run redoes them (a plain `patch_apply_failed` is a real answer about the patch and stays)."""
    n = 0
    for f in (SWET / "trials" / tag).glob("*/judge_verdict.json"):
        try:
            v = json.loads(f.read_text())
        except (OSError, ValueError):
            v = {"error": "unreadable"}
        if v.get("judge_score") is not None:
            continue
        err, tail = v.get("error"), (v.get("judge_stdout_tail") or "") + (v.get("judge_stderr_tail") or "") + str(v.get("stderr") or "")
        if err in ("verdict_read_failed", "unreadable", "claude_install_failed", "sandbox_failed") or (
                err == "patch_apply_failed" and "timed out" in tail):
            f.unlink()
            n += 1
    return n


def _judge(a) -> int:
    """Correctness judge + message tagging (Gemini). Intent-coverage is skipped, but the tagger writes
    into intent_coverage_verdict.json, so give each trial an empty one to tag into."""
    openrouter = getattr(a, "openrouter", False)
    if openrouter:
        if a.tag != "expA":
            print("--openrouter is a one-off for expA judging only; other tags use the subscription")
            return 2
        if not secrets.get("GEMINI_OPENROUTER_API_KEY"):
            print("OpenRouter judging needs GEMINI_OPENROUTER_API_KEY in ~/.metaharness/.env")
            return 2
        print("ONE-OFF expA judge via OpenRouter (Claude Opus 4.6); existing verdicts with scores are kept", flush=True)
    else:
        _wait_for_claude()          # the normal judge still runs on the Claude subscription
    print(f"cleared {clear_failed_verdicts(a.tag)} failed verdicts to redo", flush=True)
    for t in (SWET / "trials" / a.tag).glob("*/agent"):
        if not (t.parent / "result.json").exists():
            continue                      # still running: the tagger would freeze tags on a half conversation
        stub = t.parent / "intent_coverage_verdict.json"
        if not stub.exists():
            stub.write_text("{}")
    return _py("eval/run_eval.py", "--trials-root", f"trials/{a.tag}", "--output-dir", f"results/{a.tag}",
               "--model-tag", a.tag, "--skip-intent-coverage", "--tag-model", _GEMINI, "--tag-workers", "4",
               "--correctness-workers", str(a.workers), tag=a.tag, judge_openrouter=openrouter)


def _control(action: str) -> int:
    """pause: agents stop at their next turn boundary (a running turn finishes) and idle until resume."""
    from metaharness.bench.swet_control import PAUSE_FILE
    if action == "pause":
        PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PAUSE_FILE.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"))
        print("paused: each trial stops at its next turn boundary; `metaharness bench swet resume` continues")
    elif action == "resume":
        PAUSE_FILE.unlink(missing_ok=True)
        print("resumed")
    paused = PAUSE_FILE.exists()
    inflight = []
    for tdir in (SWET / "trials").glob("*/*"):
        if (tdir / "agent" / "routing.jsonl").exists() and not (tdir / "result.json").exists():
            age = time.time() - (tdir / "agent" / "routing.jsonl").stat().st_mtime
            if age < 30 * 60:   # older ones are leftovers of killed runs
                inflight.append((tdir.name, age))
    print(f"state: {'PAUSED' if paused else 'running'}; trials in flight: {len(inflight)}")
    for name, age in sorted(inflight):
        print(f"  {name[:48]:48} last routing/log activity {age / 60:.1f} min ago")
    return 0


def register(bs) -> None:
    p = bs.add_parser("swet", help="SWE-Together, re-routed every user turn (subscription only)")
    ss = p.add_subparsers(dest="swet_cmd", required=True)

    pl = ss.add_parser("pull", help="pre-pull the task docker images (resumable; do this before `run`)")
    pl.add_argument("--workers", type=int, default=3)
    pl.add_argument("--tasks", default=None, help="comma-separated task names (default: all 109)")
    pl.add_argument("--max-minutes", type=float, default=None,
                    help="stop starting new pulls after this long; exits 3 while images remain")
    pl.add_argument("--force", action="store_true", help="skip the free-disk check")
    pl.set_defaults(func=lambda a: swet_pull.run(
        a.workers, a.tasks.split(",") if a.tasks else None, a.max_minutes, a.force))

    r = ss.add_parser("run", help="run trials (resumable: finished tasks are skipped)")
    r.add_argument("--tag", required=True)
    r.add_argument("--tasks", default=None, help="comma-separated task names (default: all 109)")
    r.add_argument("--workers", type=int, default=3)
    r.add_argument("--ignore-price-mismatch", action="store_true")
    r.add_argument("--api-cap", type=float, default=100.0,
                   help="cumulative GPT API spend (USD) of this experiment above which Codex turns stop using the API")
    r.add_argument("--claude-api", type=float, default=None, metavar="USD",
                   help="ONE-OFF: run ALL Claude turns of this run through OpenRouter (cost cap USD) instead of the subscription")
    r.add_argument("--claude-api-fallback", type=float, default=None, metavar="USD",
                   help="ONE-OFF: when Claude's WEEKLY limit is hit, run Claude turns through OpenRouter (up to USD) until "
                        "it resets. Off by default; the 5-hour limit still waits for its reset")
    r.add_argument("--lock-harness", action="store_true",
                   help="route turn 1 freely, then lock the task to that harness (later menus list only its models)")
    r.add_argument("--kv-hint", action="store_true",
                   help="add the KV-cache cost note about switching models to the router prompt (experiment B)")
    r.set_defaults(func=_run)

    for name, h in (("pause", "stop at the next turn boundary until `resume` (running turns finish first)"),
                    ("resume", "continue after `pause`"), ("status", "paused/running + trials in flight")):
        ss.add_parser(name, help=h).set_defaults(func=lambda a, n=name: _control(n))

    pd = ss.add_parser("pending", help="tasks of the tag without a counted-complete trial")
    pd.add_argument("--tag", required=True)
    pd.set_defaults(func=lambda a: print(",".join(pending(a.tag))) or 0)

    rd = ss.add_parser("redo", help="list (and with --apply, archive) the trials that were stuck in plan/ask mode, so they run again")
    rd.add_argument("--tag", required=True)
    rd.add_argument("--apply", action="store_true", help="move the selected trials to _failed/ (default: dry run)")
    rd.set_defaults(func=lambda a: __import__("metaharness.bench.swet_redo", fromlist=["main"]).main(a.tag, a.apply))

    q = ss.add_parser("queue", help="unattended chain: finish -> retry failures -> pull -> judge -> report, per stage")
    q.add_argument("--stages", default="real1,expA,expB", help="comma list: real1 | expA (harness locked after the first turn) | expB (expA + KV-cache note)")
    q.add_argument("--workers", type=int, default=None, help="override for every stage (default: real1=3, expA=2, expB=2)")
    q.add_argument("--claude-api", type=float, default=None, metavar="USD",
                   help="ONE-OFF, passed to every `swet run` of this queue: all Claude turns via OpenRouter (see `swet run --help`)")
    q.add_argument("--claude-api-fallback", type=float, default=None, metavar="USD",
                   help="ONE-OFF, passed to every `swet run` of this queue (see `swet run --help`)")
    q.add_argument("--pipeline", action="store_true",
                   help="judge in the background while later stages run (sized by the free Docker memory). Off by default: "
                        "stages run one phase at a time, so judging and trials never run together")
    q.add_argument("--redo-stuck", action="store_true",
                   help="at the start of each stage archive the trials that were stuck in plan/ask mode (`swet redo`) so they run again")
    qm = q.add_mutually_exclusive_group()
    qm.add_argument("--run-only", action="store_true", help="run trials (with retries) but do not judge")
    qm.add_argument("--judge-only", action="store_true", help="skip running trials: judge + report only")
    q.set_defaults(func=lambda a: __import__("metaharness.bench.swet_queue", fromlist=["main"]).main(
        a.stages.split(","), a.workers, "run" if a.run_only else "judge" if a.judge_only else "all",
        (["--claude-api", str(a.claude_api)] if a.claude_api is not None else
         ["--claude-api-fallback", str(a.claude_api_fallback)] if a.claude_api_fallback is not None else None),
        a.redo_stuck, not a.pipeline))

    j = ss.add_parser("judge", help="correctness judge (local docker) + message tagging (Gemini)")
    j.add_argument("--tag", required=True)
    j.add_argument("--workers", type=int, default=3)
    j.add_argument("--openrouter", action="store_true",
                   help="ONE-OFF expA only: judge unfinished trials with Claude Opus 4.6 via OpenRouter, bypassing subscription waits")
    j.set_defaults(func=_judge)

    rp = ss.add_parser("report", help="leaderboard metrics + cost breakdown + routing detail")
    rp.add_argument("--tag", required=True)
    rp.set_defaults(func=lambda a: subprocess.call(
        [sys.executable, "-m", "metaharness.bench.swet_report", str(SWET / "trials" / a.tag),
         "--results", str(SWET / "results" / a.tag)], cwd=REPO))

    cp = ss.add_parser("compare", help="side-by-side metrics of several experiments (first = baseline)")
    cp.add_argument("tags", nargs="+")
    cp.set_defaults(func=lambda a: subprocess.call(
        [sys.executable, "-m", "metaharness.bench.swet_report", "--compare", *a.tags], cwd=REPO))

    c = ss.add_parser("simcompare", help="replay recorded sim calls on Gemini 3.1 Pro (OpenRouter)")
    c.add_argument("--tag", required=True)
    c.add_argument("--n", type=int, default=30)
    c.add_argument("--budget", type=float, default=0.50)
    c.set_defaults(func=lambda a: subprocess.call(
        [str(SWET / ".venv" / "bin" / "python"), "-m", "metaharness.bench.swet_simcompare",
         f"trials/{a.tag}", "--n", str(a.n), "--budget", str(a.budget)],
        cwd=SWET, env={**os.environ, "PYTHONPATH": f"src:{REPO}",
                       **({"SSL_CERT_FILE": str(_CA_BUNDLE)} if _CA_BUNDLE.exists() else {})}))
