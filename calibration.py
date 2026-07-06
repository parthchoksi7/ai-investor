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


def _welch_p(a: list, b: list):
    """Two-sided p-value for H0: mean(a) == mean(b), via Welch's z-approximation
    (unequal variances, no scipy). None if either sample has < 2 observations or
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
    z = (mb - ma) / se
    return round(min(1.0, _norm_sf(z)), 4)


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
    inflate the count.

    `significant` requires BOTH `min_n` on each side AND a real two-sample test
    (Welch's z-approximation, `_welch_p`) clearing BH_ALPHA — matching the rigor
    agent_scorecard applies to the forecast metrics next to it (Phase 1, 2026-07-05).
    Before this fix, `significant` was `n_flagged >= min_n and n_kept >= min_n` alone
    — a sample-size floor with no actual significance test — which could label a
    result "ADDS_VALUE, significant: true" on a difference indistinguishable from
    noise. At ~weekly cadence this stays NOT_SIGNIFICANT for months (plan §7.4), by
    design; `p_value` is still reported once both sides reach `min_n`, so the read
    is honest about HOW insignificant, not just a boolean.
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
        n_floor_met = len(flagged) >= min_n and len(kept) >= min_n
        p_value = _welch_p(flagged, kept)
        significant = n_floor_met and p_value is not None and p_value < BH_ALPHA
        report[f"{agent}@{horizon}d"] = {
            "n_flagged": len(flagged), "n_kept": len(kept),
            "mean_return_flagged": round(mf, 5), "mean_return_kept": round(mk, 5),
            "gap_kept_minus_flagged": gap, "expected_direction": direction,
            "p_value": p_value, "n_floor_met": bool(n_floor_met),
            "adds_value": bool(adds_value), "significant": bool(significant),
            "verdict": ("NOT_SIGNIFICANT" if not significant
                        else ("ADDS_VALUE" if adds_value else "NO_VALUE")),
        }
    report["_meta"] = {
        "note": ("Counterfactual: does each model's reject/veto/select decision predict "
                 "the right forward-return direction? gap = mean_kept − mean_flagged. "
                 "Block-sampled effective N. `significant` requires BOTH sides >= "
                 f"{min_n} AND a Welch's-z two-sample p-value < {BH_ALPHA} (matches "
                 "agent_scorecard's rigor — a sample-size floor alone is not a "
                 "significance test). At ~weekly cadence this stays NOT_SIGNIFICANT "
                 "for months (§7.4), by design."),
        "min_n": min_n,
        "alpha": BH_ALPHA,
        "signals": _CF_SIGNALS,
    }
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)
    return report
