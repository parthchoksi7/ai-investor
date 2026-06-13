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
from datetime import datetime, timezone

AGENT_LOG = "agent_log.json"
SNAPSHOT  = "market_snapshot.json"
REPORT    = "performance_report.json"

TRADING_DAYS = 252


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


def main() -> None:
    report = build_report()
    with open(REPORT, "w") as f:
        json.dump(report, f, indent=2)
    print_report(report)
    print(f"   📄 Written to {REPORT}")


if __name__ == "__main__":
    main()
