"""
cost_model.py — Shared cost & tax model (the spine for the backtest and the
live net-edge gate). Single source of truth so the simulated and live
economics can never drift apart.

Centralizes:
  - California top-bracket tax rates + IRS-style ST/LT netting.
  - A transaction-cost estimate (effective spread + vol-scaled slippage).
  - net_edge(): gross expected return minus round-trip cost and short-term tax —
    the "is this trade worth it after CA tax + friction?" number.

Consumers:
  - performance.after_tax_scorecard  → realized tax (live reporting; imports the
    rates + tax_on_realized here instead of redefining them).
  - backtest/                        → after-cost, after-tax simulation (P1).
  - guardrails net-edge gate         → #6 / P5 (future).

All rates are ESTIMATES for a scorecard/backtest, not tax advice. See the
caveats in performance.after_tax_scorecard.
"""

# ── California top-bracket combined marginal rates on trading gains (taxable) ──
# Short-term = ordinary income (37% fed + 3.8% NIIT + 13.3% CA); long-term =
# 20% + 3.8% + 13.3% (CA gives no preferential cap-gains rate).
CA_SHORT_TERM_RATE = 0.54
CA_LONG_TERM_RATE  = 0.371
LONG_TERM_DAYS     = 365      # held > 365 days → long-term

# ── Transaction-cost model ────────────────────────────────────────────────────
# Robinhood is commission-free, so the real costs are effective spread + slippage.
# For liquid mega-caps the round-trip effective spread is a few bps; slippage
# scales with volatility. These are deliberately conservative and tunable; the
# backtest and any live gate share them so simulated cost == modeled live cost.
ROUND_TRIP_SPREAD_BPS   = 3.0    # round-trip effective spread, basis points
SLIPPAGE_BPS_PER_VOL    = 2.0    # additional bps per 1.0 of annualized vol (fraction)


def tax_on_realized(short_term_gain: float, long_term_gain: float) -> tuple[float, float]:
    """CA top-bracket tax on realized ST/LT gains, with IRS-style netting.

    Nets ST and LT separately, then a net loss in one term offsets a net gain in
    the other before tax; remaining net loss carries forward. (The $3k/yr
    ordinary-income offset cap, cross-YEAR carryover, and wash-sale disallowance
    are NOT modeled.) Returns (tax, loss_carryforward), both rounded to cents.
    """
    st, lt = float(short_term_gain), float(long_term_gain)
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
    return round(tax, 2), round(carryforward, 2)


def round_trip_cost(notional: float,
                    annualized_vol: float | None = None,
                    spread_bps: float = ROUND_TRIP_SPREAD_BPS,
                    slippage_bps_per_vol: float = SLIPPAGE_BPS_PER_VOL) -> float:
    """Estimated $ cost of a round trip (buy + later sell) of `notional` dollars.

    `annualized_vol` is a FRACTION (0.30 = 30%); pass None to skip slippage.
    Cost = notional × (spread_bps + slippage_bps_per_vol × vol) / 10_000.
    """
    notional = abs(float(notional))
    bps = spread_bps
    if annualized_vol:
        bps += slippage_bps_per_vol * float(annualized_vol)
    return round(notional * bps / 1e4, 4)


def net_edge(expected_return: float,
             notional: float,
             annualized_vol: float | None = None,
             short_term: bool = True) -> dict:
    """Net $ edge of a trade after round-trip cost and CA tax on any gain.

    `expected_return` is a gross fraction (0.02 = 2%). Tax applies only to a
    positive post-cost gain; `short_term=True` (the account's churn default)
    taxes it at the ST rate. Returns {gross, cost, tax, net, net_return}.
    """
    notional = float(notional)
    gross    = float(expected_return) * notional
    cost     = round_trip_cost(notional, annualized_vol)
    pre_tax  = gross - cost
    if pre_tax > 0:
        rate = CA_SHORT_TERM_RATE if short_term else CA_LONG_TERM_RATE
        tax = round(pre_tax * rate, 4)
    else:
        tax = 0.0
    net = round(pre_tax - tax, 4)
    return {
        "gross":      round(gross, 4),
        "cost":       cost,
        "tax":        tax,
        "net":        net,
        "net_return": round(net / notional, 6) if notional else 0.0,
    }
