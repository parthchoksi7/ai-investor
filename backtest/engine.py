"""
backtest/engine.py — event-loop backtest over market_snapshot history.

Reuses quant_engine.score_all_tickers UNCHANGED so the backtest scores exactly
what production scores. Signals are computed on close data up to day t; fills
happen at the NEXT day's open (no look-ahead). Costs come from cost_model so
simulated and live economics share one source of truth. No LLM (see __init__).

Data source: the committed market_snapshot.json carries ~210 daily OHLCV bars per
ticker (and grows into a point-in-time archive as it is committed each day).
"""

import json
from bisect import bisect_right
from datetime import datetime, timezone

from quant_engine import score_all_tickers
from cost_model import round_trip_cost

BENCHMARK = "SPY"
EXCLUDE   = {"SPY", "QQQ"}


def _iso(epoch_ms) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def load_snapshot(path: str = "market_snapshot.json") -> dict:
    with open(path) as f:
        return json.load(f)


def _build_index(history: dict) -> dict:
    """Per ticker: sorted bars + a date→bar map, so a slice up to day t is cheap."""
    idx = {}
    for t, bars in history.items():
        bars = sorted(bars, key=lambda b: b["date"])
        idx[t] = {
            "dates":   [b["date"] for b in bars],
            "bars":    bars,
            "by_date": {b["date"]: b for b in bars},
        }
    return idx


def _hist_up_to(idx: dict, t: str, d) -> list:
    info = idx[t]
    k = bisect_right(info["dates"], d)
    return info["bars"][:k]


def run_backtest(strategy,
                 snapshot: dict | None = None,
                 initial_capital: float = 50_000.0,
                 rebalance_days: int = 5,
                 warmup: int = 63,
                 fundamentals: dict | None = None) -> dict:
    """Replay `strategy` over the snapshot. Returns equity curve, trades, benchmark.

    strategy(scores) -> {ticker: target_weight}. Signals at close(day t), fills at
    open(day t+1). `initial_capital` defaults to the paper-shadow $50k book.
    """
    snapshot     = snapshot if snapshot is not None else load_snapshot()
    history      = snapshot["history"]
    idx          = _build_index(history)
    fundamentals = fundamentals if fundamentals is not None else snapshot.get("fundamentals", {})
    axis = sorted(idx[BENCHMARK]["dates"]) if BENCHMARK in idx \
        else sorted({d for t in idx for d in idx[t]["dates"]})

    positions: dict[str, float] = {}   # ticker -> shares
    cash = float(initial_capital)
    equity_curve: list[tuple[str, float]] = []
    transactions: list[dict] = []
    traded_notional_total = 0.0

    def close_px(t, d):
        b = idx.get(t, {}).get("by_date", {}).get(d)
        return b["close"] if b else None

    def open_px(t, d):
        b = idx.get(t, {}).get("by_date", {}).get(d)
        return b["open"] if b else None

    for i, d in enumerate(axis):
        # mark to market at close
        eq = cash
        for t, sh in positions.items():
            px = close_px(t, d)
            if px:
                eq += sh * px
        equity_curve.append((_iso(d), round(eq, 2)))

        # rebalance signal at close(d), fill at open(d+1)
        if i >= warmup and (i - warmup) % rebalance_days == 0 and i + 1 < len(axis):
            hist_slice = {t: _hist_up_to(idx, t, d) for t in idx}
            scores = score_all_tickers({"history": hist_slice, "fundamentals": fundamentals})
            target_w = strategy(scores)
            next_d = axis[i + 1]

            traded = 0.0
            for t in set(positions) | set(target_w):
                if t in EXCLUDE:
                    continue
                px = open_px(t, next_d)
                if not px:
                    continue
                cur_val = positions.get(t, 0.0) * px
                tgt_val = float(target_w.get(t, 0.0)) * eq
                dval = tgt_val - cur_val
                if abs(dval) < 1.0:          # ignore sub-$1 drift
                    continue
                dsh = dval / px
                positions[t] = positions.get(t, 0.0) + dsh
                cash -= dval
                traded += abs(dval)
                transactions.append({
                    "action":    "BUY" if dval > 0 else "SELL",
                    "ticker":    t,
                    "qty":       round(abs(dsh), 6),
                    "price":     px,
                    "date":      _iso(next_d),
                    "timestamp": _iso(next_d) + "T00:00:00+00:00",
                    "dry_run":   False,
                })
                if abs(positions[t]) < 1e-9:
                    positions.pop(t, None)

            # one-way cost = half the round-trip spread on the traded notional
            cash -= round_trip_cost(traded) / 2.0
            traded_notional_total += traded

    return {
        "equity_curve":          equity_curve,
        "transactions":          transactions,
        "initial_capital":       float(initial_capital),
        "final_equity":          equity_curve[-1][1] if equity_curve else float(initial_capital),
        "traded_notional_total": round(traded_notional_total, 2),
        "benchmark_curve":       _benchmark_curve(idx, axis, warmup, float(initial_capital)),
    }


def _benchmark_curve(idx: dict, axis: list, warmup: int, capital: float) -> list:
    """Buy-and-hold SPY: flat at `capital` until the strategy deploys (warmup+1),
    then SPY shares bought at that open — aligned to the strategy's deploy date."""
    if BENCHMARK not in idx or warmup + 1 >= len(axis):
        return []
    entry_d  = axis[warmup + 1]
    entry_px = idx[BENCHMARK]["by_date"].get(entry_d, {}).get("open")
    if not entry_px:
        return []
    shares = capital / entry_px
    curve = []
    for d in axis:
        if d < entry_d:
            curve.append((_iso(d), round(capital, 2)))
        else:
            b = idx[BENCHMARK]["by_date"].get(d)
            if b:
                curve.append((_iso(d), round(shares * b["close"], 2)))
    return curve
