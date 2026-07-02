"""
corporate_actions.py — split/dividend adjustment assertions + delisting detection.

Phase 2 / P0-3. Two silent-corruption classes this guards against:

  1. **Unhandled splits** — a 2:1 split halves the raw close overnight. If the price
     series isn't split-adjusted, momentum and volatility read a -50% "crash" that
     never happened, poisoning every factor score for that name. Polygon aggregates
     ARE split-adjusted when `adjusted=true` (set explicitly in market_data, not left
     to the API default). This module's `detect_price_outliers` is the belt-and-
     suspenders check: any 1-day move beyond the IPS `price_outlier_pct` threshold
     with no known corporate action is a SUSPECT print (unhandled split or bad data)
     and is flagged in `data_quality`, not silently scored.

  2. **Delisted / acquired held names** — a name we hold that gets delisted or
     acquired stops returning price data; sizing the next rebalance against a stale
     price is a capital-integrity bug. `find_unpriced_holdings` surfaces held tickers
     with no fresh price so they can be resolved (manual exit / M&A cash) instead of
     silently mis-sized.

DETECTION ONLY — these functions return findings; they never place or cancel an
order. Wiring the delisting signal into the live SELL path lands in Phase 5
(risk_watch), where a new order path is reviewed at the `ultra` gate. Dividends are
intentionally NOT adjusted: both the book and the SPY/QQQ benchmarks are measured
price-return, so omitting dividends is consistent on both sides (documented in
performance.py's report caveats).
"""

from __future__ import annotations

from policy import get as _policy_get


def _outlier_threshold_pct() -> float:
    """1-day move (percent) beyond which a print is suspect. From policy.yaml."""
    return float(_policy_get("price_outlier_pct", 35))


def detect_price_outliers(history: dict, threshold_pct: float | None = None) -> list[dict]:
    """Scan each ticker's close series for a suspect 1-day move.

    A move whose absolute percent change exceeds ``threshold_pct`` (default: the
    IPS ``price_outlier_pct``) with no corporate-action metadata is flagged. On
    split-adjusted data a real 35%+ single-day move is rare (earnings blowup / bid),
    so the flag is a REVIEW signal, not an auto-drop — a genuine crash should not be
    thrown away, and an unhandled split should not be silently scored.

    ``history`` is ``{ticker: [bar, ...]}`` with bars carrying ``close`` and
    ``date`` (the market_snapshot shape). Returns a list of
    ``{ticker, date, prev_close, close, change_pct}`` sorted by |change_pct| desc.
    """
    thr = float(threshold_pct) if threshold_pct is not None else _outlier_threshold_pct()
    findings: list[dict] = []
    for ticker, bars in (history or {}).items():
        if not isinstance(bars, list) or len(bars) < 2:
            continue
        prev = None
        for bar in bars:
            if not isinstance(bar, dict):
                prev = None
                continue
            close = bar.get("close")
            if not isinstance(close, (int, float)) or close <= 0:
                prev = None            # break the chain across a bad/missing bar
                continue
            if prev is not None and prev > 0:
                change_pct = (close - prev) / prev * 100.0
                if abs(change_pct) >= thr:
                    findings.append({
                        "ticker":     ticker,
                        "date":       bar.get("date"),
                        "prev_close": prev,
                        "close":      close,
                        "change_pct": round(change_pct, 2),
                    })
            prev = close
    findings.sort(key=lambda f: abs(f["change_pct"]), reverse=True)
    return findings


def find_unpriced_holdings(holdings, prices: dict) -> list[str]:
    """Return held tickers that have NO fresh price in the snapshot.

    A held name absent from ``prices`` (or priced at a non-positive/None close) is a
    likely delisting / acquisition / halt — sizing a rebalance against its stale
    cost basis would be wrong. ``holdings`` may be a list of ticker strings or a list
    of position dicts carrying a ``ticker`` key (the mcp_portfolio shape). Sorted,
    de-duplicated. Detection only — the caller decides how to resolve.
    """
    tickers: list[str] = []
    for h in (holdings or []):
        if isinstance(h, str):
            t = h
        elif isinstance(h, dict):
            t = h.get("ticker") or h.get("symbol")
        else:
            t = None
        if t:
            tickers.append(t)

    unpriced = []
    for t in tickers:
        px = prices.get(t) if isinstance(prices, dict) else None
        close = px.get("close") if isinstance(px, dict) else px
        if not isinstance(close, (int, float)) or close <= 0:
            unpriced.append(t)
    return sorted(set(unpriced))
