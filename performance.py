"""
performance.py — local portfolio-vs-SPY performance report (no Supabase needed).

`publish.py` pushes a portfolio-vs-SPY cumulative comparison to Supabase, but
it's price-return SPY, carries no risk-adjusted metrics, and needs Supabase
access (blocked in the Anthropic cloud). This module builds the same comparison
locally from files already in the repo and adds max drawdown / annualized vol /
Sharpe.

Sources (both already committed):
  - agent_log.json      → portfolio equity curve (each run's
                          portfolio_snapshot.total_value, keyed by date).
  - market_snapshot.json → SPY daily closes (SPY is in the universe), aligned to
                          the portfolio's date range.

Run ad-hoc:  python performance.py
Output:      prints a table and writes performance_report.json

Honesty caveats (printed in the report header):
  * SPY here is PRICE return, not total return — it excludes dividends and
    understates the index by roughly ~1.3%/yr.
  * The portfolio figure includes cash drag.
  * With only a handful of trading days of history, Sharpe is not yet meaningful.
"""

import json
import math
import os
from collections import defaultdict, deque
from datetime import datetime, timezone

AGENT_LOG    = "agent_log.json"
SNAPSHOT     = "market_snapshot.json"
TRANSACTIONS = "transactions.json"
PORTFOLIO    = "mcp_portfolio.json"
REPORT       = "performance_report.json"

TRADING_DAYS = 252

# A4 — SPY TOTAL return, not price return. The portfolio curve is the brokerage
# account's total value, which captures dividends paid in as cash → it is a
# total-return series. Comparing it against a price-return SPY flatters the
# portfolio by ~the dividend yield. We correct this by grossing the SPY price
# series up to a total-return basis (the snapshot only carries raw OHLC, so an
# exact adjusted-close series is unavailable offline; this is a documented,
# directionally-correct estimate that removes the flattering bias). SPY's
# trailing dividend yield is ~1.2–1.3%/yr.
SPY_DIVIDEND_YIELD = 0.0125

# Tax rates + ST/LT netting live in cost_model (single source of truth, shared
# with the backtest and the future net-edge gate). Imported into this namespace
# so performance.CA_SHORT_TERM_RATE / .LONG_TERM_DAYS still resolve.
from cost_model import CA_SHORT_TERM_RATE, CA_LONG_TERM_RATE, LONG_TERM_DAYS, tax_on_realized

MIN_SIGNIFICANT_DAYS = 60     # pre-registered: fewer trading days → "not significant"


# ── curves ──────────────────────────────────────────────────────────────────

def _portfolio_curve(agent_log_path: str = AGENT_LOG) -> list[tuple[str, float]]:
    """date → total_value from agent_log.json, one (last) point per date, sorted."""
    if not os.path.isfile(agent_log_path):
        return []
    with open(agent_log_path) as f:
        log = json.load(f)
    if not isinstance(log, list):
        return []
    by_date: dict[str, float] = {}
    for run in log:
        d  = run.get("date") or (run.get("timestamp") or "")[:10]
        tv = (run.get("portfolio_snapshot") or {}).get("total_value")
        if d and tv:
            by_date[d] = float(tv)   # last run of the day wins
    return sorted(by_date.items())


def _spy_curve(snapshot_path: str = SNAPSHOT) -> dict[str, float]:
    """SPY {iso_date: close} from market_snapshot.json's 210-day history.

    Bars carry an epoch-millisecond `date` (the real snapshot shape — verified).
    """
    if not os.path.isfile(snapshot_path):
        return {}
    with open(snapshot_path) as f:
        snap = json.load(f)
    bars = (snap.get("history", {}) or {}).get("SPY", []) or []
    out: dict[str, float] = {}
    for b in bars:
        raw, close = b.get("date"), b.get("close")
        if raw is None or close is None:
            continue
        # epoch ms → ISO date (UTC); a plain ISO string is taken as-is.
        if isinstance(raw, (int, float)):
            iso = datetime.fromtimestamp(raw / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        else:
            iso = str(raw)[:10]
        out[iso] = float(close)
    return out


def _align(portfolio: list[tuple[str, float]],
           spy_by_date: dict[str, float]) -> tuple[list[str], list[float], list[float]]:
    """Align SPY to the portfolio's dates using an as-of (latest prior) match.

    Returns (dates, portfolio_values, spy_values) over the portfolio dates that
    have a SPY observation on or before them. SPY snapshots only contain trading
    days, so a non-trading portfolio date falls back to the most recent close.
    """
    spy_dates = sorted(spy_by_date)
    dates, pv, sv = [], [], []
    for d, val in portfolio:
        prior = [sd for sd in spy_dates if sd <= d]
        if not prior:
            continue  # portfolio point predates available SPY history
        dates.append(d)
        pv.append(val)
        sv.append(spy_by_date[prior[-1]])
    return dates, pv, sv


# ── A4: total-return SPY, net exposure, realized beta ─────────────────────────

def _spy_total_return(dates: list[str], sv: list[float],
                      div_yield: float = SPY_DIVIDEND_YIELD) -> list[float]:
    """Gross a price-return SPY series up to a TOTAL-return basis (A4).

    tr(t) = price(t) · (1 + div_yield · days_since_inception/365). This adds the
    accrued dividend the price series omits, so the SPY benchmark is measured on
    the same dividend-inclusive basis as the portfolio's total-value curve. A
    documented estimate (exact adjusted close is unavailable from the raw-OHLC
    snapshot), but it removes the directional bias rather than ignoring it."""
    if not dates or not sv:
        return list(sv)
    try:
        d0 = datetime.strptime(dates[0][:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return list(sv)
    out = []
    for d, v in zip(dates, sv):
        try:
            days = (datetime.strptime(d[:10], "%Y-%m-%d").date() - d0).days
        except (ValueError, TypeError):
            days = 0
        out.append(v * (1 + div_yield * max(0, days) / 365.0))
    return out


def _avg_net_exposure(agent_log_path: str = AGENT_LOG) -> float | None:
    """Average invested fraction (1 − cash/total_value) across logged runs.

    The book holds cash (8–15 names + dividend/residual cash), so it runs below
    full market exposure; raw return vs a fully-invested SPY is therefore not
    risk-matched. Reporting average net exposure makes that explicit (A4)."""
    if not os.path.isfile(agent_log_path):
        return None
    with open(agent_log_path) as f:
        log = json.load(f)
    if not isinstance(log, list):
        return None
    exps = []
    for run in log:
        snap = run.get("portfolio_snapshot") or {}
        tv, cash = snap.get("total_value"), snap.get("cash")
        if tv and cash is not None and float(tv) > 0:
            exps.append(1.0 - float(cash) / float(tv))
    return round(sum(exps) / len(exps), 4) if exps else None


def cash_drag_report(agent_log_path: str = AGENT_LOG,
                     snapshot_path: str = SNAPSHOT,
                     band_pct: float = 10.0) -> dict | None:
    """Opportunity cost of cash held ABOVE the IPS 0–10% band, vs SPY.

    For each consecutive pair of logged run-dates, the excess cash at the start
    of the period (cash − band_pct%·total, floored at 0) is marked against SPY's
    price return over the period: drag = Σ excess_cash_i × spy_ret_i. Positive
    drag = return left on the table by the over-band cash stance; negative =
    the cash protected capital while SPY fell. Reported so the year-end verdict
    prices the persistent defensive posture (29 consecutive over-band runs
    through Jul 15 2026) instead of silently ignoring it. Returns None when
    fewer than 2 aligned observations exist.
    """
    if not os.path.isfile(agent_log_path):
        return None
    with open(agent_log_path) as f:
        log = json.load(f)
    if not isinstance(log, list):
        return None
    by_date: dict[str, tuple[float, float]] = {}
    for run in log:
        d    = run.get("date") or (run.get("timestamp") or "")[:10]
        snap = run.get("portfolio_snapshot") or {}
        tv, cash = snap.get("total_value"), snap.get("cash")
        if d and tv and cash is not None and float(tv) > 0:
            by_date[d] = (float(cash), float(tv))   # last run of the day wins
    spy = _spy_curve(snapshot_path)
    spy_dates = sorted(spy)
    obs = []                     # (date, excess_cash_$, spy_close_as_of)
    excess_pcts = []             # over the SAME aligned dates the drag covers
    for d in sorted(by_date):
        prior = [sd for sd in spy_dates if sd <= d]
        if not prior:
            continue
        cash, tv = by_date[d]
        excess = max(0.0, cash - band_pct / 100.0 * tv)
        obs.append((d, excess, spy[prior[-1]]))
        excess_pcts.append(max(0.0, cash / tv * 100 - band_pct))
    if len(obs) < 2:
        return None
    total_drag = 0.0
    periods = []
    for (d0, excess, s0), (d1, _, s1) in zip(obs, obs[1:]):
        # A non-finite or non-positive SPY close (NaN closes do occur in the
        # snapshot — see the Jun 16 2026 quant NaN fix) must not poison the sum:
        # `s0 <= 0` is False for NaN, so guard isfinite explicitly or drag → NaN.
        if not (math.isfinite(s0) and math.isfinite(s1)) or s0 <= 0:
            continue
        ret  = s1 / s0 - 1.0
        drag = excess * ret
        total_drag += drag
        periods.append({"from": d0, "to": d1, "excess_cash": round(excess, 2),
                        "spy_return": round(ret, 4), "drag": round(drag, 2)})
    avg_excess_pct = sum(excess_pcts) / len(excess_pcts)
    return {
        "band_pct": band_pct,
        "cumulative_drag": round(total_drag, 2),
        "avg_excess_cash_pct": round(avg_excess_pct, 2),
        "n_periods": len(periods),
        "periods": periods[-10:],   # tail only — full detail reproducible from logs
        "note": ("Drag = Σ over-band cash × SPY price return per inter-run period. "
                 "Positive = cost of the defensive stance; negative = cash was "
                 "protective. SPY price-return basis (no dividend gross-up) — a "
                 "mild UNDERSTATEMENT of the true drag."),
    }


def _beta(pv: list[float], sv: list[float]) -> float | None:
    """Realized beta = cov(rp, rm)/var(rm) on aligned daily returns (A4).

    With only a handful of days this is noisy; reported with a sample-size caveat,
    not as a precise estimate."""
    if len(pv) < 3 or len(sv) < 3:
        return None
    rp = [pv[i] / pv[i - 1] - 1 for i in range(1, len(pv)) if pv[i - 1]]
    rm = [sv[i] / sv[i - 1] - 1 for i in range(1, len(sv)) if sv[i - 1]]
    n = min(len(rp), len(rm))
    if n < 2:
        return None
    rp, rm = rp[:n], rm[:n]
    mp, mm = sum(rp) / n, sum(rm) / n
    var_m = sum((x - mm) ** 2 for x in rm)
    if var_m == 0:
        return None
    cov = sum((rp[i] - mp) * (rm[i] - mm) for i in range(n))
    return round(cov / var_m, 3)


# ── metrics ─────────────────────────────────────────────────────────────────

def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _pstdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5


def _metrics(curve: list[float]) -> dict:
    """Cumulative return, max drawdown, annualized vol, daily-return Sharpe (rf=0)."""
    if len(curve) < 2 or curve[0] == 0:
        return {"cumulative_return": 0.0, "max_drawdown": 0.0,
                "annualized_vol": None, "sharpe": None}
    rets = [curve[i] / curve[i - 1] - 1 for i in range(1, len(curve)) if curve[i - 1]]
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd  = min(mdd, v / peak - 1)
    mean, sd = _mean(rets), _pstdev(rets)
    ann_vol = sd * (TRADING_DAYS ** 0.5)
    sharpe  = (mean / sd) * (TRADING_DAYS ** 0.5) if sd > 0 else None
    # Sortino (§7.6): downside deviation vs a 0 target — penalizes only losses, the
    # honest risk-adjusted read for an asymmetric book. Return alone is insufficient.
    dd = (sum(min(r, 0.0) ** 2 for r in rets) / len(rets)) ** 0.5 if rets else 0.0
    sortino = (mean / dd) * (TRADING_DAYS ** 0.5) if dd > 0 else None
    return {
        "cumulative_return": round(curve[-1] / curve[0] - 1, 4),
        "max_drawdown":      round(mdd, 4),
        "annualized_vol":    round(ann_vol, 4),
        "sharpe":            round(sharpe, 2) if sharpe is not None else None,
        "sortino":           round(sortino, 2) if sortino is not None else None,
    }


# ── §7.6 measurement rigor: TWR, information ratio, breadth ceiling ───────────

def _twr(dates: list[str], pv: list[float], cash_flows: dict | None = None) -> float | None:
    """Time-weighted return — chains sub-period returns, removing the effect of EXTERNAL
    cash flows (deposits/withdrawals) on the day they occur. With no recorded flows this
    EQUALS the simple cumulative return; it diverges only once deposits are logged. This
    is the methodologically-correct fix for the documented 'a deposit inflates total_value
    → a false new peak / wrong return' distortion. `cash_flows` maps an ISO date to the NET
    external flow that day (deposit > 0, withdrawal < 0), applied at period start."""
    if len(pv) < 2 or len(dates) != len(pv):
        return None
    cf = cash_flows or {}
    factor = 1.0
    for i in range(1, len(pv)):
        base = pv[i - 1] + cf.get(dates[i], 0.0)     # capital base after the flow
        if base <= 0:
            continue
        factor *= pv[i] / base
    return round(factor - 1, 4)


def _information_ratio(pv: list[float], bench: list[float]) -> float | None:
    """Annualized active-return / tracking-error vs the benchmark curve (§7.6). Beating
    the benchmark's RETURN while running materially higher vol is not a win — this is the
    risk-adjusted active-management read."""
    if len(pv) < 3 or len(bench) != len(pv):
        return None
    # Pair the two series by index in ONE pass — independent `if pv[i-1]` / `if bench[i-1]`
    # filters can drop a period from only one list and silently MISALIGN the active-return
    # pairing. Include a period only when BOTH bases are non-zero.
    active = [(pv[i] / pv[i - 1] - 1) - (bench[i] / bench[i - 1] - 1)
              for i in range(1, len(pv)) if pv[i - 1] and bench[i - 1]]
    if len(active) < 2:
        return None
    te = _pstdev(active)
    return round((_mean(active) / te) * (TRADING_DAYS ** 0.5), 2) if te > 0 else None


def breadth_ceiling(scorecard_path: str = "agent_scorecards.json") -> dict:
    """Grinold's Fundamental Law of Active Management: IR ≈ IC × √breadth (§7.6). At
    ~weekly cadence breadth is tiny, so the achievable risk-adjusted outperformance is
    STRUCTURALLY CAPPED even with genuine skill — the honest December conclusion may be
    'the LLM has positive IC but breadth is too low to beat SPY after tax.' Reads the
    pre-registered primary metric's block-IC + effective N from the scorecard."""
    import json
    import os as _os
    if not _os.path.isfile(scorecard_path):
        return {"available": False, "note": "no scorecard yet (clock ticking)"}
    try:
        card = json.load(open(scorecard_path))
    except (ValueError, OSError):
        return {"available": False, "note": "scorecard unreadable"}
    pk = card.get("_meta", {}).get("primary_metric")
    m = card.get(pk, {}) if pk else {}
    ic, n_eff = m.get("ic_block"), m.get("n_effective")
    if ic is None or not n_eff:
        return {"available": False, "primary_metric": pk,
                "note": "primary metric not matured yet (NOT_SIGNIFICANT)"}
    return {
        "available": True, "primary_metric": pk, "ic_block": ic, "effective_breadth": n_eff,
        "implied_ir_ceiling": round(ic * (n_eff ** 0.5), 3),
        "note": ("Fundamental Law IR ≈ IC×√breadth — low breadth caps achievable "
                 "risk-adjusted outperformance even with genuine skill (§7.4/§7.6)."),
    }


# ── report ──────────────────────────────────────────────────────────────────

def build_report(agent_log_path: str = AGENT_LOG,
                 snapshot_path: str = SNAPSHOT) -> dict:
    portfolio = _portfolio_curve(agent_log_path)
    spy       = _spy_curve(snapshot_path)
    dates, pv, sv = _align(portfolio, spy)

    # A4: total-return SPY (dividend-inclusive, same basis as the portfolio's
    # total-value curve) is the honest benchmark; price-return is kept for
    # transparency. Alpha is reported against TOTAL return.
    sv_tr = _spy_total_return(dates, sv)

    port_m   = _metrics(pv)
    spy_m    = _metrics(sv)        # price return (reference)
    spy_tr_m = _metrics(sv_tr)     # total return (headline benchmark)

    net_exposure = _avg_net_exposure(agent_log_path)
    beta = _beta(pv, sv_tr)

    # §7.6: time-weighted return (deposit-neutral) + risk-adjusted active read vs SPY-TR.
    twr = _twr(dates, pv)
    info_ratio = _information_ratio(pv, sv_tr)

    alpha_tr = alpha_pr = None
    if pv and sv:
        alpha_pr = round(port_m["cumulative_return"] - spy_m["cumulative_return"], 4)
        alpha_tr = round(port_m["cumulative_return"] - spy_tr_m["cumulative_return"], 4)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inception":    dates[0] if dates else None,
        "as_of":        dates[-1] if dates else None,
        "trading_days": len(dates),
        "time_weighted_return":  twr,                 # §7.6 — deposit-neutral; == cumulative until a flow is logged
        "information_ratio":     info_ratio,          # §7.6 — risk-adjusted active return vs SPY-TR
        "breadth_ceiling":       breadth_ceiling(),   # §7.6 — Grinold IR ≈ IC×√breadth
        "tax_reconciliation":    {                    # §7.6 — internal estimate; broker is authoritative
            "status": "UNRECONCILED",
            "note": ("After-tax realized gain (cost_model/tax_lots) is an ESTIMATE. The "
                     "broker's realized P&L / 1099 is authoritative — reconcile quarterly "
                     "and at year-end before the headline after-tax figure is trusted."),
        },
        "verdict_scope": (         # §7.6 three-clocks: what the current window can/can't conclude
            "Three clocks run at different speeds: the 12-month evaluation window, the "
            "9-12mo holding horizon, and the 1-2yr (or never) validation power for per-name "
            "LLM IC. The first 252-day forecasts mature ~month 12, so any near-term verdict "
            "rests on the quant/shadow arm and shorter horizons, NOT validated LLM IC."),
        "caveats": [
            f"SPY is reported on a TOTAL-return basis (price + ~{SPY_DIVIDEND_YIELD:.2%}/yr "
            "dividend gross-up), matching the portfolio's dividend-inclusive total-value "
            "curve. Price-return SPY is kept as a reference only.",
            f"Average net exposure ≈ {net_exposure if net_exposure is not None else 'n/a'} "
            "(book holds cash); raw return vs a fully-invested SPY is NOT risk-matched. "
            "See realized beta.",
            "Few trading days of history — Sharpe and beta are not yet meaningful.",
        ],
        "portfolio":        port_m,
        "spy":              spy_m,        # price return (reference)
        "spy_total_return": spy_tr_m,     # total return (headline)
        "net_exposure":     net_exposure,
        "realized_beta":    beta,
        "cash_drag":        cash_drag_report(agent_log_path, snapshot_path),
        "alpha_cumulative_return":           alpha_tr,   # vs SPY TOTAL return (headline)
        "alpha_cumulative_return_vs_price":  alpha_pr,   # vs price return (reference)
        "portfolio_curve": [{"date": d, "value": round(v, 2)} for d, v in zip(dates, pv)],
    }


def _fmt_pct(x) -> str:
    return f"{x:+.2%}" if isinstance(x, (int, float)) else "n/a"


def print_report(report: dict) -> None:
    print("\n" + "=" * 60)
    print("📊  PERFORMANCE — Portfolio vs SPY (local; SPY total-return + price-return)")
    print("=" * 60)
    print(f"   Inception: {report['inception']}  →  As of: {report['as_of']}  "
          f"({report['trading_days']} trading day(s))")
    for c in report["caveats"]:
        print(f"   ⚠  {c}")
    print(f"\n   {'metric':<22}{'PORTFOLIO':>14}{'SPY (TR)':>14}{'SPY (PR)':>14}")
    rows = [
        ("Cumulative return", "cumulative_return", True),
        ("Max drawdown",      "max_drawdown",      True),
        ("Annualized vol",    "annualized_vol",    True),
        ("Sharpe (rf=0)",     "sharpe",            False),
    ]
    for label, key, is_pct in rows:
        p  = report["portfolio"].get(key)
        tr = report.get("spy_total_return", {}).get(key)
        pr = report["spy"].get(key)
        fmt = _fmt_pct if is_pct else (lambda x: f"{x:>.2f}" if isinstance(x, (int, float)) else "n/a")
        print(f"   {label:<22}{fmt(p):>14}{fmt(tr):>14}{fmt(pr):>14}")
    print(f"\n   Net exposure (avg):  {report.get('net_exposure')}")
    print(f"   Realized beta:       {report.get('realized_beta')}")
    print(f"   Alpha (vs SPY total return): {_fmt_pct(report['alpha_cumulative_return'])}")
    print(f"   Alpha (vs SPY price return): {_fmt_pct(report.get('alpha_cumulative_return_vs_price'))}")
    print("=" * 60 + "\n")


# ── after-tax scorecard ──────────────────────────────────────────────────────
#
# The one number that matters for a CA top-bracket TAXABLE account: net return
# AFTER short-term tax, vs just holding SPY in the same account (which defers all
# tax). Realized gain and after-tax realized gain are tracked SEPARATELY so the
# ~54% short-term drag is visible the moment a round-trip closes.

def _days_between(buy_date, sell_date) -> int:
    try:
        a = datetime.strptime(str(buy_date)[:10], "%Y-%m-%d").date()
        b = datetime.strptime(str(sell_date)[:10], "%Y-%m-%d").date()
        return (b - a).days
    except (ValueError, TypeError):
        return 0


def _load_transactions(path: str = TRANSACTIONS) -> list:
    if not os.path.isfile(path):
        return []
    with open(path) as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def _load_portfolio(path: str = PORTFOLIO) -> dict:
    if not os.path.isfile(path):
        return {}
    with open(path) as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def compute_realized_lots(transactions: list) -> tuple[list[dict], list[dict]]:
    """FIFO-match SELLs against prior BUYs. Returns (realized_lots, uncovered).

    realized_lots: one entry per matched (buy lot → sell) slice, carrying qty,
      buy/sell price+date, holding_days, term ('ST'|'LT'), and gain ($).
    uncovered:     SELL quantity with no in-log BUY to source a cost basis
      (a position opened before transaction logging began). Reported, never
      guessed — fabricating a basis would corrupt the realized-gain number.

    dry_run rows are excluded; rows are processed in timestamp order so a SELL
    only ever consumes lots bought before it.
    """
    txs = sorted(
        (t for t in transactions if not t.get("dry_run")),
        key=lambda t: (t.get("timestamp") or t.get("date") or ""),
    )
    lots: dict[str, deque] = defaultdict(deque)   # ticker → deque of [qty, price, date]
    realized, uncovered = [], []

    for t in txs:
        action = str(t.get("action", "")).upper()
        ticker = t.get("ticker", "")
        qty    = float(t.get("qty") or 0)
        price  = float(t.get("price") or 0)
        date   = t.get("date") or (t.get("timestamp") or "")[:10]
        if qty <= 0:
            continue

        if action == "BUY":
            lots[ticker].append([qty, price, date])
        elif action == "SELL":
            remaining = qty
            while remaining > 1e-9 and lots[ticker]:
                lot  = lots[ticker][0]
                take = min(remaining, lot[0])
                holding = _days_between(lot[2], date)
                realized.append({
                    "ticker":       ticker,
                    "qty":          round(take, 6),
                    "buy_price":    lot[1],
                    "sell_price":   price,
                    "buy_date":     lot[2],
                    "sell_date":    date,
                    "holding_days": holding,
                    "term":         "LT" if holding > LONG_TERM_DAYS else "ST",
                    "gain":         round((price - lot[1]) * take, 2),
                })
                lot[0]    -= take
                remaining -= take
                if lot[0] <= 1e-9:
                    lots[ticker].popleft()
            if remaining > 1e-9:
                uncovered.append({"ticker": ticker, "qty": round(remaining, 6), "sell_date": date})

    return realized, uncovered


def realized_summary(realized_lots: list[dict]) -> dict:
    """Aggregate realized lots into pre-tax and AFTER-tax realized gain (separate).

    Tax model (estimate): short-term and long-term net gains taxed at their CA
    combined marginal rate; a net LOSS in a term is not monetized here — it is
    surfaced as `loss_carryforward`. Cross-term and cross-year offsets are NOT
    modeled (documented simplification).
    """
    st = round(sum(l["gain"] for l in realized_lots if l["term"] == "ST"), 2)
    lt = round(sum(l["gain"] for l in realized_lots if l["term"] == "LT"), 2)
    realized_pretax = round(st + lt, 2)

    # ST/LT netting lives in cost_model (shared with the backtest / live gate).
    tax, carryforward = tax_on_realized(st, lt)
    return {
        "realized_gain_pretax":    realized_pretax,
        "short_term_gain":         st,
        "long_term_gain":          lt,
        "realized_tax_estimate":   tax,
        "realized_gain_after_tax": round(realized_pretax - tax, 2),
        "loss_carryforward":       round(carryforward, 2),
        "n_realized_lots":         len(realized_lots),
    }


def after_tax_scorecard(transactions: list | None = None,
                        portfolio: dict | None = None,
                        agent_log_path: str = AGENT_LOG,
                        snapshot_path: str = SNAPSHOT) -> dict:
    """The honest scorecard: after-CA-tax net return vs holding SPY in this account.

    Realized gain and after-tax realized gain are reported separately. Unrealized
    gains on open positions are untaxed (deferred — same treatment as the SPY
    buy-and-hold alternative), so the comparison is apples-to-apples.
    """
    if transactions is None:
        transactions = _load_transactions()
    if portfolio is None:
        portfolio = _load_portfolio()

    realized_lots, uncovered = compute_realized_lots(transactions)
    rs = realized_summary(realized_lots)

    unrealized = None
    if portfolio.get("positions") is not None:
        unrealized = round(sum(float(p.get("unrealized_pnl", 0) or 0)
                               for p in portfolio.get("positions", [])), 2)

    # Strategy vs SPY-hold from the equity curve. A4: SPY on a TOTAL-return basis
    # (dividend-inclusive) to match the portfolio's total-value curve — comparing
    # against price-return SPY would flatter the strategy by ~the dividend yield.
    dates, pv, sv = _align(_portfolio_curve(agent_log_path), _spy_curve(snapshot_path))
    sv_tr = _spy_total_return(dates, sv)
    have_curve = len(pv) >= 2 and pv[0]
    strat_ret = round(pv[-1] / pv[0] - 1, 4) if have_curve else None
    spy_ret   = round(sv_tr[-1] / sv_tr[0] - 1, 4) if (len(sv_tr) >= 2 and sv_tr[0]) else None

    current_value = pv[-1] if pv else (portfolio.get("total_value") if portfolio else None)
    # After-tax mark: subtract the (future) tax liability on realized gains.
    after_tax_value = (round(current_value - rs["realized_tax_estimate"], 2)
                       if current_value is not None else None)
    strat_after_tax_ret = (round(after_tax_value / pv[0] - 1, 4)
                           if (after_tax_value is not None and have_curve) else None)
    after_tax_alpha = (round(strat_after_tax_ret - spy_ret, 4)
                       if (strat_after_tax_ret is not None and spy_ret is not None) else None)

    n_days = len(dates)
    return {
        "generated_at":      datetime.now(timezone.utc).isoformat(),
        "inception":         dates[0] if dates else None,
        "as_of":             dates[-1] if dates else None,
        "trading_days":      n_days,
        "not_significant":   n_days < MIN_SIGNIFICANT_DAYS,
        "tax_rates":         {"short_term": CA_SHORT_TERM_RATE, "long_term": CA_LONG_TERM_RATE},
        "realized":          rs,                      # pre-tax AND after-tax, separate
        "uncovered_sells":   uncovered,
        "unrealized_gain_untaxed": unrealized,
        "current_value":     round(current_value, 2) if current_value is not None else None,
        "after_tax_value":   after_tax_value,
        "strategy_return":            strat_ret,
        "strategy_return_after_tax":  strat_after_tax_ret,
        "spy_hold_return":            spy_ret,
        "after_tax_alpha_vs_spy":     after_tax_alpha,
        "caveats": [
            f"Tax estimate uses CA top-bracket rates (ST {CA_SHORT_TERM_RATE:.0%}, "
            f"LT {CA_LONG_TERM_RATE:.1%}); not tax advice. ST/LT netting is modeled "
            "(IRS ordering); the $3k ordinary-income offset cap, cross-YEAR "
            "carryover, and wash-sale disallowance are NOT.",
            "Uncovered SELLs (no in-log cost basis — positions opened before "
            "transaction logging) are excluded from realized gain, not guessed.",
            f"SPY is TOTAL return (price + ~{SPY_DIVIDEND_YIELD:.2%}/yr dividend gross-up), "
            "matching the portfolio's dividend-inclusive curve. Unrealized gains are "
            "untaxed (deferred), matching the SPY buy-and-hold alternative.",
            f"{'NOT STATISTICALLY SIGNIFICANT — ' if n_days < MIN_SIGNIFICANT_DAYS else ''}"
            f"{n_days} trading day(s); needs ≥ {MIN_SIGNIFICANT_DAYS} before any "
            "skill claim. Treat as plumbing, not proof.",
        ],
    }


def _fmt_money(x) -> str:
    return f"${x:,.2f}" if isinstance(x, (int, float)) else "n/a"


def print_after_tax_scorecard(s: dict) -> None:
    print("\n" + "=" * 60)
    print("💸  AFTER-TAX SCORECARD — net of CA tax, vs holding SPY")
    print("=" * 60)
    if s.get("not_significant"):
        print("   🚩 NOT STATISTICALLY SIGNIFICANT — too few days; plumbing, not proof.")
    print(f"   Inception: {s['inception']}  →  As of: {s['as_of']}  "
          f"({s['trading_days']} trading day(s))")
    r = s["realized"]
    print(f"\n   {'Realized gain (pre-tax)':<32}{_fmt_money(r['realized_gain_pretax']):>14}")
    print(f"   {'  short-term / long-term':<32}"
          f"{_fmt_money(r['short_term_gain'])} / {_fmt_money(r['long_term_gain'])}")
    print(f"   {'Est. tax on realized gain':<32}{_fmt_money(-r['realized_tax_estimate']):>14}")
    print(f"   {'Realized gain (AFTER tax)':<32}{_fmt_money(r['realized_gain_after_tax']):>14}")
    if r["loss_carryforward"]:
        print(f"   {'  (loss carryforward)':<32}{_fmt_money(r['loss_carryforward']):>14}")
    print(f"   {'Unrealized gain (untaxed)':<32}{_fmt_money(s['unrealized_gain_untaxed']):>14}")
    if s["uncovered_sells"]:
        tks = ", ".join(sorted({u['ticker'] for u in s['uncovered_sells']}))
        print(f"   ⚠  {len(s['uncovered_sells'])} uncovered SELL(s) excluded (no in-log basis): {tks}")
    print(f"\n   {'Strategy return':<32}{_fmt_pct(s['strategy_return']):>14}")
    print(f"   {'Strategy return (after tax)':<32}{_fmt_pct(s['strategy_return_after_tax']):>14}")
    print(f"   {'SPY buy-and-hold return':<32}{_fmt_pct(s['spy_hold_return']):>14}")
    print(f"   {'After-tax alpha vs SPY':<32}{_fmt_pct(s['after_tax_alpha_vs_spy']):>14}")
    for c in s["caveats"]:
        print(f"   ⚠  {c}")
    print("=" * 60 + "\n")


def main() -> None:
    report = build_report()
    report["after_tax_scorecard"] = after_tax_scorecard()
    with open(REPORT, "w") as f:
        json.dump(report, f, indent=2)
    print_report(report)
    print_after_tax_scorecard(report["after_tax_scorecard"])
    print(f"   📄 Written to {REPORT}")


if __name__ == "__main__":
    main()
