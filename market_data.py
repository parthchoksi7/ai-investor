"""
market_data.py — Fetches stock prices and news from Polygon.io
"""

import os
import requests
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv()

POLYGON_KEY = os.getenv("POLYGON_API_KEY")

# Default tickers to fetch price data for
WATCHLIST = [
    # Mega-cap Tech / AI / Cloud (15)
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "TSLA",
    "ORCL", "IBM", "INTC", "QCOM", "TXN", "MU", "AMAT",
    # Software / SaaS (12)
    "CRM", "ADBE", "NOW", "SNOW", "DDOG", "ZS", "CRWD", "PANW",
    "TEAM", "WDAY", "MDB", "NET",
    # Semiconductors (5)
    "AMD", "AVGO", "ARM", "MRVL", "SMCI",
    # Consumer Tech / Internet (6)
    "NFLX", "SPOT", "UBER", "ABNB", "BKNG", "EBAY",
    # Financials (11)
    "JPM", "BAC", "WFC", "GS", "MS", "C", "BLK", "AXP", "V", "MA", "PYPL",
    # Healthcare / Biotech / Pharma (14)
    "JNJ", "UNH", "LLY", "ABBV", "PFE", "MRK", "BMY", "GILD", "AMGN",
    "REGN", "VRTX", "ISRG", "TMO", "DHR",
    # Consumer Discretionary / Retail (11)
    "HD", "LOW", "TGT", "WMT", "COST", "NKE", "SBUX", "MCD", "CMG",
    "LULU", "TJX",
    # Energy (7)
    "XOM", "CVX", "COP", "EOG", "SLB", "OXY", "NEE",
    # Industrials / Aerospace (8)
    "CAT", "DE", "HON", "GE", "RTX", "LMT", "BA", "UPS",
    # Materials / Real Estate (6)
    "FCX", "NEM", "LIN", "AMT", "PLD", "EQIX",
    # Crypto-adjacent (2)
    "COIN", "MSTR",
    # ETF Benchmarks (3)
    "SPY", "QQQ", "PLTR",
]


def get_price(ticker):
    """Get the latest closing price for a ticker."""
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/prev"
    params = {"apiKey": POLYGON_KEY}

    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get("results"):
            result = data["results"][0]
            return {
                "ticker": ticker,
                "close": result["c"],
                "open": result["o"],
                "high": result["h"],
                "low": result["l"],
                "volume": result["v"],
                "change_pct": round(((result["c"] - result["o"]) / result["o"]) * 100, 2)
            }
    except Exception as e:
        print(f"   ⚠ Could not fetch price for {ticker}: {e}")

    return None


def get_news_summary():
    """Get recent market news headlines."""
    url = "https://api.polygon.io/v2/reference/news"
    params = {
        "apiKey": POLYGON_KEY,
        "limit": 10,
        "order": "desc"
    }

    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        headlines = [item["title"] for item in data.get("results", [])]
        return headlines
    except Exception as e:
        print(f"   ⚠ Could not fetch news: {e}")
        return []


def get_market_snapshot():
    """
    Returns a full snapshot of the market:
    - Price data for all tickers in the watchlist
    - Recent news headlines
    """
    prices = {}
    for ticker in WATCHLIST:
        data = get_price(ticker)
        if data:
            prices[ticker] = data

    news = get_news_summary()

    return {
        "prices": prices,
        "news": news,
        "date": date.today().strftime("%Y-%m-%d")
    }
