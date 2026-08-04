"""Shared cache-aware token/cost meter for the three benchmark arms.

Every arm reports the SAME fields so rows are directly comparable:
  calls · peak_in · in_total · in_cached · in_fresh · out_total · cost_usd

cost_usd is computed from the per-tier provider price sheet at the arm's real hit/miss split —
NEVER from a blended average. On the ContextBench run the blended-vs-tiered distinction was worth
more than 2x: an 8.3x cumulative-token gap priced out at only 3.70x because 94-97% of both arms'
input was cache-read at 1/50th the miss price. A meter that cannot see the cache split reports
token ratios that read as cost ratios and aren't.

Prices are per-token USD, verified 2026-08-04 against api-docs.deepseek.com (cross-checked with
deepseek.ai/pricing). Peak-hour pricing (2x, announced for 9:00-12:00/14:00-18:00 UTC+8) scales
all tiers equally, so cross-arm RATIOS are time-invariant even when absolute dollars are not.
"""
from __future__ import annotations

PRICES = {
    # model id -> per-token USD
    "deepseek-v4-flash": {"hit": 0.0028e-6, "miss": 0.14e-6, "out": 0.28e-6},
    "deepseek-v4-pro": {"hit": 0.003625e-6, "miss": 0.435e-6, "out": 0.87e-6},
}


def _price(model: str) -> dict:
    m = (model or "").split("/")[-1]          # "deepseek/v4-flash" (kimi alias) -> "v4-flash"
    for key, p in PRICES.items():
        if key == m or key.endswith(m) or m.endswith(key):
            return p
    raise KeyError(f"no price sheet for model {model!r} — add it to benchmarks/meter.py PRICES "
                   "rather than letting cost silently report 0")


def enrich(u: dict, model: str) -> dict:
    """Add in_fresh and cost_usd to a usage dict carrying in_total/in_cached/out_total."""
    p = _price(model)
    fresh = max(int(u.get("in_total", 0)) - int(u.get("in_cached", 0)), 0)
    cost = (int(u.get("in_cached", 0)) * p["hit"] + fresh * p["miss"]
            + int(u.get("out_total", 0)) * p["out"])
    out = dict(u)
    out["in_fresh"] = fresh
    out["cost_usd"] = round(cost, 6)
    return out


def summarize(turns: list[dict], model: str) -> dict:
    """Whole-run totals from per-turn usage dicts (each already carrying the base fields)."""
    tot = {
        "calls": sum(int(t.get("calls", 0)) for t in turns),
        "peak_in": max((int(t.get("peak_in", 0)) for t in turns), default=0),
        "in_total": sum(int(t.get("in_total", 0)) for t in turns),
        "in_cached": sum(int(t.get("in_cached", 0)) for t in turns),
        "out_total": sum(int(t.get("out_total", 0)) for t in turns),
    }
    return enrich(tot, model)
