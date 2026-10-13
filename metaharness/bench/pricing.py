"""Fallback $ estimates for harnesses whose CLI output doesn't report cost_usd
(codex is the known case — dash/usage.py has the same gap), and the always-on
list-price recomputation for subscription-auth trials (subscription cost_usd
from Claude Code may be $0 or absent — see bench/runner.py). Hand-maintained,
only for models we actually route to. Callers must label these `estimated`
— never presented as a real charge (cost-honesty convention, see dash/usage.py).
"""
from __future__ import annotations

# $ per 1M tokens, (input, output). Update as models/pricing change.
_RATES: dict[str, tuple[float, float]] = {
    # Cross-checked directly against litellm.model_cost — the same table
    # harbor's codex agent uses for its own authoritative cost_usd (see
    # harbor/agents/installed/codex.py:_compute_cost_from_pricing) — rather
    # than web pricing pages. Caught two real errors this way: sol was
    # (5.0, 30.0) instead of (4.0, 20.0), and gpt-5.5 was (1.5, 6.0) instead
    # of (5.0, 30.0) — a 5x understatement. Cache multipliers confirmed
    # uniform across every OpenAI model here (0.1x read, 1.25x write; no
    # TTL-tiered write rate the way Anthropic has) — no per-model overrides
    # needed on the codex side of _CACHE_READ_MULT.
    # gpt-6.x short-context (<=272K input) rates, checked against the OpenAI pricing page 2026-10-01.
    # >272K-input requests are repriced (2x input, 1.5x output for sol/luna; 1.5x output for astra);
    # per-request context isn't visible in the aggregated per-turn usage, so that surcharge is not modeled.
    "gpt-6-sol": (2.0, 10.0),
    "gpt-6-luna": (0.1, 0.5),
    "gpt-6-astra": (10.0, 50.0),  # standard tier; no long-context/fast-mode repricing here
    "claude-opus-4-6": (5.0, 25.0),  # judge model
    "gpt-5.6-sol": (4.0, 20.0),
    "gpt-5.6-terra": (2.0, 12.0),
    "gpt-5.6-luna": (0.2, 1.2),
    "gpt-5.5": (5.0, 30.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-5-5": (4.0, 20.0),  # Opus 5.5 — cheaper than 5, own cache-read discount
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}

# Cache read/write multipliers on the input rate (Anthropic pricing). Claude
# Fable 5.1 reads at 0.025x instead of the usual 0.1x — everything else in
# _RATES uses the standard multiplier.
_CACHE_READ_MULT: dict[str, float] = {"claude-fable-5-1": 0.025, "claude-opus-5-5": 0.05}
_CACHE_READ_MULT_DEFAULT = 0.1
_CACHE_WRITE_5M_MULT = 1.25
_CACHE_WRITE_1H_MULT = 2.0


# OpenAI long-context tier: a single request with >272K input tokens is repriced
# (input & cached & cache-write x2; output x1.5 for gpt-6-sol/luna, 1.5x for astra's 50 -> 75).
LONG_CONTEXT_INPUT_TOKENS = 272_000
_LONG_MULT: dict[str, tuple[float, float]] = {
    "gpt-6-sol": (2.0, 1.5), "gpt-6-luna": (2.0, 1.5), "gpt-6-astra": (2.0, 1.5),
}


def long_context_extra(model: str | None, *, call_input: int, cached: int, write: int, output: int) -> float:
    """Extra $ for ONE request if its input crosses the long-context threshold (else 0)."""
    if not model or model not in _RATES or model not in _LONG_MULT or call_input <= LONG_CONTEXT_INPUT_TOKENS:
        return 0.0
    in_rate, out_rate = _RATES[model]
    im, om = _LONG_MULT[model]
    fresh = call_input - cached - write
    base = fresh * in_rate + cached * in_rate * _CACHE_READ_MULT_DEFAULT + write * in_rate * _CACHE_WRITE_5M_MULT + output * out_rate
    longp = (fresh * in_rate + cached * in_rate * _CACHE_READ_MULT_DEFAULT + write * in_rate * _CACHE_WRITE_5M_MULT) * im + output * out_rate * om
    return round((longp - base) / 1_000_000, 6)


def estimate(model: str | None, input_tokens: int, output_tokens: int) -> float | None:
    """Plain (no cache breakdown) estimate — kept for callers that only have
    the harbor-reported n_input_tokens/n_output_tokens totals. None if we have
    no rate for this model — caller must not invent a number."""
    if not model or model not in _RATES:
        return None
    in_rate, out_rate = _RATES[model]
    return round(input_tokens / 1_000_000 * in_rate + output_tokens / 1_000_000 * out_rate, 6)


def estimate_with_cache(
    model: str | None,
    *,
    fresh_input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_5m_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
) -> float | None:
    """Cache-aware list-price estimate. Claude Code's own n_input_tokens is
    input + cache_read + cache_creation summed together (harbor's
    claude_code.py: "Align with Anthropic session totals") — callers must
    subtract cache_read/cache_write out of it themselves before passing
    fresh_input_tokens here; this function does not do that subtraction.
    None if we have no rate for this model — caller must not invent a number.
    """
    if not model or model not in _RATES:
        return None
    in_rate, out_rate = _RATES[model]
    read_mult = _CACHE_READ_MULT.get(model, _CACHE_READ_MULT_DEFAULT)
    cost = (
        fresh_input_tokens / 1_000_000 * in_rate
        + output_tokens / 1_000_000 * out_rate
        + cache_read_tokens / 1_000_000 * in_rate * read_mult
        + cache_write_5m_tokens / 1_000_000 * in_rate * _CACHE_WRITE_5M_MULT
        + cache_write_1h_tokens / 1_000_000 * in_rate * _CACHE_WRITE_1H_MULT
    )
    return round(cost, 6)


# Our ids -> OpenRouter slugs, for the drift check below.
_OPENROUTER_SLUG = {
    "claude-opus-5-5": "anthropic/claude-opus-5.5", "claude-sonnet-5-5": "anthropic/claude-sonnet-5.5",
    "claude-fable-5-1": "anthropic/claude-fable-5.1", "claude-haiku-4-5-20251001": "anthropic/claude-haiku-4.5",
    "claude-opus-4-6": "anthropic/claude-opus-4.6", "gpt-6-sol": "openai/gpt-6-sol",
    "gpt-6-luna": "openai/gpt-6-luna", "gpt-6-astra": "openai/gpt-6-astra", "gpt-5.6-terra": "openai/gpt-5.6-terra",
}


def check_against_openrouter(models: list[str] | None = None, timeout: float = 20.0) -> dict:
    """Compare _RATES (and the cache-read multiplier) with OpenRouter's live price list.
    Returns {"checked": n, "mismatches": [str], "error": str|None}; never raises."""
    import json
    import urllib.request
    try:
        data = json.load(urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=timeout))["data"]
    except Exception as e:  # network down: report, don't block the run
        return {"checked": 0, "mismatches": [], "error": f"{type(e).__name__}: {e}"}
    live = {m["id"]: m["pricing"] for m in data}
    bad, n = [], 0
    for model in models or list(_OPENROUTER_SLUG):
        pr = live.get(_OPENROUTER_SLUG.get(model, ""))
        if not pr or model not in _RATES:
            bad.append(f"{model}: not found on OpenRouter" if model in _RATES else f"{model}: no local rate")
            continue
        n += 1
        in_rate, out_rate = _RATES[model]
        want = {"input": in_rate, "output": out_rate,
                "cache_read": in_rate * _CACHE_READ_MULT.get(model, _CACHE_READ_MULT_DEFAULT)}
        got = {"input": float(pr["prompt"]) * 1e6, "output": float(pr["completion"]) * 1e6,
               "cache_read": float(pr.get("input_cache_read") or 0) * 1e6}
        for k in want:
            if abs(want[k] - got[k]) > 1e-6 * max(1.0, want[k]):
                bad.append(f"{model} {k}: table ${want[k]:g}/M vs OpenRouter ${got[k]:g}/M")
    return {"checked": n, "mismatches": bad, "error": None}
