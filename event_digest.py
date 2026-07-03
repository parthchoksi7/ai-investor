"""
event_digest.py — Haiku news→events digest (Phase 4 §11.3 step 4).

Turns the snapshot's raw news feed (~50 articles) into a small, deduped, per-ticker
`events.jsonl` time series that `build_dossier` folds into each dossier record. The
value-add is compression + attribution: 50 noisy headlines become a handful of
MATERIAL, ticker-specific structured events ({date, ticker, type, summary, url}) the
Wednesday agents can read at a glance instead of scanning the firehose.

Runs in GitHub Actions (the research plane can reach the Anthropic API), as Step 4 of
`fetch_snapshot.py` — BEFORE `build_dossier` (Step 5) so the dossier picks up today's
events. Uses Haiku (cheap) with a cached system prompt; the news is chunked so token
spend is bounded and a single bad chunk degrades gracefully instead of losing the run.

Design guarantees (mirroring the rest of the research pipeline):
  • Capital-integrity (§11.5): research artifact ONLY — zero order code.
  • Enrichment, never gating (§11.4): a parse failure is recorded (health DEGRADED at
    >20% chunk-failure) but NEVER blocks the pipeline — events enrich, they don't gate.
  • No look-ahead: an event dated AFTER `as_of` (a future-stamped article) is dropped.
  • Idempotent / append-only: dedup by a stable (ticker,date,type,summary) key against
    TODAY's existing rows, so re-running the same day never duplicates.

The LLM call is injected (`safe_call=`) so tests exercise the parse/dedup/no-look-ahead
logic against a stub and never hit the network.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from hashlib import sha1
from pathlib import Path

EVENTS_FILE = "events.jsonl"
BATCH_SIZE  = 20            # articles per Haiku call — bounds tokens + truncation risk
_SUMMARY_MAX = 140
_DEDUP_WINDOW_DAYS = 60     # dedup against events this many days back (the news feed
                           # re-surfaces multi-day-old articles; must exceed that span)
# Token-budget cap (§15.2 P2-13): hard ceiling on Haiku calls per run so digest cost is
# bounded regardless of feed size. At BATCH_SIZE=20 this is 300 articles/run — well above
# today's ~50-article feed, but a real guardrail once UNIVERSE_EXPANDED drives more
# ticker_news. On a cap, the newest chunks are kept (feed is order=desc) and the run is
# flagged `capped` (surfaced into data_quality_report → DEGRADED) so it is not silent.
MAX_CHUNKS = 15

# Small fixed taxonomy — the model must map to one of these (else "other").
EVENT_TYPES = {
    "earnings", "guidance", "rating_change", "price_target", "m&a",
    "product", "legal_regulatory", "executive", "capital_return", "macro", "other",
}

_SYSTEM = (
    "You extract MATERIAL, company-specific events from financial news for a systematic "
    "equity system. You are given a batch of news articles (title, description, date, and "
    "the tickers each is tagged with) plus the set of tickers the system tracks.\n\n"
    "Return ONLY a JSON array. Each element: "
    '{"ticker": <one tracked ticker the event is genuinely about>, '
    f'"type": <one of {sorted(EVENT_TYPES)}>, '
    '"summary": <=140 chars, factual, no hype>, '
    '"date": <YYYY-MM-DD, the article date>}.\n\n'
    "Rules: (1) emit an event ONLY for a tracked ticker the article is materially about — "
    "skip generic market/macro commentary and articles that merely mention a ticker in a list. "
    "(2) one event per (ticker, distinct development); do not repeat. (3) if an article is not "
    "material to any tracked ticker, emit nothing for it. (4) never invent facts or dates. "
    "Return [] if nothing is material."
)


def _cached_system_prompt():
    """Prompt-cached system block; imported lazily so tests don't require the anthropic SDK."""
    from analysis import _cached_system
    return _cached_system(_SYSTEM)


def _article_date(article: dict) -> str | None:
    """ISO date (YYYY-MM-DD) from an article's published_utc, or None."""
    pub = article.get("published_utc")
    if isinstance(pub, str) and len(pub) >= 10:
        return pub[:10]
    return None


def event_key(e: dict) -> str:
    """Stable dedup key: (ticker, date, type, FULL summary). Same event on a re-run
    hashes identically, so append-only never duplicates. Uses the whole (≤140-char)
    summary — a prefix would collide two genuinely-distinct same-day/type developments
    that share an opening clause, silently dropping a real event."""
    raw = "|".join([
        str(e.get("ticker", "")).upper(),
        str(e.get("date", ""))[:10],
        str(e.get("type", "")).lower(),
        str(e.get("summary", "")).strip().lower(),
    ])
    return sha1(raw.encode()).hexdigest()[:16]


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _fmt_batch(batch: list[dict]) -> str:
    lines = []
    for a in batch:
        lines.append(json.dumps({
            "title": (a.get("title") or "")[:200],
            "description": (a.get("description") or "")[:300],
            "date": _article_date(a),
            "tickers": [t for t in (a.get("tickers") or []) if isinstance(t, str)][:8],
        }, ensure_ascii=False))
    return "\n".join(lines)


def extract_events(news: list[dict], universe: set[str], as_of: str,
                   safe_call=None) -> tuple[list[dict], dict]:
    """Run the Haiku digest over `news`, chunked. Returns (events, stats) where stats
    carries chunk parse-success for the health signal. `safe_call` defaults to
    analysis._safe_call (injected in tests). Only tracked-universe tickers, valid types,
    and events dated <= as_of are kept (no look-ahead)."""
    if safe_call is None:
        from analysis import _safe_call as safe_call          # lazy: no SDK import in tests
    if not news or not as_of:
        # No news, or no dateable snapshot → nothing to attribute. Return empty rather
        # than risk a `date > None` TypeError downstream (as_of gates the no-look-ahead
        # comparison in _normalize_event).
        return [], {"chunks": 0, "chunks_ok": 0, "parse_success_rate": 1.0, "raw_events": 0}

    system = _cached_system_prompt()
    universe_u = {t.upper() for t in universe}
    events, chunks, chunks_ok, raw_count = [], 0, 0, 0
    universe_hint = ", ".join(sorted(universe_u))
    all_batches = list(_chunks(news, BATCH_SIZE))
    capped = len(all_batches) > MAX_CHUNKS
    batches = all_batches[:MAX_CHUNKS]             # token-budget cap: keep the newest chunks
    for batch in batches:
        chunks += 1
        user_msg = (f"TRACKED TICKERS: {universe_hint}\n\nARTICLES (one JSON per line):\n"
                    f"{_fmt_batch(batch)}\n\nReturn the JSON array of material events.")
        result, meta = safe_call(_model(), system, user_msg, default=[],
                                  max_tokens=1200, return_meta=True)
        if meta.get("parsed_ok"):
            # A parse can succeed but return a non-array: Haiku sometimes emits a lone
            # event object, or wraps the array as {"events":[...]}. Coerce all of these
            # to a list so the events aren't silently dropped AND the chunk isn't
            # miscounted as a parse failure (which would spuriously trip the DEGRADED gate).
            items = _as_event_list(result)
            chunks_ok += 1
            for e in items:
                raw_count += 1
                norm = _normalize_event(e, universe_u, as_of)
                if norm:
                    events.append(norm)
    rate = (chunks_ok / chunks) if chunks else 1.0
    return events, {"chunks": chunks, "chunks_ok": chunks_ok,
                    "parse_success_rate": round(rate, 3), "raw_events": raw_count,
                    "capped": capped, "chunks_available": len(all_batches),
                    "max_chunks": MAX_CHUNKS}


def _model():
    from analysis import MODEL_FAST
    return MODEL_FAST


def _as_event_list(result) -> list:
    """Coerce a parsed model result into a list of event dicts: a bare list passes
    through; {"events":[...]} (or the first list value) is unwrapped; a lone event
    dict is wrapped; anything else → []."""
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for v in result.values():
            if isinstance(v, list):
                return v
        return [result]                               # a single event object
    return []


def _normalize_event(e: dict, universe_u: set[str], as_of: str) -> dict | None:
    """Validate + clean one model-emitted event; drop if unusable or look-ahead."""
    if not isinstance(e, dict):
        return None
    ticker = str(e.get("ticker", "")).upper().strip()
    if ticker not in universe_u:
        return None                                   # hallucinated / untracked ticker
    etype = str(e.get("type", "")).lower().strip()
    if etype not in EVENT_TYPES:
        etype = "other"
    summary = str(e.get("summary", "")).strip()[:_SUMMARY_MAX]
    if not summary:
        return None
    # The date MUST be an ISO string (the article's published_utc is one, and Haiku is
    # asked for YYYY-MM-DD). Anything else — an int, an epoch, a garbage string — is an
    # anomaly, dropped. This closes the guard a raw epoch would otherwise slip through
    # (str(1725000000) string-compares below as_of; or an epoch-ms coercion lands on a
    # bogus 1970 date). Require a real, parseable, non-future YYYY-MM-DD.
    raw = e.get("date")
    if not isinstance(raw, str):
        return None
    date = raw[:10]
    try:
        d = datetime.strptime(date, "%Y-%m-%d").date()
        a = datetime.strptime(as_of, "%Y-%m-%d").date()
    except ValueError:
        return None                                   # unparseable date → drop
    if d > a:                                          # no look-ahead: drop future-dated
        return None
    return {"date": date, "ticker": ticker, "type": etype, "summary": summary, "url": e.get("url")}


def existing_keys(path: str = EVENTS_FILE, since: str | None = None) -> set[str]:
    """Dedup keys already in events.jsonl, over rows dated >= `since` (None = all).

    The dedup window MUST span more than one day: the Polygon news feed (order=desc,
    no date filter) re-returns the same multi-day-old article on consecutive runs, so
    an event dated 06-30 logged on the 06-30 run reappears on the 07-03 run. Bounding
    dedup to a single day would re-append it every day it persists in the feed. `since`
    (caller passes ~as_of − 60d) both catches that cross-day duplication and bounds the
    effective key set."""
    keys: set[str] = set()
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if isinstance(r, dict) and (since is None or str(r.get("date", ""))[:10] >= since):
                keys.add(event_key(r))
    except FileNotFoundError:
        pass
    return keys


def _minus_days(iso: str, days: int) -> str:
    """as_of minus `days` as an ISO date string (dedup-window lower bound)."""
    try:
        return (datetime.strptime(iso[:10], "%Y-%m-%d").date() - timedelta(days=days)).strftime("%Y-%m-%d")
    except Exception:
        return iso


def append_events(events: list[dict], path: str = EVENTS_FILE) -> int:
    """Append new events (already deduped by the caller). Plain append — the repo
    JSONL convention. Returns the count written."""
    if not events:
        return 0
    with open(path, "a") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return len(events)


def digest(snapshot: dict, universe: set[str], path: str = EVENTS_FILE,
           safe_call=None) -> dict:
    """Full run: extract events from the snapshot's news, drop dups already logged
    today, append the rest. Returns a stats dict for the health signal. Never raises
    into the pipeline (events are enrichment) — the caller wraps it too."""
    as_of = snapshot.get("_data_date") or snapshot.get("date")
    news = list(snapshot.get("news") or [])
    # Fold in per-mover ticker_news (also article dicts) so material single-name news
    # isn't missed when it's absent from the broad feed.
    for arts in (snapshot.get("ticker_news") or {}).values():
        if isinstance(arts, list):
            news.extend(arts)
    events, stats = extract_events(news, universe, as_of, safe_call=safe_call)

    # Dedup against a WINDOW (not just today): the feed re-surfaces multi-day-old
    # articles, so a same-day-only check would re-append them every day they persist.
    since = _minus_days(as_of, _DEDUP_WINDOW_DAYS) if as_of else None
    seen = existing_keys(path, since=since)
    fresh, batch_seen = [], set()
    for e in events:
        k = event_key(e)
        if k in seen or k in batch_seen:
            continue
        batch_seen.add(k)
        fresh.append(e)
    written = append_events(fresh, path)
    stats.update({"as_of": as_of, "events_written": written,
                  "events_deduped": len(events) - written})
    return stats


def main() -> int:
    from universe import CORE_UNIVERSE
    try:
        snapshot = json.loads(Path("market_snapshot.json").read_text())
    except Exception:
        print("event_digest: no market_snapshot.json — skipping")
        return 1
    try:
        stats = digest(snapshot, set(CORE_UNIVERSE))
    except Exception as e:
        print(f"event_digest: FAILED (non-fatal, events are enrichment) — {e}")
        return 2
    degraded = stats.get("chunks", 0) and stats["parse_success_rate"] < 0.8
    print(f"event_digest: as_of={stats.get('as_of')} "
          f"chunks={stats.get('chunks')} parse_ok_rate={stats.get('parse_success_rate')} "
          f"written={stats.get('events_written')} deduped={stats.get('events_deduped')}"
          f"{'  ⚠ DEGRADED (parse<80%)' if degraded else ''}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
