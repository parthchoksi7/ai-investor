"""
market_data.py — Fetches stock prices, history, fundamentals, and news.
Primary: Polygon.io (local). Fallback: yfinance (cloud, no API key needed).
"""

import json
import os
import re
import requests
from datetime import date, datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

POLYGON_KEY = os.getenv("POLYGON_API_KEY")
FUNDAMENTALS_CACHE = "fundamentals_cache.json"
PROVIDER_CACHE = "provider_cache.json"   # provider enrichment (#1), alternate-day 50/50 cache

# The trading/scoring universe is owned by universe.py (single source of truth), so
# the coverage gate + fetch cursor can reason about it without importing this module.
# WATCHLIST is the always-active CORE (100 names); the gated ~400-name expansion
# (universe.EXPANDED_UNIVERSE) is admitted only past the coverage floor + operator
# flag — see universe.get_active_universe. Aliased here for backward compatibility.
from universe import CORE_UNIVERSE as WATCHLIST

SP500_HOLDINGS = {
    "AAPL":  0.070,
    "MSFT":  0.065,
    "NVDA":  0.060,
    "AMZN":  0.040,
    "GOOGL": 0.035,
    "META":  0.030,
    "TSLA":  0.018,
    "AVGO":  0.018,
    "JPM":   0.015,
    "LLY":   0.013,
}


def _history_yfinance(ticker: str, days: int = 210) -> list[dict]:
    """Fetch OHLCV history from Yahoo Finance (no API key, used when Polygon is blocked)."""
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).history(period=f"{days + 60}d", auto_adjust=True)
        if df.empty:
            return []
        return [
            {
                "date":   int(idx.timestamp() * 1000),
                "open":   float(row["Open"]),
                "high":   float(row["High"]),
                "low":    float(row["Low"]),
                "close":  float(row["Close"]),
                "volume": float(row["Volume"]),
            }
            for idx, row in df.iterrows()
        ][-days:]
    except Exception as e:
        print(f"   ⚠ yfinance failed for {ticker}: {e}")
        return []


def _fundamentals_yfinance(ticker: str) -> dict | None:
    """Fetch basic fundamentals from Yahoo Finance as fallback."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        result = {}
        if info.get("grossMargins")     is not None: result["gross_margin"]     = round(float(info["grossMargins"]), 4)
        if info.get("operatingMargins") is not None: result["operating_margin"] = round(float(info["operatingMargins"]), 4)
        rev = info.get("totalRevenue")
        fcf = info.get("freeCashflow")
        if rev and fcf: result["fcf_margin"] = round(float(fcf) / float(rev), 4)
        if info.get("debtToEquity") is not None: result["debt_to_equity"] = round(float(info["debtToEquity"]) / 100, 4)
        if info.get("trailingPE")   is not None: result["pe_ratio"]       = round(float(info["trailingPE"]), 2)
        if info.get("enterpriseToEbitda") is not None: result["ev_ebitda"] = round(float(info["enterpriseToEbitda"]), 2)
        return result or None
    except Exception:
        return None


def get_extended_history(ticker: str, days: int = 210) -> list[dict]:
    """Returns up to `days` trading days of daily OHLCV bars, oldest first.
    Tries Polygon first; falls back to yfinance if Polygon is unavailable or blocked.
    """
    if POLYGON_KEY:
        to_date   = date.today().strftime("%Y-%m-%d")
        from_date = (date.today() - timedelta(days=days + 90)).strftime("%Y-%m-%d")
        url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{from_date}/{to_date}"
        # adjusted=true EXPLICITLY (not left to the API default): an unadjusted split
        # reads as a ~-50% one-day crash and poisons momentum/vol for that name (P0-3).
        params = {"apiKey": POLYGON_KEY, "sort": "asc", "limit": days, "adjusted": "true"}
        try:
            r = requests.get(url, params=params, timeout=10)
            results = r.json().get("results", [])
            if results:
                return [
                    {"date": res["t"], "open": res["o"], "high": res["h"],
                     "low": res["l"], "close": res["c"], "volume": res["v"]}
                    for res in results
                ]
        except Exception as e:
            print(f"   ⚠ Polygon history failed for {ticker}: {e}")

    return _history_yfinance(ticker, days)


def get_price(ticker: str) -> dict | None:
    """Returns the latest price snapshot for a ticker."""
    history = get_extended_history(ticker, days=7)
    if len(history) >= 2:
        prev, curr = history[-2], history[-1]
        change_pct = round(((curr["close"] - prev["close"]) / prev["close"]) * 100, 2)
    elif len(history) == 1:
        curr = history[0]
        change_pct = round(((curr["close"] - curr["open"]) / curr["open"]) * 100, 2) if curr["open"] else 0
    else:
        return None
    return {
        "ticker":     ticker,
        "close":      curr["close"],
        "open":       curr["open"],
        "high":       curr["high"],
        "low":        curr["low"],
        "volume":     curr["volume"],
        "change_pct": change_pct,
    }


def _fetch_fundamentals(ticker: str) -> dict | None:
    """Fetch annual financial metrics. Tries Polygon first, falls back to yfinance."""
    if not POLYGON_KEY:
        return _fundamentals_yfinance(ticker)

    url = "https://api.polygon.io/vX/reference/financials"
    params = {"apiKey": POLYGON_KEY, "ticker": ticker, "timeframe": "annual", "limit": 1}
    try:
        r = requests.get(url, params=params, timeout=10)
        results = r.json().get("results", [])
        if not results:
            return None

        f   = results[0].get("financials", {})
        inc = f.get("income_statement", {})
        bal = f.get("balance_sheet", {})
        cf  = f.get("cash_flow_statement", {})

        def v(section: dict, key: str) -> float | None:
            entry = section.get(key, {})
            return entry.get("value") if isinstance(entry, dict) else None

        revenues    = v(inc, "revenues")
        gross       = v(inc, "gross_profit")
        op_income   = v(inc, "operating_income_loss")
        equity      = v(bal, "equity")
        lt_debt     = v(bal, "long_term_debt") or 0.0
        ocf         = v(cf,  "net_cash_flow_from_operating_activities")
        capex       = v(cf,  "capital_expenditure")

        result: dict = {}
        if revenues and revenues > 0:
            if gross     is not None: result["gross_margin"]     = round(gross     / revenues, 4)
            if op_income is not None: result["operating_margin"] = round(op_income / revenues, 4)
            if ocf is not None and capex is not None:
                result["fcf_margin"] = round((ocf - abs(capex)) / revenues, 4)
        if equity and equity > 0:
            result["debt_to_equity"] = round(lt_debt / equity, 4)

        return result if result else None
    except Exception:
        return _fundamentals_yfinance(ticker)


def get_all_fundamentals(tickers: list[str]) -> dict:
    """Fetch fundamentals for all tickers, using a weekly file cache to limit API calls."""
    cache: dict = {}
    if os.path.isfile(FUNDAMENTALS_CACHE):
        with open(FUNDAMENTALS_CACHE) as f:
            cache = json.load(f)

    week_ago = (date.today() - timedelta(days=7)).isoformat()
    today    = date.today().isoformat()
    result   = {}

    for ticker in tickers:
        entry = cache.get(ticker, {})
        if entry.get("fetched", "") > week_ago:
            result[ticker] = entry.get("data")
        else:
            data = _fetch_fundamentals(ticker)
            cache[ticker] = {"data": data, "fetched": today}
            result[ticker] = data

    tmp = FUNDAMENTALS_CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, FUNDAMENTALS_CACHE)

    return result


def get_news_summary(limit: int = 50) -> list[dict]:
    if not POLYGON_KEY:
        return []
    url = "https://api.polygon.io/v2/reference/news"
    params = {"apiKey": POLYGON_KEY, "limit": limit, "order": "desc"}
    try:
        r = requests.get(url, params=params, timeout=10)
        return [
            {
                "title":         item.get("title", ""),
                "description":   (item.get("description") or "")[:300],
                "published_utc": item.get("published_utc", ""),
                "tickers":       item.get("tickers", []),
            }
            for item in r.json().get("results", [])
        ]
    except Exception as e:
        print(f"   ⚠ News fetch failed: {e}")
        return []


def get_ticker_news(ticker: str, limit: int = 5) -> list[dict]:
    """Fetch recent news specifically for a single ticker."""
    if not POLYGON_KEY:
        return []
    url = "https://api.polygon.io/v2/reference/news"
    params = {"apiKey": POLYGON_KEY, "ticker": ticker, "limit": limit, "order": "desc"}
    try:
        r = requests.get(url, params=params, timeout=10)
        return [
            {
                "title":         item.get("title", ""),
                "description":   (item.get("description") or "")[:300],
                "published_utc": item.get("published_utc", ""),
                "tickers":       item.get("tickers", []),
            }
            for item in r.json().get("results", [])
        ]
    except Exception as e:
        print(f"   ⚠ Ticker news failed for {ticker}: {e}")
        return []


def _provider_group(ticker: str) -> int:
    """Stable alternate-day group (0 or 1) for a ticker via a deterministic hash.

    Python's built-in hash() is per-process salted, so it would not be stable
    across runs — use hashlib. Half the universe is in each group; a group refreshes
    on the days whose ordinal matches it, so each ticker refreshes every ~2 days and
    any single day fetches only ~half the universe.
    """
    import hashlib
    return int(hashlib.md5(ticker.encode()).hexdigest(), 16) % 2


# Absolute coverage floor (IPS Appendix A). A steady low coverage — not a drop —
# was the June bug, so the gate is an ABSOLUTE floor, not a week-over-week delta.
FUNDAMENTAL_COVERAGE_FLOOR_PCT = 80.0


def _compute_fundamental_coverage(all_tickers: list, fundamentals: dict,
                                  cik_map_ok: bool | None) -> dict:
    """Measure real fundamental coverage over the active universe.

    Coverage silently collapsing (the SEC CIK-map swallow) was invisible before
    this: it is now a first-class ``data_quality`` field on the snapshot so the
    observability layer (Phase 3) can gate the strategy shift on the IPS floor. The
    quality/valuation counts come from the ONE shared `data_providers.fundamental_coverage`
    so the backtest and live paths never disagree. ``coverage_ok`` gates on the
    QUALITY floor (EDGAR-achievable); valuation is reported for transparency but is
    structurally capped by FMP's free tier and is NOT part of the gate.
    ``cik_map_ok`` is None when the provider path isn't SEC-backed (e.g. tests).
    """
    from data_providers import fundamental_coverage
    cov = fundamental_coverage(all_tickers, fundamentals)
    cov["coverage_floor_pct"] = FUNDAMENTAL_COVERAGE_FLOOR_PCT
    cov["coverage_ok"]        = cov["fundamental_coverage_pct"] >= FUNDAMENTAL_COVERAGE_FLOOR_PCT
    cov["cik_map_ok"]         = cik_map_ok
    return cov


def _enrich_with_provider(all_tickers: list, fundamentals: dict, today=None):
    """Overlay real provider fundamentals + a verified earnings calendar onto the
    snapshot, using an ALTERNATE-DAY 50/50 cache.

    The universe is split into two hash groups; one group refreshes each day
    (alternating by calendar-date ordinal), and only if its cached entry is missing
    or ≥ 2 days old. ~½ the universe (~50 tickers) is fetched per day, each ticker
    refreshes every ~2 days, and the full universe is covered in 2 days.

    Provider selection (from data_providers.get_provider()):
      - FMP_API_KEY set → FMPProvider: all 6 factors + earnings calendar (250/day limit).
      - No key          → SECProvider: 3 quality factors from EDGAR (free, no limit).
      - StubProvider    → immediate no-op (test injection point).

    Mutates `fundamentals` with the provider overlay; returns earnings_calendar.
    """
    from datetime import date as _date
    earnings_calendar: dict = {}

    from data_providers import get_provider, StubProvider
    provider = get_provider()
    if isinstance(provider, StubProvider):
        return earnings_calendar, None   # test stub → no-op, no HTTP; coverage unmeasured

    # Surface a SEC CIK-map load failure loudly. cik_map_ok() loads the map once;
    # if it failed, every SEC lookup would return None (0% coverage) with no trace —
    # exactly the swallowed failure mode. We still proceed (FMP data, if any, and the
    # warm provider_cache remain valid), but the failure is recorded on data_quality.
    cik_map_ok = None
    if hasattr(provider, "cik_map_ok"):
        cik_map_ok = provider.cik_map_ok()
        if not cik_map_ok:
            # SEC EDGAR is the fallback for names FMP's free tier misses. If it's
            # unreachable (it blocks residential IPs), FMP data + the warm cache are
            # still valid — this is a coverage caveat, not a run failure.
            print("   ⚠ SEC EDGAR CIK map unavailable — SEC-fallback fundamentals off "
                  "this run (FMP data + cached entries still apply); recorded in data_quality.")

    today = today or _date.today()
    today_group = today.toordinal() % 2               # alternates every calendar day

    cache: dict = {}
    if os.path.isfile(PROVIDER_CACHE):
        try:
            with open(PROVIDER_CACHE) as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    full_refresh = os.getenv("FULL_REFRESH", "").lower() in ("1", "true")
    refreshed = 0
    for t in all_tickers:
        entry = cache.get(t)
        age = None
        if entry and entry.get("fetched"):
            try:
                age = (today - _date.fromisoformat(entry["fetched"])).days
            except (ValueError, TypeError):
                age = None
        # Coverage-aware TTL: a ticker that returned real data refreshes every 2
        # days; one that came back empty (non-US, ADR, or FMP premium-only) waits
        # 7 days before re-checking so the daily budget isn't burned on misses.
        # full_refresh bypasses the TTL entirely — a manual "refresh all" must be
        # able to recover stale EMPTY entries (e.g. the ones the SEC-403 era wrote),
        # otherwise those empties are pinned for 7 days and coverage can't heal.
        has_data = bool(entry and (entry.get("fundamentals") or entry.get("next_earnings")))
        ttl = 2 if (entry is None or has_data) else 7
        due = full_refresh or entry is None or age is None or age >= ttl
        if (full_refresh or _provider_group(t) == today_group) and due:
            entry = {"fundamentals":  provider.fundamentals(t),
                     "next_earnings": provider.next_earnings_date(t),
                     "fetched":       today.isoformat()}
            cache[t] = entry
            refreshed += 1
        if entry:
            if entry.get("fundamentals"):
                fundamentals[t] = {**(fundamentals.get(t) or {}), **entry["fundamentals"]}
            if entry.get("next_earnings"):
                earnings_calendar[t] = entry["next_earnings"]

    tmp = PROVIDER_CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=2)
    os.replace(tmp, PROVIDER_CACHE)
    data_quality = _compute_fundamental_coverage(all_tickers, fundamentals, cik_map_ok)
    if refreshed or earnings_calendar:
        print(f"   📅 Provider enrichment: refreshed {refreshed} ticker(s) today; "
              f"{len(earnings_calendar)} earnings date(s) live")
    print(f"   📊 Fundamental coverage: {data_quality['fundamental_coverage_pct']}% "
          f"({data_quality['fundamentals_covered']}/{data_quality['active_universe']}) "
          f"floor={FUNDAMENTAL_COVERAGE_FLOOR_PCT}% "
          f"{'OK' if data_quality['coverage_ok'] else '⚠ BELOW FLOOR'}")
    return earnings_calendar, data_quality


def get_market_snapshot(force: bool = False) -> dict:
    """
    Full market snapshot:
    - prices:         {ticker: current snapshot}
    - history:        {ticker: [210-day OHLCV list]}
    - fundamentals:   {ticker: financial metrics} (weekly cached)
    - news:           [recent headlines with tickers]
    - news_discovered:{ticker: price snapshot} for non-watchlist tickers in the news
    - date:           today's date string

    If market_snapshot.json exists and is dated today, loads it directly
    (written by the GitHub Actions market data job at 9:40 AM ET).
    """
    today_str = date.today().isoformat()

    # Check 1: local file (written by fetch_snapshot.py / GitHub Actions at 9:20 AM ET)
    snapshot_path = "market_snapshot.json"
    if not force and os.path.isfile(snapshot_path):
        with open(snapshot_path) as f:
            cached = json.load(f)
        if cached.get("date") == today_str:
            cached["_source"]    = "market_snapshot_file"
            cached["_data_date"] = cached.get("date")
            print(f"   📦 Loaded market_snapshot.json ({len(cached.get('prices', {}))} tickers)")
            return cached
        else:
            print(f"   ⚠ market_snapshot.json is stale (date={cached.get('date')}, today={today_str}) — skipping")

    # Check 2: Supabase (written by GitHub Actions fetch_snapshot.py at 9:20 AM ET)
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY")
    if not force and supabase_url and supabase_key:
        try:
            from supabase import create_client
            _sb = create_client(supabase_url, supabase_key)
            res = _sb.table("market_snapshots").select("snapshot").eq("date", today_str).execute()
            if res.data:
                cached = json.loads(res.data[0]["snapshot"])
                cached["_source"]    = "supabase"
                cached["_data_date"] = cached.get("date")
                print(f"   ☁️  Loaded market_snapshot from Supabase ({len(cached.get('prices', {}))} tickers)")
                return cached
        except Exception:
            pass  # fall through to Polygon fetch

    # Fetch news FIRST to guarantee it gets a fresh rate-limit budget (free tier: 5 calls/min).
    # The history loop burns the Polygon budget immediately via 429s; fetching news afterward
    # risks missing the reset window. News + 4 ticker-specific calls = 5 total Polygon calls.
    articles = get_news_summary()  # 1 Polygon call

    all_tickers = list(set(WATCHLIST) | set(SP500_HOLDINGS.keys()))
    prices:  dict = {}
    history: dict = {}

    for ticker in all_tickers:
        hist = get_extended_history(ticker, days=210)
        if not hist:
            continue
        history[ticker] = hist
        curr = hist[-1]
        prev = hist[-2] if len(hist) >= 2 else None
        change_pct = (
            round(((curr["close"] - prev["close"]) / prev["close"]) * 100, 2)
            if prev and prev["close"] else 0
        )
        prices[ticker] = {
            "ticker":     ticker,
            "close":      curr["close"],
            "open":       curr["open"],
            "high":       curr["high"],
            "low":        curr["low"],
            "volume":     curr["volume"],
            "change_pct": change_pct,
        }

    fundamentals = get_all_fundamentals(all_tickers)

    # Per-ticker deep-dive for top 4 movers (|change_pct| > 3%) — 4 Polygon calls.
    # Cap at 4 so that together with get_news_summary() above we stay within 5 calls/min.
    ticker_news: dict = {}
    movers = sorted(
        [t for t, p in prices.items() if abs(p.get("change_pct", 0)) > 3 and t not in ("SPY", "QQQ")],
        key=lambda t: abs(prices[t].get("change_pct", 0)),
        reverse=True,
    )[:4]
    for t in movers:
        tn = get_ticker_news(t, limit=5)
        if tn:
            ticker_news[t] = tn
            print(f"   📰 Ticker news: {t} {prices[t].get('change_pct', 0):+.1f}% ({len(tn)} articles)")

    # Discover non-watchlist tickers mentioned in news.
    # Skip preferred share tickers (Polygon format: BASE + "P" + SERIES, e.g. JPMPC = JPM pref C).
    # They're not common equity and yfinance can't resolve them.
    _pref_re = re.compile(r'^[A-Z]{2,5}P[A-Z]$')
    news_tickers = {
        t
        for article in articles
        for t in article.get("tickers", [])
        if t and t not in prices and not _pref_re.match(t)
    }
    news_discovered: dict = {}
    for ticker in sorted(news_tickers):
        data = get_price(ticker)
        if data:
            news_discovered[ticker] = data

    if news_discovered:
        print(f"   📰 News-discovered: {', '.join(news_discovered.keys())}")

    # Fallback: load MCP-injected market data when Polygon/yfinance are blocked.
    # WARNING: mcp_market_data.json only contains 2 history bars (current quotes, not
    # historical OHLCV). Quant scores will all default to 50. Pipeline should abort.
    mcp_source = None
    if not prices:
        mcp_path = "mcp_market_data.json"
        if os.path.isfile(mcp_path):
            with open(mcp_path) as _f:
                mcp_data = json.load(_f)
            prices    = mcp_data.get("prices", {})
            history   = mcp_data.get("history", {})
            mcp_source = "mcp_fallback"
            mcp_date   = mcp_data.get("date", "unknown")
            print(f"   📡 Loaded {len(prices)} tickers from mcp_market_data.json (date={mcp_date})")
            print(f"   ⚠ mcp_market_data.json has only 2 history bars — quant scores will be 50")
            # Reload fundamentals cache so quant scores use cached data
            fundamentals = get_all_fundamentals(list(prices.keys()))

    source    = mcp_source or "live_polygon_yfinance"
    data_date = mcp_data.get("date", "unknown") if mcp_source else today_str

    # ── Real-data enrichment (#1) — alternate-day 50/50 cache (FMP or SEC EDGAR) ──
    earnings_calendar: dict = {}
    data_quality: dict | None = None
    try:
        earnings_calendar, data_quality = _enrich_with_provider(all_tickers, fundamentals)
    except Exception as e:
        print(f"   ⚠ provider enrichment skipped: {e}")

    if data_quality is None:
        data_quality = _compute_fundamental_coverage(all_tickers, fundamentals, None)

    # Corporate-action / bad-print guard (P0-3): flag suspect 1-day moves so an
    # unhandled split or bad print is surfaced, not silently scored. Detection only.
    try:
        from corporate_actions import detect_price_outliers
        outliers = detect_price_outliers(history)
        data_quality["price_outliers"] = outliers
        data_quality["price_outlier_count"] = len(outliers)
        if outliers:
            top = outliers[0]
            print(f"   ⚠ {len(outliers)} suspect 1-day price move(s) flagged "
                  f"(worst: {top['ticker']} {top['change_pct']}% on {top['date']})")
    except Exception as e:
        print(f"   ⚠ price-outlier scan skipped: {e}")

    return {
        "date":             today_str,
        "fetched_at":       datetime.now(timezone.utc).isoformat(),
        "_source":          source,
        "_data_date":       data_date,
        "prices":           prices,
        "history":          history,
        "fundamentals":     fundamentals,
        "data_quality":     data_quality,
        "earnings_calendar": earnings_calendar,
        "news":             articles,
        "ticker_news":      ticker_news,
        "news_discovered":  news_discovered,
    }
