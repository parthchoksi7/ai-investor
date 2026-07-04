"""
build_dossier.py — Step 5 synthesis: the per-ticker research dossier (Phase 4, §11.3/§12.2).

THE single synthesis point of the research pipeline. It collapses the raw append-only
layer (market_snapshot + factor_history + fundamentals + events + decision_journal)
into one small, denormalized, as-of-dated record per ticker — the ONLY thing the
Wednesday decision agents read. That is what prevents "Wednesday overload": an agent
reads a ~1 KB digest per name, not 206 OHLCV bars + 50 news articles.

Capital-integrity invariant (§11.5, non-negotiable): this module runs in GitHub
Actions and writes a research artifact ONLY. It contains ZERO order code — the blast
radius of any bug here is "degraded dossier," never "unintended trade." The cloud
routine consuming the dossier for sizing/execution is a SEPARATE, later change; until
then this builds + validates + commits the dossier so the producer is proven first.

No look-ahead (§11.4): a fundamental whose `_as_of_filing` is AFTER `as_of` is dropped;
persistence is computed only within one `formula_version` (never across a re-weight
boundary — P0-2). Per-ticker `price_as_of` is stamped (P0-1) so the consumer knows how
stale each price is and can re-quote live rather than trust the slice price.

Reuses the deterministic spine unchanged: `quant_engine._pct_return`,
`compute_risk_metrics`, `_daily_returns`; `journal.get_ticker_history`,
`recently_exited`; `corporate_actions._norm_date`.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from quant_engine import _pct_return, compute_risk_metrics
from corporate_actions import _norm_date

SCHEMA_VERSION = "dossier-1.0"
DOSSIER_FILE   = "research_dossier.json"
FACTOR_HISTORY = "factor_history.jsonl"
EVENTS_FILE    = "events.jsonl"
JOURNAL_FILE   = "decision_journal.json"

# A dossier ticker record MUST carry these keys (schema validation, P1-5).
_REQUIRED_TICKER_KEYS = {
    "ticker", "as_of", "price_as_of", "price", "factors",
    "persistence", "fundamentals", "events", "earnings",
    "history_summary", "data_quality",
}
_PERSISTENCE_WINDOW = 7          # trading days for composite persistence (§3)
_FUNDAMENTALS_STALE_DAYS = 100   # honest composite drops the factor beyond this (§11.4)


# ── raw-layer readers ─────────────────────────────────────────────────────────

def _read_jsonl(path: str) -> list[dict]:
    rows = []
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    d = json.loads(line)
                    if isinstance(d, dict):
                        rows.append(d)
                except Exception:
                    continue
    except Exception:
        pass
    return rows


def _load_json(path: str, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


# ── per-ticker synthesis pieces ───────────────────────────────────────────────

def _latest_factor_row(rows: list[dict]) -> dict | None:
    """Most-recent factor_history row for a ticker (rows already filtered to it)."""
    return max(rows, key=lambda r: str(r.get("date", "")), default=None)


def _persistence(rows: list[dict], as_of: str, window: int = _PERSISTENCE_WINDOW) -> dict:
    """Composite-score persistence over the last `window` rows, WITHIN the current
    formula_version only (never mix a re-weight boundary — P0-2). Rows are this
    ticker's factor_history entries. `rank_chg_7d` is filled by the caller (rank is a
    cross-ticker property). ALWAYS returns the same key set (None when unknown) so a
    consumer can read persistence["formula_version"] / ["rank_chg_7d"] without a
    KeyError on a no-history name."""
    empty = {"composite_7d_mean": None, "composite_7d_std": None, "n": 0,
             "formula_version": None, "rank_chg_7d": None}
    if not rows:
        return empty
    latest = _latest_factor_row(rows)
    fv = latest.get("formula_version") if latest else None
    same = [r for r in rows if r.get("formula_version") == fv
            and as_of is not None and str(r.get("date", "")) <= as_of
            and isinstance(r.get("composite_score"), (int, float))]
    same.sort(key=lambda r: str(r.get("date", "")))
    vals = [float(r["composite_score"]) for r in same[-window:]]
    if not vals:
        return {**empty, "formula_version": fv}
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    return {"composite_7d_mean": round(mean, 2), "composite_7d_std": round(var ** 0.5, 2),
            "n": len(vals), "formula_version": fv, "rank_chg_7d": None}


def _history_summary(history: list[dict], spy_history: list[dict]) -> dict:
    """Multi-horizon returns + annualized vol/beta/max-drawdown from the raw bars.
    Reuses quant_engine helpers for returns + risk so the dossier can't diverge from
    the scoring spine."""
    closes = [b["close"] for b in history
              if isinstance(b.get("close"), (int, float)) and b["close"] > 0]
    # _pct_return is a PERCENT (×100); the §12.2 dossier contract uses FRACTIONS
    # (ret_21d: 0.04), so rescale. Keeps the scoring spine reused, matches the schema.
    def _frac(n):
        v = _pct_return(closes, n)
        return round(v / 100.0, 4) if v is not None else None
    out: dict = {"ret_21d": _frac(21), "ret_63d": _frac(63), "ret_126d": _frac(126)}
    risk = compute_risk_metrics(history, spy_history)
    # compute_risk_metrics returns the annualized vol under key "volatility" (NOT
    # "annualized_vol" — that key does not exist, so the old read was always None).
    out["vol_ann"] = risk.get("volatility") if risk.get("volatility_available") else None
    out["beta"] = risk.get("beta")
    out["max_dd_126d"] = _max_drawdown(closes[-126:]) if len(closes) >= 2 else None
    return out


def _max_drawdown(closes: list[float]) -> float | None:
    """Largest peak-to-trough drawdown over the series (negative fraction), or None."""
    if len(closes) < 2:
        return None
    peak, mdd = closes[0], 0.0
    for c in closes:
        peak = max(peak, c)
        if peak > 0:
            mdd = min(mdd, (c - peak) / peak)
    return round(mdd, 4)


def _fundamentals_block(fund: dict | None, as_of: str) -> tuple[dict, int | None]:
    """(fundamentals-with-age, age_days). No look-ahead: a value whose `_as_of_filing`
    is AFTER as_of is dropped. The filing date is compared as a real date (parsed), so
    a non-ISO string or an epoch-ms int can't silently bypass the guard. age_days is
    None when the filing date is unknown/unparseable — the SEC provider does not yet
    stamp `_as_of_filing` (Phase 4 follow-up), so callers must treat age=None as
    'vintage unknown', NOT 'fresh'."""
    if not isinstance(fund, dict) or not fund:
        return {}, None
    filing_iso = _norm_date(fund.get("_as_of_filing"))     # epoch-ms → ISO; passes ISO through
    age = None
    if isinstance(filing_iso, str) and as_of:
        try:
            fdt = datetime.strptime(filing_iso, "%Y-%m-%d").date()
            adt = datetime.strptime(as_of, "%Y-%m-%d").date()
        except Exception:
            fdt = adt = None
        if fdt and adt:
            if fdt > adt:                        # look-ahead: filing not yet available
                return {}, None
            age = (adt - fdt).days
    return dict(fund), age


def _earnings_block(next_date: str | None, as_of: str) -> dict:
    if not next_date:
        return {"next_date": None, "days_until": None, "imminent": False}
    try:
        d = (datetime.strptime(next_date, "%Y-%m-%d").date()
             - datetime.strptime(as_of, "%Y-%m-%d").date()).days
    except Exception:
        return {"next_date": next_date, "days_until": None, "imminent": False}
    return {"next_date": next_date, "days_until": d, "imminent": 0 <= d <= 7}


def _last_decision(ticker: str, journal: list[dict], current_close: float | None,
                   as_of: str) -> tuple[dict | None, dict | None]:
    """(last_decision, since_entry) for a held/recently-touched name, from the journal.
    last_decision anchors the entry; since_entry is the cumulative move from the entry
    price to the current close. Both None when the journal has no entry for the ticker."""
    entries = [e for e in journal if e.get("ticker") == ticker and e.get("action")]
    if not entries:
        return None, None
    last = max(entries, key=lambda e: str(e.get("date", "")))
    ld = {"date": last.get("date"), "action": last.get("action"),
          "target_weight": last.get("target_weight"), "thesis": last.get("thesis"),
          "status": last.get("status"), "confidence": last.get("confidence")}
    since = None
    entry_px = last.get("entry_price") or last.get("price")
    if isinstance(entry_px, (int, float)) and entry_px > 0 and isinstance(current_close, (int, float)):
        since = {"entry_price": entry_px, "current_price": current_close,
                 "cum_return": round((current_close - entry_px) / entry_px, 4),
                 "days_since_entry": _days_since(last.get("date"), as_of)}
    return ld, since


def _days_since(d: str | None, as_of: str) -> int | None:
    # Measure from as_of (the dossier's date), NOT date.today() — the dossier is a
    # reproducible, as-of-dated research artifact; a rebuild/backfill must yield the
    # same value, so wall-clock 'today' would make it non-deterministic.
    try:
        return (datetime.strptime(as_of, "%Y-%m-%d").date()
                - datetime.strptime(d, "%Y-%m-%d").date()).days
    except Exception:
        return None


# ── the builder ───────────────────────────────────────────────────────────────

def build_dossier(snapshot: dict, factor_rows: list[dict], journal: list[dict],
                  events_rows: list[dict], holdings: set[str] | None = None,
                  as_of: str | None = None) -> dict:
    """Synthesize research_dossier.json from the raw layer. `holdings` (current
    position tickers) get last_decision/since_entry; all scored names get the rest."""
    as_of = as_of or snapshot.get("_data_date") or snapshot.get("date")
    if not as_of:
        # No dateable snapshot → can't build an as-of-dated artifact. Fail loud
        # rather than crash mid-loop on `str(...) <= None` (TypeError) or emit a
        # dossier with as_of=None that the freshness gate can't reason about.
        raise ValueError("build_dossier: snapshot has no _data_date/date — cannot set as_of")
    price_as_of = snapshot.get("_data_date") or snapshot.get("date")
    # Stage D (P0-1): carried-forward names have an older per-ticker vintage — the
    # consumer must re-quote them live rather than size on the stale slice price.
    price_as_of_map = snapshot.get("price_as_of_by_ticker") or {}
    holdings = holdings or set()
    prices = snapshot.get("prices") or {}
    history = snapshot.get("history") or {}
    fundamentals = snapshot.get("fundamentals") or {}
    earnings_cal = snapshot.get("earnings_calendar") or {}
    spy_history = history.get("SPY") or []

    # Index the raw layer by ticker once (avoid O(n²) rescans over the universe).
    # Skip rows with a falsy ticker — a None key would otherwise pollute the
    # cross-ticker rank maps (consuming an ordinal slot) while never surfacing in output.
    factors_by_ticker: dict[str, list] = {}
    for r in factor_rows:
        rt = r.get("ticker")
        if rt:
            factors_by_ticker.setdefault(rt, []).append(r)
    events_by_ticker: dict[str, list] = {}
    for e in events_rows:
        events_by_ticker.setdefault(e.get("ticker"), []).append(
            {"date": _norm_date(e.get("date")), "type": e.get("type"),
             "summary": e.get("summary"), "url": e.get("url")})

    tickers: dict[str, dict] = {}
    for t, p in prices.items():
        if t in ("SPY", "QQQ"):
            continue                              # benchmarks, not candidates
        frows = factors_by_ticker.get(t, [])
        latest = _latest_factor_row(frows) or {}
        fund_block, fund_age = _fundamentals_block(fundamentals.get(t), as_of)
        coverage = list(latest.get("factors_used", []))
        rec = {
            "ticker": t, "as_of": as_of,
            "price_as_of": price_as_of_map.get(t, price_as_of),
            "price": {"close": p.get("close"), "change_pct": p.get("change_pct")},
            "factors": {
                "momentum": latest.get("momentum_score"),
                "quality": latest.get("quality_score"),
                "valuation": latest.get("valuation_score"),
                "volatility": latest.get("volatility_score"),
                "composite": latest.get("composite_score"),
                "factors_used": coverage,
                "formula_version": latest.get("formula_version"),
            },
            "persistence": _persistence(frows, as_of),
            "fundamentals": fund_block,
            "events": sorted(events_by_ticker.get(t, []),
                             key=lambda e: str(e.get("date", "")), reverse=True)[:10],
            "earnings": _earnings_block(earnings_cal.get(t), as_of),
            "history_summary": _history_summary(history.get(t, []), spy_history),
            "data_quality": {
                "fundamentals_age_days": fund_age,
                # None (not False) when vintage is UNKNOWN — the SEC provider does not
                # yet stamp _as_of_filing, so we must NOT imply "fresh". Only a known
                # age > threshold is affirmatively stale.
                "fundamentals_stale": (fund_age > _FUNDAMENTALS_STALE_DAYS) if fund_age is not None
                                      else (None if fund_block else False),
                "factors_fresh": str(latest.get("date", "")) == as_of,
                "coverage": coverage,
            },
        }
        if t in holdings:
            ld, since = _last_decision(t, journal, p.get("close"), as_of)
            rec["last_decision"], rec["since_entry"] = ld, since
        tickers[t] = rec

    # rank_chg_7d: composite rank today vs `window` rows ago, cross-ticker.
    _annotate_rank_change(tickers, factors_by_ticker, as_of)

    built_from = sorted({str(r.get("date")) for r in factor_rows if r.get("date")})[-_PERSISTENCE_WINDOW:]
    # Top-level formula_version: the version of the MOST RECENT factor row overall,
    # not an arbitrary first ticker (which may have had no rows → None). This is the
    # file-level provenance a re-weight-boundary check trusts.
    _newest = max(factor_rows, key=lambda r: str(r.get("date", "")), default={}) or {}
    return {
        "schema": SCHEMA_VERSION,
        "as_of": as_of,
        "price_as_of": price_as_of,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "built_from_days": built_from,
        "n_tickers": len(tickers),
        "formula_version": _newest.get("formula_version"),
        "tickers": tickers,
    }


def _annotate_rank_change(tickers: dict, factors_by_ticker: dict, as_of: str) -> None:
    """Add persistence.rank_chg_7d = (rank 7d-ago − rank today) by composite; +ve = improved.
    Prior = the composite `_PERSISTENCE_WINDOW` trading days before the latest row (a
    TRUE 7-day lookback: same[-(window+1)]), within the current formula_version only
    (P0-2), and only over numeric composite scores (mirrors _persistence)."""
    def _rank_map(pick) -> dict:
        scored = []
        for t, rows in factors_by_ticker.items():
            if t in ("SPY", "QQQ"):
                continue
            v = pick(rows)
            if isinstance(v, (int, float)):
                scored.append((t, v))
        scored.sort(key=lambda x: x[1], reverse=True)
        return {t: i + 1 for i, (t, _) in enumerate(scored)}

    def _numeric_same(rows):
        fv = (_latest_factor_row(rows) or {}).get("formula_version")
        return sorted([r for r in rows if r.get("formula_version") == fv
                       and as_of is not None and str(r.get("date", "")) <= as_of
                       and isinstance(r.get("composite_score"), (int, float))],
                      key=lambda r: str(r.get("date", "")))

    def _today(rows):
        same = _numeric_same(rows)
        return same[-1]["composite_score"] if same else None

    def _prior(rows):
        same = _numeric_same(rows)
        return same[-(_PERSISTENCE_WINDOW + 1)]["composite_score"] if len(same) >= _PERSISTENCE_WINDOW + 1 else None

    today_rank, prior_rank = _rank_map(_today), _rank_map(_prior)
    for t, rec in tickers.items():
        rt, rp = today_rank.get(t), prior_rank.get(t)
        rec["persistence"]["rank_chg_7d"] = (rp - rt) if (rt and rp) else None


# ── schema validation (P1-5) ──────────────────────────────────────────────────

def validate_dossier(dossier: dict, as_of: str | None = None) -> tuple[bool, list[str]]:
    """(ok, errors). A malformed/stale dossier must ABORT the Wednesday gate, never be
    silently traded on (P1-5). Checks top-level shape, per-ticker required keys, and —
    when `as_of` is given — freshness (`dossier.as_of == as_of` AND `built_from_days ≥ 2`)."""
    errors: list[str] = []
    if not isinstance(dossier, dict):
        return False, ["dossier is not a JSON object"]
    for k in ("schema", "as_of", "tickers", "built_from_days"):
        if k not in dossier:
            errors.append(f"missing top-level key: {k}")
    tickers = dossier.get("tickers")
    if not isinstance(tickers, dict) or not tickers:
        errors.append("tickers is empty or not an object")
    else:
        for t, rec in list(tickers.items())[:5000]:
            missing = _REQUIRED_TICKER_KEYS - set(rec.keys())
            if missing:
                errors.append(f"{t}: missing keys {sorted(missing)}")
                break                             # one representative failure is enough
    if as_of is not None:
        if dossier.get("as_of") != as_of:
            errors.append(f"stale: dossier.as_of={dossier.get('as_of')} != {as_of}")
        bfd = dossier.get("built_from_days", []) or []
        if len(bfd) < 2:
            errors.append(f"insufficient history: built_from_days={bfd} (< 2)")
        # built_from_days ≥ 2 only proves SOME history exists — it does not prove the
        # NEWEST factor data is today's. Require max(built_from_days) == as_of so a
        # snapshot that failed to refresh (old factor rows survive) is caught, not
        # passed as fresh. Pass the REAL trading date as `as_of` for this to bite.
        if bfd and max(str(d) for d in bfd) != as_of:
            errors.append(f"stale factors: newest built_from_day={max(str(d) for d in bfd)} != {as_of}")
    return (not errors), errors


def write_dossier(dossier: dict, path: str = DOSSIER_FILE) -> dict:
    tmp = path + ".tmp"
    Path(tmp).write_text(json.dumps(dossier, indent=2, default=str))
    Path(tmp).replace(path)
    return dossier


def load_dossier(path: str = DOSSIER_FILE) -> dict:
    return _load_json(path, {})


def main() -> int:
    from market_calendar import today_et
    snapshot = _load_json("market_snapshot.json", {})
    if not snapshot:
        print("build_dossier: no market_snapshot.json — nothing to build")
        return 1
    factor_rows = _read_jsonl(FACTOR_HISTORY)
    events_rows = _read_jsonl(EVENTS_FILE)
    journal = _load_json(JOURNAL_FILE, [])
    if not isinstance(journal, list):
        journal = []
    holdings = {e.get("ticker") for e in journal
                if e.get("status") == "open" and e.get("ticker")}
    try:
        dossier = build_dossier(snapshot, factor_rows, journal, events_rows, holdings=holdings)
    except Exception as e:
        print(f"build_dossier: BUILD FAILED — {e} (keeping prior research_dossier.json)")
        return 3
    # Validate against the REAL trading date (ET), not the dossier's own as_of — else
    # the freshness check is a tautology (as_of == as_of) and a stale snapshot passes.
    real_today = today_et().strftime("%Y-%m-%d")
    ok, errors = validate_dossier(dossier, as_of=real_today)
    if not ok:
        # Do NOT overwrite the committed (prior, valid) dossier with a stale/invalid one
        # — market_data.yml git-adds whatever is on disk, so writing a bad file here
        # would commit it. Leave the prior file; surface the failure (non-zero exit).
        print(f"build_dossier: NOT writing — dossier invalid/stale vs {real_today} "
              f"(as_of={dossier.get('as_of')}, tickers={dossier.get('n_tickers')}):")
        for e in errors[:10]:
            print(f"   ⚠ {e}")
        return 2
    write_dossier(dossier)
    print(f"build_dossier: wrote {DOSSIER_FILE} — {dossier['n_tickers']} tickers, "
          f"as_of={dossier['as_of']}, built_from_days={len(dossier['built_from_days'])}, valid")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
