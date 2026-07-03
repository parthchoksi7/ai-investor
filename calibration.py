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

LEDGER    = "forecasts.jsonl"
SCORED    = "forecasts_scored.jsonl"
SCORECARD = "agent_scorecards.json"

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
    """
    prov = provenance or {}
    rows = []
    for ticker in candidates:
        signal_close = (prices.get(ticker) or {}).get("close")
        if not signal_close:
            continue
        for agent, (key, field, _sign) in _FORECASTS.items():
            v = (pipeline_state.get(key, {}).get(ticker) or {}).get(field)
            if isinstance(v, (int, float)):
                for h in horizons:
                    rows.append({
                        "run_id": run_id, "date": date_str, "agent": agent, "field": field,
                        "ticker": ticker, "value": float(v),
                        "signal_close": float(signal_close), "horizon_days": int(h),
                        "schema": SCHEMA_VERSION, **prov,
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
    selected = {d.get("ticker") for d in (pipeline_state.get("final_decisions", []) or [])
                if str(d.get("action", "")).upper() == "BUY"}
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
                rows.append({
                    "run_id": run_id, "date": date_str, "agent": agent, "field": "flag",
                    "ticker": t, "value": float(val), "signal_close": float(signal_close),
                    "horizon_days": int(h), "schema": SCHEMA_VERSION,
                })
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


def _norm_sf(z: float) -> float:
    """Two-sided survival probability 2·(1−Φ(|z|)) via erf — no scipy dependency."""
    return math.erfc(abs(z) / math.sqrt(2))


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


def _ic_hit(rows: list, sign: int):
    """(ic, hit_rate, n) over rows carrying numeric value + realized_return."""
    vals = [x["value"] for x in rows if isinstance(x.get("value"), (int, float))]
    rets = [x["realized_return"] for x in rows if isinstance(x.get("realized_return"), (int, float))]
    n = min(len(vals), len(rets))
    if n == 0:
        return None, None, 0
    vals, rets = vals[:n], rets[:n]
    ic = _spearman(vals, rets)
    mean = sum(vals) / n
    hits = sum(1 for v, rr in zip(vals, rets) if (sign * (v - mean)) * rr > 0)
    return ic, round(hits / n, 3), n


def agent_scorecard(scored_path: str = SCORED, out_path: str = SCORECARD,
                    shrink_k: int = 50) -> dict:
    """Per (agent.field): raw + block-sampled IC, hit-rate, shrunk IC, effective-N
    CI, and a Benjamini-Hochberg-adjusted p-value. Plus a `_meta` block naming the
    pre-registered primary metric.

    `ic_shrunk = ic · n/(n+shrink_k)` pulls small samples toward 0 (no skill). The
    *honest* significance read is `ic_block` / `p_value_bh` on the block sample
    (A2/A3): the raw `ic` over overlapping daily windows overstates precision.
    Nothing consumes this to size trades yet — it is a scoreboard, gated behind
    sample size (future work).
    """
    # Group by (agent, field, HORIZON) so each horizon gets its own IC — the IC curve
    # across horizons is the whole point of multi-horizon (§7.3.2). Pooling horizons
    # would average incomparable return windows.
    groups: dict = {}
    for r in _iter_jsonl(scored_path):
        h = int(r["horizon_days"]) if isinstance(r.get("horizon_days"), (int, float)) else DEFAULT_HORIZON
        groups.setdefault((r.get("agent"), r.get("field"), h), []).append(r)

    orient = {agent: sign for agent, (_k, _f, sign) in _FORECASTS.items()}
    card: dict = {}
    for (agent, field, horizon), rows in groups.items():
        sgn = orient.get(agent, +1)

        ic, hit, n = _ic_hit(rows, sgn)                       # raw (overlapping)
        if n == 0:
            continue
        block = _block_sample(rows, horizon)                  # A2: effective sample
        ic_b, hit_b, n_b = _ic_hit(block, sgn)
        n_eff = max(1, round(n / horizon)) if horizon else n  # quick scalar effective N

        card[f"{agent}.{field}@{horizon}d"] = {
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
            "is_primary":   (agent, field) == PRIMARY_METRIC and horizon == PRIMARY_HORIZON,
        }

    # A3: Benjamini-Hochberg across all metrics that produced a p-value.
    pvals = [(k, card[k]["p_value"]) for k in card if card[k].get("p_value") is not None]
    m = len(pvals)
    for k in card:
        card[k]["p_value_bh"] = None
        card[k]["significant_bh"] = None
    if m:
        pvals.sort(key=lambda x: x[1])
        adj = []
        for rank, (k, p) in enumerate(pvals, start=1):
            adj.append((k, min(1.0, p * m / rank)))
        # step-up monotonicity: a smaller-rank q can't exceed a larger-rank q
        for i in range(len(adj) - 2, -1, -1):
            adj[i] = (adj[i][0], min(adj[i][1], adj[i + 1][1]))
        for k, q in adj:
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
    }

    with open(out_path, "w") as f:
        json.dump(card, f, indent=2)
    return card


def counterfactual_report(scored_path: str = DECISIONS_SCORED,
                          out_path: str = COUNTERFACTUAL, min_n: int = 10) -> dict:
    """§7.5 — per (signal, horizon): mean forward return of FLAGGED (value=1) vs KEPT
    (value=0) names, the gap, and whether the model added value in its expected direction.

    For da_reject / cro_veto the model adds value when flagged names UNDERPERFORM
    (gap = mean_kept − mean_flagged > 0); for pm_selected when selected names OUTPERFORM
    (gap < 0). Uses the block-sampled effective sample (A2) so overlapping windows don't
    inflate the count, and reports NOT_SIGNIFICANT until each side clears `min_n` — at
    ~weekly cadence this stays NOT_SIGNIFICANT for months (plan §7.4), by design.
    """
    groups: dict = {}
    for r in _iter_jsonl(scored_path):
        h = int(r["horizon_days"]) if isinstance(r.get("horizon_days"), (int, float)) else DEFAULT_HORIZON
        groups.setdefault((r.get("agent"), h), []).append(r)

    report: dict = {}
    for (agent, horizon), rows in groups.items():
        block = _block_sample(rows, horizon)
        flagged = [r["realized_return"] for r in block
                   if r.get("value") == 1.0 and isinstance(r.get("realized_return"), (int, float))]
        kept = [r["realized_return"] for r in block
                if r.get("value") == 0.0 and isinstance(r.get("realized_return"), (int, float))]
        if not flagged or not kept:
            continue
        mf, mk = sum(flagged) / len(flagged), sum(kept) / len(kept)
        gap = round(mk - mf, 5)                      # kept minus flagged
        direction = _CF_SIGNALS.get(agent, "underperform")
        adds_value = (gap > 0) if direction == "underperform" else (gap < 0)
        significant = len(flagged) >= min_n and len(kept) >= min_n
        report[f"{agent}@{horizon}d"] = {
            "n_flagged": len(flagged), "n_kept": len(kept),
            "mean_return_flagged": round(mf, 5), "mean_return_kept": round(mk, 5),
            "gap_kept_minus_flagged": gap, "expected_direction": direction,
            "adds_value": bool(adds_value), "significant": bool(significant),
            "verdict": ("NOT_SIGNIFICANT" if not significant
                        else ("ADDS_VALUE" if adds_value else "NO_VALUE")),
        }
    report["_meta"] = {
        "note": ("Counterfactual: does each model's reject/veto/select decision predict "
                 "the right forward-return direction? gap = mean_kept − mean_flagged. "
                 "Block-sampled effective N; NOT_SIGNIFICANT until both sides >= "
                 f"{min_n} (months at this cadence — §7.4)."),
        "signals": _CF_SIGNALS,
    }
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)
    return report
