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

WATCHLIST = [
    # Mega-cap Tech / AI / Cloud
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "TSLA",
    "ORCL", "IBM", "INTC", "QCOM", "TXN", "MU", "AMAT",
    # Software / SaaS
    "CRM", "ADBE", "NOW", "SNOW", "DDOG", "ZS", "CRWD", "PANW",
    "TEAM", "WDAY", "MDB", "NET",
    # Semiconductors
    "AMD", "AVGO", "ARM", "MRVL", "SMCI",
    # Consumer Tech / Internet
    "NFLX", "SPOT", "UBER", "ABNB", "BKNG", "EBAY",
    # Financials
    "JPM", "BAC", "WFC", "GS", "MS", "C", "BLK", "AXP", "V", "MA", "PYPL",
    # Healthcare / Biotech / Pharma
    "JNJ", "UNH", "LLY", "ABBV", "PFE", "MRK", "BMY", "GILD", "AMGN",
    "REGN", "VRTX", "ISRG", "TMO", "DHR",
    # Consumer Discretionary / Retail
    "HD", "LOW", "TGT", "WMT", "COST", "NKE", "SBUX", "MCD", "CMG",
    "LULU", "TJX",
    # Energy
    "XOM", "CVX", "COP", "EOG", "SLB", "OXY", "NEE",
    # Industrials / Aerospace
    "CAT", "DE", "HON", "GE", "RTX", "LMT", "BA", "UPS",
    # Materials / Real Estate
    "FCX", "NEM", "LIN", "AMT", "PLD", "EQIX",
    # Crypto-adjacent
    "COIN", "MSTR",
    # ETF Benchmarks
    "SPY", "QQQ", "PLTR",
]

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
        params = {"apiKey": POLYGON_KEY, "sort": "asc", "limit": days}
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

    with open(FUNDAMENTALS_CACHE, "w") as f:
        json.dump(cache, f)

    return result


def get_news_summary() -> list[dict]:
    url = "https://api.polygon.io/v2/reference/news"
    params = {"apiKey": POLYGON_KEY, "limit": 20, "order": "desc"}
    try:
        r = requests.get(url, params=params, timeout=10)
        return [
            {"title": item.get("title", ""), "tickers": item.get("tickers", [])}
            for item in r.json().get("results", [])
        ]
    except Exception as e:
        print(f"   ⚠ News fetch failed: {e}")
        return []


def get_market_snapshot() -> dict:
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

    # Check 1: local file (written by fetch_snapshot.py in dev, or as fallback)
    snapshot_path = "market_snapshot.json"
    if os.path.isfile(snapshot_path):
        with open(snapshot_path) as f:
            cached = json.load(f)
        if cached.get("date") == today_str:
            print(f"   📦 Loaded market_snapshot.json ({len(cached.get('prices', {}))} tickers)")
            return cached

    # Check 2: Supabase (written by GitHub Actions fetch_snapshot.py at 9:20 AM ET)
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY")
    if supabase_url and supabase_key:
        try:
            from supabase import create_client
            _sb = create_client(supabase_url, supabase_key)
            res = _sb.table("market_snapshots").select("snapshot").eq("date", today_str).execute()
            if res.data:
                cached = json.loads(res.data[0]["snapshot"])
                print(f"   ☁️  Loaded market_snapshot from Supabase ({len(cached.get('prices', {}))} tickers)")
                return cached
        except Exception:
            pass  # fall through to Polygon fetch

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

    articles = get_news_summary()

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

    # Fallback: load MCP-injected market data when Polygon/yfinance are blocked
    if not prices:
        mcp_path = "mcp_market_data.json"
        if os.path.isfile(mcp_path):
            with open(mcp_path) as _f:
                mcp_data = json.load(_f)
            prices = mcp_data.get("prices", {})
            history = mcp_data.get("history", {})
            print(f"   📡 Loaded {len(prices)} tickers from mcp_market_data.json")
            # Reload fundamentals cache so quant scores use cached data
            fundamentals = get_all_fundamentals(list(prices.keys()))

    return {
        "date":            date.today().strftime("%Y-%m-%d"),
        "fetched_at":      datetime.now(timezone.utc).isoformat(),
        "prices":          prices,
        "history":         history,
        "fundamentals":    fundamentals,
        "news":            articles,
        "news_discovered": news_discovered,
    }
