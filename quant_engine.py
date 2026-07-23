"""
quant_engine.py — Deterministic scoring. No LLM involved.

Composite scoring is HONEST about missing data. Each sub-score carries a
`*_available` flag; score_all_tickers weights only the factors that have real
data and renormalizes. When fundamentals are absent (the live case today — the
free-tier Polygon financials endpoint returns nothing, so quality/valuation
have no inputs), the composite reflects momentum + volatility alone rather than
silently blending in a constant 50 for the two missing factors and advertising
a "4-factor" score that is really 2-factor. See README/CLAUDE.md.
"""

import math

# Base factor weights. They sum to 1.0 when every factor has real data; when a
# factor is unavailable it is dropped and the remaining weights are renormalized
# (so the composite is always a weighted average over real factors only).
#
# Phase 2 re-weight (formula 2.0): tilted toward quality + valuation + low-vol,
# with momentum DEMOTED to a minor confirm. Rationale (IPS §horizon = 9–12 months):
# momentum is a short-horizon, high-turnover signal that is tax-suicidal in a CA
# top-bracket account; quality (margins, low leverage), valuation, and low-vol are
# the persistent, lower-turnover factors appropriate to a multi-quarter hold. This
# is a DETERMINISTIC change — its edge is proven or falsified in backtest/, not on
# faith. The change is gated on the §8 fundamental-coverage fix: quality/valuation
# are only real once SEC EDGAR coverage clears the 80% floor.
#
# ⚠ VALUATION IS INACTIVE IN PRODUCTION (as of Jul 22 2026). PE / FCF-yield /
# EV-EBITDA require FMP_API_KEY, which is not set, so `valuation_available` is
# False for the ENTIRE universe → the 0.25 valuation weight is renormalized out
# on every name. The OPERATIVE live formula is therefore 3-factor — momentum /
# quality / volatility, renormalized to effective ~0.20 / 0.467 / 0.333. The
# weights below are deliberately LEFT UNCHANGED (not collapsed to a 3-factor
# table): (1) the renormalization already makes the composite honest — valuation
# is dropped, never blended as a fake 50; (2) editing the weights would silently
# change the live composite that selects candidates AND would force a
# FORMULA_VERSION bump, resetting the factor-persistence / IC evidence clock
# (P0-2) mid-accumulation for zero signal benefit. Activating the 4th factor is a
# CONFIG decision (provision FMP_API_KEY), not a code change — and would then need
# its own version bump + fresh backtest. Until then, read this table as "quality-
# tilted 3-factor, valuation reserved." See _fmt_scores / the PM quant menu, which
# now render valuation as N/A (not 50) so no agent misreads the gap as a real call.
FACTOR_WEIGHTS = {
    "momentum":   0.15,
    "quality":    0.35,
    "valuation":  0.25,   # INACTIVE without FMP_API_KEY — renormalized out (see above)
    "volatility": 0.25,
}

# Stamped on every composite score and every factor_history row. It is the KEY a
# future factor-IC / persistence analyzer MUST group by — mixing pre- and
# post-reweight composites corrupts the signal (P0-2). NOTE: this is a provenance
# label, not an enforced invariant; nothing computes factor IC across the boundary
# *yet* (the harness scores agent forecasts, not factor_history), so the guarantee
# is "the data is grouped-by-able", and the eventual analyzer must honor it. Bump
# this string whenever FACTOR_WEIGHTS or any sub-score formula changes.
FORMULA_VERSION = "2.0-quality-tilt"


def _mean(values: list) -> float:
    return sum(values) / len(values)


def _variance(values: list) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    m = _mean(values)
    return sum((x - m) ** 2 for x in values) / (n - 1)


def _stdev(values: list) -> float:
    return math.sqrt(_variance(values))


def _pct_return(closes: list[float], n: int) -> float | None:
    if len(closes) < n + 1:
        return None
    base = closes[-(n + 1)]
    return ((closes[-1] - base) / base) * 100 if base else None


def compute_momentum_score(history: list[dict]) -> dict:
    """Score 0-100. Higher = stronger momentum."""
    if not history:
        return {
            "momentum_score": 50, "momentum_available": False,
            "return_1m": None, "return_3m": None, "return_6m": None,
            "above_50dma": None, "above_200dma": None,
        }

    closes = [float(d["close"]) for d in history]
    current = closes[-1]

    r1m = _pct_return(closes, 21)
    r3m = _pct_return(closes, 63)
    r6m = _pct_return(closes, 126)

    dma50  = _mean(closes[-50:])  if len(closes) >= 50  else None
    dma200 = _mean(closes[-200:]) if len(closes) >= 200 else None
    above_50  = bool(current > dma50)  if dma50  else None
    above_200 = bool(current > dma200) if dma200 else None

    score = 50.0
    if r1m is not None: score += max(-15.0, min(15.0, r1m * 1.5))
    if r3m is not None: score += max(-12.0, min(12.0, r3m * 0.8))
    if r6m is not None: score += max(-8.0,  min(8.0,  r6m * 0.4))
    if above_50  is True:  score += 5
    elif above_50  is False: score -= 5
    if above_200 is True:  score += 10
    elif above_200 is False: score -= 10

    return {
        "momentum_score": round(max(0.0, min(100.0, score)), 1),
        "momentum_available": True,
        "return_1m":  round(r1m, 2) if r1m is not None else None,
        "return_3m":  round(r3m, 2) if r3m is not None else None,
        "return_6m":  round(r6m, 2) if r6m is not None else None,
        "above_50dma":  above_50,
        "above_200dma": above_200,
    }


def compute_quality_score(fundamentals: dict | None) -> dict:
    """Score 0-100. Higher = better quality."""
    if not fundamentals:
        return {"quality_score": 50, "quality_available": False}

    scores = []

    gm = fundamentals.get("gross_margin")
    if gm is not None:
        scores.append(90 if gm > 0.60 else 70 if gm > 0.40 else 50 if gm > 0.20 else 25)

    om = fundamentals.get("operating_margin")
    if om is not None:
        scores.append(90 if om > 0.25 else 70 if om > 0.15 else 50 if om > 0.05 else 30 if om > 0 else 10)

    fm = fundamentals.get("fcf_margin")
    if fm is not None:
        scores.append(90 if fm > 0.20 else 70 if fm > 0.10 else 45 if fm > 0 else 15)

    de = fundamentals.get("debt_to_equity")
    if de is not None:
        scores.append(90 if de < 0.5 else 70 if de < 1.0 else 50 if de < 2.0 else 25)

    return {
        "quality_score": round(_mean(scores), 1) if scores else 50,
        "quality_available": bool(scores),
    }


def compute_valuation_score(fundamentals: dict | None) -> dict:
    """Score 0-100. Higher = better value (cheaper relative to fundamentals)."""
    if not fundamentals:
        return {"valuation_score": 50, "valuation_available": False}

    scores = []

    pe = fundamentals.get("pe_ratio")
    if pe is not None and pe > 0:
        scores.append(90 if pe < 15 else 70 if pe < 25 else 50 if pe < 35 else 30 if pe < 50 else 10)

    fy = fundamentals.get("fcf_yield")
    if fy is not None:
        scores.append(90 if fy > 0.06 else 70 if fy > 0.03 else 50 if fy > 0.01 else 30 if fy > 0 else 10)

    ev_ebitda = fundamentals.get("ev_ebitda")
    if ev_ebitda is not None and ev_ebitda > 0:
        scores.append(90 if ev_ebitda < 10 else 70 if ev_ebitda < 15 else 50 if ev_ebitda < 25 else 30 if ev_ebitda < 40 else 10)

    return {
        "valuation_score": round(_mean(scores), 1) if scores else 50,
        "valuation_available": bool(scores),
    }


def compute_risk_metrics(history: list[dict], spy_history: list[dict]) -> dict:
    """Returns annualized volatility, beta vs SPY, and a risk score (higher = lower risk)."""
    if len(history) < 22:
        return {"volatility": None, "beta": None,
                "volatility_score": 50, "volatility_available": False}

    closes = [float(d["close"]) for d in history]
    # A non-finite or non-positive close (a NaN/None/0 that slipped into the
    # snapshot — e.g. a Polygon gap for TXN/TJX/CAT) propagates through the return
    # series into a NaN annualized volatility. That NaN then poisons JSON
    # serialization downstream (Supabase rejects it: "Out of range float values
    # are not JSON compliant"). Treat a degenerate price series as "volatility
    # unavailable" so it is dropped from the honest composite rather than blended
    # in or emitted as NaN.
    if any((not math.isfinite(c)) or c <= 0 for c in closes):
        return {"volatility": None, "beta": None,
                "volatility_score": 50, "volatility_available": False}

    daily_ret = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
    recent = daily_ret[-63:]  # 3-month window

    vol = _stdev(recent) * math.sqrt(252) * 100  # annualized %
    if not math.isfinite(vol):  # belt-and-suspenders: never let a NaN vol escape
        return {"volatility": None, "beta": None,
                "volatility_score": 50, "volatility_available": False}

    beta = None
    if spy_history and len(spy_history) >= 22:
        spy_closes = [float(d["close"]) for d in spy_history]
        spy_ret = [(spy_closes[i] - spy_closes[i - 1]) / spy_closes[i - 1] for i in range(1, len(spy_closes))]
        n = min(len(recent), len(spy_ret))
        sr, mr = recent[-n:], spy_ret[-n:]
        if n > 2:
            mean_s, mean_m = _mean(sr), _mean(mr)
            cov = sum((s - mean_s) * (m - mean_m) for s, m in zip(sr, mr)) / (n - 1)
            spy_var = _variance(mr)
            beta = round(cov / spy_var, 2) if spy_var > 0 else None

    # Normalize 15%–80% annualized vol range to 100→0 score
    vol_score = max(0.0, min(100.0, 100.0 - (vol - 15.0) * (100.0 / 65.0)))

    return {
        "volatility": round(vol, 1),
        "beta": beta,
        "volatility_score": round(vol_score, 1),
        "volatility_available": True,
    }


def _pearson(a: list[float], b: list[float]) -> float | None:
    """Pearson correlation of two equal-length return series; None if degenerate."""
    n = len(a)
    if n < 2:
        return None
    ma, mb = _mean(a), _mean(b)
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (n - 1)
    sa, sb = _stdev(a), _stdev(b)
    if sa == 0 or sb == 0:
        return None
    return cov / (sa * sb)


def _daily_returns(history: list[dict], window: int) -> list[float]:
    closes = [float(b["close"]) for b in history][-(window + 1):]
    return [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes)) if closes[i - 1]]


def compute_return_correlations(
    history_map: dict,
    tickers: list[str],
    window: int = 120,
    top_n: int = 8,
    min_overlap: int = 22,
) -> list[tuple[str, str, float]]:
    """Top pairwise daily-return correlations among `tickers`.

    Returns [(t1, t2, corr), ...] sorted by |corr| descending, length ≤ top_n.
    Gives the CRO REAL correlation data instead of a fabricated judgment — the
    prompt asks it to assess "correlation risk" but it was previously fed only
    per-ticker weight/vol/beta. Pairs with fewer than `min_overlap` overlapping
    daily returns are skipped (insufficient data → no fake number).
    """
    rets: dict[str, list[float]] = {}
    for t in tickers:
        series = _daily_returns(history_map.get(t) or [], window)
        if len(series) >= min_overlap:
            rets[t] = series

    pairs: list[tuple[str, str, float]] = []
    names = sorted(rets)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = rets[names[i]], rets[names[j]]
            n = min(len(a), len(b))
            if n < min_overlap:
                continue
            c = _pearson(a[-n:], b[-n:])
            if c is not None:
                pairs.append((names[i], names[j], round(c, 2)))

    pairs.sort(key=lambda p: -abs(p[2]))
    return pairs[:top_n]


def score_all_tickers(market_data: dict) -> dict:
    """Returns {ticker: score_dict} for all tickers that have price history."""
    spy_history = market_data.get("history", {}).get("SPY", [])
    scores = {}

    for ticker, history in market_data.get("history", {}).items():
        fundamentals = market_data.get("fundamentals", {}).get(ticker)
        momentum  = compute_momentum_score(history)
        quality   = compute_quality_score(fundamentals)
        valuation = compute_valuation_score(fundamentals)
        risk      = compute_risk_metrics(history, spy_history)

        # Weight only the factors that have real data, then renormalize. A
        # missing factor (e.g. quality/valuation when fundamentals are absent)
        # is dropped entirely rather than blended in as a constant 50 — the
        # composite stays an honest weighted average over the real factors.
        factor_values = {
            "momentum":   (momentum["momentum_score"],   momentum["momentum_available"]),
            "quality":    (quality["quality_score"],     quality["quality_available"]),
            "valuation":  (valuation["valuation_score"], valuation["valuation_available"]),
            "volatility": (risk["volatility_score"],     risk["volatility_available"]),
        }
        factors_used = [f for f, (_, avail) in factor_values.items() if avail]
        weight_sum   = sum(FACTOR_WEIGHTS[f] for f in factors_used)
        if weight_sum > 0:
            composite = sum(
                FACTOR_WEIGHTS[f] * factor_values[f][0] for f in factors_used
            ) / weight_sum
        else:
            composite = 50.0  # no real factor — fully neutral, flagged below

        scores[ticker] = {
            "ticker": ticker,
            "data_available": len(history) > 0,
            "composite_score": round(composite, 1),
            "factors_used": factors_used,
            "formula_version": FORMULA_VERSION,
            **momentum,
            **quality,
            **valuation,
            **risk,
        }

    return scores


# Sub-score fields worth persisting per ticker/day (the raw factor inputs to IC).
_FACTOR_HISTORY_FIELDS = (
    "composite_score", "factors_used", "formula_version",
    "momentum_score", "momentum_available",
    "quality_score", "quality_available",
    "valuation_score", "valuation_available",
    "volatility_score", "volatility_available",
    "beta",
)


def log_factor_history(scores: dict, as_of: str, path: str = "factor_history.jsonl") -> int:
    """Append one factor row per scored ticker to an append-only JSONL time series.

    Written by the GH Actions scoring step (full-universe, point-in-time) — this
    is the substrate for factor-persistence / IC analysis. Every row carries
    `formula_version` so downstream IC is computed WITHIN a weighting regime, never
    across a boundary (P0-2). Idempotent per (ticker, formula_version) WITHIN today's
    date: a re-run for the same day+formula does not duplicate rows. Returns rows
    appended.

    Rows accumulate append-only (repo convention, like calibration's ledgers); a
    plain line append is the atomic-enough idiom used for every ledger here — the
    prior temp-file copy added I/O and a stranded-temp risk without any real atomicity.
    The dedup set is bounded to TODAY's rows (older dates can never collide with today),
    so memory stays O(universe) rather than O(whole file). File compaction/rotation is
    Phase 4 (§12 storage split).
    """
    import json as _json
    import os as _os

    # Only today's (ticker, formula_version) keys can collide with today's append.
    today_keys: set = set()
    if _os.path.isfile(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if r.get("date") == as_of:
                    today_keys.add((r.get("ticker"), r.get("formula_version")))

    rows_out = []
    for ticker, s in sorted(scores.items()):
        fv = s.get("formula_version", FORMULA_VERSION)
        if (ticker, fv) in today_keys:
            continue
        row = {"date": as_of, "ticker": ticker}
        for field in _FACTOR_HISTORY_FIELDS:
            if field in s:
                row[field] = s[field]
        rows_out.append(row)

    if rows_out:
        with open(path, "a") as f:
            for row in rows_out:
                f.write(_json.dumps(row) + "\n")
    return len(rows_out)
