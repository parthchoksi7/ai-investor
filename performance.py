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
import os
from collections import defaultdict, deque
from datetime import datetime, timezone

AGENT_LOG    = "agent_log.json"
SNAPSHOT     = "market_snapshot.json"
TRANSACTIONS = "transactions.json"
PORTFOLIO    = "mcp_portfolio.json"
REPORT       = "performance_report.json"

TRADING_DAYS = 252

# California top-bracket combined marginal rates on trading gains (taxable
# account). Short-term = ordinary income (37% fed + 3.8% NIIT + 13.3% CA);
# long-term = 20% + 3.8% + 13.3% (CA gives no preferential cap-gains rate).
# These are estimates for a scorecard, not tax advice — see caveats.
CA_SHORT_TERM_RATE = 0.54
CA_LONG_TERM_RATE  = 0.371
LONG_TERM_DAYS     = 365      # held > 365 days → long-term
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
    return {
        "cumulative_return": round(curve[-1] / curve[0] - 1, 4),
        "max_drawdown":      round(mdd, 4),
        "annualized_vol":    round(ann_vol, 4),
        "sharpe":            round(sharpe, 2) if sharpe is not None else None,
    }


# ── report ──────────────────────────────────────────────────────────────────

def build_report(agent_log_path: str = AGENT_LOG,
                 snapshot_path: str = SNAPSHOT) -> dict:
    portfolio = _portfolio_curve(agent_log_path)
    spy       = _spy_curve(snapshot_path)
    dates, pv, sv = _align(portfolio, spy)

    port_m = _metrics(pv)
    spy_m  = _metrics(sv)
    spread = None
    if pv and sv:
        spread = round(port_m["cumulative_return"] - spy_m["cumulative_return"], 4)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inception":    dates[0] if dates else None,
        "as_of":        dates[-1] if dates else None,
        "trading_days": len(dates),
        "caveats": [
            "SPY is PRICE return (no dividends) — understates the index ~1.3%/yr.",
            "Portfolio figure includes cash drag.",
            "Few trading days of history — Sharpe is not yet meaningful.",
        ],
        "portfolio": port_m,
        "spy":       spy_m,
        "alpha_cumulative_return": spread,   # portfolio minus SPY, both price-return
        "portfolio_curve": [{"date": d, "value": round(v, 2)} for d, v in zip(dates, pv)],
    }


def _fmt_pct(x) -> str:
    return f"{x:+.2%}" if isinstance(x, (int, float)) else "n/a"


def print_report(report: dict) -> None:
    print("\n" + "=" * 60)
    print("📊  PERFORMANCE — Portfolio vs SPY (local, price-return)")
    print("=" * 60)
    print(f"   Inception: {report['inception']}  →  As of: {report['as_of']}  "
          f"({report['trading_days']} trading day(s))")
    for c in report["caveats"]:
        print(f"   ⚠  {c}")
    print(f"\n   {'metric':<22}{'PORTFOLIO':>14}{'SPY':>14}")
    rows = [
        ("Cumulative return", "cumulative_return", True),
        ("Max drawdown",      "max_drawdown",      True),
        ("Annualized vol",    "annualized_vol",    True),
        ("Sharpe (rf=0)",     "sharpe",            False),
    ]
    for label, key, is_pct in rows:
        p, s = report["portfolio"].get(key), report["spy"].get(key)
        fmt = _fmt_pct if is_pct else (lambda x: f"{x:>.2f}" if isinstance(x, (int, float)) else "n/a")
        print(f"   {label:<22}{fmt(p):>14}{fmt(s):>14}")
    print(f"\n   Alpha (cumulative, vs SPY): {_fmt_pct(report['alpha_cumulative_return'])}")
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

    # IRS-style netting: net ST and LT separately, then a net loss in one term
    # offsets a net gain in the other before tax; remaining net loss carries
    # forward. (The $3k/yr ordinary-income offset cap, cross-YEAR carryover, and
    # wash-sale disallowance are not modeled.)
    carryforward = 0.0
    if st >= 0 and lt >= 0:
        tax = st * CA_SHORT_TERM_RATE + lt * CA_LONG_TERM_RATE
    elif st < 0 and lt < 0:
        tax = 0.0
        carryforward = -(st + lt)
    elif st < 0 <= lt:                  # ST loss offsets LT gain
        net = lt + st
        tax = max(0.0, net) * CA_LONG_TERM_RATE
        carryforward = -min(0.0, net)
    else:                               # lt < 0 <= st: LT loss offsets ST gain
        net = st + lt
        tax = max(0.0, net) * CA_SHORT_TERM_RATE
        carryforward = -min(0.0, net)

    tax = round(tax, 2)
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

    # Strategy vs SPY-hold from the equity curve.
    dates, pv, sv = _align(_portfolio_curve(agent_log_path), _spy_curve(snapshot_path))
    have_curve = len(pv) >= 2 and pv[0]
    strat_ret = round(pv[-1] / pv[0] - 1, 4) if have_curve else None
    spy_ret   = round(sv[-1] / sv[0] - 1, 4) if (len(sv) >= 2 and sv[0]) else None

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
            "SPY is price-return (no dividends). Unrealized gains are untaxed "
            "(deferred), matching the SPY buy-and-hold alternative.",
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
