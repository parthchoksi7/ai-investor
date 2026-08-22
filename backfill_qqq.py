#!/usr/bin/env python3
"""One-off backfill: populate qqq_close / qqq_cumulative_return_pct on every
existing portfolio_snapshots row.

Run AFTER migrations/2026-08-22_add_qqq_benchmark.sql has been applied in the
Supabase SQL Editor. Idempotent — safe to re-run; it recomputes from source
rather than incrementing anything.

  ./venv/bin/python backfill_qqq.py --dry-run   # print the plan, write nothing
  ./venv/bin/python backfill_qqq.py             # write

── Why it aligns to SPY's session, not to the row's date ─────────────────────
`spy_close` is NOT reliably the close of the row's own date. publish.py stamps
the most recently completed session at publish time, and the historical record
carries known staleness: a morning publish stamps the prior session, and the
7/6-7/30 and 7/31-8/5 incidents (documented in publish.py) stamped a stale bar
outright. Backfilling QQQ by row date would therefore pair a same-day QQQ close
against a day-old SPY close on those rows, inventing a spread between the two
benchmarks that never existed on any real session.

So for each row we resolve the session SPY was actually priced from — by
matching spy_close back to Polygon's SPY history — and take QQQ's close from
that SAME session. Whatever staleness the SPY series carries, QQQ now carries
identically, and the two benchmark curves are directly comparable.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.request

from dotenv import load_dotenv

load_dotenv()

# Widened a week before inception so an as-of fallback always has a prior bar.
HISTORY_START = "2026-05-25"


def _polygon_closes(ticker: str, api_key: str, start: str, end: str) -> dict[str, float]:
    """{iso_date: close} of adjusted daily bars for `ticker`."""
    url = (f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}"
           f"?adjusted=true&sort=asc&limit=50000&apiKey={api_key}")
    req = urllib.request.Request(url, headers={"User-Agent": "ai-investor/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    results = data.get("results") or []
    if not results:
        raise SystemExit(f"Polygon returned no bars for {ticker}: {data.get('status')}")
    return {
        dt.datetime.fromtimestamp(b["t"] / 1000, dt.UTC).date().isoformat(): float(b["c"])
        for b in results
    }


def _resolve_session(spy_close: float, row_date: str,
                     spy_by_date: dict[str, float], sessions: list[str]) -> tuple[str, str]:
    """The session date `spy_close` was priced from.

    Exact close match, preferring the latest such session at or before the row's
    own date (SPY can print the same close twice; the row can only have come
    from a session that had already happened). Falls back to an as-of match —
    the latest session on or before the row date — when no close matches, which
    happens only if Polygon's adjusted history has been re-adjusted since the
    row was written.

    Returns (session_date, method) where method is "exact" or "as-of".
    """
    candidates = [d for d in sessions
                  if d <= row_date and abs(spy_by_date[d] - spy_close) < 0.005]
    if candidates:
        return candidates[-1], "exact"
    prior = [d for d in sessions if d <= row_date]
    if not prior:
        raise SystemExit(f"No SPY session on or before {row_date}")
    return prior[-1], "as-of"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be written and exit")
    args = ap.parse_args()

    api_key = os.getenv("POLYGON_API_KEY")
    if not api_key:
        raise SystemExit("POLYGON_API_KEY not set")
    supabase_url, supabase_key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY")
    if not (supabase_url and supabase_key):
        raise SystemExit("SUPABASE_URL / SUPABASE_SERVICE_KEY not set")

    from supabase import create_client
    client = create_client(supabase_url, supabase_key)

    rows = (client.table("portfolio_snapshots")
            .select("date, spy_close, spy_cumulative_return_pct")
            .order("date", desc=False).execute().data or [])
    if not rows:
        print("No snapshot rows — nothing to backfill.")
        return 0

    end = rows[-1]["date"]
    spy_by_date = _polygon_closes("SPY", api_key, HISTORY_START, end)
    qqq_by_date = _polygon_closes("QQQ", api_key, HISTORY_START, end)
    sessions = sorted(spy_by_date)
    print(f"Polygon: {len(spy_by_date)} SPY bars, {len(qqq_by_date)} QQQ bars "
          f"({HISTORY_START} → {end})")

    # Inception mirrors publish.py: the first row carrying benchmark data. Its
    # QQQ close is the 100 baseline, and the dividend gross-up accrues from its
    # ROW date (not its session date) so QQQ's clock matches SPY's exactly.
    from publish import QQQ_DIVIDEND_YIELD

    inception_row = next((r for r in rows if r.get("spy_close") is not None), None)
    if inception_row is None:
        print("No row has spy_close — nothing to align QQQ to.")
        return 0
    inc_session, _ = _resolve_session(float(inception_row["spy_close"]),
                                      inception_row["date"], spy_by_date, sessions)
    inc_qqq = qqq_by_date.get(inc_session)
    if not inc_qqq:
        raise SystemExit(f"No QQQ bar for inception session {inc_session}")
    inc_date = dt.date.fromisoformat(inception_row["date"])
    print(f"Inception: row {inception_row['date']} → session {inc_session}, "
          f"QQQ ${inc_qqq:.2f} (dividend gross-up {QQQ_DIVIDEND_YIELD:.2%}/yr)\n")

    updates, skipped, approx = [], 0, 0
    for r in rows:
        date_, spy_close = r["date"], r.get("spy_close")
        if spy_close is None:
            # publish.py leaves the row's benchmark untouched when it cannot get
            # a trustworthy close. QQQ must stay null on those rows too, or the
            # chart would show a Nasdaq point on a day with no S&P point.
            print(f"  {date_}  spy_close is NULL → leaving qqq NULL")
            skipped += 1
            continue
        session, method = _resolve_session(float(spy_close), date_, spy_by_date, sessions)
        if method == "as-of":
            approx += 1
        qqq_close = qqq_by_date.get(session)
        if qqq_close is None:
            print(f"  {date_}  no QQQ bar for session {session} → skipped")
            skipped += 1
            continue
        price_ret = (qqq_close - inc_qqq) / inc_qqq
        days = max(0, (dt.date.fromisoformat(date_) - inc_date).days)
        div = QQQ_DIVIDEND_YIELD * days / 365.0
        cumulative = round((price_ret + div) * 100, 4)
        spy_cum = r.get("spy_cumulative_return_pct")
        spy_txt = f"{float(spy_cum):+7.2f}%" if spy_cum is not None else "      —"
        flag = " ~" if method == "as-of" else "  "
        print(f"  {date_}  session {session}{flag} QQQ ${qqq_close:8.2f}  "
              f"qqq {cumulative:+7.2f}%   spy {spy_txt}")
        updates.append({"date": date_,
                        "qqq_close": round(qqq_close, 4),
                        "qqq_cumulative_return_pct": cumulative})

    print(f"\n{len(updates)} row(s) to write · {skipped} skipped"
          + (f" · {approx} resolved by as-of fallback (~)" if approx else ""))
    if args.dry_run:
        print("--dry-run: nothing written.")
        return 0
    if not updates:
        return 0

    # Update, not upsert: these rows already exist and carry columns this script
    # has no business touching. An upsert of a partial row would blank them.
    for u in updates:
        client.table("portfolio_snapshots").update(
            {k: v for k, v in u.items() if k != "date"}
        ).eq("date", u["date"]).execute()
    print(f"✅ Backfilled {len(updates)} row(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
