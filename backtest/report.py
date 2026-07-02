"""
backtest/report.py — metrics + after-CA-tax return + vs SPY.

Reuses the LIVE lot/tax logic (performance.compute_realized_lots + cost_model)
so the backtest's after-tax number is computed exactly the way the live
scorecard computes it — no second, drifting implementation.
"""

TRADING_DAYS = 252


def _returns(vals: list[float]) -> list[float]:
    return [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals)) if vals[i - 1]]


def _metrics(curve: list[tuple[str, float]]) -> dict:
    """CAGR, total return, annualized vol, Sharpe (rf=0), max drawdown."""
    vals = [v for _, v in curve]
    if len(vals) < 2 or vals[0] <= 0:
        return {"total_return": 0.0, "cagr": 0.0, "annualized_vol": None,
                "sharpe": None, "max_drawdown": 0.0}
    rets  = _returns(vals)
    years = len(vals) / TRADING_DAYS
    total = vals[-1] / vals[0] - 1
    cagr  = (vals[-1] / vals[0]) ** (1 / years) - 1 if years > 0 else 0.0
    mean  = sum(rets) / len(rets) if rets else 0.0
    sd    = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5 if rets else 0.0
    sharpe = (mean / sd) * (TRADING_DAYS ** 0.5) if sd > 0 else None
    peak, mdd = vals[0], 0.0
    for v in vals:
        peak = max(peak, v)
        mdd  = min(mdd, v / peak - 1)
    return {
        "total_return":   round(total, 4),
        "cagr":           round(cagr, 4),
        "annualized_vol": round(sd * (TRADING_DAYS ** 0.5), 4),
        "sharpe":         round(sharpe, 2) if sharpe is not None else None,
        "max_drawdown":   round(mdd, 4),
    }


def build_report(result: dict) -> dict:
    from performance import compute_realized_lots   # reuse the live FIFO lot logic
    from cost_model import tax_on_realized

    strat = _metrics(result["equity_curve"])
    bench = _metrics(result["benchmark_curve"]) if result.get("benchmark_curve") else None

    realized, _unc = compute_realized_lots(result["transactions"])
    st  = sum(l["gain"] for l in realized if l["term"] == "ST")
    lt  = sum(l["gain"] for l in realized if l["term"] == "LT")
    tax, _cf = tax_on_realized(st, lt)

    init  = result["initial_capital"]
    final = result["final_equity"]
    after_tax_final = round(final - tax, 2)
    strat_after_tax_total = round(after_tax_final / init - 1, 4) if init else None

    days   = len(result["equity_curve"])
    avg_eq = (sum(v for _, v in result["equity_curve"]) / days) if days else init
    years  = days / TRADING_DAYS if days else 1
    turnover = round((result["traded_notional_total"] / avg_eq) / years, 2) if (avg_eq and years) else None

    spy_total = bench["total_return"] if bench else None

    from quant_engine import FORMULA_VERSION
    coverage = result.get("fundamental_coverage_pct")

    caveats = [
        "Quant-only (no LLM) — this IS the quant-only shadow arm (IPS §3.3 baseline); "
        "the LLM book is forward-tested against it, not backtested.",
        "SPY is price-return (no dividends). Costs = effective spread + slippage (cost_model).",
        "After-tax subtracts CA tax on realized gains (mostly short-term in a <1yr window); "
        "unrealized gains untaxed (deferred), matching the SPY-hold alternative.",
        "SURVIVORSHIP BIAS: the universe is only tickers in today's snapshot (no delisted/"
        "bankrupt names) and is FIXED over the whole window — this biases returns upward. "
        "A point-in-time universe is needed before trusting the absolute numbers.",
        "~10 months of bars from a single snapshot — short sample; not yet a skill claim.",
    ]
    if isinstance(coverage, (int, float)) and coverage < 80.0:
        caveats.insert(0,
            f"⛔ RE-WEIGHT NOT FAIRLY TESTED: fundamental coverage is {coverage}% (< 80% floor), "
            f"so the quality/valuation tilt in formula {FORMULA_VERSION} cannot express — most "
            f"names score momentum+vol only. Re-run once GH Actions coverage clears the floor "
            f"(plan §9-3) before drawing any verdict on the re-weight.")

    return {
        "strategy":  strat,
        "spy":       bench,
        "formula_version":             FORMULA_VERSION,
        "fundamental_coverage_pct":    coverage,
        "alpha_total_return":          round(strat["total_return"] - spy_total, 4) if spy_total is not None else None,
        "realized_gain":               round(st + lt, 2),
        "short_term_gain":             round(st, 2),
        "long_term_gain":              round(lt, 2),
        "tax_estimate":                tax,
        "after_tax_final_equity":      after_tax_final,
        "strategy_return_after_tax":   strat_after_tax_total,
        "after_tax_alpha_vs_spy":      round(strat_after_tax_total - spy_total, 4)
                                       if (strat_after_tax_total is not None and spy_total is not None) else None,
        "annualized_turnover":         turnover,
        "n_trades":                    len(result["transactions"]),
        "trading_days":                days,
        "final_equity":                round(final, 2),
        "initial_capital":             round(init, 2),
        "caveats":                     caveats,
    }


def _pct(x):
    return f"{x:+.2%}" if isinstance(x, (int, float)) else "n/a"


def print_report(rep: dict) -> None:
    print("\n" + "=" * 60)
    print("🧪  BACKTEST — quant-only SHADOW ARM vs SPY (after CA tax)")
    print("=" * 60)
    cov = rep.get("fundamental_coverage_pct")
    print(f"   formula {rep.get('formula_version', '?')} | "
          f"fundamental coverage {cov if cov is not None else '?'}%")
    print(f"   {rep['trading_days']} trading days | {rep['n_trades']} trades | "
          f"ann. turnover {rep['annualized_turnover']}x | "
          f"${rep['initial_capital']:,.0f} → ${rep['final_equity']:,.0f}")
    s, b = rep["strategy"], rep["spy"] or {}
    print(f"\n   {'metric':<22}{'STRATEGY':>14}{'SPY':>14}")
    for label, key, pct in [("Total return", "total_return", True), ("CAGR", "cagr", True),
                            ("Annualized vol", "annualized_vol", True),
                            ("Sharpe (rf=0)", "sharpe", False), ("Max drawdown", "max_drawdown", True)]:
        sv, bv = s.get(key), b.get(key)
        fmt = _pct if pct else (lambda x: f"{x:>.2f}" if isinstance(x, (int, float)) else "n/a")
        print(f"   {label:<22}{fmt(sv):>14}{fmt(bv):>14}")
    print(f"\n   Gross alpha vs SPY:        {_pct(rep['alpha_total_return'])}")
    print(f"   Realized gain:             ${rep['realized_gain']:,.2f} "
          f"(ST ${rep['short_term_gain']:,.0f} / LT ${rep['long_term_gain']:,.0f})")
    print(f"   Est. CA tax on realized:   ${rep['tax_estimate']:,.2f}")
    print(f"   Strategy return AFTER tax: {_pct(rep['strategy_return_after_tax'])}")
    print(f"   After-tax alpha vs SPY:    {_pct(rep['after_tax_alpha_vs_spy'])}")
    for c in rep["caveats"]:
        print(f"   ⚠  {c}")
    print("=" * 60 + "\n")
