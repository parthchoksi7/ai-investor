"""
data_providers.py — pluggable real-data providers (#1 / FINAL_PLAN P2).

Why: the system runs on free-tier Polygon, which returns NO fundamentals (so the
quant quality/valuation factors are permanently N/A) and NO earnings calendar (so
the earnings agent invents dates — a live fabrication vector feeding real orders).
This module adds a provider abstraction + two concrete providers so the snapshot
can carry real fundamentals + a verified earnings calendar.

Provider chain (selected by `get_provider()`):
  - `FMPProvider` (FMP_API_KEY set): all 6 quant factors + earnings calendar + estimates.
    FMP free tier covers ~35% of the universe (mega-caps); the rest return 402.
  - `SECProvider` (no key): gross_margin / operating_margin / debt_to_equity from
    SEC EDGAR company-facts — completely free, no API key, ~100% US equity coverage.
    No earnings calendar (EDGAR has no forward calendar). Degrades gracefully for
    non-US-listed names (returns None).
  - `StubProvider`: deterministic in-memory data for tests / offline dev.

Upgrade path: add FMP_API_KEY to get the remaining 3 valuation factors (P/E, FCF
yield, EV/EBITDA) and the earnings calendar. Without it, quality factors are real
for the full universe; valuation stays N/A — a major improvement over the all-N/A
pre-provider behavior.
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
    """Financial Modeling Prep client (stable API). Returns None/{} without a key.

    Endpoints + field names validated against a live response on 2026-06-14. The
    legacy `/api/v3` endpoints are deprecated for keys issued after 2025-08-31
    (they 403 with "Legacy Endpoint"), so this uses the `/stable` API, which takes
    the symbol as a query parameter.
    """

    BASE = "https://financialmodelingprep.com/stable"

    def __init__(self, api_key: str | None = None, timeout: int = 15):
        self.api_key = api_key or os.getenv("FMP_API_KEY")
        self.timeout = timeout

    def _get(self, path: str, **params):
        """GET BASE/path?…&apikey=…; returns parsed JSON or None (no key / any error)."""
        if not self.api_key:
            return None
        import requests
        params["apikey"] = self.api_key
        try:
            r = requests.get(f"{self.BASE}/{path}", params=params, timeout=self.timeout)
            return r.json()
        except Exception:
            return None

    @staticmethod
    def _first(data) -> dict:
        return data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}

    @staticmethod
    def _num(d: dict, key: str):
        v = d.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def fundamentals(self, ticker: str) -> dict | None:
        """Map FMP stable TTM ratios + key-metrics → the field names quant_engine
        consumes. Margins / debt / P/E come from `ratios-ttm`; FCF yield and
        EV/EBITDA from `key-metrics-ttm` (two calls)."""
        r = self._first(self._get("ratios-ttm", symbol=ticker))
        m = self._first(self._get("key-metrics-ttm", symbol=ticker))
        out: dict = {}
        gm = self._num(r, "grossProfitMarginTTM")
        om = self._num(r, "operatingProfitMarginTTM")
        de = self._num(r, "debtToEquityRatioTTM")
        pe = self._num(r, "priceToEarningsRatioTTM")
        fy = self._num(m, "freeCashFlowYieldTTM")
        ev = self._num(m, "evToEBITDATTM")
        if gm is not None: out["gross_margin"]     = round(gm, 4)
        if om is not None: out["operating_margin"] = round(om, 4)
        if de is not None: out["debt_to_equity"]   = round(de, 4)
        if pe is not None: out["pe_ratio"]          = round(pe, 2)
        if fy is not None: out["fcf_yield"]         = round(fy, 4)
        if ev is not None: out["ev_ebitda"]         = round(ev, 2)
        return out or None

    def next_earnings_date(self, ticker: str) -> str | None:
        """Soonest earnings date on or after today (an upcoming event has a future
        `date` with epsActual still null), else None."""
        data = self._get("earnings", symbol=ticker)
        if not isinstance(data, list):
            return None
        today = date.today().isoformat()
        future = sorted(
            e["date"] for e in data
            if isinstance(e, dict) and isinstance(e.get("date"), str) and e["date"] >= today
        )
        return future[0] if future else None

    def estimates(self, ticker: str) -> dict | None:
        d = self._first(self._get("analyst-estimates", symbol=ticker, period="annual", limit=1))
        out = {}
        if d.get("epsAvg") is not None:     out["eps"] = d["epsAvg"]
        if d.get("revenueAvg") is not None: out["revenue"] = d["revenueAvg"]
        return out or None


class SECProvider:
    """SEC EDGAR fundamentals — free, no API key, ~100% US equity coverage.

    Uses the XBRL company-facts API (data.sec.gov/api/xbrl/companyfacts).
    Extracts gross_margin / operating_margin / debt_to_equity from the most recent
    annual (10-K) filing. No forward earnings calendar → next_earnings_date and
    estimates always return None (FMP_API_KEY is needed for those).

    Why EDGAR over SimFin Free: truly free, no key management, all SEC-registered
    US equities covered, and the XBRL data is the authoritative source used by
    every financial terminal. SimFin also requires a key and has narrower coverage.

    CIK lookup: company_tickers.json is fetched once per instance and cached in
    memory (lazy load). EDGAR rate limit is 10 req/s — far above what we need.

    Cache invalidation note: provider_cache.json may hold FMP-empty entries (from
    before this provider was added). Those expire naturally after 30 days via the
    coverage-aware TTL in _enrich_with_provider. Delete provider_cache.json to
    force an immediate refresh.
    """

    TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
    FACTS_URL   = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    HEADERS     = {"User-Agent": "ai-investor-bot/1.0 ai-investor-bot@github.com"}

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self._cik: dict[str, str] = {}   # ticker → 10-digit zero-padded CIK (lazy)

    def _ensure_cik_map(self) -> None:
        if self._cik:
            return
        import requests
        try:
            r = requests.get(self.TICKERS_URL, headers=self.HEADERS, timeout=self.timeout)
            self._cik = {
                v["ticker"].upper(): str(v["cik_str"]).zfill(10)
                for v in r.json().values()
            }
        except Exception:
            self._cik = {}

    def _get_us_gaap(self, ticker: str) -> dict:
        import requests
        self._ensure_cik_map()
        cik = self._cik.get(ticker.upper())
        if not cik:
            return {}
        try:
            r = requests.get(
                self.FACTS_URL.format(cik=cik),
                headers=self.HEADERS,
                timeout=self.timeout,
            )
            return r.json().get("facts", {}).get("us-gaap", {})
        except Exception:
            return {}

    @staticmethod
    def _latest_annual(us_gaap: dict, *concepts: str) -> float | None:
        """Most-recent 10-K USD value for the first matching XBRL concept."""
        for concept in concepts:
            entries = us_gaap.get(concept, {}).get("units", {}).get("USD", [])
            annual = [
                e for e in entries
                if e.get("form") in ("10-K", "10-K/A") and isinstance(e.get("val"), (int, float))
            ]
            if annual:
                return float(max(annual, key=lambda x: x.get("end", ""))["val"])
        return None

    def fundamentals(self, ticker: str) -> dict | None:
        """Return gross_margin, operating_margin, debt_to_equity from the latest
        10-K. Returns None if the ticker is not found in EDGAR or has no annual
        filing. P/E, FCF yield, EV/EBITDA require price data and are omitted
        (use FMPProvider for those)."""
        g = self._get_us_gaap(ticker)
        if not g:
            return None
        rev = self._latest_annual(
            g, "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet",
            "SalesRevenueGoodsNet",
        )
        gp  = self._latest_annual(g, "GrossProfit")
        op  = self._latest_annual(g, "OperatingIncomeLoss")
        eq  = self._latest_annual(
            g, "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        )
        ltd = self._latest_annual(g, "LongTermDebt", "LongTermDebtNoncurrent")

        out: dict[str, float] = {}
        if rev and rev > 0:
            if gp is not None:  out["gross_margin"]     = round(gp / rev, 4)
            if op is not None:  out["operating_margin"] = round(op / rev, 4)
        if eq and eq > 0 and ltd is not None:
            out["debt_to_equity"] = round(ltd / eq, 4)
        return out or None

    def next_earnings_date(self, ticker: str) -> str | None:
        return None   # EDGAR has no forward earnings calendar; use FMPProvider for this

    def estimates(self, ticker: str) -> dict | None:
        return None


def get_provider() -> MarketDataProvider:
    """Provider selection:
      - FMP_API_KEY set → FMPProvider: all 6 quant factors + earnings calendar.
      - No key         → SECProvider: 3 quality factors free from EDGAR (full US coverage).
    """
    if os.getenv("FMP_API_KEY"):
        return FMPProvider()
    return SECProvider()
