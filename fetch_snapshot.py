"""
fetch_snapshot.py — Fetches the daily market snapshot and uploads to Supabase.
Run by GitHub Actions at 8:00 AM ET, 105 min before the Anthropic cloud routine.

Always writes market_snapshot.json to disk so the git commit step can push it
to the repo. The cloud routine reads it from the repo — Supabase is blocked
by Anthropic's cloud network policy.
"""

import json
import os
import sys
from market_data import get_market_snapshot


print("Fetching market snapshot...")
# force=True bypasses the local-file and Supabase caches so every GH Actions run
# always fetches live from Polygon. Those caches exist for the cloud routine only.
snapshot = get_market_snapshot(force=True)

history_depths = [len(h) for h in snapshot.get("history", {}).values()]
min_depth = min(history_depths) if history_depths else 0
real_scores = sum(1 for v in snapshot.get("prices", {}).values() if v)  # rough proxy

print(
    f"Snapshot ready: "
    f"{len(snapshot['prices'])} tickers | "
    f"{len(snapshot.get('news', []))} news articles | "
    f"min history depth: {min_depth} bars | "
    f"source: {snapshot.get('_source', 'unknown')}"
)

if min_depth < 22:
    print(f"ERROR: Snapshot has only {min_depth} history bars — insufficient for quant scoring (need 22+).")
    print("This means Polygon was unreachable or returned empty data. Cloud routine will abort.")
    sys.exit(1)

# Always write market_snapshot.json so GitHub Actions can commit it to the repo.
# The cloud routine (Anthropic, 9:45 AM ET) reads this file — Supabase is blocked there.
with open("market_snapshot.json", "w") as f:
    json.dump(snapshot, f)
print(f"Saved market_snapshot.json (date={snapshot['date']}, {len(snapshot['prices'])} tickers)")

# Also upload to Supabase for website and health_check.yml use.
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
        # Log but don't fail — the committed file is the authoritative path for the cloud routine.
        print(f"WARNING: Supabase upload failed — {e}")
        print("market_snapshot.json was still written and will be committed to the repo.")
else:
    print("Supabase not configured — skipping upload (local file is sufficient).")
