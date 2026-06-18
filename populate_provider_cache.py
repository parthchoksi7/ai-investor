#!/usr/bin/env python3
"""
populate_provider_cache.py — Bootstrap provider_cache.json for all 100 watchlist
tickers using SEC EDGAR (no API key required).

NOTE: SEC EDGAR (data.sec.gov) blocks residential IPs via Akamai CDN. This script
only works from GitHub Actions IP ranges. The preferred way to trigger a full
refresh is via workflow_dispatch with full_refresh=true:

    gh workflow run market_data.yml --repo parthchoksi7/ai-investor -f full_refresh=true

That dispatches fetch_snapshot.py with FULL_REFRESH=true, which bypasses the
alternate-day 50/50 group split and populates all 100 tickers in a single run.
"""
import json
import time
from datetime import date

from data_providers import SECProvider

WATCHLIST = [
    "TMO", "CVX", "ARM", "SNOW", "UNH", "AMT", "COST", "LMT", "UBER", "EQIX",
    "NET", "DDOG", "MSTR", "OXY", "ADBE", "C", "NKE", "COIN", "VRTX", "NFLX",
    "GE", "SPY", "NOW", "BMY", "AXP", "PFE", "MU", "CMG", "IBM", "GOOG",
    "LULU", "DHR", "SMCI", "ABNB", "MS", "LOW", "CRWD", "BKNG", "TEAM", "TSLA",
    "MA", "HD", "AAPL", "EBAY", "ORCL", "QQQ", "WFC", "BLK", "SLB", "GILD",
    "GOOGL", "MRVL", "XOM", "JPM", "CRM", "AVGO", "ZS", "TGT", "PLD", "COP",
    "AMD", "ABBV", "UPS", "V", "HON", "SPOT", "PANW", "WDAY", "MSFT", "JNJ",
    "PYPL", "NEE", "WMT", "FCX", "LIN", "SBUX", "CAT", "QCOM", "TXN", "ISRG",
    "AMZN", "INTC", "LLY", "EOG", "MDB", "NVDA", "AMAT", "BA", "AMGN", "TJX",
    "MCD", "META", "REGN", "MRK", "PLTR", "BAC", "GS", "RTX", "DE", "NEM",
]

CACHE_PATH = "provider_cache.json"

def main():
    today_str = date.today().isoformat()
    provider = SECProvider()

    cache: dict = {}
    try:
        with open(CACHE_PATH) as f:
            cache = json.load(f)
        print(f"Loaded existing cache ({len(cache)} entries)")
    except FileNotFoundError:
        print("No existing cache — starting fresh")

    total = len(WATCHLIST)
    fetched = skipped = no_data = 0

    print(f"\nFetching SEC EDGAR data for {total} tickers...\n")

    for i, ticker in enumerate(WATCHLIST, 1):
        existing = cache.get(ticker, {})
        if existing.get("fundamentals"):
            print(f"  [{i:>3}/{total}] {ticker:<6}  skip (already has data)")
            skipped += 1
            continue

        print(f"  [{i:>3}/{total}] {ticker:<6}  fetching...", end="", flush=True)
        try:
            result = provider.fundamentals(ticker)
            cache[ticker] = {
                "fundamentals":  result,
                "next_earnings": None,
                "fetched":       today_str,
            }
            if result:
                print(f"  {list(result.keys())}")
                fetched += 1
            else:
                print("  no EDGAR data")
                no_data += 1
        except Exception as e:
            print(f"  ERROR: {e}")
            no_data += 1

        time.sleep(0.12)  # ~8 req/s — safely under EDGAR's 10 req/s limit

    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)

    has_data = sum(1 for e in cache.values() if e.get("fundamentals"))
    print(f"\nDone.")
    print(f"  Fetched:  {fetched}")
    print(f"  Skipped:  {skipped} (already had data)")
    print(f"  No data:  {no_data} (non-US / ETF / not on EDGAR)")
    print(f"  Total with fundamentals: {has_data}/{total}")
    print(f"\nCommit provider_cache.json to git so GH Actions uses it as a seed.")


if __name__ == "__main__":
    main()
