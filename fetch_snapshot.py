"""
fetch_snapshot.py — Fetches the daily market snapshot and saves to market_snapshot.json.
Run by GitHub Actions at 9:40 AM ET, 5 minutes before the Anthropic cloud routine.
"""

import json
from market_data import get_market_snapshot

print("Fetching market snapshot...")
snapshot = get_market_snapshot()

with open("market_snapshot.json", "w") as f:
    json.dump(snapshot, f)

print(
    f"Saved market_snapshot.json: "
    f"{len(snapshot['prices'])} tickers | "
    f"{len(snapshot.get('news', []))} news articles | "
    f"{sum(1 for v in snapshot.get('fundamentals', {}).values() if v)} tickers with fundamentals"
)
