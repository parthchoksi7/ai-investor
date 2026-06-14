"""
data_providers.py — pluggable real-data providers (#1 / FINAL_PLAN P2).

Why: the system runs on free-tier Polygon, which returns NO fundamentals (so the
quant quality/valuation factors are permanently N/A) and NO earnings calendar (so
the earnings agent invents dates — a live fabrication vector feeding real orders).
This module adds a provider abstraction + one real provider (Financial Modeling
Prep) so the snapshot can carry real fundamentals + a verified earnings calendar.

Design:
  - `MarketDataProvider` Protocol: fundamentals / next_earnings_date / estimates.
  - `StubProvider`: deterministic in-memory data → everything is testable WITHOUT
    a live key.
  - `FMPProvider`: concrete Financial Modeling Prep client. **Degrades gracefully**
    — with no `FMP_API_KEY` it returns None/{}, so the pipeline falls back to the
    existing free-tier behavior and never hard-fails on a missing vendor key.
  - `get_provider()`: factory — FMP when a key is present, else a no-op stub.

⚠️ VENDOR KEY REQUIRED to go live: set `FMP_API_KEY` in `.env` (local) and as a
GitHub Actions secret (for `market_data.yml`). Until then the StubProvider path
is used and quality/valuation stay N/A — exactly today's behavior, no regression.

⚠️ The FMP field mappings below are best-effort against the FMP v3 schema and
must be validated against a live response before trusting the numbers (there is
no live key in this environment to verify against). The interface / stub / factory
/ graceful-degradation are the tested core.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Protocol, runtime_checkable


@runtime_checkable
class MarketDataProvider(Protocol):
    def fundamentals(self, ticker: str) -> dict | None: ...
    def next_earnings_date(self, ticker: str) -> str | None: ...   # 'YYYY-MM-DD' or None
    def estimates(self, ticker: str) -> dict | None: ...           # {eps, revenue, ...}


class StubProvider:
    """Deterministic in-memory provider for tests / offline dev.

    Pass dicts keyed by ticker. Anything not provided returns None — the same
    contract a real provider honors for an unknown name.
    """

    def __init__(self, fundamentals: dict | None = None,
                 earnings: dict | None = None,
                 estimates: dict | None = None):
        self._fundamentals = fundamentals or {}
        self._earnings     = earnings or {}
        self._estimates    = estimates or {}

    def fundamentals(self, ticker: str) -> dict | None:
        return self._fundamentals.get(ticker)

    def next_earnings_date(self, ticker: str) -> str | None:
        return self._earnings.get(ticker)

    def estimates(self, ticker: str) -> dict | None:
        return self._estimates.get(ticker)


class FMPProvider:
    """Financial Modeling Prep client. Returns None/{} without a key (graceful)."""

    BASE = "https://financialmodelingprep.com/api/v3"

    def __init__(self, api_key: str | None = None, timeout: int = 10):
        self.api_key = api_key or os.getenv("FMP_API_KEY")
        self.timeout = timeout

    def _get(self, path: str, **params):
        """GET BASE/path; returns parsed JSON or None (no key / any error)."""
        if not self.api_key:
            return None
        import requests
        params["apikey"] = self.api_key
        try:
            r = requests.get(f"{self.BASE}/{path}", params=params, timeout=self.timeout)
            return r.json()
        except Exception:
            return None

    def fundamentals(self, ticker: str) -> dict | None:
        """Map FMP TTM ratios → the field names quant_engine already consumes
        (gross_margin / operating_margin / fcf_margin / debt_to_equity / pe_ratio
        / fcf_yield / ev_ebitda). Validate field names against a live response."""
        data = self._get(f"ratios-ttm/{ticker}")
        d = data[0] if isinstance(data, list) and data else data
        if not isinstance(d, dict):
            return None

        def num(key):
            v = d.get(key)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        out: dict = {}
        gm  = num("grossProfitMarginTTM")
        om  = num("operatingProfitMarginTTM")
        de  = num("debtEquityRatioTTM")
        pe  = num("peRatioTTM")
        fy  = num("freeCashFlowYieldTTM")
        ev  = num("enterpriseValueMultipleTTM")
        fcf = num("freeCashFlowPerShareTTM")
        rps = num("revenuePerShareTTM")
        if gm is not None: out["gross_margin"]     = round(gm, 4)
        if om is not None: out["operating_margin"] = round(om, 4)
        if de is not None: out["debt_to_equity"]   = round(de, 4)
        if pe is not None: out["pe_ratio"]          = round(pe, 2)
        if fy is not None: out["fcf_yield"]         = round(fy, 4)
        if ev is not None: out["ev_ebitda"]         = round(ev, 2)
        if fcf is not None and rps:
            out["fcf_margin"] = round(fcf / rps, 4)
        return out or None

    def next_earnings_date(self, ticker: str) -> str | None:
        """Soonest confirmed/estimated earnings date on or after today, else None."""
        data = self._get("earning_calendar", symbol=ticker)
        if not isinstance(data, list):
            return None
        today = date.today().isoformat()
        future = sorted(
            e["date"] for e in data
            if isinstance(e, dict) and e.get("symbol") == ticker
            and isinstance(e.get("date"), str) and e["date"] >= today
        )
        return future[0] if future else None

    def estimates(self, ticker: str) -> dict | None:
        data = self._get(f"analyst-estimates/{ticker}", limit=1)
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            return None
        d = data[0]
        out = {}
        if d.get("estimatedEpsAvg") is not None:     out["eps"] = d["estimatedEpsAvg"]
        if d.get("estimatedRevenueAvg") is not None: out["revenue"] = d["estimatedRevenueAvg"]
        return out or None


def get_provider() -> MarketDataProvider:
    """FMP when FMP_API_KEY is set, else a no-op StubProvider (free-tier fallback).

    The stub returns None for everything, so wiring this into market_data.py is a
    no-op until a key is added — no behavior change, no regression risk.
    """
    if os.getenv("FMP_API_KEY"):
        return FMPProvider()
    return StubProvider()
