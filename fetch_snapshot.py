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
from market_data import (
    get_market_snapshot, _held_tickers, SP500_HOLDINGS, full_depth_scope, MIN_VIABLE_BARS,
)
from universe import CORE_UNIVERSE


print("Fetching market snapshot...")
# force=True bypasses the local-file and Supabase caches so every GH Actions run
# always fetches live from Polygon. Those caches exist for the cloud routine only.
snapshot = get_market_snapshot(force=True)

# The "always-fetch, full-depth" scope — the SAME function select_fetch_batch calls
# internally to decide what gets fetched at full depth every run, so this can never
# independently drift from what actually got fetched. Computed once here and reused
# below by BOTH the depth-abort gate and the commit-time slim step.
FULL_DEPTH_SCOPE = full_depth_scope(CORE_UNIVERSE, SP500_HOLDINGS.keys(), _held_tickers(),
                                     benchmarks=("SPY", "QQQ"))

# PLAN_SEC_VALUATION Phase 2 — light up valuation (pe_ratio/fcf_yield/ev_ebitda) for
# the tickers FMP's free tier misses, combining Phase 1's SEC EDGAR components with
# today's close price. Must run BEFORE the snapshot is written/scored/classified so
# the committed file, quant scoring, and the data-quality report all see the same
# enriched fundamentals. Refreshes the valuation-coverage numbers already embedded
# in snapshot["data_quality"] in place so the before/after lift is visible below and
# in data_quality_report.json (quality-coverage fields are untouched — derive only
# adds valuation fields). Never fails the fetch — valuation just stays FMP-only.
try:
    from data_providers import fundamental_coverage
    from market_data import derive_valuation_ratios
    _val_tickers = list(snapshot.get("prices", {}).keys())
    _val_before = fundamental_coverage(_val_tickers, snapshot.get("fundamentals", {}))
    derive_valuation_ratios(snapshot.get("prices", {}), snapshot.get("fundamentals", {}))
    _val_after = fundamental_coverage(_val_tickers, snapshot.get("fundamentals", {}))
    if isinstance(snapshot.get("data_quality"), dict):
        snapshot["data_quality"]["valuation_covered"]      = _val_after["valuation_covered"]
        snapshot["data_quality"]["valuation_coverage_pct"] = _val_after["valuation_coverage_pct"]
    print(f"Valuation coverage (SEC-derived): {_val_before['valuation_coverage_pct']}% -> "
          f"{_val_after['valuation_coverage_pct']}% "
          f"(+{_val_after['valuation_covered'] - _val_before['valuation_covered']} ticker(s))")
except Exception as e:
    print(f"WARNING: derive_valuation_ratios failed (valuation stays FMP-only) — {e}")

# Depth gate scope: FULL_DEPTH_SCOPE (core + held + SP500 + benchmarks) is what every
# downstream decision (quant scoring, risk_watch, the CRO correlation matrix) actually
# reads — it's ALSO what slim_snapshot_for_commit keeps at full depth below, so the
# gate can never disagree with what actually ships full-depth. The optional ~300-name
# expansion batch is candidate-discovery only; one thin name in it (2026-08-10/11:
# PARA, a merged-away symbol Polygon still 200s with a single stub bar) must never
# abort a run whose real decision inputs are fine — that's what happened for 5 straight
# runs before market_data.py's per-ticker MIN_VIABLE_BARS drop (added alongside this
# fix, and itself scoped to expansion-only names — see market_data.py's comment on
# why core/held tickers deliberately do NOT get silently dropped the same way) started
# filtering thin expansion tickers out before they reach here at all. This gate is now
# a second, differently-scoped line of defense, not the only one.
def _min_depth(history_dict: dict) -> int:
    depths = [len(h) for h in history_dict.values()]
    return min(depths) if depths else 0


_core_history = {t: h for t, h in snapshot.get("history", {}).items() if t in FULL_DEPTH_SCOPE}
min_depth = _min_depth(_core_history)
real_scores = sum(1 for v in snapshot.get("prices", {}).values() if v)  # rough proxy

# Full-universe depth (incl. expansion) is still surfaced for visibility, just not
# used to decide the abort — a thin expansion name is a data_quality DEGRADED signal,
# not a pipeline-halting one.
_all_min = _min_depth(snapshot.get("history", {}))

print(
    f"Snapshot ready: "
    f"{len(snapshot['prices'])} tickers | "
    f"{len(snapshot.get('news', []))} news articles | "
    f"min history depth: {min_depth} bars (core+held+benchmarks, {len(_core_history)} names) | "
    f"{_all_min} bars (full universe) | "
    f"source: {snapshot.get('_source', 'unknown')}"
)

if min_depth < MIN_VIABLE_BARS:
    print(f"ERROR: Core/held/benchmark snapshot has only {min_depth} history bars — "
          f"insufficient for quant scoring (need {MIN_VIABLE_BARS}+).")
    print("This means Polygon was unreachable or returned empty data. Cloud routine will abort.")
    sys.exit(1)

# Always write market_snapshot.json so GitHub Actions can commit it to the repo.
# The cloud routine (Anthropic, 9:45 AM ET) reads this file — Supabase is blocked there.
with open("market_snapshot.json", "w") as f:
    json.dump(snapshot, f)
print(f"Saved market_snapshot.json (date={snapshot['date']}, {len(snapshot['prices'])} tickers)")

dq = snapshot.get("data_quality") or {}
if dq:
    ok = "OK" if dq.get("coverage_ok") else "⚠ BELOW 80% FLOOR"
    print(f"Fundamental coverage: {dq.get('fundamental_coverage_pct')}% "
          f"({dq.get('fundamentals_covered')}/{dq.get('active_universe')}) — {ok}")

# Data-quality gate (§15.2) — classify the snapshot against the ABSOLUTE floors and
# write data_quality_report.json + append the data_quality_history.jsonl time series.
# This is the first-class, gating, time-logged data-integrity signal the cloud
# routine + heartbeat + weekly digest all read. Never fail the fetch on a classify
# error — the snapshot itself was already written above.
try:
    from data_quality import classify_data_quality, write_report
    _dq_report = classify_data_quality(snapshot)
    write_report(_dq_report)
    print(f"data_quality_report.json: status={_dq_report['status']} "
          f"score={_dq_report['data_quality_score']} "
          f"strategy_shift_ok={_dq_report['strategy_shift_ok']}")
    if _dq_report["breaches"]:
        for b in _dq_report["breaches"]:
            print(f"   ⚠ {b}")
except Exception as e:
    print(f"WARNING: data_quality classification failed — {e}")

# Score the FULL universe point-in-time and append to the factor_history time
# series. This runs in GH Actions (not the cloud routine, which scores only
# candidates) so factor-persistence / IC analysis has a complete daily record.
# Every row carries formula_version so IC is never computed across a re-weight
# boundary (P0-2). Idempotent per (date, ticker, formula_version).
try:
    from quant_engine import score_all_tickers, log_factor_history, FORMULA_VERSION
    scores = score_all_tickers(snapshot)
    n = log_factor_history(scores, as_of=snapshot["date"])
    print(f"factor_history.jsonl: +{n} row(s) (formula {FORMULA_VERSION}, {len(scores)} scored)")
except Exception as e:
    print(f"WARNING: factor_history append failed — {e}")

# Step 4 (§11.3) — Haiku event digest: compress the raw news feed into a small, deduped,
# per-ticker events.jsonl the dossier folds in. Runs BEFORE build_dossier so today's
# events reach the dossier. Enrichment, NEVER gating — a failure here must not stop the
# pipeline (events enrich; they don't gate). Needs ANTHROPIC_API_KEY in the GH env.
try:
    if os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_SESSION_INGRESS_TOKEN_FILE"):
        from event_digest import digest as _digest
        from universe import CORE_UNIVERSE as _UNIV
        _stats = _digest(snapshot, set(_UNIV))
        print(f"event_digest: written={_stats.get('events_written')} "
              f"deduped={_stats.get('events_deduped')} "
              f"parse_ok_rate={_stats.get('parse_success_rate')}")
        # Surface a digest parse-failure into the data-quality report so it is NOT
        # silent (§15.2): <80% parse rate floors the report at DEGRADED, which the
        # cloud routine's data_quality health check then carries to alert.yml.
        try:
            from data_quality import merge_event_digest_into_report
            merge_event_digest_into_report(_stats)
        except Exception as _me:
            print(f"   ⚠ could not record event_digest stats to data_quality_report — {_me}")
    else:
        print("event_digest: skipped — no ANTHROPIC_API_KEY in env (events stay empty)")
except Exception as e:
    print(f"WARNING: event_digest failed (non-fatal, events are enrichment) — {e}")

# GH-plane forecast maturation (Stage D): score matured forecasts HERE too, where the
# full-depth history for the whole (possibly expanded) universe is in memory. The cloud
# runs the same call, but its committed snapshot carries only 63-bar tails for expansion
# names — long-horizon (126/189/252d) forecasts on those names can only mature on this
# plane. score_matured is idempotent per (forecast_id, horizon), so both planes calling
# it is safe. Observational — never fails the fetch.
try:
    from calibration import score_matured, agent_scorecard
    _n = score_matured(snapshot)
    agent_scorecard()
    print(f"calibration: {_n} matured forecast(s) scored on the GH plane")
except Exception as e:
    print(f"WARNING: GH-plane forecast scoring failed — {e}")

# Step 5 (§11.3) — build the per-ticker research dossier: the single synthesis point
# that collapses the raw layer (snapshot + factor_history + fundamentals + events +
# journal) into the small denormalized record the Wednesday agents will read. Research
# artifact ONLY — zero order code. Reads the files fetch_snapshot just wrote above.
try:
    import build_dossier as _bd
    rc = _bd.main()
    print(f"build_dossier exit={rc}")
except Exception as e:
    print(f"WARNING: build_dossier failed — {e}")

# Step 6 (Stage D, interim §12.4 storage split) — slim the COMMITTED snapshot when the
# universe is expanded: expansion names keep a 63-bar tail (enough for the ≥22-bar gate
# + short momentum); core + held + benchmarks keep full depth. Everything above (factor
# scoring, dossier, maturation) already ran on the FULL in-memory snapshot, and the
# full copy still goes to Supabase below — only the git-committed file shrinks
# (~13 MB/day → ~6 MB/day at 400 names).
try:
    if snapshot.get("universe_expanded"):
        from market_data import slim_snapshot_for_commit
        # Reuse the exact scope the depth gate above already validated — see
        # FULL_DEPTH_SCOPE's definition at the top of this file.
        slim = slim_snapshot_for_commit(snapshot, FULL_DEPTH_SCOPE)
        with open("market_snapshot.json", "w") as f:
            json.dump(slim, f)
        print(f"Slimmed committed snapshot: {len(slim.get('history_tail_tickers', []))} "
              f"expansion name(s) at 63-bar tails; core+held at full depth")
except Exception as e:
    print(f"WARNING: snapshot slim failed (committing full snapshot) — {e}")

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
