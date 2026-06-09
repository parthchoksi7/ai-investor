"""
fetch_snapshot.py — Fetches the daily market snapshot and uploads to Supabase.
Run by GitHub Actions at 9:20 AM ET, 25 minutes before the Anthropic cloud routine.
"""

import json
import os
from market_data import get_market_snapshot

print("Fetching market snapshot...")
snapshot = get_market_snapshot()

print(
    f"Snapshot ready: "
    f"{len(snapshot['prices'])} tickers | "
    f"{len(snapshot.get('news', []))} news articles | "
    f"{sum(1 for v in snapshot.get('fundamentals', {}).values() if v)} tickers with fundamentals"
)

# Upload to Supabase so the cloud routine can read it without git
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_SERVICE_KEY")
if supabase_url and supabase_key:
    try:
        from supabase import create_client
        client = create_client(supabase_url, supabase_key)
        client.table("market_snapshots").upsert(
            {"date": snapshot["date"], "snapshot": json.dumps(snapshot)},
            on_conflict="date",
        ).execute()
        print(f"Uploaded to Supabase market_snapshots (date={snapshot['date']})")
    except Exception as e:
        print(f"ERROR: Supabase upload failed — {e}")
        raise  # fail the workflow so GitHub Actions alerts on it
else:
    # No Supabase credentials — write local file (dev fallback)
    with open("market_snapshot.json", "w") as f:
        json.dump(snapshot, f)
    print("Saved market_snapshot.json (Supabase not configured)")
