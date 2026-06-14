"""
calibration.py — observational forecast ledger (#2).

Every agent already emits structured numeric forecasts; the system just never
scored them. This module closes that loop WITHOUT touching the trade path:

  log_forecasts()   appends one line per (agent, ticker, field) to forecasts.jsonl
                    every run — pure logging, no decision is affected.
  score_matured()   joins forecasts whose horizon has elapsed to the realized
                    forward return (from price history) → forecasts_scored.jsonl.
  agent_scorecard() reports per-agent skill (rank-IC, sign-hit-rate) with
                    SHRINKAGE toward a no-skill prior, so a handful of forecasts
                    can't masquerade as signal.

Scoring the FULL candidate universe (not just executed trades) is deliberate: it
accrues hundreds of (forecast, outcome) pairs per month, beating the small-sample
problem long before the trade count would. Nothing here weights or sizes a trade
— that stays gated behind a sample threshold (future work). This is the data
clock you want started.
"""

import json
import math
import os
from datetime import datetime, timedelta, timezone

LEDGER    = "forecasts.jsonl"
SCORED    = "forecasts_scored.jsonl"
SCORECARD = "agent_scorecards.json"

DEFAULT_HORIZON = 21   # ~1 trading month

# agent -> (pipeline_state key, field, orientation). orientation = +1 when a
# HIGHER value should predict a HIGHER forward return, -1 when higher predicts
# lower (a risk score). Used to orient the sign-hit-rate.
_FORECASTS = {
    "quant":           ("quant_scores",     "composite_score",   +1),
    "research":        ("research",          "confidence",        +1),
    "earnings":        ("earnings",          "earnings_alpha_score", +1),
    "devils_advocate": ("devils_advocate",   "overall_risk_score", -1),
    "position_review": ("position_reviews",  "hold_score",        +1),
}


def _append_jsonl(path: str, rows: list) -> None:
    with open(path, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _iter_jsonl(path: str):
    if not os.path.isfile(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def log_forecasts(run_id: str, date_str: str, pipeline_state: dict,
                  candidates: list, prices: dict,
                  horizon_days: int = DEFAULT_HORIZON, path: str = LEDGER) -> int:
    """Append this run's per-(agent, ticker) numeric forecasts. Returns the count.

    Logging only — never raises into the caller's critical path (wrap the call).
    """
    rows = []
    for ticker in candidates:
        entry = (prices.get(ticker) or {}).get("close")
        if not entry:
            continue
        for agent, (key, field, _sign) in _FORECASTS.items():
            v = (pipeline_state.get(key, {}).get(ticker) or {}).get(field)
            if isinstance(v, (int, float)):
                rows.append({
                    "run_id": run_id, "date": date_str, "agent": agent, "field": field,
                    "ticker": ticker, "value": float(v),
                    "entry_price": float(entry), "horizon_days": horizon_days,
                })
    if rows:
        _append_jsonl(path, rows)
    return len(rows)


def _history_to_series(history: dict) -> dict:
    """{ticker: [bars]} → {ticker: sorted [(iso_date, close)]}. Bars carry an
    epoch-ms or ISO `date`."""
    out: dict[str, list] = {}
    for t, bars in (history or {}).items():
        s = []
        for b in bars:
            raw, close = b.get("date"), b.get("close")
            if raw is None or close is None:
                continue
            iso = (datetime.fromtimestamp(raw / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                   if isinstance(raw, (int, float)) else str(raw)[:10])
            s.append((iso, float(close)))
        out[t] = sorted(s)
    return out


def _price_on_or_after(series: list, target_iso: str):
    for d, c in series:
        if d >= target_iso:
            return c
    return None


def score_matured(snapshot: dict, ledger_path: str = LEDGER,
                  scored_path: str = SCORED) -> int:
    """Score forecasts whose horizon has elapsed against realized forward return.

    Idempotent: a (run_id, agent, field, ticker) already in scored_path is skipped,
    so re-running never double-counts. Returns the number newly scored.
    """
    series = _history_to_series(snapshot.get("history", {}))
    scored_keys = {(r.get("run_id"), r.get("agent"), r.get("field"), r.get("ticker"))
                   for r in _iter_jsonl(scored_path)}

    out = []
    for fc in _iter_jsonl(ledger_path):
        key = (fc.get("run_id"), fc.get("agent"), fc.get("field"), fc.get("ticker"))
        if key in scored_keys:
            continue
        s = series.get(fc.get("ticker"))
        entry = fc.get("entry_price")
        if not s or not entry:
            continue
        try:
            target = (datetime.fromisoformat(fc["date"]).date()
                      + timedelta(days=int(fc.get("horizon_days", DEFAULT_HORIZON)))).isoformat()
        except (ValueError, TypeError, KeyError):
            continue
        future_px = _price_on_or_after(s, target)
        if future_px:
            out.append({**fc, "future_price": future_px,
                        "realized_return": round((future_px - entry) / entry, 5)})
            scored_keys.add(key)
    if out:
        _append_jsonl(scored_path, out)
    return len(out)


def _spearman(xs: list, ys: list):
    """Rank correlation; None if degenerate or n < 3."""
    n = len(xs)
    if n < 3:
        return None

    def ranks(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        for rank, i in enumerate(order):
            r[i] = rank
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    sx = math.sqrt(sum((rx[i] - mx) ** 2 for i in range(n)))
    sy = math.sqrt(sum((ry[i] - my) ** 2 for i in range(n)))
    if sx == 0 or sy == 0:
        return None
    return cov / (sx * sy)


def agent_scorecard(scored_path: str = SCORED, out_path: str = SCORECARD,
                    shrink_k: int = 50) -> dict:
    """Per (agent.field): n, rank-IC, sign-hit-rate, SHRUNK IC, 95% CI half-width.

    `ic_shrunk = ic * n/(n+shrink_k)` pulls small samples toward 0 (no skill), so a
    lucky handful of forecasts doesn't read as signal. Nothing consumes this to
    size trades yet — it is a scoreboard, gated behind sample size (future work).
    """
    groups: dict = {}
    for r in _iter_jsonl(scored_path):
        groups.setdefault((r.get("agent"), r.get("field")), []).append(r)

    orient = {agent: sign for agent, (_k, _f, sign) in _FORECASTS.items()}
    card: dict = {}
    for (agent, field), rows in groups.items():
        vals = [x["value"] for x in rows if isinstance(x.get("value"), (int, float))]
        rets = [x["realized_return"] for x in rows if isinstance(x.get("realized_return"), (int, float))]
        n = min(len(vals), len(rets))
        vals, rets = vals[:n], rets[:n]
        if n == 0:
            continue
        ic   = _spearman(vals, rets)
        mean = sum(vals) / n
        sgn  = orient.get(agent, +1)
        hits = sum(1 for v, rr in zip(vals, rets) if (sgn * (v - mean)) * rr > 0)
        card[f"{agent}.{field}"] = {
            "n": n,
            "ic":           round(ic, 3) if ic is not None else None,
            "ic_shrunk":    round(ic * n / (n + shrink_k), 3) if ic is not None else None,
            "hit_rate":     round(hits / n, 3),
            "ci_halfwidth": round(1.96 / math.sqrt(n), 3),
            "orientation":  sgn,
        }
    with open(out_path, "w") as f:
        json.dump(card, f, indent=2)
    return card
