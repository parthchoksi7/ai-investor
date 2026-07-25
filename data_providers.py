"""
data_providers.py — pluggable real-data providers (#1 / FINAL_PLAN P2).

Why: the system runs on free-tier Polygon, which returns NO fundamentals (so the
quant quality/valuation factors were permanently N/A) and NO earnings calendar (so
the earnings agent invents dates — a live fabrication vector feeding real orders).
This module adds a provider abstraction + concrete providers so the snapshot can
carry real fundamentals + a verified earnings calendar.

Provider chain (selected by `get_provider()`):
  - `FMPProvider` (FMP_API_KEY set): quality factors + earnings calendar + estimates.
    FMP free tier covers ~35% of the universe (mega-caps) for quality; the rest
    return 402. FMP CAN also return TTM valuation ratios, but as of
    PLAN_SEC_VALUATION Phase 3 those are discarded by `CascadeProvider` — valuation
    is single-sourced from SEC (below).
  - `SECProvider` (no key): gross_margin / operating_margin / debt_to_equity (annual
    10-K basis), PLUS the valuation components `market_data.derive_valuation_ratios`
    turns into pe_ratio / fcf_yield / ev_ebitda once the snapshot price is in scope —
    flow components (`_eps_diluted_ttm` / `_fcf_ttm` / `_ebitda_ttm`) on a trailing-
    twelve-month basis (PLAN_SEC_VALUATION Phase 4), balance components
    (`_shares_diluted` / `_total_debt` / `_cash`) at the latest available point in
    time (may come from a 10-Q, not just the latest 10-K) — from SEC EDGAR
    company-facts, completely free, no API key, ~100% US equity coverage. No
    earnings calendar (EDGAR has no forward calendar). Degrades gracefully for
    non-US-listed names (returns None).
  - `CascadeProvider` (FMP_API_KEY set): FMP for quality-when-covered + the
    earnings calendar; SEC EDGAR for the single-sourced valuation components +
    the quality fallback. Always consults both (see its docstring).
  - `StubProvider`: deterministic in-memory data for tests / offline dev.

Net effect: quality AND valuation factors are real for ~the full US universe for
free (no FMP_API_KEY required); FMP, when present, only sharpens quality (TTM) for
the names it covers and supplies the earnings calendar EDGAR doesn't have.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
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

    # ── PLAN_SEC_VALUATION Phase 4 — TTM basis ──────────────────────────────
    # Flow valuation components (EPS, CFO, capex, D&A, operating income for
    # EBITDA) move from latest-ANNUAL to trailing-twelve-months: sum of the 4
    # most recent CONTIGUOUS fiscal quarters. The catch: XBRL never discloses a
    # standalone "Q4" duration fact — a company's 4th fiscal quarter is only
    # ever visible baked into the 10-K's full-year figure. So the engine below
    # builds a quarterly time series from real 10-Q "three/six/nine months
    # ended" entries, DERIVES the one missing quarter per fiscal year as
    # (full-year 10-K value − the 3 known quarters of that year), and sums the
    # most recent 4 entries of the combined series. Refuses to derive (and the
    # caller gets an honest None) on any ambiguity — more than one gap, an
    # overlapping/non-quarter-shaped gap, or fewer than 4 contiguous quarters
    # available at all (e.g. a recent IPO with limited history) — rather than
    # risk silently blending mismatched periods into a fabricated number.
    _QUARTER_MIN_DAYS, _QUARTER_MAX_DAYS = 75, 100   # ~1 fiscal quarter's span
    _ANNUAL_MIN_DAYS, _ANNUAL_MAX_DAYS = 350, 380     # ~1 fiscal year's span (allows a 53-week year)
    # Live-verified (Phase 4 validation, MCD): some filers report
    # WeightedAverageNumberOfDilutedSharesOutstanding SCALED IN MILLIONS
    # (e.g. "713.5" meaning 713.5M) rather than the raw count (AAPL: "14725873000")
    # — a valid XBRL choice (the `decimals` attribute carries the scale), but
    # SEC's companyfacts JSON exposes only the raw `val`, with no scale field to
    # correct it. A pre-existing gap since Phase 1 (this concept has always been
    # extracted this way) that only surfaces as a wildly wrong market_cap once
    # something actually multiplies shares × price — exactly what Phase 4's
    # derive step does. No real exchange-listed company has under 1M diluted
    # shares; refuse rather than silently fabricate a ~37,000x fcf_yield.
    _MIN_PLAUSIBLE_SHARES = 1_000_000

    @staticmethod
    def _dedup_by_period(entries: list[dict]) -> list[dict]:
        """Collapse duplicate (start, end) periods to the MOST RECENTLY FILED
        entry. The same quarter routinely appears twice in the raw payload —
        once in its own 10-Q, again as a prior-year comparative in a later
        filing — and a genuine restatement also re-reports the same period
        with a new value; "most recent filed wins" handles both the same way,
        matching `_latest_annual_ex`'s existing philosophy for annual entries."""
        best: dict[tuple, dict] = {}
        for e in entries:
            key = (e.get("start"), e.get("end"))
            filed = e.get("filed") or ""
            if key not in best or filed >= (best[key].get("filed") or ""):
                best[key] = e
        return list(best.values())

    @classmethod
    def _quarterly_series(cls, us_gaap: dict, concept: str, unit: str = "USD") -> list[dict]:
        """Deduped, ascending-by-end list of standalone (~1 quarter) duration
        entries for ONE concept: `[{start, end, val, filed}]` with `start`/`end`
        parsed to `date` objects. Excludes YTD-cumulative entries (a 10-Q tags
        BOTH the standalone quarter and the YTD-to-date figure under the same
        concept; only the ~90-day-duration one is a true single quarter)."""
        entries = us_gaap.get(concept, {}).get("units", {}).get(unit, [])
        quarterish = []
        for e in entries:
            s, en = e.get("start"), e.get("end")
            if not (isinstance(s, str) and isinstance(en, str) and isinstance(e.get("val"), (int, float))):
                continue
            try:
                sd, ed = date.fromisoformat(s), date.fromisoformat(en)
            except ValueError:
                continue
            if cls._QUARTER_MIN_DAYS <= (ed - sd).days <= cls._QUARTER_MAX_DAYS:
                quarterish.append(e)
        out = [
            {"start": date.fromisoformat(e["start"]), "end": date.fromisoformat(e["end"]),
             "val": float(e["val"]), "filed": e.get("filed") if isinstance(e.get("filed"), str) else None}
            for e in cls._dedup_by_period(quarterish)
        ]
        out.sort(key=lambda q: q["end"])
        return out

    @classmethod
    def _ytd_derived_quarters(cls, us_gaap: dict, concept: str, unit: str = "USD") -> list[dict]:
        """Standalone quarters derived by DIFFERENCING consecutive year-to-date
        entries that share the same fiscal-year-start `start` date. Cash-flow-
        statement items (CFO, capex, D&A) are, in practice, very often tagged
        by filers ONLY as YTD-cumulative ("six/nine months ended") — never as
        a standalone quarter the way income-statement items (revenue, EPS)
        usually are (live-verified: AAPL's CFO has zero standalone Q2/Q3
        entries, only a Q1 — which is trivially both — and 6-/9-month YTD
        figures). Without this, `_quarterly_series` alone starves the TTM
        engine of quarters for exactly these concepts. `quarter[i].val =
        ytd[i].val − ytd[i-1].val` (quarter[0] = ytd[0] itself — the first
        YTD point in a fiscal year IS the first quarter, no subtraction
        needed); `filed` = the later of the two bracketing YTD filings (a
        derived figure isn't public until both are). A derived quarter whose
        OWN span isn't quarter-shaped (75-100 days — a spacing anomaly) is
        silently dropped rather than risking a bogus figure."""
        entries = us_gaap.get(concept, {}).get("units", {}).get(unit, [])
        candidates = [
            e for e in entries
            if isinstance(e.get("start"), str) and isinstance(e.get("end"), str)
            and isinstance(e.get("val"), (int, float))
        ]
        by_start: dict[str, list[dict]] = {}
        for e in cls._dedup_by_period(candidates):
            by_start.setdefault(e["start"], []).append(e)

        out = []
        for start_str, rungs in by_start.items():
            if len(rungs) < 2:
                continue   # need >=2 cumulative points to derive ANY quarter by differencing
            try:
                prev_end = date.fromisoformat(start_str) - timedelta(days=1)
            except ValueError:
                continue
            prev_val: float = 0.0
            prev_filed: str | None = None
            for rung in sorted(rungs, key=lambda e: e["end"]):
                try:
                    rung_end = date.fromisoformat(rung["end"])
                except ValueError:
                    break
                q_start = prev_end + timedelta(days=1)
                q_val = float(rung["val"]) - prev_val
                rung_filed = rung.get("filed") if isinstance(rung.get("filed"), str) else None
                filed_parts = [rung_filed] + ([prev_filed] if prev_filed is not None else [])
                q_filed = max(filed_parts) if all(isinstance(x, str) for x in filed_parts) else None
                if cls._QUARTER_MIN_DAYS <= (rung_end - q_start).days <= cls._QUARTER_MAX_DAYS:
                    out.append({"start": q_start, "end": rung_end, "val": q_val, "filed": q_filed})
                prev_end, prev_val, prev_filed = rung_end, float(rung["val"]), rung_filed
        out.sort(key=lambda q: q["end"])
        return out

    @classmethod
    def _combined_quarterly_series(cls, us_gaap: dict, concept: str, unit: str = "USD") -> list[dict]:
        """Union of `_quarterly_series` (true standalone) and
        `_ytd_derived_quarters` (differenced from YTD ladders) for one
        concept — a real reported standalone quarter wins any (start, end)
        collision over a derived one (simpler, more directly authoritative)."""
        merged = {(q["start"], q["end"]): q for q in cls._ytd_derived_quarters(us_gaap, concept, unit)}
        for q in cls._quarterly_series(us_gaap, concept, unit):
            merged[(q["start"], q["end"])] = q   # true standalone overwrites any derived collision
        out = list(merged.values())
        out.sort(key=lambda q: q["end"])
        return out

    @classmethod
    def _annual_series(cls, us_gaap: dict, concept: str, unit: str = "USD") -> list[dict]:
        """Deduped, ascending-by-end list of ANNUAL (10-K/10-K/A, ~365-day
        duration) entries for ONE concept, same shape as `_quarterly_series`.
        Used only to derive the one missing quarter per fiscal year — not
        itself the TTM value (Phase 4 replaces the annual basis, it doesn't
        blend it in). The duration check (in addition to the form check)
        guards against a transition-period report (a fiscal-year-change
        filer's short "10-K" covering only a few months) being mistaken for
        a full year and corrupting a derived quarter."""
        entries = us_gaap.get(concept, {}).get("units", {}).get(unit, [])
        annual = []
        for e in entries:
            if e.get("form") not in ("10-K", "10-K/A"):
                continue
            s, en = e.get("start"), e.get("end")
            if not (isinstance(s, str) and isinstance(en, str) and isinstance(e.get("val"), (int, float))):
                continue
            try:
                sd, ed = date.fromisoformat(s), date.fromisoformat(en)
            except ValueError:
                continue
            if cls._ANNUAL_MIN_DAYS <= (ed - sd).days <= cls._ANNUAL_MAX_DAYS:
                annual.append(e)
        out = [
            {"start": date.fromisoformat(e["start"]), "end": date.fromisoformat(e["end"]),
             "val": float(e["val"]), "filed": e.get("filed") if isinstance(e.get("filed"), str) else None}
            for e in cls._dedup_by_period(annual)
        ]
        out.sort(key=lambda a: a["end"])
        return out

    @classmethod
    def _fill_derived_quarters(cls, quarters: list[dict], annuals: list[dict]) -> list[dict]:
        """Merge `quarters` with one DERIVED entry per fiscal year whose 10-K
        period is covered by exactly 3 known quarters with a single
        quarter-shaped gap — the gap is the fiscal-year-end quarter XBRL never
        discloses standalone. `derived_value = annual.val − sum(the 3 known
        quarters)`; `derived_filed = max(annual.filed, the 3 quarters' filed)`
        (a derived figure isn't public until every piece behind it is).

        Refuses to derive for a given fiscal year (silently skips it — the
        caller then simply has one fewer usable quarter, which surfaces as
        "insufficient history" rather than a wrong number) on: overlapping
        quarter coverage, more than one gap, or a gap that isn't itself
        quarter-shaped (75-100 days) — every one of these means the data
        doesn't cleanly support a derivation, so guessing would be dishonest.
        A real reported quarter always wins over a derived one on collision.
        """
        derived = []
        for a in annuals:
            covered = sorted(
                (q for q in quarters if a["start"] <= q["start"] and q["end"] <= a["end"]),
                key=lambda q: q["start"],
            )
            if len(covered) != 3:
                continue
            cursor = a["start"]
            gap = None
            ok = True
            for q in covered:
                if q["start"] < cursor:
                    ok = False   # overlapping coverage — refuse rather than risk double-counting
                    break
                if q["start"] > cursor:
                    if gap is not None:
                        ok = False   # a second gap — ambiguous, refuse
                        break
                    gap = (cursor, q["start"] - timedelta(days=1))
                cursor = q["end"] + timedelta(days=1)
            if not ok:
                continue
            if cursor <= a["end"]:
                if gap is not None:
                    continue   # a second gap at the tail — refuse
                gap = (cursor, a["end"])
            if gap is None:
                continue       # no gap — all 4 quarters already directly reported
            gap_start, gap_end = gap
            if not (cls._QUARTER_MIN_DAYS <= (gap_end - gap_start).days <= cls._QUARTER_MAX_DAYS):
                continue       # the "gap" isn't quarter-shaped — refuse rather than guess
            implied_val = a["val"] - sum(q["val"] for q in covered)
            candidate_filed = [a["filed"]] + [q["filed"] for q in covered]
            implied_filed = max(candidate_filed) if all(candidate_filed) else None
            derived.append({"start": gap_start, "end": gap_end, "val": implied_val, "filed": implied_filed})

        merged = {(q["start"], q["end"]): q for q in quarters}
        for d in derived:
            merged.setdefault((d["start"], d["end"]), d)   # real reported quarter wins any collision
        out = list(merged.values())
        out.sort(key=lambda q: q["end"])
        return out

    @classmethod
    def _ttm_ex(cls, us_gaap: dict, *concepts: str, unit: str = "USD",
               prefer_recent: bool = False
               ) -> tuple[float | None, str | None, str | None]:
        """(ttm_value, filed_date, window_end) — sum of the 4 most recent
        CONTIGUOUS fiscal quarters (real + derived, see `_fill_derived_quarters`)
        for the first (or, if `prefer_recent`, the freshest-windowed) matching
        concept. `filed_date` is the latest of the 4 quarters' filed dates — the
        no-look-ahead availability date. `window_end` is the newest quarter's
        end (a string, matching `_latest_annual_ex`'s convention) — callers
        combining TWO TTM fields (e.g. cfo_ttm − capex_ttm) must verify both
        windows agree before combining, same cross-field vintage discipline as
        the annual basis (see `fundamentals()`).

        Returns (None, None, None) if no concept can assemble 4 contiguous
        quarters — thin trading history, sparse XBRL tagging, or an
        un-derivable gap — honest N/A, never a partial or guessed TTM."""
        best = None   # (window_end, value, filed) for the freshest concept seen so far
        for concept in concepts:
            quarters = cls._combined_quarterly_series(us_gaap, concept, unit)
            annuals  = cls._annual_series(us_gaap, concept, unit)
            series = cls._fill_derived_quarters(quarters, annuals)
            if len(series) < 4:
                continue
            recent4 = series[-4:]
            contiguous = all(
                recent4[i]["start"] == recent4[i - 1]["end"] + timedelta(days=1)
                for i in range(1, 4)
            )
            if not contiguous:
                continue
            value = sum(q["val"] for q in recent4)
            filed_candidates = [q["filed"] for q in recent4]
            filed = max(filed_candidates) if all(filed_candidates) else None
            window_end = recent4[-1]["end"]
            if not prefer_recent:
                return value, filed, window_end.isoformat()
            if best is None or window_end > best[0]:
                best = (window_end, value, filed)
        if best is not None:
            return best[1], best[2], best[0].isoformat()
        return None, None, None

    @classmethod
    def _latest_any_form_ex(cls, us_gaap: dict, *concepts: str, unit: str = "USD",
                            prefer_recent: bool = True
                            ) -> tuple[float | None, str | None, str | None]:
        """(value, filed_date, end_date) — the single freshest available value
        for a BALANCE item (shares outstanding, debt, cash), accepting 10-K,
        10-K/A, 10-Q, AND 10-Q/A. Unlike a flow item, a balance item is never
        summed — Phase 4 ("balance items stay point-in-time latest") just wants
        the most CURRENT figure, whether that's the latest annual or a
        more-recent quarterly filing. On a tie for the newest `end` (a 10-Q
        commonly tags the same balance-sheet date under both a quarter and a
        YTD duration), the SHORTEST (most instantaneous) duration wins — an
        instant fact (no `start`) always wins such a tie. `prefer_recent`
        mirrors `_latest_annual_ex`: True picks the concept whose latest value
        has the newest `end` (guards the same cross-concept vintage-mismatch
        class Phase 1 found for NVDA/JPM); every Phase 4 caller passes True."""
        def _duration_days(e: dict) -> int:
            s = e.get("start")
            if not isinstance(s, str):
                return 0
            try:
                return (date.fromisoformat(e["end"]) - date.fromisoformat(s)).days
            except ValueError:
                return 0

        best = None   # (end, value, filed) for the freshest concept seen so far
        for concept in concepts:
            entries = us_gaap.get(concept, {}).get("units", {}).get(unit, [])
            candidates = [
                e for e in entries
                if e.get("form") in ("10-K", "10-K/A", "10-Q", "10-Q/A")
                and isinstance(e.get("end"), str) and isinstance(e.get("val"), (int, float))
            ]
            if not candidates:
                continue
            deduped = cls._dedup_by_period(candidates)
            max_end = max(e["end"] for e in deduped)
            tied = [e for e in deduped if e["end"] == max_end]
            chosen = min(tied, key=_duration_days)
            filed = chosen.get("filed")
            result = (max_end, float(chosen["val"]), filed if isinstance(filed, str) else None)
            if not prefer_recent:
                return result[1], result[2], result[0]
            if best is None or max_end > best[0]:
                best = result
        if best is not None:
            return best[1], best[2], best[0]
        return None, None, None

    def fundamentals(self, ticker: str) -> dict | None:
        """Return gross_margin, operating_margin, debt_to_equity from the latest 10-K,
        plus `_as_of_filing` (the latest SEC filing date among the inputs used — the
        no-look-ahead availability date the dossier reads to compute fundamentals age /
        drop future-dated filings). Returns None if the ticker is not found in EDGAR or
        has no annual filing.

        Also emits valuation components as underscore intermediates — flow items on a
        TTM basis (`_eps_diluted_ttm`, `_fcf_ttm`, `_ebitda_ttm`, PLAN_SEC_VALUATION
        Phase 4), balance items at the latest available point in time regardless of
        form (`_shares_diluted`, `_total_debt`, `_cash`) — the raw inputs
        market_data.derive_valuation_ratios turns into P/E, FCF yield, EV/EBITDA once
        the snapshot price is in scope. The finished ratios still require price and
        are NOT emitted here."""
        g = self._get_us_gaap(ticker)
        if not g:
            return None
        # prefer_recent=True on every multi-concept fallback list below (Revenues,
        # StockholdersEquity, LongTermDebt) — found live during Phase 4 validation:
        # AAPL's "Revenues" concept has 11 entries but the newest is FILED 2018-11-05
        # (companies abandoned it for RevenueFromContractWithCustomerExcludingAssessed
        # Tax after ASC 606); first-match-wins (the old default) picked that 2018 tag
        # over the fresh one because "Revenues" merely HAS *an* annual entry, however
        # ancient — silently computing gross_margin from a 2018 revenue figure. Same
        # bug, same live-verified symptom, for JPM's LongTermDebt (frozen since 2014 —
        # already known from Phase 1's valuation-side fix, but the quality-factor
        # debt_to_equity extraction below was never given the same fix). Production
        # impact was masked for FMP-covered names (FMP's quality wins on overlap —
        # CascadeProvider) but live for the SEC-only-quality majority of the universe.
        # This mirrors the exact fix Phase 1 already applied to the valuation
        # components for the identical reason (NVDA's stale capex tag).
        rev, rev_f = self._latest_annual(
            g, "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet",
            "SalesRevenueGoodsNet",
            prefer_recent=True,
        )
        gp,  gp_f  = self._latest_annual(g, "GrossProfit")
        op,  op_f, op_end = self._latest_annual_ex(g, "OperatingIncomeLoss")
        eq,  eq_f  = self._latest_annual(
            g, "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            prefer_recent=True,
        )
        ltd, ltd_f, ltd_end = self._latest_annual_ex(
            g, "LongTermDebt", "LongTermDebtNoncurrent", prefer_recent=True)

        out: dict[str, float] = {}
        filed_dates: list[str] = []
        if rev and rev > 0:
            if gp is not None:
                out["gross_margin"]     = round(gp / rev, 4); filed_dates += [rev_f, gp_f]
            if op is not None:
                out["operating_margin"] = round(op / rev, 4); filed_dates += [rev_f, op_f]
        if eq and eq > 0 and ltd is not None:
            out["debt_to_equity"] = round(ltd / eq, 4); filed_dates += [eq_f, ltd_f]
        # NOTE: `ltd`/`ltd_f`/`ltd_end` above feed ONLY debt_to_equity (a quality
        # factor — annual basis, now prefer_recent per the fix above, but still
        # ANNUAL, unchanged by Phase 4). `_total_debt`
        # (a valuation component) gets its OWN point-in-time-latest extraction below —
        # deliberately NOT reusing this `ltd`, because as of Phase 4 the two now have
        # DIFFERENT required semantics (annual-only vs. latest-any-form) and sharing a
        # variable would either regress the quality factor or under-serve valuation.

        # ── PLAN_SEC_VALUATION §4.1/§5 (Phase 1) / §11 (Phase 4): valuation
        # components. Phase 2's market_data.derive_valuation_ratios combines them with
        # the snapshot's close price → pe_ratio / fcf_yield / ev_ebitda. Flow items
        # (EPS, CFO, capex, D&A, operating income for EBITDA) are TTM (`_ttm_ex`,
        # Phase 4); balance items (shares, debt, cash) are point-in-time-latest
        # (`_latest_any_form_ex`, Phase 4 — accepts a 10-Q, not just the latest 10-K).
        # They deliberately do NOT feed `_as_of_filing` — §7 stamps the vintage over
        # the QUALITY-factor inputs only; this has been true since Phase 1.
        # prefer_recent=True on every fallback list: pick the concept whose latest
        # data is NEWEST, so a stale earlier-priority tag never pairs with fresh data.
        # NOTE: prefer_recent only protects WITHIN one field's own concept list. Every
        # metric below that COMBINES two independently-resolved fields (cfo−capex,
        # op+dna, ltd+std) additionally checks their vintage (TTM `window_end` /
        # annual-or-point-in-time `end`) agrees before combining — found via live
        # NVDA/JPM verification (Phase 1) that the within-field fix alone still leaves
        # a cross-field version of the same vintage-mismatch bug reachable.
        eps,   _, _ = self._ttm_ex(g, "EarningsPerShareDiluted", "EarningsPerShareBasic",
                                   unit="USD/shares", prefer_recent=True)
        shares, _, _ = self._latest_any_form_ex(
            g, "WeightedAverageNumberOfDilutedSharesOutstanding",
            "WeightedAverageNumberOfSharesOutstandingBasic",
            "CommonStockSharesOutstanding", unit="shares")
        cfo,   _, cfo_end   = self._ttm_ex(
            g, "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
            prefer_recent=True)
        capex, _, capex_end = self._ttm_ex(
            g, "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsToAcquireProductiveAssets", prefer_recent=True)
        cash,  _, _ = self._latest_any_form_ex(
            g, "CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents")
        ltd_v, _, ltd_v_end = self._latest_any_form_ex(g, "LongTermDebt", "LongTermDebtNoncurrent")
        std,   _, std_end   = self._latest_any_form_ex(
            g, "LongTermDebtCurrent", "DebtCurrent", "ShortTermBorrowings")
        op_ttm, _, op_ttm_end = self._ttm_ex(g, "OperatingIncomeLoss", prefer_recent=True)
        dna,   _, dna_end   = self._ttm_ex(
            g, "DepreciationDepletionAndAmortization",
            "DepreciationAmortizationAndAccretionNet", prefer_recent=True)
        if dna is None:
            # Composite fallback: some filers tag depreciation and intangible
            # amortization separately rather than a single combined D&A concept.
            dep,   _, dep_end   = self._ttm_ex(g, "Depreciation", prefer_recent=True)
            amort, _, amort_end = self._ttm_ex(g, "AmortizationOfIntangibleAssets", prefer_recent=True)
            if dep is not None and amort is not None:
                if dep_end and dep_end == amort_end:
                    dna, dna_end = dep + amort, dep_end
                # else: the two halves are from different TTM windows — omit rather
                # than silently blend (honest N/A over a fabricated composite).
            elif dep is not None:
                dna, dna_end = dep, dep_end
            elif amort is not None:
                dna, dna_end = amort, amort_end

        if eps is not None:
            out["_eps_diluted_ttm"] = round(eps, 4)
        if shares is not None and shares >= self._MIN_PLAUSIBLE_SHARES:
            out["_shares_diluted"] = shares
        if cfo is not None and capex is not None and cfo_end and cfo_end == capex_end:
            # capex (PaymentsTo…) is reported as a positive outflow → subtract. Requires
            # matching TTM windows — see the vintage-mismatch note above.
            out["_fcf_ttm"] = round(cfo - capex, 2)
        if ltd_v is not None or std is not None:
            if ltd_v is not None and std is not None:
                # Same point-in-time date → sum; mismatched → the long-term figure
                # alone is still valid, just without a possibly-stale short-term add-on.
                out["_total_debt"] = round(ltd_v + std, 2) if ltd_v_end and ltd_v_end == std_end \
                    else round(ltd_v, 2)
            else:
                out["_total_debt"] = round(ltd_v if ltd_v is not None else std, 2)
        if cash is not None:
            out["_cash"] = round(cash, 2)
        if op_ttm is not None and dna is not None and op_ttm_end and op_ttm_end == dna_end:
            out["_ebitda_ttm"] = round(op_ttm + dna, 2)

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
# PLAN_SEC_VALUATION Phase 3 (2026-07-24): valuation is no longer FMP-gated. SEC
# EDGAR supplies these too — CascadeProvider always fetches SEC's price-INDEPENDENT
# components (Phase 1) and market_data.derive_valuation_ratios (Phase 2) turns them
# into pe_ratio/fcf_yield/ev_ebitda for the full US-equity universe, single-source
# (Phase 3 strips FMP's own TTM valuation fields so basis never mixes — §7/§10.1).
_VALUATION_FIELDS = {"pe_ratio", "fcf_yield", "ev_ebitda"}


def fundamental_coverage(tickers, fundamentals: dict) -> dict:
    """Single source of truth for 'how much real fundamental data do we have'.

    Returns quality AND valuation coverage separately over ``tickers``. Both the live
    snapshot gate (market_data) and the backtest caveat (backtest/engine) call this, so
    the number that gates the quality-tilt re-weight is computed ONE way — a fork here
    would let the backtest clear the 80% floor while the live snapshot doesn't (or vice
    versa). Quality (EDGAR, ~all US equities) is the primary gate. Valuation is reported
    for transparency but still does NOT gate — as of Phase 3 it is SEC-derived
    (single-source, no FMP dependency) and climbs toward the same ~90%+ level as
    quality, but a per-ticker XBRL tag gap (thin ADR/20-F filings, a vintage-mismatch
    guard) can still leave a name N/A, so it stays informational rather than a floor.
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
    """FMP for quality when covered (TTM, more current) + the earnings calendar;
    SEC EDGAR for EVERYTHING valuation-related (PLAN_SEC_VALUATION Phase 3 — single
    valuation source, §10.1 Option B) plus the quality fallback for FMP misses.

    Always consults BOTH providers: SEC's valuation components (Phase 1's raw
    extraction, Phase 4's TTM/point-in-time-latest basis — `_eps_diluted_ttm` /
    `_shares_diluted` / `_fcf_ttm` / etc.) are needed for market_data.
    derive_valuation_ratios to compute ratios for the FULL universe, not just the
    ~35% of names FMP's free tier misses on quality — the old "FMP quality hit →
    SEC never consulted" short-circuit would have starved derive_valuation_ratios
    of components for exactly the mega-caps most likely to have them. FMP's own
    pe_ratio/fcf_yield/ev_ebitda (TTM) are dropped before merging: SEC-derived
    (no-look-ahead-stamped) is the SINGLE valuation source now, so the composite
    never mixes FMP-TTM and SEC bases across tickers.

    Quality fields (gross_margin/operating_margin/debt_to_equity) are UNCHANGED by
    Phase 3: FMP still wins on overlap (TTM, more current than EDGAR's annual 10-K),
    SEC fills the quality fields FMP's free tier doesn't cover. Merge order
    {sec, fmp_no_valuation} — FMP wins ties on quality; SEC uniquely supplies the
    underscore valuation components and `_as_of_filing` (FMP has neither key, so
    there's no overlap to arbitrate there).
    """

    def __init__(self, primary: "FMPProvider", fallback: "SECProvider"):
        self._primary  = primary
        self._fallback = fallback

    def fundamentals(self, ticker: str) -> dict | None:
        fmp = self._primary.fundamentals(ticker) or {}
        # Phase 3: FMP no longer contributes valuation ratios — SEC-derived (via
        # market_data.derive_valuation_ratios, downstream of this call) is the
        # single source, avoiding a mixed TTM(FMP)/annual(SEC) basis.
        fmp_no_valuation = {k: v for k, v in fmp.items() if k not in _VALUATION_FIELDS}
        sec = self._fallback.fundamentals(ticker)
        if sec is None and not fmp_no_valuation:
            return None
        return {**(sec or {}), **fmp_no_valuation}

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
      - FMP_API_KEY set → CascadeProvider: FMP for quality (when covered) + the
                          earnings calendar; SEC EDGAR for valuation (single source,
                          PLAN_SEC_VALUATION Phase 3) + the quality fallback.
      - No key          → SECProvider: quality (+ valuation components) from EDGAR,
                          free, full US coverage. No earnings calendar without FMP.
    """
    if os.getenv("FMP_API_KEY"):
        return CascadeProvider(FMPProvider(), SECProvider())
    return SECProvider()
