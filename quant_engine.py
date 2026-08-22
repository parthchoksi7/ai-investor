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
# VALUATION COVERAGE (PLAN_SEC_VALUATION Phase 3, shipped Jul 24 2026 — supersedes
# the Phase 2 note about a mixed TTM/annual basis). `data_providers.CascadeProvider`
# now strips FMP's own TTM valuation fields; `market_data.derive_valuation_ratios`
# is the SINGLE valuation source for the full universe, computed from SEC EDGAR's
# price-independent components (Phase 1) on one consistent annual (10-K) basis —
# no more mixed-basis wart for the subset FMP used to cover. FMP is now used only
# for quality (when covered, TTM, more current) + the earnings calendar. Valuation
# coverage climbs toward the SEC quality-coverage level (~90%+) as the alternate-
# day cache rotates. `valuation_available` is still genuinely PER-TICKER (a name
# with no EDGAR filing, thin XBRL tagging, a vintage-mismatch guard, or a not-yet-
# refreshed cache entry stays N/A) — the composite renormalizes it out where absent
# and blends the 0.25 weight where present, same honest-composite mechanism as
# before. The weights below are UNCHANGED (plan §4.3): only the coverage AND the
# basis consistency of the existing valuation slot changed, not the bet size.
# _fmt_scores / the PM quant menu render valuation as N/A (never a fake 50) when
# it is absent.
FACTOR_WEIGHTS = {
    "momentum":   0.15,
    "quality":    0.35,
    "valuation":  0.25,   # FMP: real for ~mega-caps, renormalized out per-ticker elsewhere (see above)
    "volatility": 0.25,
}

# Stamped on every composite score and every factor_history row. It is the KEY a
# future factor-IC / persistence analyzer MUST group by — mixing pre- and
# post-reweight composites corrupts the signal (P0-2). NOTE: this is a provenance
# label, not an enforced invariant; nothing computes factor IC across the boundary
# *yet* (the harness scores agent forecasts, not factor_history), so the guarantee
# is "the data is grouped-by-able", and the eventual analyzer must honor it. Bump
# this string whenever FACTOR_WEIGHTS or any sub-score formula changes.
#
# 2.1-valuation-live (PLAN_SEC_VALUATION Phase 2, Jul 24 2026): valuation coverage
# jumped from ~18% (FMP mega-caps) toward the SEC quality-coverage level via
# market_data.derive_valuation_ratios — a real signal change (many more names now
# carry a non-N/A valuation_score), so the evidence clock resets as expected (§8),
# unlike a bump for zero benefit. FACTOR_WEIGHTS themselves are unchanged.
#
# 2.2-valuation-sec-only (PLAN_SEC_VALUATION Phase 3, Jul 24 2026): SEC-derived
# valuation is now the SINGLE source (§10.1 Option B) — for the subset of tickers
# FMP used to cover (TTM basis), pe_ratio/fcf_yield/ev_ebitda now come from SEC
# EDGAR's annual 10-K basis instead. Same class of real signal change as 2.1 (the
# REALIZED valuation numbers, not just their coverage, changed for those names), so
# the evidence clock resets again — bumped rather than silently reusing 2.1 across
# a basis change. FACTOR_WEIGHTS unchanged; `compute_valuation_score`'s formula
# unchanged.
#
# 2.3-valuation-ttm (PLAN_SEC_VALUATION Phase 4, Jul 24 2026): flow valuation
# components (EPS, CFO, capex, D&A, operating income for EBITDA) move from
# latest-ANNUAL to trailing-twelve-months (sum of the 4 most recent contiguous
# fiscal quarters, deriving the one quarter XBRL never discloses standalone —
# see data_providers.SECProvider._ttm_ex); balance components (shares, debt,
# cash) move from latest-10-K-only to latest-available-any-form (may now come
# from a 10-Q). Live-verified: TTM P/E differs materially from the prior
# annual-basis figure for the same names (e.g. GOOGL 29.39 -> 15.96 — crosses
# a valuation-score bucket boundary), so this is a real signal change, not a
# cosmetic one — evidence clock resets again. FACTOR_WEIGHTS and
# `compute_valuation_score`'s formula are unchanged.
FORMULA_VERSION = "3.0-beta-neutral"


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


# ── Beta estimation windows ──────────────────────────────────────────────────
# `beta` (63 sessions, unshrunk) is the LEGACY estimate. It is kept byte-identical
# because it is stamped into factor_history.jsonl and rendered into the CRO's risk
# block, so changing it in place would silently break the comparability of both.
#
# It is also far too short to be a beta. Over the live 100-name universe the 63-day
# estimate ranges −1.01 to +5.01 — values that are estimation error, not market
# sensitivity. Screening on it is measurably destructive: in the backtest harness a
# 0.5–0.95 band applied to the RAW 63-day estimate returned −7.93% after-tax alpha
# at 5.27x turnover, versus +1.41% for the identical band applied to the shrunk
# long-window estimate.
#
# `beta_stable` is the estimate any beta-targeting logic should consume: a
# 252-session window with Blume shrinkage toward the market. Shrinkage is not a
# nicety — the cross-sectional dispersion of SAMPLE betas is systematically wider
# than the dispersion of TRUE betas, so a raw estimate overstates how far a name
# sits from 1.0, and a band drawn on raw estimates chases that error every rebalance.
BETA_STABLE_WINDOW   = 252   # sessions of returns to use when enough history exists
BETA_STABLE_MIN_BARS = 120   # below this the long-window estimate is not offered
BETA_SHORT_MIN_BARS  = 22    # fallback floor: shrink the short-window estimate instead
BLUME_SLOPE          = 0.67  # β_adj = 0.67·β_raw + 0.33·1.0 (Blume 1971/1975)
BLUME_INTERCEPT      = 0.33


def _ols_beta(asset_ret: list[float], mkt_ret: list[float]) -> float | None:
    """Cov/Var beta over the overlapping tail of two return series.

    None when the sample is too short, the market series has no variance, or the
    result is non-finite — the same fail-quiet contract compute_risk_metrics uses
    for a degenerate price series.
    """
    n = min(len(asset_ret), len(mkt_ret))
    if n < 3:
        return None
    a, m = asset_ret[-n:], mkt_ret[-n:]
    mean_a, mean_m = _mean(a), _mean(m)
    cov = sum((x - mean_a) * (y - mean_m) for x, y in zip(a, m)) / (n - 1)
    var = _variance(m)
    if var <= 0:
        return None
    beta = cov / var
    return beta if math.isfinite(beta) else None


def shrink_beta(raw: float | None) -> float | None:
    """Blume shrinkage of a sample beta toward the market beta of 1.0."""
    if raw is None or not math.isfinite(raw):
        return None
    return BLUME_SLOPE * raw + BLUME_INTERCEPT


def _degenerate_closes(closes: list[float]) -> bool:
    """A close series unusable for returns: non-finite or non-positive anywhere.

    A 0.0 close raises ZeroDivisionError out of the return computation; a NaN
    silently poisons every downstream statistic and, once, the Supabase publish
    itself. Screen the series, not just the result.
    """
    return any((not math.isfinite(c)) or c <= 0 for c in closes)


def _date_joined_returns(history: list[dict],
                         spy_history: list[dict]) -> tuple[list[float], list[float]]:
    """Daily returns for asset and market over their COMMON bar dates.

    Index-tail alignment (the house convention the legacy 63-day beta uses) silently
    regresses mismatched pairs whenever a ticker is missing bars inside the window —
    a halt, a late listing, a provider gap — biasing beta toward the shrink target
    while still reporting a full-length window. `beta_stable` is the estimate meant
    to drive sizing, so it joins on the bar date instead.

    Returns two equal-length, date-ordered series (empty when the overlap is too thin).
    """
    a_by_date = {b["date"]: float(b["close"]) for b in history if "date" in b}
    m_by_date = {b["date"]: float(b["close"]) for b in spy_history if "date" in b}
    common = sorted(set(a_by_date) & set(m_by_date))
    if len(common) < 3:
        return [], []
    a = [a_by_date[d] for d in common]
    m = [m_by_date[d] for d in common]
    if _degenerate_closes(a) or _degenerate_closes(m):
        return [], []
    a_ret = [(a[i] - a[i - 1]) / a[i - 1] for i in range(1, len(a))]
    m_ret = [(m[i] - m[i - 1]) / m[i - 1] for i in range(1, len(m))]
    return a_ret, m_ret


def _risk_metrics_unavailable() -> dict:
    """The 'no usable risk metrics' shape, shared by every early return below so
    the key set never drifts between the available and unavailable paths."""
    return {"volatility": None, "beta": None,
            "volatility_score": 50, "volatility_available": False,
            "beta_stable": None, "beta_stable_raw": None,
            "beta_stable_window": 0, "beta_stable_available": False,
            "beta_stable_basis": None}


def compute_risk_metrics(history: list[dict], spy_history: list[dict]) -> dict:
    """Returns annualized volatility, beta vs SPY, and a risk score (higher = lower risk).

    Also returns `beta_stable` — the long-window, Blume-shrunk beta. It is ADDITIVE:
    no caller reads it yet, `composite_score` does not consume it, and
    FORMULA_VERSION is deliberately unchanged.
    """
    if len(history) < 22:
        return _risk_metrics_unavailable()

    closes = [float(d["close"]) for d in history]
    # A non-finite or non-positive close (a NaN/None/0 that slipped into the
    # snapshot — e.g. a Polygon gap for TXN/TJX/CAT) propagates through the return
    # series into a NaN annualized volatility. That NaN then poisons JSON
    # serialization downstream (Supabase rejects it: "Out of range float values
    # are not JSON compliant"). Treat a degenerate price series as "volatility
    # unavailable" so it is dropped from the honest composite rather than blended
    # in or emitted as NaN.
    if _degenerate_closes(closes):
        return _risk_metrics_unavailable()

    daily_ret = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
    recent = daily_ret[-63:]  # 3-month window

    vol = _stdev(recent) * math.sqrt(252) * 100  # annualized %
    if not math.isfinite(vol):  # belt-and-suspenders: never let a NaN vol escape
        return _risk_metrics_unavailable()

    beta = None
    beta_stable = beta_stable_raw = beta_stable_basis = None
    beta_stable_window = 0
    spy_closes = [float(d["close"]) for d in spy_history] if spy_history else []
    # SPY gets the SAME screen the asset series gets above. Without it a 0.0 SPY
    # close raises ZeroDivisionError straight out of score_all_tickers — the one
    # degenerate case that does NOT already degrade to None (a NaN does, via the
    # spy_var > 0 test). Fail quiet, like every other degenerate-data path here.
    if len(spy_closes) >= 22 and not _degenerate_closes(spy_closes):
        spy_ret = [(spy_closes[i] - spy_closes[i - 1]) / spy_closes[i - 1] for i in range(1, len(spy_closes))]
        n = min(len(recent), len(spy_ret))
        sr, mr = recent[-n:], spy_ret[-n:]
        if n > 2:
            mean_s, mean_m = _mean(sr), _mean(mr)
            cov = sum((s - mean_s) * (m - mean_m) for s, m in zip(sr, mr)) / (n - 1)
            spy_var = _variance(mr)
            beta = round(cov / spy_var, 2) if spy_var > 0 else None

        # beta_stable — long window + Blume shrinkage, over DATE-JOINED returns (not
        # `recent`, which is truncated to 63 by design for the vol score, and not
        # index-tail-aligned, which mismatches pairs when bars are missing).
        a_ret, m_ret = _date_joined_returns(history, spy_history)
        overlap = len(a_ret)
        if overlap >= BETA_STABLE_MIN_BARS:
            window = min(BETA_STABLE_WINDOW, overlap)
            basis = "long"
        elif overlap >= BETA_SHORT_MIN_BARS:
            # Still computed — a young or thinly-covered ticker should degrade toward
            # the market rather than vanish from beta-aware sizing. But it is NOT
            # marked available: a 22–119 session estimate is shorter than the 63-day
            # beta this whole change exists to condemn, and a consumer gating on
            # `beta_stable_available` must never be handed one. Under
            # UNIVERSE_EXPANDED, expansion names ship at 63-bar tails and land here.
            window = overlap
            basis = "short"
        else:
            window = 0
            basis = None
        if window >= 3:
            beta_stable_raw = _ols_beta(a_ret[-window:], m_ret[-window:])
            beta_stable = shrink_beta(beta_stable_raw)
            if beta_stable is not None:
                beta_stable_window = window
                beta_stable_basis = basis

    # Normalize 15%–80% annualized vol range to 100→0 score
    vol_score = max(0.0, min(100.0, 100.0 - (vol - 15.0) * (100.0 / 65.0)))

    return {
        "volatility": round(vol, 1),
        "beta": beta,
        "volatility_score": round(vol_score, 1),
        "volatility_available": True,
        "beta_stable": round(beta_stable, 3) if beta_stable is not None else None,
        "beta_stable_raw": round(beta_stable_raw, 3) if beta_stable_raw is not None else None,
        "beta_stable_window": beta_stable_window,
        "beta_stable_basis": beta_stable_basis,
        "beta_stable_available": beta_stable is not None and beta_stable_basis == "long",
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


# ── Phase 2: cross-sectional beta neutralization ─────────────────────────────
# The composite was never meant to express a market-exposure view, but it did:
# measured 2026-08-22 on the live universe, composite_score correlated -0.653 with
# beta (Spearman -0.592, n=100), with mean beta running monotonically from +2.41 in
# the worst-rated quintile to -0.04 in the best. Half the factor weight drives it
# (volatility_score rho=-0.737, valuation_score rho=-0.471), and the live book
# inherited a -0.17 stocks-only beta nobody chose.
#
# The fix is to regress the composite on beta cross-sectionally each run and keep the
# RESIDUAL — the part of the score not explained by market sensitivity. This strips
# exactly the beta channel without re-deriving any sub-score formula, and leaves every
# factor's other information intact.
#
# Deliberately NOT a per-name beta screen. A 0.5-0.95 band on the raw 63-session beta
# was the worst arm measured in the backtest harness (-7.93% after-tax alpha, 5.27x
# turnover) because a hard cutoff amplifies estimation error at the boundary. A linear
# adjustment is far gentler: a noisy beta yields a noisy but unbiased correction, and
# the errors partially wash out cross-sectionally.
#
# HONEST SCOPE: this removes an unintentional bet. It does not add alpha. The
# composite's measured IC is -0.201 and insignificant; neutralizing a signal with no
# demonstrated edge yields a signal with no demonstrated edge — just without a hidden
# short-market position attached.
BETA_NEUTRALIZE = True

# Below this many usable (composite, beta) pairs the fit is not trustworthy and the
# run degrades to the raw composite rather than applying a noisy correction.
MIN_NEUTRALIZE_NAMES = 20

# Benchmarks are excluded from the fit: SPY is beta 1.0 by construction and would
# anchor the regression on a point that is not a candidate.
_NEUTRALIZE_EXCLUDE = ("SPY", "QQQ")


def _ols_fit(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    """Least-squares intercept/slope of ys on xs. None when degenerate."""
    n = len(xs)
    if n < 3:
        return None
    mx, my = _mean(xs), _mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:                      # no cross-sectional spread in beta
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    intercept = my - slope * mx
    if not (math.isfinite(slope) and math.isfinite(intercept)):
        return None
    return intercept, slope


def neutralize_beta(scores: dict, min_names: int = MIN_NEUTRALIZE_NAMES) -> dict:
    """Strip the cross-sectional beta loading from every composite_score, in place.

    Preserves the pre-neutralization value as `composite_raw` and flags each ticker
    with `beta_neutralized`, so the change is auditable per-name and the evidence
    ledger can partition on it.

    Uses `beta_stable` (long-window, Blume-shrunk) for BOTH the fit and the
    adjustment, at whatever basis is available. A short-basis estimate is noisier,
    but excluding those names would apply the correction to only part of the universe
    and leave two incomparable populations in one ranking — worse than a slightly
    noisy correction applied uniformly.

    Residuals are re-centred on the fitted sample's mean so the output keeps the
    familiar 0-100 scale, then clamped to that range.
    """
    usable = [(t, s) for t, s in scores.items()
              if t not in _NEUTRALIZE_EXCLUDE
              and isinstance(s.get("composite_score"), (int, float))
              and isinstance(s.get("beta_stable"), (int, float))]

    fit = _ols_fit([s["beta_stable"] for _, s in usable],
                   [s["composite_score"] for _, s in usable]) if len(usable) >= min_names else None

    if fit is None:
        for s in scores.values():
            s.setdefault("composite_raw", s.get("composite_score"))
            s["beta_neutralized"] = False
        return scores

    intercept, slope = fit
    centre = _mean([s["composite_score"] for _, s in usable])
    for t, s in scores.items():
        s["composite_raw"] = s.get("composite_score")
        beta = s.get("beta_stable")
        if t in _NEUTRALIZE_EXCLUDE or not isinstance(beta, (int, float)) \
                or not isinstance(s.get("composite_score"), (int, float)):
            s["beta_neutralized"] = False
            continue
        residual = s["composite_score"] - (intercept + slope * beta)
        s["composite_score"] = round(max(0.0, min(100.0, residual + centre)), 1)
        s["beta_neutralized"] = True
    return scores


def score_all_tickers(market_data: dict, beta_neutralize: bool | None = None) -> dict:
    """Returns {ticker: score_dict} for all tickers that have price history.

    `beta_neutralize` defaults to the module-level BETA_NEUTRALIZE. It is a parameter
    so the backtest harness can run neutralized and un-neutralized arms against the
    identical scoring path, rather than an A/B across two code versions.
    """
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

    if BETA_NEUTRALIZE if beta_neutralize is None else beta_neutralize:
        neutralize_beta(scores)
    else:
        for s in scores.values():
            s["composite_raw"] = s.get("composite_score")
            s["beta_neutralized"] = False

    return scores


# Sub-score fields worth persisting per ticker/day (the raw factor inputs to IC).
_FACTOR_HISTORY_FIELDS = (
    "composite_score", "factors_used", "formula_version",
    "momentum_score", "momentum_available",
    "quality_score", "quality_available",
    "valuation_score", "valuation_available",
    "volatility_score", "volatility_available",
    "beta", "beta_stable", "beta_stable_window", "beta_stable_basis",
    "composite_raw", "beta_neutralized",
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
