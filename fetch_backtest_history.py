"""
fetch_backtest_history.py — build a long OHLCV archive for the backtest harness.

WHY THIS EXISTS
───────────────
The committed `market_snapshot.json` carries ~210 bars by design — enough for the live
scoring path, and deliberately capped so the daily commit stays small (§12.4). That
cap is fine for trading and useless for validation.

Phase 3 of the beta/alpha split needs a 125-bar warmup before `beta_stable_available`
is true, which leaves **85 usable sessions** in a 210-bar snapshot. Every conclusion
drawn on that window has to be caveated into meaninglessness — and it already has
been, twice: Phase 2's "improvement" and Phase 3's realized-beta gap both ended in
"this data cannot settle it."

This tool fetches ~2 years per ticker into a SEPARATE archive so the harness can warm
up properly and still have ~375 sessions to test on.

WHAT IT DOES NOT TOUCH
──────────────────────
`market_snapshot.json` — the file the live routine reads. This writes only to
`backtest_history.json`, which is gitignored: it is large, entirely regenerable, and
carries no information the live path needs. Research tooling, zero order code.

Resumable by design: the archive is re-read on start and any ticker already holding
`--min-bars` is skipped, so a rate-limit stall or a Ctrl-C costs only the ticker in
flight. Polygon's free tier is 5 calls/minute; `--sleep` paces the loop and
`get_extended_history` already backs off on a 429.

    python fetch_backtest_history.py                 # full universe, ~2y
    python fetch_backtest_history.py --days 1100     # ~3y
    python fetch_backtest_history.py --only SPY,QQQ  # top up specific names
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

ARCHIVE = "backtest_history.json"
DEFAULT_DAYS = 760          # ~2 calendar years -> ~500 trading sessions
DEFAULT_MIN_BARS = 400      # below this a ticker is considered not yet archived
BENCHMARKS = ("SPY", "QQQ")


def _iso(epoch_ms) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


class _ArchiveLock:
    """Exclusive lock around the archive for the life of a fetch run.

    Learned the hard way: three copies of this script once ran concurrently, and
    because each loads the archive into memory at start and checkpoints its own view,
    the last writer silently CLOBBERED a complete 174-ticker archive back down to 100
    — SPY included. Atomic writes make each save safe in isolation; they do nothing
    about two processes with divergent views of the whole file. A stale lock (from a
    killed run) is reported rather than silently stolen.
    """

    def __init__(self, path: str = ARCHIVE):
        self.lock_path = path + ".lock"
        self.fd = None

    def __enter__(self):
        try:
            self.fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.fd, str(os.getpid()).encode())
            return self
        except FileExistsError:
            try:
                with open(self.lock_path) as f:
                    holder = f.read().strip()
            except Exception:
                holder = "unknown"
            raise SystemExit(
                f"❌ {self.lock_path} exists (pid {holder}) — another fetch is running.\n"
                f"   If that process is dead, remove the lock file and re-run.")

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
        try:
            os.unlink(self.lock_path)
        except FileNotFoundError:
            pass


def load_archive(path: str = ARCHIVE) -> dict:
    if os.path.isfile(path):
        try:
            with open(path) as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("history"), dict):
                return d
        except Exception:
            print(f"   ⚠ {path} unreadable — starting a fresh archive")
    return {"history": {}, "fetched_at": None, "days_requested": None}


def save_archive(arch: dict, path: str = ARCHIVE) -> None:
    """Atomic — a Ctrl-C mid-write must not destroy hours of fetching."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(arch, f)
    os.replace(tmp, path)


def universe_from_snapshot(snapshot: str = "market_snapshot.json") -> list[str]:
    """The tickers the harness actually scores, taken from the committed snapshot so
    the archive matches the live universe rather than a separately-drifting list."""
    with open(snapshot) as f:
        snap = json.load(f)
    names = set(snap.get("history") or {})
    names.update(BENCHMARKS)
    return sorted(names)


def main() -> int:
    ap = argparse.ArgumentParser(prog="fetch_backtest_history")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help="calendar days of history to request per ticker")
    ap.add_argument("--min-bars", type=int, default=DEFAULT_MIN_BARS,
                    help="skip a ticker already holding at least this many bars")
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="seconds between calls (Polygon free tier is 5/min → 12.0)")
    ap.add_argument("--only", default="", help="comma-separated tickers to fetch")
    ap.add_argument("--checkpoint", type=int, default=10,
                    help="save the archive every N fetched tickers")
    ap.add_argument("--archive", default=ARCHIVE)
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    import market_data as md
    if not md.POLYGON_KEY:
        print("❌ No POLYGON_API_KEY — cannot build the archive.")
        return 1

    tickers = ([t.strip().upper() for t in args.only.split(",") if t.strip()]
               if args.only else universe_from_snapshot())
    with _ArchiveLock(args.archive):
        return _fetch(args, md, tickers)


def _fetch(args, md, tickers) -> int:
    arch = load_archive(args.archive)
    hist = arch["history"]

    todo = [t for t in tickers if len(hist.get(t) or []) < args.min_bars]
    print(f"📚 archive {args.archive}: {len(hist)} ticker(s) present, "
          f"{len(todo)} to fetch (of {len(tickers)} in universe)")
    if not todo:
        print("   nothing to do — archive already complete")
        return 0

    ok = failed = 0
    t_start = time.time()
    for i, t in enumerate(todo, 1):
        try:
            bars = md.get_extended_history(t, days=args.days)
        except Exception as e:                      # never lose the archive to one ticker
            bars, err = [], repr(e)
            print(f"   [{i}/{len(todo)}] {t:6} ERROR {err[:60]}")
        if bars:
            hist[t] = bars
            ok += 1
            if i % 10 == 0 or i == len(todo):
                rate = i / max(time.time() - t_start, 1e-9) * 60
                print(f"   [{i}/{len(todo)}] {t:6} {len(bars):>4} bars "
                      f"{_iso(bars[0]['date'])}..{_iso(bars[-1]['date'])}  "
                      f"({rate:.0f}/min)")
        else:
            failed += 1
            print(f"   [{i}/{len(todo)}] {t:6} no data")
        if i % args.checkpoint == 0:
            arch["history"] = hist
            save_archive(arch, args.archive)
        if args.sleep:
            time.sleep(args.sleep)

    arch["history"] = hist
    arch["fetched_at"] = datetime.now(timezone.utc).isoformat()
    arch["days_requested"] = args.days
    save_archive(arch, args.archive)

    depths = sorted(len(v) for v in hist.values() if v)
    print(f"\n✅ archive written: {len(hist)} tickers, {ok} fetched, {failed} failed")
    if depths:
        print(f"   bars: min {depths[0]}  median {depths[len(depths)//2]}  max {depths[-1]}")
        spy = hist.get("SPY") or []
        if spy:
            print(f"   SPY spans {_iso(spy[0]['date'])} .. {_iso(spy[-1]['date'])} "
                  f"({len(spy)} sessions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
