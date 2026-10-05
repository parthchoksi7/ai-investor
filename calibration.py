"""
calibration.py — observational forecast ledger (#2).

Every agent already emits structured numeric forecasts; the system just never
scored them. This module closes that loop WITHOUT touching the trade path:

  log_forecasts()   appends one line per (agent, ticker, field) to forecasts.jsonl
                    every run — pure logging, no decision is affected.
  score_matured()   joins forecasts whose horizon has elapsed to the realized
                    forward return (from price history) → forecasts_scored.jsonl.
  agent_scorecard() reports per-agent skill (rank-IC, sign-hit-rate) with
                    SHRINKAGE toward a no-skill prior, BLOCK-SAMPLED effective-N
                    statistics, and a Benjamini-Hochberg multiplicity control, so
                    a handful of overlapping forecasts can't masquerade as signal.

Scoring the FULL candidate universe (not just executed trades) is deliberate: it
accrues hundreds of (forecast, outcome) pairs per month, beating the small-sample
problem long before the trade count would. Nothing here weights or sizes a trade
— that stays gated behind a sample threshold (future work). This is the data
clock you want started.

Measurement-bias fixes (see PAPER_DRAFT §3.7, REVIEWER_FEEDBACK_BACKLOG A1/A2/A3):

  A1 — Executable entry, no one-bar look-ahead. The forward return is measured
       from the NEXT SESSION'S OPEN (the first price actually tradable after the
       signal), derived at scoring time — NOT from the signal-day close the
       signal was computed on. `signal_close` is retained on each row for
       reference only; it is never the return base. This matches backtest/engine
       (signal at close(t) → fill at open(t+1)).

  A2 — Overlapping-window correction. Daily forecasts on a 21-day horizon share
       20/21 of their return window, so raw N massively overstates independence.
       The scorecard reports a BLOCK-SAMPLED IC/hit-rate over non-overlapping
       ~horizon-spaced observations and bases every confidence interval and
       p-value on that effective N, not the raw count.

  A3 — Multiplicity control + one pre-registered primary metric. One IC + one
       hit-rate is computed per (agent, field) across several series; reporting
       the best is data dredging. A Benjamini-Hochberg adjustment is applied
       across all metrics, and ONE metric is pre-registered as primary:
       quant.composite_score at the 21-day horizon (see PRIMARY_METRIC). It is
       the deterministic foundation with the largest, most stable sample; if even
       it shows no rank-IC, the LLM layer on top is building on sand.
"""

import json
import math
import os
from datetime import datetime, date, timedelta, timezone

# The CURRENT operative quant formula (e.g. "2.0-quality-tilt"). Rows tagged with
# an OLDER formula_version are a different scoring regime — pooling their IC with
# the current formula's would silently blend two composites into one number
# (Phase 1, 2026-07-05: the evidence clock must measure the strategy that is
# actually live, not an average of it and its predecessor). Deferred import
# avoided here since quant_engine has no heavy/circular dependency on this module.
from quant_engine import FORMULA_VERSION as _CURRENT_QUANT_FORMULA

LEDGER         = "forecasts.jsonl"
SCORED         = "forecasts_scored.jsonl"
SCORECARD      = "agent_scorecards.json"
FACTOR_HISTORY = "factor_history.jsonl"  # (date, ticker) -> formula_version join source

# §7.5 counterfactual decision ledger — binary flags (reject / veto / select) whose
# forward return tests whether the rejecting/vetoing models actually reduce risk.
DECISIONS        = "decisions_ledger.jsonl"
DECISIONS_SCORED = "decisions_scored.jsonl"
COUNTERFACTUAL   = "counterfactual.json"

DEFAULT_HORIZON = 21   # ~1 trading month

# Multi-horizon ladder (§7.3.2 of the redesign plan). A medium/long-term signal should
# look weak at 21d and STRENGTHEN at 63/126/189/252d — judging it only at 21d would
# wrongly condemn the strategy we want. Each forecast is logged at EVERY horizon; the
# scorecard reports an IC curve per (agent, field) across them. 189/252 ≈ 9/12 months
# = the owner's primary holding horizon (IPS §4). 21d remains the pre-registered PRIMARY
# metric (longest, most stable sample); the longer horizons are BH-adjusted secondary
# and take 1-2 years to mature (do not sequence anything behind them — plan §7.4).
HORIZONS = (21, 63, 126, 189, 252)

SCHEMA_VERSION  = 2    # 2 = executable-entry semantics (A1); rows < 2 are legacy

# A3 — the single pre-registered primary metric + horizon. Commit this BEFORE
# reading results and (ideally) register it externally (OSF/AsPredicted). The
# headline skill number is THIS one; everything else is secondary and BH-adjusted.
PRIMARY_METRIC  = ("quant", "composite_score")
PRIMARY_HORIZON = 21
BH_ALPHA        = 0.05
# Externally pre-registered (immutable, timestamped) — the primary metric,
# horizon, executable-entry basis, total-return benchmark, BH control, and the
# 60-trading-day reporting threshold. See PREREGISTRATION.md / PAPER_DRAFT §4.4.
PREREGISTRATION_URL = "https://aspredicted.org/zm7a2p.pdf"  # AsPredicted #296637

# agent -> (pipeline_state key, field, orientation). orientation = +1 when a
# HIGHER value should predict a HIGHER forward return, -1 when higher predicts
# lower (a risk score). Used to orient the sign-hit-rate.
#
# NOTE: "pm" (the Portfolio Manager's expected_return) is deliberately NOT here — it
# doesn't fit this per-ticker-dict shape (portfolio_manager_proposed is a LIST of
# decisions, and only BUYs carry the field). It is scored by the separate
# log_pm_forecasts() emitter below, which reuses the same forecast-row schema.
# agent_scorecard's orientation lookup already defaults unlisted agents to +1, which
# is the correct sign for "pm" too, so no entry is needed here for that either.
_FORECASTS = {
    "quant":           ("quant_scores",     "composite_score",   +1),
    "research":        ("research",          "confidence",        +1),
    "earnings":        ("earnings",          "earnings_alpha_score", +1),
    "devils_advocate": ("devils_advocate",   "overall_risk_score", -1),
    "position_review": ("position_reviews",  "hold_score",        +1),
}

# §7.5 counterfactual signals — the DIRECTION of forward return that means the model
# ADDED value. The whole system rejects far more than it buys; a fund that never tracks
# what it passed on loses the information to judge its own process. "underperform" = the
# flagged (rejected/vetoed) names SHOULD return less than the kept set; "outperform" =
# the flagged (selected) names should return more. Feeds MODEL_REGISTER M4 (DA) / M7 (CRO).
_CF_SIGNALS = {
    "da_reject":   "underperform",   # Devil's Advocate (M4): devils_advocate[t].recommend_reject
    "cro_veto":    "underperform",   # Chief Risk Officer (M7): cro.rejected_tickers
    "pm_selected": "outperform",     # Portfolio Manager (M6): final BUYs — does selection add value?
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
                  horizons: tuple = HORIZONS, path: str = LEDGER,
                  provenance: dict | None = None) -> int:
    """Append this run's per-(agent, ticker, HORIZON) numeric forecasts. Returns count.

    Each forecast is logged at EVERY horizon in `horizons` (§7.3.2), so the scorecard
    can build an IC curve per agent across horizons. Logging only — never raises into
    the caller's critical path (wrap the call).

    `signal_close` is stamped for REFERENCE ONLY (the close the signal was computed on).
    It is NOT the return base: score_matured() derives the executable entry (next-session
    open) at scoring time so there is no one-bar look-ahead (A1).

    `provenance` (§15.1) — the run's data-quality stamp (score/status/hash) merged into
    every row, so the harness can PARTITION the year-end IC comparison by data quality
    and exclude below-floor runs instead of silently averaging a starved run in.

    `formula_version` is stamped from the source entry when present (only
    quant_scores carries it — see quant_engine.score_all_tickers) so agent_scorecard
    can partition the quant metric by scoring regime instead of pooling forecasts
    from a re-weighted composite with its predecessor (P0-2, same rule build_dossier
    already applies to persistence). Non-quant agents simply carry `None`.
    """
    prov = provenance or {}
    rows = []
    for ticker in candidates:
        signal_close = (prices.get(ticker) or {}).get("close")
        if not signal_close:
            continue
        for agent, (key, field, _sign) in _FORECASTS.items():
            entry = pipeline_state.get(key, {}).get(ticker) or {}
            v = entry.get(field)
            if isinstance(v, (int, float)):
                fv = entry.get("formula_version")
                for h in horizons:
                    rows.append({
                        "run_id": run_id, "date": date_str, "agent": agent, "field": field,
                        "ticker": ticker, "value": float(v),
                        "signal_close": float(signal_close), "horizon_days": int(h),
                        "schema": SCHEMA_VERSION, "formula_version": fv, **prov,
                    })
    if rows:
        _append_jsonl(path, rows)
    return len(rows)


def log_decisions(run_id: str, date_str: str, pipeline_state: dict,
                  prices: dict, horizons: tuple = HORIZONS, path: str = DECISIONS) -> int:
    """§7.5 — log per-candidate binary decision flags (DA reject / CRO veto / PM select)
    at every horizon, so score_matured can later test whether the rejected/vetoed names
    actually underperform the kept set (the counterfactual that proves M4/M7 add value).

    Reuses the forecast-row schema, so score_matured scores it unchanged. Logging only;
    never raises into the caller's critical path (wrap the call).
    """
    da       = pipeline_state.get("devils_advocate", {}) or {}
    vetoed   = set((pipeline_state.get("cro", {}) or {}).get("rejected_tickers", []) or [])
    # A `cash_deployment` top-up of a holding is a deterministic rule, not a PM choice —
    # counting it as "PM selected" would let the mechanical rule pass Gate 1b for the AI.
    selected = {d.get("ticker") for d in (pipeline_state.get("final_decisions", []) or [])
                if str(d.get("action", "")).upper() == "BUY" and not d.get("cash_deployment")}
    # `final_decisions` changed meaning on 2026-08-19 (post-CRO intent →
    # post-guard-chain traded set), so `pm_selected` rows written before and after
    # measure different things. Stamp the vintage on every row so a longitudinal
    # consumer can PARTITION instead of pooling two definitions — the same
    # confound `formula_version` was introduced to fix. Absent ⇒ "post_cro".
    _fd_vintage = pipeline_state.get("final_decisions_vintage", "post_cro")
    candidates = pipeline_state.get("candidates", []) or []

    rows = []
    for t in candidates:
        signal_close = (prices.get(t) or {}).get("close")
        if not signal_close:
            continue
        flags = {
            "da_reject":   1.0 if (da.get(t) or {}).get("recommend_reject") else 0.0,
            "cro_veto":    1.0 if t in vetoed else 0.0,
            "pm_selected": 1.0 if t in selected else 0.0,
        }
        for agent, val in flags.items():
            for h in horizons:
                _row = {
                    "run_id": run_id, "date": date_str, "agent": agent, "field": "flag",
                    "ticker": t, "value": float(val), "signal_close": float(signal_close),
                    "horizon_days": int(h), "schema": SCHEMA_VERSION,
                }
                if agent == "pm_selected":
                    _row["final_decisions_vintage"] = _fd_vintage
                rows.append(_row)
    if rows:
        _append_jsonl(path, rows)
    return len(rows)


def log_dossier_signals(run_id: str, date_str: str, dossier: dict, prices: dict,
                        horizons: tuple = HORIZONS, path: str = LEDGER,
                        provenance: dict | None = None) -> int:
    """OBSERVATIONAL-ONLY (Stage A): log the dossier's derived signals — persistence
    (`composite_7d_mean`) and event-presence — as scored forecast rows, so their forward
    IC is measured INDEPENDENTLY *before* any consumer trusts them (ml_ai review ask).
    Never affects a decision. Reuses the forecast-row schema so score_matured scores it
    unchanged. Logs nothing when the dossier is stale (`as_of != date_str`) — a stale
    dossier's signals must not enter the evidence clock. Never raises into the caller.

    Iterates the FULL dossier universe (not the screened candidate subset) so the IC is an
    UNBIASED, full-cross-section measurement — measuring only among already-strong
    candidates would condition the signal on the screen and mislead the Stage B/C go/no-go.
    Emits two agents: `persist_mean` (does the 7-day-smoothed composite predict returns as
    well as / better than raw composite?) and `event_present` (does having a material event
    predict forward return?)."""
    if not isinstance(dossier, dict):
        return 0
    if str(dossier.get("as_of", ""))[:10] != str(date_str)[:10]:
        return 0                                  # stale dossier → keep its signals out
    tickers = dossier.get("tickers", {}) or {}
    prov = provenance or {}
    rows = []
    for t, rec in tickers.items():
        if not isinstance(rec, dict):
            continue
        signal_close = (prices.get(t) or {}).get("close")
        if not signal_close:
            continue
        pmean = (rec.get("persistence") or {}).get("composite_7d_mean")
        emit = [("event_present", "flag", 1.0 if (rec.get("events") or []) else 0.0)]
        if isinstance(pmean, (int, float)):
            emit.append(("persist_mean", "composite_7d_mean", float(pmean)))
        for agent, field, val in emit:
            for h in horizons:
                rows.append({
                    "run_id": run_id, "date": date_str, "agent": agent, "field": field,
                    "ticker": t, "value": float(val), "signal_close": float(signal_close),
                    "horizon_days": int(h), "schema": SCHEMA_VERSION, **prov,
                })
    if rows:
        _append_jsonl(path, rows)
    return len(rows)


def log_pm_forecasts(run_id: str, date_str: str, pipeline_state: dict, prices: dict,
                     horizons: tuple = HORIZONS, path: str = LEDGER,
                     provenance: dict | None = None) -> int:
    """Log the Portfolio Manager's own `expected_return` estimate as a first-class
    forecast (Phase 1 batch, MANUAL_TODO #16). `guardrails.enforce_net_edge` gates
    every BUY on this self-reported number, but nothing has ever measured whether the
    PM's estimate is CALIBRATED against realized returns — this closes that loop the
    same way log_forecasts closes it for quant/research/earnings/devils_advocate/
    position_review, so the net-edge floor can eventually be tuned on evidence instead
    of faith.

    Scored from `portfolio_manager_proposed` (the PM's RAW proposal, BEFORE the CRO
    veto or any guardrail) rather than `final_decisions` — scoring only guard-survived
    decisions would introduce a selection bias (only ever measuring predictions that
    already cleared the net-edge floor, never the over-confident ones that got
    rejected or vetoed). Only BUY decisions carry an expected_return (SELLs/HOLDs
    don't — same convention as `enforce_net_edge`), so this necessarily scores a
    smaller, PM-selected subset of tickers each run, not the full candidate universe.
    Reuses the forecast-row schema unchanged so score_matured/agent_scorecard score it
    exactly like every other agent (orientation defaults to +1: a higher expected
    return should predict a higher realized one). Logging only; never raises into the
    caller's critical path (wrap the call, same as log_forecasts).
    """
    decisions = pipeline_state.get("portfolio_manager_proposed") or []
    prov = provenance or {}
    rows = []
    for d in decisions:
        if not isinstance(d, dict) or str(d.get("action", "")).upper() != "BUY":
            continue
        ticker = d.get("ticker")
        if not ticker:
            continue
        try:
            er = float(d.get("expected_return"))
        except (TypeError, ValueError):
            continue
        signal_close = (prices.get(ticker) or {}).get("close")
        if not signal_close:
            continue
        for h in horizons:
            rows.append({
                "run_id": run_id, "date": date_str, "agent": "pm", "field": "expected_return",
                "ticker": ticker, "value": er,
                "signal_close": float(signal_close), "horizon_days": int(h),
                "schema": SCHEMA_VERSION, **prov,
            })
    if rows:
        _append_jsonl(path, rows)
    return len(rows)


def _history_to_series(history: dict) -> dict:
    """{ticker: [bars]} → {ticker: sorted [(iso_date, open, close)]}. Bars carry an
    epoch-ms or ISO `date`; `open` may be missing on some feeds (kept as None)."""
    out: dict[str, list] = {}
    for t, bars in (history or {}).items():
        s = []
        for b in bars:
            raw, op, close = b.get("date"), b.get("open"), b.get("close")
            if raw is None or close is None:
                continue
            iso = (datetime.fromtimestamp(raw / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                   if isinstance(raw, (int, float)) else str(raw)[:10])
            s.append((iso, (float(op) if op is not None else None), float(close)))
        out[t] = sorted(s)
    return out


def _next_open_after(series: list, signal_iso: str):
    """Executable entry: (date, open) of the first bar STRICTLY AFTER the signal
    date. This is the price actually tradable the morning after the signal (A1).
    Returns (None, None) if there is no later bar or its open is missing."""
    for d, op, _c in series:
        if d > signal_iso and op is not None:
            return d, op
    return None, None


def _close_on_or_after(series: list, target_iso: str):
    for d, _op, c in series:
        if d >= target_iso:
            return c
    return None


def score_matured(snapshot: dict, ledger_path: str = LEDGER,
                  scored_path: str = SCORED) -> int:
    """Score forecasts whose horizon has elapsed against the realized forward return.

    A1: the return is measured from the NEXT SESSION'S OPEN (executable entry,
    derived here) to the close on/after entry + horizon days — never from the
    signal-day close. A forecast with no next-session bar yet (or no open on it)
    is treated as immature and skipped until it matures.

    Idempotent: a (run_id, agent, field, ticker) already in scored_path is skipped,
    so re-running never double-counts. Returns the number newly scored.
    """
    series = _history_to_series(snapshot.get("history", {}))
    # Idempotency key INCLUDES horizon_days (P1-9): one forecast now matures at several
    # horizons, so (run_id, agent, field, ticker) alone would score only the first.
    scored_keys = {(r.get("run_id"), r.get("agent"), r.get("field"), r.get("ticker"),
                    r.get("horizon_days"))
                   for r in _iter_jsonl(scored_path)}

    out = []
    for fc in _iter_jsonl(ledger_path):
        key = (fc.get("run_id"), fc.get("agent"), fc.get("field"), fc.get("ticker"),
               fc.get("horizon_days"))
        if key in scored_keys:
            continue
        s = series.get(fc.get("ticker"))
        signal_date = fc.get("date")
        if not s or not signal_date:
            continue

        # A1: executable entry = next-session open (no signal-close look-ahead).
        entry_date, entry = _next_open_after(s, signal_date)
        if not entry:
            continue  # next session not available yet → immature, retry next run

        try:
            horizon = int(fc.get("horizon_days", DEFAULT_HORIZON))
            target = (date.fromisoformat(entry_date) + timedelta(days=horizon)).isoformat()
        except (ValueError, TypeError, KeyError):
            continue

        exit_px = _close_on_or_after(s, target)
        if exit_px is None:
            continue  # horizon not yet elapsed in the available history
        if not (math.isfinite(entry) and math.isfinite(exit_px) and entry > 0):
            # A NaN close in the snapshot (the Jun 16 2026 class) must not become a NaN
            # realized_return: 14 such rows reached the ledgers in June and a NaN
            # inside a Spearman rank silently corrupts the IC. Left unscored, not guessed.
            continue

        out.append({**fc, "entry_date": entry_date, "entry_price": round(entry, 4),
                    "future_price": exit_px,
                    "realized_return": round((exit_px - entry) / entry, 5),
                    "basis": "next_open"})
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
        # Ties get their AVERAGE rank. Order-based ranks (the original) gave tied
        # values arbitrary, input-order-dependent ranks — composites are rounded to
        # 0.1 and agent scores are small integers, so ties are the common case.
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    sx = math.sqrt(sum((rx[i] - mx) ** 2 for i in range(n)))
    sy = math.sqrt(sum((ry[i] - my) ** 2 for i in range(n)))
    if sx == 0 or sy == 0:
        return None
    return cov / (sx * sy)


def _norm_sf(z: float) -> float:
    """Two-sided survival probability 2·(1−Φ(|z|)) via erf — no scipy dependency."""
    return math.erfc(abs(z) / math.sqrt(2))


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the regularized incomplete beta (Numerical Recipes 6.4)."""
    tiny, qab, qap, qam = 1e-300, a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d; d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c; c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d; d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c; c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-14:
            break
    return h


def _t_sf2(t: float, df: float) -> float:
    """Two-sided Student-t tail probability P(|T| > |t|), no scipy: I_{df/(df+t²)}(df/2, 1/2)."""
    if df <= 0 or not math.isfinite(t):
        return 0.0 if math.isinf(t) else 1.0
    x = df / (df + t * t)
    a, b = df / 2.0, 0.5
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return min(1.0, front * _betacf(a, b, x) / a)
    return min(1.0, 1.0 - front * _betacf(b, a, 1.0 - x) / b)


def _welch_p(a: list, b: list):
    """Two-sided p-value for H0: mean(a) == mean(b) — Welch's t with Welch–Satterthwaite
    degrees of freedom (unequal variances, no scipy). Student-t, not the normal
    approximation (Oct 4 2026 review): at the gate's n ≥ 10-per-side floor a z read
    understates p (z=2.2 → 0.028 vs t≈0.044 at df 15), enough to pass Gate 1b on noise. None if either sample has < 2 observations or
    is CONSTANT (a single distinct value — checked via set(), not `variance == 0`:
    summing many copies of the same float accumulates rounding noise, so a truly
    constant sample can compute a variance of ~1e-35 instead of exact 0 — small
    enough to be meaningless but large enough to pass an `== 0` check, producing
    an astronomically tiny standard error and a spurious p≈0 "extreme
    significance" out of pure floating-point dust rather than real signal)."""
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return None
    if len(set(a)) <= 1 or len(set(b)) <= 1:
        return None  # a constant sample has no variance to test against
    ma, mb = sum(a) / na, sum(b) / nb
    va = sum((x - ma) ** 2 for x in a) / (na - 1)
    vb = sum((x - mb) ** 2 for x in b) / (nb - 1)
    se = math.sqrt(va / na + vb / nb)
    if se == 0:
        return None
    t = (mb - ma) / se
    df = (va / na + vb / nb) ** 2 / ((va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1))
    return round(min(1.0, _t_sf2(t, df)), 4)


def _ic_pvalue(ic, n: int):
    """Approximate two-sided p-value for a rank-IC under H0: IC=0.

    Uses the standard z ≈ IC·√(n−1) large-sample approximation. `n` MUST be the
    effective (block-sampled, non-overlapping) count, not the raw overlapping
    count — passing raw n understates the p-value (overstates significance)."""
    if ic is None or n is None or n < 4:
        return None
    z = ic * math.sqrt(n - 1)
    return round(min(1.0, _norm_sf(z)), 4)


def _block_sample(rows: list, horizon: int) -> list:
    """A2: collapse overlapping forecasts to a non-overlapping effective sample.

    Per ticker, sort by date and greedily keep forecasts spaced ≥ horizon CALENDAR
    days apart, so the 21-day return windows of the kept rows do not overlap and
    can be treated as approximately independent. Rows with no parseable date pass
    through (small test/legacy samples). The kept set is pooled across tickers."""
    by_t: dict = {}
    for r in rows:
        by_t.setdefault(r.get("ticker"), []).append(r)
    kept = []
    for _t, rs in by_t.items():
        rs = sorted(rs, key=lambda r: r.get("date") or "")
        last = None
        for r in rs:
            d = r.get("date")
            try:
                dd = date.fromisoformat(str(d)[:10])
            except (ValueError, TypeError):
                kept.append(r)
                continue
            if last is None or (dd - last).days >= horizon:
                kept.append(r)
                last = dd
    return kept


def _drop_zero_variance_days(rows: list) -> list:
    """Exclude forecast rows from a run-date whose cross-sectional values are ALL
    IDENTICAL — a degenerate default-value emission (e.g. the Jun 8-10 outage,
    where every Haiku agent returned its fallback score for the whole universe),
    not a real forecast. Spearman rank-IC has no discriminative signal to measure
    on a constant; keeping these rows in the pool dilutes genuine observations
    with noise from a day the agent produced no real output. Rows with < 2 values
    or an unparseable date can't be judged degenerate and pass through unfiltered."""
    by_date: dict = {}
    for r in rows:
        by_date.setdefault(r.get("date"), []).append(r)
    kept = []
    for d, rs in by_date.items():
        vals = [x["value"] for x in rs if isinstance(x.get("value"), (int, float))]
        if d is not None and len(vals) >= 2 and len(set(vals)) == 1:
            continue  # zero cross-sectional variance this day -> drop the whole day
        kept.extend(rs)
    return kept


def _finite_pair(row: dict) -> bool:
    """True when the row carries a finite numeric value AND realized_return. The
    ledgers are append-only, so historical NaN rows are excluded at read time."""
    v, r = row.get("value"), row.get("realized_return")
    return (isinstance(v, (int, float)) and isinstance(r, (int, float))
            and math.isfinite(v) and math.isfinite(r))


def _ic_hit(rows: list, sign: int):
    """(ic, hit_rate, n) over rows carrying numeric value + realized_return."""
    # Filter PAIRS, not each list separately: two independent filters truncated to a
    # common length would misalign every pair after the first row missing one side.
    pairs = [(x["value"], x["realized_return"]) for x in rows if _finite_pair(x)]
    n = len(pairs)
    if n == 0:
        return None, None, 0
    vals, rets = [v for v, _ in pairs], [r for _, r in pairs]
    ic = _spearman(vals, rets)
    mean = sum(vals) / n
    hits = sum(1 for v, rr in zip(vals, rets) if (sign * (v - mean)) * rr > 0)
    return ic, round(hits / n, 3), n


def _formula_version_lookup(path: str = FACTOR_HISTORY) -> dict:
    """(date, ticker) -> formula_version, read from the factor-history ledger.

    A READ-ONLY join used to recover the true historical formula_version for
    quant forecast rows logged BEFORE log_forecasts started stamping it directly
    (this fix's rollout day). Rewriting the append-only forecasts/forecasts_scored
    ledgers in place to backfill the field would violate the same append-only
    invariant journal.py and the other ledgers in this codebase deliberately
    preserve for audit; factor_history.jsonl already carries formula_version per
    (date, ticker) from day one (P0-2), so joining at scorecard-BUILD time gets
    the same correctness without ever mutating a historical record."""
    lut: dict = {}
    for r in _iter_jsonl(path):
        d, t, fv = r.get("date"), r.get("ticker"), r.get("formula_version")
        if d is not None and t is not None and fv is not None:
            lut[(d, t)] = fv
    return lut


def agent_scorecard(scored_path: str = SCORED, out_path: str = SCORECARD,
                    shrink_k: int = 50, factor_history_path: str = FACTOR_HISTORY) -> dict:
    """Per (agent.field): raw + block-sampled IC, hit-rate, shrunk IC, effective-N
    CI, and a Benjamini-Hochberg-adjusted p-value. Plus a `_meta` block naming the
    pre-registered primary metric.

    `ic_shrunk = ic · n/(n+shrink_k)` pulls small samples toward 0 (no skill). The
    *honest* significance read is `ic_block` / `p_value_bh` on the block sample
    (A2/A3): the raw `ic` over overlapping daily windows overstates precision.
    Nothing consumes this to size trades yet — it is a scoreboard, gated behind
    sample size (future work).

    Formula-version partition (Phase 1, 2026-07-05): the `quant` agent's rows carry
    `formula_version` (stamped by log_forecasts from quant_scores going forward).
    A row tagged with the CURRENT operative formula (quant_engine.FORMULA_VERSION)
    keys the PLAIN metric name (`quant.composite_score@21d`) so existing consumers
    (stage_c_readiness.py) keep reading the live regime's evidence unchanged. Rows
    tagged with an older formula_version key a SUFFIXED name instead
    (`~<version>`) — visible for audit, never silently blended into the number
    that gates a go/no-go decision. A row with NO tag at all (logged before this
    fix shipped) is resolved via a READ-ONLY join against factor_history.jsonl
    (see _formula_version_lookup) — that ledger has carried formula_version per
    (date, ticker) since Phase 4 (P0-2), so pre-fix rows recover their TRUE
    historical version instead of all collapsing into one undifferentiated
    "legacy" bucket; a row with no match in either place keys `~unknown`. Non-quant
    agents have no version concept at all (formula_version is always None for
    them) and are unaffected.

    Zero-variance days (§ same date) are dropped before scoring (see
    _drop_zero_variance_days) — a day where every forecast in the pool is
    identical (e.g. the Jun 8-10 outage: every Haiku agent returned its default)
    is a degenerate emission, not a real forecast, and dilutes genuine signal.
    """
    fv_lookup = _formula_version_lookup(factor_history_path)

    # Group by (agent, field, HORIZON, formula_version) so each horizon AND each
    # scoring regime gets its own IC — the IC curve across horizons is the whole
    # point of multi-horizon (§7.3.2), and pooling across a formula re-weight
    # would silently average two different composites into one number (P0-2).
    groups: dict = {}
    for r in _iter_jsonl(scored_path):
        h = int(r["horizon_days"]) if isinstance(r.get("horizon_days"), (int, float)) else DEFAULT_HORIZON
        fv = r.get("formula_version")
        if fv is None and r.get("agent") == "quant":
            fv = fv_lookup.get((r.get("date"), r.get("ticker")))
        groups.setdefault((r.get("agent"), r.get("field"), h, fv), []).append(r)

    orient = {agent: sign for agent, (_k, _f, sign) in _FORECASTS.items()}
    card: dict = {}
    for (agent, field, horizon, fv), rows in groups.items():
        rows = _drop_zero_variance_days(rows)
        sgn = orient.get(agent, +1)

        ic, hit, n = _ic_hit(rows, sgn)                       # raw (overlapping)
        if n == 0:
            continue
        block = _block_sample(rows, horizon)                  # A2: effective sample
        ic_b, hit_b, n_b = _ic_hit(block, sgn)
        n_eff = max(1, round(n / horizon)) if horizon else n  # quick scalar effective N

        # Only the "quant" agent has a versioned formula; every other agent's
        # forecast has no such concept (fv is always None) and always keys plain.
        # fv is None here only when BOTH the row's own tag AND the
        # factor_history join above failed to resolve it — a genuinely unknown
        # vintage (e.g. no matching factor_history row that day), not merely
        # "pre-fix" (the join already recovers those).
        if agent == "quant":
            is_current = fv == _CURRENT_QUANT_FORMULA
            version_tag = fv if fv is not None else "unknown"
        else:
            is_current = True
            version_tag = None
        base_key = f"{agent}.{field}@{horizon}d"
        key = base_key if is_current else f"{base_key}~{version_tag}"

        card[key] = {
            "n": n,
            "n_effective":  n_b,           # non-overlapping block count (use THIS)
            "n_eff_approx": n_eff,         # n/horizon sanity scalar
            "ic":           round(ic, 3) if ic is not None else None,
            "ic_block":     round(ic_b, 3) if ic_b is not None else None,
            "ic_shrunk":    round(ic * n / (n + shrink_k), 3) if ic is not None else None,
            "hit_rate":     hit,
            "hit_rate_block": hit_b,
            "ci_halfwidth": round(1.96 / math.sqrt(n_b), 3) if n_b else None,  # on effective N
            "p_value":      _ic_pvalue(ic_b, n_b),            # block IC, effective N
            "orientation":  sgn,
            "horizon_days": horizon,
            "formula_version": fv,
            "is_primary":   (is_current and (agent, field) == PRIMARY_METRIC
                             and horizon == PRIMARY_HORIZON),
        }

    # A3: Benjamini-Hochberg across all metrics that produced a p-value.
    pvals = [(k, card[k]["p_value"]) for k in card if card[k].get("p_value") is not None]
    m = len(pvals)
    for k in card:
        card[k]["p_value_bh"] = None
        card[k]["significant_bh"] = None
    for k, q in _bh_adjust(pvals).items():
        card[k]["p_value_bh"] = round(q, 4)
        card[k]["significant_bh"] = q < BH_ALPHA

    primary_key = f"{PRIMARY_METRIC[0]}.{PRIMARY_METRIC[1]}@{PRIMARY_HORIZON}d"
    card["_meta"] = {
        "primary_metric":  primary_key,
        "primary_horizon": PRIMARY_HORIZON,
        "n_metrics":       m,
        "bh_alpha":        BH_ALPHA,
        "preregistration": PREREGISTRATION_URL,
        "note": ("Headline skill = the primary metric only; all others are "
                 "BH-adjusted secondary. Read ic_block / p_value_bh on the "
                 "effective (block-sampled) N, not the raw overlapping ic."),
        "gate_1b": ("p_value_bh here is BH over THIS file's metrics (the preregistered "
                    "secondary family). SCALE_DECISION_RULE Gate 1b uses a different, "
                    "joint family — read counterfactual.json → _gate_1b, never this "
                    "file's significant_bh, for the scale decision."),
    }

    with open(out_path, "w") as f:
        json.dump(card, f, indent=2)
    return card


# ── Gate 1a on the FULL universe (Oct 4 2026 review, item 5) ──────────────────
# The preregistered calibration ledger logs the quant score only for the ~20 weekly
# CANDIDATES — holdings plus the top of the ranking — so its IC is range-restricted
# and accrues ~20 names a week, while SCALE_DECISION_RULE §2's power math assumes the
# whole scored universe (~174 names). factor_history.jsonl already records every
# scored name daily (GH plane, full history). This report measures the preregistered
# hypothesis — cross-sectional rank association of composite_score with the 21-day
# next-open forward return — on that full cross-section, ONE IC PER DATE, and infers
# on non-overlapping dates only (one 21-day window cannot count as many independent
# observations just because 170 stocks moved through it together).
QUANT_IC_REPORT   = "quant_ic_report.json"
# Append-only per-date IC ledger. The snapshot keeps ~210 bars, so recomputing every
# date from it each day would silently DROP old periods as their prices age out and
# move the greedy non-overlap anchor — a rolling, re-anchoring window instead of the
# cumulative fixed record a preregistered gate needs. A date is computed once (when
# its forward window has matured) and never recomputed.
QUANT_IC_HISTORY  = "quant_ic_history.jsonl"
MIN_CROSS_SECTION = 30     # fewer scored names on a date → that date's IC is skipped
# A date is PERSISTED only once its exit date is this many calendar days behind the
# newest bar in the snapshot. Expansion names refresh in batches and may carry a price
# up to market_data.CARRY_FORWARD_MAX_DAYS (5 trading days ≈ 7 calendar) stale, so on
# the morning a window first matures many of them have no exit bar yet — persisting
# then would freeze a thinner cross-section forever. Before the grace passes the date
# is still computed and shown, marked provisional, and recomputed next run.
PERSIST_GRACE_DAYS = 10
# Two-sided 95% Student-t critical values by degrees of freedom (df ≥ 30 → normal).
# The per-period IC sample is small for a long time; a normal interval on 3 periods
# would be badly overconfident.
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
        8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
        15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086, 21: 2.080,
        22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056, 27: 2.052, 28: 2.048,
        29: 2.045}
# SCALE_DECISION_RULE §3 Gate 1a thresholds — read here, never tuned here.
_GATE_1A_ROUTES = {"fast": {"min_ic": 0.08, "min_obs": 400},
                   "slow": {"min_ic": 0.05, "min_obs": 1600}}


def _sector_demeaned_ic(pairs: list, sector_of) -> float | None:
    """IC after removing each sector's mean score and mean return: what the score says
    about a stock RELATIVE TO ITS SECTOR. Sectors with < 3 scored names are dropped."""
    by_sec: dict = {}
    for t, v, r in pairs:
        by_sec.setdefault(sector_of(t), []).append((v, r))
    xs, ys = [], []
    for _sec, rows in by_sec.items():
        if len(rows) < 3:
            continue
        mv = sum(v for v, _ in rows) / len(rows)
        mr = sum(r for _, r in rows) / len(rows)
        xs += [v - mv for v, _ in rows]
        ys += [r - mr for _, r in rows]
    return _spearman(xs, ys) if len(xs) >= MIN_CROSS_SECTION else None


def _mean_ci(values: list) -> dict:
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": None, "sd": None, "t_stat": None, "ci95": None}
    mean = sum(values) / n
    if n < 2:
        return {"n": n, "mean": round(mean, 4), "sd": None, "t_stat": None, "ci95": None}
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))
    se = sd / math.sqrt(n)
    crit = _T95.get(n - 1, 1.96)
    return {"n": n, "mean": round(mean, 4), "sd": round(sd, 4),
            "t_stat": round(mean / se, 3) if se > 0 else None,
            "ci95": [round(mean - crit * se, 4), round(mean + crit * se, 4)]}


def universe_ic_report(snapshot: dict, factor_history_path: str = FACTOR_HISTORY,
                       out_path: str = QUANT_IC_REPORT, horizon: int = PRIMARY_HORIZON,
                       formula_version: str = _CURRENT_QUANT_FORMULA,
                       history_path: str | None = QUANT_IC_HISTORY) -> dict:
    """Per-date full-universe IC of composite_score under ONE formula version, with
    inference on non-overlapping dates. Writes and returns the report.

    Entry/exit match the preregistration (A1): next-session open after the signal date
    to the close on/after entry + `horizon` calendar days. Only rows stamped exactly
    `formula_version` count — the `~raw` degrade variant is a different population.
    Non-overlapping dates are chosen greedily from the FIRST eligible date with
    spacing > horizon days, so the selection is mechanical, not picked.

    Per-date ICs are persisted append-only to `history_path` (None = in-memory only,
    for tests/diagnostics): a persisted date is never recomputed, so the record and its
    anchor stay fixed as the snapshot's price window rolls forward. ONLY the GH plane
    (full history for every scored name) should persist — a run on the slimmed
    committed snapshot would freeze a thinner cross-section into the ledger.
    """
    from guardrails import sector_of
    series = _history_to_series(snapshot.get("history", {}))
    by_date: dict = {}
    for r in _iter_jsonl(factor_history_path):
        if r.get("formula_version") != formula_version or r.get("ticker") in ("SPY", "QQQ"):
            continue
        v = r.get("composite_score")
        if isinstance(v, (int, float)) and math.isfinite(v):
            by_date.setdefault(r.get("date"), []).append((r.get("ticker"), float(v)))

    newest = max((s[-1][0] for s in series.values() if s), default=None)
    persisted = {r["date"]: r for r in _iter_jsonl(history_path)
                 if r.get("formula_version") == formula_version and r.get("horizon_days") == horizon
                 } if history_path else {}
    periods = []
    for d in sorted(k for k in by_date if k and k not in persisted):
        pairs = []
        for t, v in by_date[d]:
            s = series.get(t)
            if not s:
                continue
            entry_date, entry = _next_open_after(s, d)
            if not entry or not math.isfinite(entry) or entry <= 0:
                continue
            target = (date.fromisoformat(entry_date) + timedelta(days=horizon)).isoformat()
            exit_px = _close_on_or_after(s, target)
            if exit_px is None or not math.isfinite(exit_px):
                continue
            pairs.append((t, v, exit_px / entry - 1))
        if len(pairs) < MIN_CROSS_SECTION:
            continue          # immature (forward window not elapsed) or too thin
        ic = _spearman([v for _, v, _ in pairs], [r for _, _, r in pairs])
        if ic is None:
            continue
        sn = _sector_demeaned_ic(pairs, sector_of)
        exit_by = (date.fromisoformat(d) + timedelta(days=horizon + 1)).isoformat()
        final = bool(newest and (date.fromisoformat(newest)
                                 - date.fromisoformat(exit_by)).days >= PERSIST_GRACE_DAYS)
        periods.append({"date": d, "n": len(pairs), "ic": round(ic, 4),
                        "ic_sector_neutral": round(sn, 4) if sn is not None else None,
                        "formula_version": formula_version, "horizon_days": horizon,
                        "provisional": not final,
                        "computed_at": datetime.now(timezone.utc).isoformat()})
    to_persist = [p for p in periods if not p["provisional"]]
    if history_path and to_persist:
        _append_jsonl(history_path, to_persist)
    periods = sorted(list(persisted.values()) + periods, key=lambda p: p["date"])

    # Only FINAL (persisted-grade) periods count toward the gate; provisional ones are
    # reported but may still change as late names refresh.
    chosen, last = [], None
    for p in (q for q in periods if not q.get("provisional")):
        dd = date.fromisoformat(p["date"])
        if last is None or (dd - last).days > horizon:
            chosen.append(p)
            last = dd
    ics = [p["ic"] for p in chosen]
    sn_ics = [p["ic_sector_neutral"] for p in chosen if p["ic_sector_neutral"] is not None]
    stats = _mean_ci(ics)
    n_obs = sum(p["n"] for p in chosen)
    ci = stats["ci95"]
    ci_excludes_zero = bool(ci and (ci[0] > 0 or ci[1] < 0))
    routes = {name: {**cfg,
                     "met": bool(stats["mean"] is not None and stats["mean"] >= cfg["min_ic"]
                                 and n_obs >= cfg["min_obs"] and ci and ci[0] > 0)}
              for name, cfg in _GATE_1A_ROUTES.items()}
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "formula_version": formula_version,
        "horizon_days": horizon,
        "basis": "next_open → close on/after entry + horizon calendar days (A1)",
        "history_ledger": history_path,
        "non_overlapping": {**stats, "n_obs": n_obs,
                            "dates": [p["date"] for p in chosen],
                            "ci_excludes_zero": ci_excludes_zero,
                            "sector_neutral": _mean_ci(sn_ics)},
        "provisional_dates": [p["date"] for p in periods if p.get("provisional")],
        "all_dates_descriptive": {
            "n_dates": len(periods),
            "mean_ic": round(sum(p["ic"] for p in periods) / len(periods), 4) if periods else None,
            "note": "Consecutive dates share most of their 21-day window — descriptive only, "
                    "never a significance input."},
        "gate_1a": {"routes": routes,
                    "status": "PASS" if any(r["met"] for r in routes.values()) else "NOT_YET",
                    "rule": "SCALE_DECISION_RULE §3 Gate 1a + §7 measurement amendment "
                            "2026-10-04: mean per-period IC ≥ route IC, n_obs (names summed "
                            "over non-overlapping periods) ≥ route minimum, and the per-period "
                            "Student-t 95% CI strictly above zero."},
        "periods": periods,
        "caveats": [
            "Survivorship: the universe is current members only — historical ICs are biased upward.",
            "Sector-neutral IC is a DIAGNOSTIC (how much of the signal is sector rotation), "
            "not the gate.",
            "One IC per non-overlapping period: n_periods, not n_obs, sets the width of the CI.",
        ],
    }
    _atomic_write_json(out_path, report)
    return report


def _atomic_write_json(path: str, obj) -> None:
    """Write via a temp file + os.replace, so a crash mid-dump never leaves a
    truncated file for a workflow to commit."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def _bh_adjust(pvals: list) -> dict:
    """Benjamini-Hochberg step-up adjustment. `pvals` is [(key, p)]; returns {key: q}.
    Shared by agent_scorecard and counterfactual_report so both apply the identical
    multiplicity control (SCALE_DECISION_RULE Gate 1b names both outputs)."""
    m = len(pvals)
    if not m:
        return {}
    ranked = sorted(pvals, key=lambda x: x[1])
    adj = [(k, min(1.0, p * m / rank)) for rank, (k, p) in enumerate(ranked, start=1)]
    # step-up monotonicity: a smaller-rank q can't exceed a larger-rank q
    for i in range(len(adj) - 2, -1, -1):
        adj[i] = (adj[i][0], min(adj[i][1], adj[i + 1][1]))
    return dict(adj)


# SCALE_DECISION_RULE §3 Gate 1b — the ONE family the "any one of these signals"
# allowance is corrected over. cro_veto and pm.expected_return are excluded by the rule
# as structurally unmeasurable, so they are not in the family (and get no p_value_bh).
GATE_1B_AGENT_SIGNALS  = ("research.confidence", "devils_advocate.overall_risk_score",
                          "position_review.hold_score")
GATE_1B_DECISION_SIGNALS = ("pm_selected", "da_reject")


def counterfactual_report(scored_path: str = DECISIONS_SCORED,
                          out_path: str = COUNTERFACTUAL, min_n: int = 10,
                          scorecard_path: str = SCORECARD) -> dict:
    """§7.5 — per (signal, horizon): mean forward return of FLAGGED (value=1) vs KEPT
    (value=0) names, the gap, and whether the model added value in its expected direction.

    For da_reject / cro_veto the model adds value when flagged names UNDERPERFORM
    (gap = mean_kept − mean_flagged > 0); for pm_selected when selected names OUTPERFORM
    (gap < 0). Uses the block-sampled effective sample (A2) so overlapping windows don't
    inflate the count.

    `significant` requires BOTH `min_n` on each side AND a real two-sample test
    (Welch's t, `_welch_p`) clearing BH_ALPHA — matching the rigor
    agent_scorecard applies to the forecast metrics next to it (Phase 1, 2026-07-05).
    Before this fix, `significant` was `n_flagged >= min_n and n_kept >= min_n` alone
    — a sample-size floor with no actual significance test — which could label a
    result "ADDS_VALUE, significant: true" on a difference indistinguishable from
    noise. At ~weekly cadence this stays NOT_SIGNIFICANT for months (plan §7.4), by
    design; `p_value` is still reported once both sides reach `min_n`, so the read
    is honest about HOW insignificant, not just a boolean.

    GATE 1b FIELDS (Oct 4 2026 review). The raw fields above pool returns across run
    dates, so a decision taken before a market-wide rise looks like skill: the PM
    selecting more names on a date the whole market then rallied raises the flagged
    mean without any selection ability. `*_demeaned` subtracts each run date's mean
    forward return (over every candidate scored that date, flagged and kept) before
    comparing — a pure cross-sectional read. `p_value_bh` is the Benjamini-Hochberg
    adjustment over the WHOLE Gate 1b family at once: the demeaned p of pm_selected and
    da_reject at every horizon here, PLUS the block-IC p of the three agent signals in
    agent_scorecards.json (current-formula keys, every horizon). One correction across
    every way the gate can be passed — two separate BH passes would roughly double the
    family-wide false-discovery rate the rule promises. `significant_bh` additionally
    requires the `min_n` floor on both sides. The joint family is written to
    `_gate_1b`. The raw fields are kept unchanged for continuity with earlier reports.
    """
    groups: dict = {}
    for r in _iter_jsonl(scored_path):
        h = int(r["horizon_days"]) if isinstance(r.get("horizon_days"), (int, float)) else DEFAULT_HORIZON
        groups.setdefault((r.get("agent"), h), []).append(r)

    report: dict = {}
    demeaned_p: list = []
    for (agent, horizon), rows in groups.items():
        rows = [r for r in rows if _finite_pair(r)]
        by_date: dict = {}
        for r in rows:
            by_date.setdefault(r.get("date"), []).append(r["realized_return"])
        date_mean = {d: sum(v) / len(v) for d, v in by_date.items()}
        block = _block_sample(rows, horizon)
        flagged = [r["realized_return"] for r in block if r.get("value") == 1.0]
        kept = [r["realized_return"] for r in block if r.get("value") == 0.0]
        flagged_dm = [r["realized_return"] - date_mean[r.get("date")]
                      for r in block if r.get("value") == 1.0]
        kept_dm = [r["realized_return"] - date_mean[r.get("date")]
                   for r in block if r.get("value") == 0.0]
        if not flagged or not kept:
            continue
        mf, mk = sum(flagged) / len(flagged), sum(kept) / len(kept)
        gap = round(mk - mf, 5)                      # kept minus flagged
        direction = _CF_SIGNALS.get(agent, "underperform")
        adds_value = (gap > 0) if direction == "underperform" else (gap < 0)
        n_floor_met = len(flagged) >= min_n and len(kept) >= min_n
        p_value = _welch_p(flagged, kept)
        significant = n_floor_met and p_value is not None and p_value < BH_ALPHA
        gap_dm = round(sum(kept_dm) / len(kept_dm) - sum(flagged_dm) / len(flagged_dm), 5)
        p_dm = _welch_p(flagged_dm, kept_dm)
        key = f"{agent}@{horizon}d"
        report[key] = {
            "n_flagged": len(flagged), "n_kept": len(kept),
            "mean_return_flagged": round(mf, 5), "mean_return_kept": round(mk, 5),
            "gap_kept_minus_flagged": gap, "expected_direction": direction,
            "p_value": p_value, "n_floor_met": bool(n_floor_met),
            "adds_value": bool(adds_value), "significant": bool(significant),
            "verdict": ("NOT_SIGNIFICANT" if not significant
                        else ("ADDS_VALUE" if adds_value else "NO_VALUE")),
            "gap_demeaned": gap_dm,
            "adds_value_demeaned": bool((gap_dm > 0) if direction == "underperform"
                                        else (gap_dm < 0)),
            "p_value_demeaned": p_dm,
            "p_value_bh": None, "significant_bh": None,
        }
        if p_dm is not None and agent in GATE_1B_DECISION_SIGNALS:
            demeaned_p.append((f"counterfactual:{key}", p_dm))

    # The agent-signal half of the family, from the scorecard (current formula only —
    # a `~version` key is an older population and not part of this window's gate).
    try:
        card = json.load(open(scorecard_path))
    except (OSError, ValueError):
        card = {}
    agent_p = [(f"scorecard:{k}", v["p_value"]) for k, v in card.items()
               if isinstance(v, dict) and "~" not in k
               and k.split("@")[0] in GATE_1B_AGENT_SIGNALS
               and isinstance(v.get("p_value"), (int, float))]
    family = demeaned_p + agent_p
    qs = _bh_adjust(family)
    for fk, q in qs.items():
        if not fk.startswith("counterfactual:"):
            continue
        e = report[fk.split(":", 1)[1]]
        e["p_value_bh"] = round(q, 4)
        e["significant_bh"] = bool(e["n_floor_met"] and q < BH_ALPHA)
        e["verdict_bh"] = ("NOT_SIGNIFICANT" if not e["significant_bh"]
                           else ("ADDS_VALUE" if e["adds_value_demeaned"] else "NO_VALUE"))
    report["_gate_1b"] = {
        "family_size": len(family),
        "members": {fk: {"p": p, "q_bh": round(qs[fk], 4)} for fk, p in family},
        "note": ("Benjamini-Hochberg across every Gate 1b route at once. A scorecard "
                 "member's q is necessary but not sufficient — the rule also needs that "
                 "signal's IC and observation bar (Gate 1a's) in its expected direction."),
    }
    report["_meta"] = {
        "note": ("Counterfactual: does each model's reject/veto/select decision predict "
                 "the right forward-return direction? gap = mean_kept − mean_flagged. "
                 "Block-sampled effective N. `significant` requires BOTH sides >= "
                 f"{min_n} AND a Welch's-z two-sample p-value < {BH_ALPHA} (matches "
                 "agent_scorecard's rigor — a sample-size floor alone is not a "
                 "significance test). At ~weekly cadence this stays NOT_SIGNIFICANT "
                 "for months (§7.4), by design."),
        "gate_1b_fields": ("p_value_bh / significant_bh / verdict_bh: date-demeaned "
                           "(cross-sectional) gap, Welch t, Benjamini-Hochberg across the "
                           f"whole {len(family)}-test Gate 1b family (see _gate_1b). Use "
                           "THESE for SCALE_DECISION_RULE Gate 1b, not the raw `significant`. "
                           "cro_veto is excluded from the gate by the rule: no p_value_bh."),
        "min_n": min_n,
        "alpha": BH_ALPHA,
        "signals": _CF_SIGNALS,
    }
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)
    return report
