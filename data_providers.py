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
    # SEC fair-access requires the UA be a declared identity in the documented
    # "Company Name contact@email" form. A slash-version/bot-style UA
    # ("ai-investor-bot/1.0 …") is rejected by SEC's Akamai WAF with 403 — which
    # silently collapsed EDGAR quality coverage to ~0 (the reason the CIK-map load
    # was failing even in CI). This exact string returns 200 + 10k+ CIK entries;
    # do NOT reintroduce a "/version" token. See sec.gov/os/webmaster-faq#developers
    HEADERS     = {"User-Agent": "AI Investor Research admin@parth-choksi.com"}

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self._cik: dict[str, str] = {}   # ticker → 10-digit zero-padded CIK (lazy)
        self._cik_load_attempted = False # load is tried exactly once (no retry storm)
        self._cik_load_ok        = False # True iff the map loaded with ≥1 entry
        self._cik_load_error: str | None = None

    def _ensure_cik_map(self) -> None:
        # Attempt the load exactly once. Previously any failure was swallowed into
        # ``self._cik = {}`` with no signal AND, because an empty dict is falsy,
        # every subsequent per-ticker call re-hit SEC — a silent retry storm that
        # collapsed fundamental coverage to 0% with no trace (the June 28%-coverage
        # incident class). Now: one attempt, and the outcome is recorded on
        # ``_cik_load_ok`` so the enrichment layer can tell a genuine load FAILURE
        # (→ abort / DEGRADED) apart from a legitimate ticker-not-in-map (→ None).
        if self._cik_load_attempted:
            return
        self._cik_load_attempted = True
        import requests
        try:
            r = requests.get(self.TICKERS_URL, headers=self.HEADERS, timeout=self.timeout)
            r.raise_for_status()
            self._cik = {
                v["ticker"].upper(): str(v["cik_str"]).zfill(10)
                for v in r.json().values()
            }
        except Exception as e:
            self._cik = {}
            self._cik_load_error = str(e)
        if not self._cik and self._cik_load_error is None:
            # HTTP 200 but an empty/malformed-but-valid body (e.g. transient CDN {}):
            # record WHY so a 0%-coverage run is diagnosable, not a silent blank.
            self._cik_load_error = "empty CIK map (200 OK, no entries)"
        self._cik_load_ok = bool(self._cik)

    def cik_map_ok(self) -> bool:
        """Whether the EDGAR CIK map loaded (≥1 entry). Loads it on first call.

        This is the signal the enrichment layer checks to distinguish a real load
        failure — every ticker would return None, i.e. 0% coverage — from the
        normal case where a specific ticker simply isn't SEC-registered.
        """
        self._ensure_cik_map()
        return self._cik_load_ok

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
    def _latest_annual_ex(us_gaap: dict, *concepts: str, unit: str = "USD",
                          prefer_recent: bool = False
                          ) -> tuple[float | None, str | None, str | None]:
        """(value, filed_date, end_date) for the most-recent 10-K value of a matching
        XBRL concept. `filed_date` is the SEC ``filed`` field (YYYY-MM-DD) of the chosen
        entry — when the figure became PUBLIC, i.e. the no-look-ahead availability date
        (a 2025 fiscal year's 10-K filed 2026-02 is unusable before 2026-02). `end_date`
        is the fiscal-period ``end`` — callers that combine TWO fields into one derived
        metric (e.g. cfo − capex) use it to verify both landed on the same fiscal period
        before combining (see the vintage-consistency guards in `fundamentals()`).
        Returns (None, None, None) if no matching annual entry is found.

        `unit` selects the XBRL unit bucket: ``"USD"`` (dollar figures — the default,
        every existing caller), ``"shares"`` (diluted/outstanding share counts), or
        ``"USD/shares"`` (per-share figures like diluted EPS). Company-facts stores each
        concept's values under exactly one of these buckets, so the caller must name it.

        `prefer_recent` chooses BETWEEN the fallback concepts:
          - False (default, every quality/leverage caller): FIRST concept that has any
            10-K annual entry wins — preserves the existing concept-priority behavior.
          - True (the valuation components): the concept whose latest entry has the
            newest ``end`` wins, concept order breaking ties. This avoids a vintage
            mismatch when an earlier-priority concept's latest tag is far STALER than a
            later one's — e.g. NVDA tags capex under `PaymentsToAcquirePropertyPlant…`
            only through FY2011 but `PaymentsToAcquireProductiveAssets` through FY2026;
            first-match-wins would pair FY2011 capex with FY2026 cash flow. Note this
            only protects WITHIN one field's own concept list — a caller combining two
            DIFFERENT fields (e.g. cfo and capex) must additionally compare the two
            `end_date`s itself before combining them."""
        best = None   # (sort_key, value, filed, end) for the freshest concept seen so far
        for i, concept in enumerate(concepts):
            entries = us_gaap.get(concept, {}).get("units", {}).get(unit, [])
            annual = [
                e for e in entries
                if e.get("form") in ("10-K", "10-K/A") and isinstance(e.get("val"), (int, float))
            ]
            if not annual:
                continue
            chosen = max(annual, key=lambda x: x.get("end", ""))
            filed = chosen.get("filed")
            end = chosen.get("end")
            result = (float(chosen["val"]), filed if isinstance(filed, str) else None,
                     end if isinstance(end, str) else None)
            if not prefer_recent:
                return result
            # Newest end wins; earlier concept (smaller i → larger -i) breaks ties.
            key = (chosen.get("end", ""), -i)
            if best is None or key > best[0]:
                best = (key, *result)
        if best is not None:
            return best[1], best[2], best[3]
        return None, None, None

    @classmethod
    def _latest_annual(cls, us_gaap: dict, *concepts: str, unit: str = "USD",
                       prefer_recent: bool = False) -> tuple[float | None, str | None]:
        """(value, filed_date) — thin wrapper over `_latest_annual_ex` for the
        (majority of) callers that don't need the fiscal-period `end` date."""
        value, filed, _end = cls._latest_annual_ex(
            us_gaap, *concepts, unit=unit, prefer_recent=prefer_recent)
        return value, filed

    def fundamentals(self, ticker: str) -> dict | None:
        """Return gross_margin, operating_margin, debt_to_equity from the latest 10-K,
        plus `_as_of_filing` (the latest SEC filing date among the inputs used — the
        no-look-ahead availability date the dossier reads to compute fundamentals age /
        drop future-dated filings). Returns None if the ticker is not found in EDGAR or
        has no annual filing.

        Also emits price-INDEPENDENT valuation components as underscore intermediates
        (`_eps_diluted_annual`, `_shares_diluted`, `_fcf_annual`, `_total_debt`, `_cash`,
        `_ebitda_annual`) — the raw inputs Phase 2's market_data.derive_valuation_ratios
        turns into P/E, FCF yield, EV/EBITDA once the snapshot price is in scope. The
        finished ratios still require price and are NOT emitted here."""
        g = self._get_us_gaap(ticker)
        if not g:
            return None
        rev, rev_f = self._latest_annual(
            g, "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet",
            "SalesRevenueGoodsNet",
        )
        gp,  gp_f  = self._latest_annual(g, "GrossProfit")
        op,  op_f, op_end = self._latest_annual_ex(g, "OperatingIncomeLoss")
        eq,  eq_f  = self._latest_annual(
            g, "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        )
        ltd, ltd_f, ltd_end = self._latest_annual_ex(g, "LongTermDebt", "LongTermDebtNoncurrent")

        out: dict[str, float] = {}
        filed_dates: list[str] = []
        if rev and rev > 0:
            if gp is not None:
                out["gross_margin"]     = round(gp / rev, 4); filed_dates += [rev_f, gp_f]
            if op is not None:
                out["operating_margin"] = round(op / rev, 4); filed_dates += [rev_f, op_f]
        if eq and eq > 0 and ltd is not None:
            out["debt_to_equity"] = round(ltd / eq, 4); filed_dates += [eq_f, ltd_f]

        # ── Phase 1 (PLAN_SEC_VALUATION §4.1/§5): price-INDEPENDENT valuation
        # components. Extracted from the SAME latest 10-K as the margins above, cached
        # (provider_cache.json) and carried in the snapshot as underscore intermediates,
        # but NOT consumed by any score. Phase 2's market_data.derive_valuation_ratios
        # combines them with the snapshot's close price → pe_ratio / fcf_yield /
        # ev_ebitda. Emitting them is behavior-inert: compute_valuation_score reads only
        # pe_ratio/fcf_yield/ev_ebitda, which stay absent until Phase 2 wires the derive
        # step. They deliberately do NOT feed `_as_of_filing` — §7 stamps the vintage
        # over the USED inputs, and these are unused (in any emitted ratio) until Phase 2.
        # prefer_recent=True on every fallback list: pick the concept whose latest 10-K
        # entry is NEWEST, so a stale earlier-priority tag never pairs with fresh data.
        # NOTE: prefer_recent only protects WITHIN one field's own concept list. Every
        # metric below that COMBINES two independently-resolved fields (cfo−capex,
        # op+dna, ltd+std) additionally checks their `end` dates agree before combining
        # — found via live NVDA verification that the within-field fix alone still
        # leaves a cross-field version of the same vintage-mismatch bug reachable.
        eps,   _, _ = self._latest_annual_ex(g, "EarningsPerShareDiluted",
                                             "EarningsPerShareBasic",
                                             unit="USD/shares", prefer_recent=True)
        shares, _, _ = self._latest_annual_ex(
            g, "WeightedAverageNumberOfDilutedSharesOutstanding",
            "WeightedAverageNumberOfSharesOutstandingBasic",
            "CommonStockSharesOutstanding", unit="shares", prefer_recent=True)
        cfo,   _, cfo_end   = self._latest_annual_ex(
            g, "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
            prefer_recent=True)
        capex, _, capex_end = self._latest_annual_ex(
            g, "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsToAcquireProductiveAssets", prefer_recent=True)
        cash,  _, _ = self._latest_annual_ex(
            g, "CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
            prefer_recent=True)
        std,   _, std_end   = self._latest_annual_ex(
            g, "LongTermDebtCurrent", "DebtCurrent", "ShortTermBorrowings",
            prefer_recent=True)
        dna,   _, dna_end   = self._latest_annual_ex(
            g, "DepreciationDepletionAndAmortization",
            "DepreciationAmortizationAndAccretionNet", prefer_recent=True)
        if dna is None:
            # Composite fallback: some filers tag depreciation and intangible
            # amortization separately rather than a single combined D&A concept.
            dep,   _, dep_end   = self._latest_annual_ex(g, "Depreciation")
            amort, _, amort_end = self._latest_annual_ex(g, "AmortizationOfIntangibleAssets")
            if dep is not None and amort is not None:
                if dep_end and dep_end == amort_end:
                    dna, dna_end = dep + amort, dep_end
                # else: the two halves are from different fiscal periods — omit rather
                # than silently blend (honest N/A over a fabricated composite).
            elif dep is not None:
                dna, dna_end = dep, dep_end
            elif amort is not None:
                dna, dna_end = amort, amort_end

        if eps is not None:
            out["_eps_diluted_annual"] = round(eps, 4)
        if shares is not None and shares > 0:
            out["_shares_diluted"] = shares
        if cfo is not None and capex is not None and cfo_end and cfo_end == capex_end:
            # capex (PaymentsTo…) is reported as a positive outflow → subtract. Requires
            # matching fiscal periods — see the vintage-mismatch note above.
            out["_fcf_annual"] = round(cfo - capex, 2)
        if ltd is not None or std is not None:
            if ltd is not None and std is not None:
                # Same fiscal period → sum; mismatched → the long-term figure alone is
                # still valid data (it's the same value the debt_to_equity calc above
                # uses), just without a possibly-stale short-term add-on.
                out["_total_debt"] = round(ltd + std, 2) if ltd_end and ltd_end == std_end \
                    else round(ltd, 2)
            else:
                out["_total_debt"] = round(ltd if ltd is not None else std, 2)
        if cash is not None:
            out["_cash"] = round(cash, 2)
        if op is not None and dna is not None and op_end and op_end == dna_end:
            out["_ebitda_annual"] = round(op + dna, 2)

        if not out:
            return None
        # No-look-ahead vintage: the bundle isn't fully available until the LATEST of
        # its inputs was filed. Stamp ONLY when EVERY contributing input carries a filed
        # date — a partial set would take max() over the present subset, which can
        # UNDERSTATE the true vintage (a missing-filed latest figure paired with an
        # older filed one), and an understated stamp defeats the `> as_of` look-ahead
        # drop in a historical replay. If any is missing → omit → dossier treats vintage
        # as unknown (age=null), which is honest rather than a silent understatement.
        if filed_dates and all(isinstance(d, str) for d in filed_dates):
            out["_as_of_filing"] = max(filed_dates)
        return out

    def next_earnings_date(self, ticker: str) -> str | None:
        return None   # EDGAR has no forward earnings calendar; use FMPProvider for this

    def estimates(self, ticker: str) -> dict | None:
        return None


_QUALITY_FIELDS = {"gross_margin", "operating_margin", "debt_to_equity"}
# Valuation fields require FMP (price-relative ratios); SEC EDGAR does NOT supply them,
# so valuation coverage is structurally capped near FMP's free-tier reach (~35%).
_VALUATION_FIELDS = {"pe_ratio", "fcf_yield", "ev_ebitda"}


def fundamental_coverage(tickers, fundamentals: dict) -> dict:
    """Single source of truth for 'how much real fundamental data do we have'.

    Returns quality AND valuation coverage separately over ``tickers``. Both the live
    snapshot gate (market_data) and the backtest caveat (backtest/engine) call this, so
    the number that gates the quality-tilt re-weight is computed ONE way — a fork here
    would let the backtest clear the 80% floor while the live snapshot doesn't (or vice
    versa). Quality (EDGAR, ~all US equities) is the primary gate; valuation is reported
    for transparency because it can't structurally reach the floor without paid FMP.
    """
    total = len(tickers)

    def _covered(fields: set) -> int:
        return sum(
            1 for t in tickers
            if isinstance(fundamentals.get(t), dict) and (fields & fundamentals[t].keys())
        )

    q = _covered(_QUALITY_FIELDS)
    v = _covered(_VALUATION_FIELDS)
    return {
        "active_universe":           total,
        "fundamentals_covered":      q,
        "fundamental_coverage_pct":  round(100.0 * q / total, 1) if total else 0.0,
        "valuation_covered":         v,
        "valuation_coverage_pct":    round(100.0 * v / total, 1) if total else 0.0,
    }


class CascadeProvider:
    """FMP for all 6 factors when covered; SEC EDGAR fallback for 3 quality factors on FMP misses.

    FMP free tier covers ~35% of the universe (mega-caps). For the remaining ~65%,
    SEC EDGAR provides gross_margin / operating_margin / debt_to_equity for free.
    The cascade gets quality signal for ~100% of US equities and the full 6-factor
    coverage for the ~35% FMP covers, vs. the 37/100 coverage before this class.

    Why merge order {sec, fmp}: FMP data wins on any overlap (it's more current —
    TTM vs. annual EDGAR filings), and SEC fills only the quality fields that FMP
    didn't supply.
    """

    def __init__(self, primary: "FMPProvider", fallback: "SECProvider"):
        self._primary  = primary
        self._fallback = fallback

    def fundamentals(self, ticker: str) -> dict | None:
        result = self._primary.fundamentals(ticker)
        if result and any(k in result for k in _QUALITY_FIELDS):
            return result  # FMP covered this ticker; quality fields are present
        # FMP miss (402 / premium-only on free tier): supplement with SEC EDGAR.
        # Merge as {sec, fmp} so FMP valuation fields (if any) still win on overlap.
        sec = self._fallback.fundamentals(ticker)
        if sec is None and not result:
            return None
        return {**(sec or {}), **(result or {})}

    def next_earnings_date(self, ticker: str) -> str | None:
        return self._primary.next_earnings_date(ticker)

    def estimates(self, ticker: str) -> dict | None:
        return self._primary.estimates(ticker)

    def cik_map_ok(self) -> bool:
        """SEC EDGAR is the quality-factor fallback for the ~65% of the universe
        FMP's free tier doesn't cover, so its CIK-map health gates coverage here
        too. Delegates to the SEC fallback."""
        return self._fallback.cik_map_ok()


def get_provider() -> MarketDataProvider:
    """Provider selection:
      - FMP_API_KEY set → CascadeProvider: FMP for all 6 factors + earnings calendar,
                          with SEC EDGAR fallback for 3 quality factors on FMP free-tier misses.
      - No key          → SECProvider: 3 quality factors from EDGAR (free, full US coverage).
    """
    if os.getenv("FMP_API_KEY"):
        return CascadeProvider(FMPProvider(), SECProvider())
    return SECProvider()
