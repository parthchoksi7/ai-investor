"""
data_quality.py — the data-quality gate + provenance (Phase 3 / §15.1-15.2).

Why this exists: at year-end the verdict is "did the after-tax book beat SPY?".
If data quality silently varied run-to-run (80% fundamentals one week, 30% the
next — the exact June bug), a bad result is CONFOUNDED: you can't tell a losing
strategy from a starved one. This module makes data integrity a first-class,
MEASURED, gating concern:

  1. `classify_data_quality(snapshot)` scores the run against ABSOLUTE floors
     (§15.2) — absolute, not delta, because a *steady* 28% coverage never
     "drops" and a delta check missed it in June. Returns a report with an
     OK / DEGRADED / ABORT status, a 0-100 `data_quality_score` covariate, and
     the list of floor breaches.
  2. `write_report()` persists `data_quality_report.json` (this run) AND
     append-mirrors a compact row to `data_quality_history.jsonl` (the time
     series) so slow drift (coverage creeping 85% → 60% over a month) is visible
     BEFORE it is a crisis.
  3. `report_hash()` / `data_quality_score()` feed the §15.1 provenance stamp on
     every forecast + decision, so the harness can partition the year-end
     comparison by data quality and report the exclusion rate.

Scope note (Phase 3): only floors whose upstream data exists TODAY are wired —
universe-fetched, history depth, fundamentals/quality coverage, NaN scan. The
event-digest / dossier / token-budget floors (§15.2) arrive with the Phase 4
research pipeline that produces those artifacts; the classifier is structured so
they slot in without a rewrite.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path

REPORT_FILE  = "data_quality_report.json"
HISTORY_FILE = "data_quality_history.jsonl"

OK, DEGRADED, ABORT = "OK", "DEGRADED", "ABORT"
_SEVERITY = {OK: 0, DEGRADED: 1, ABORT: 2}

# Absolute floors (§15.2). A metric is ABORT below `abort`, else DEGRADED below
# `degraded`, else OK. `abort=None` → the metric can only DEGRADE (it flags a
# problem but does not, by itself, stop the run). Fundamentals coverage is the
# canonical example: below 80% it blocks the momentum→fundamental STRATEGY SHIFT
# (§8) but does NOT abort the run — the momentum book still trades.
_FLOORS: dict[str, dict] = {
    "universe_fetched_pct":     {"degraded": 95.0, "abort": 80.0, "higher_better": True},
    "min_history_depth":        {"degraded": 60,   "abort": 22,   "higher_better": True},
    "fundamental_coverage_pct": {"degraded": 80.0, "abort": None, "higher_better": True,
                                 "blocks_strategy_shift": True},
}
# Penalty model for the 0-100 covariate: start at 100, subtract per breach. Simple,
# monotonic, and transparent for the harness's clean-vs-degraded partition.
_ABORT_PENALTY, _DEGRADED_PENALTY = 40, 15


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _classify_metric(name: str, value) -> tuple[str, str | None]:
    """(status, breach_msg|None) for one metric against its floor."""
    spec = _FLOORS[name]
    if value is None or not _finite(value):
        return ABORT, f"{name} MISSING (no value)"
    deg, ab = spec["degraded"], spec["abort"]
    if ab is not None and value < ab:
        return ABORT, f"{name} ABORT: {value} < {ab}"
    if value < deg:
        return DEGRADED, f"{name} DEGRADED: {value} < {deg}"
    return OK, None


def _scan_nan(snapshot: dict) -> int:
    """Count non-finite numeric values in prices + history closes (the NaN-close
    class that broke the Jun-16 Supabase publish). Any hit → DEGRADED."""
    bad = 0
    for p in (snapshot.get("prices") or {}).values():
        for k in ("close", "open", "high", "low", "change_pct"):
            v = (p or {}).get(k)
            if isinstance(v, float) and not math.isfinite(v):
                bad += 1
    for bars in (snapshot.get("history") or {}).values():
        for b in bars or []:
            v = (b or {}).get("close")
            if isinstance(v, float) and not math.isfinite(v):
                bad += 1
    return bad


def classify_data_quality(snapshot: dict, expected_universe: int | None = None) -> dict:
    """Build the data-quality report for one snapshot against the §15.2 floors.

    `expected_universe` defaults to the snapshot's active universe (data_quality
    .active_universe), falling back to the number of price rows. Pass it explicitly
    when the caller knows the intended universe size (e.g. a partial fetch, where
    fetched < expected is the whole point of the check).
    """
    dq = snapshot.get("data_quality") or {}
    prices = snapshot.get("prices") or {}
    history = snapshot.get("history") or {}

    active = dq.get("active_universe") or len(prices) or 0
    expected = expected_universe if expected_universe is not None else active
    fetched = sum(1 for v in prices.values() if v)
    universe_pct = round(100.0 * fetched / expected, 1) if expected else 0.0

    depths = [len(h) for h in history.values() if h]
    min_depth = min(depths) if depths else 0

    metrics_in = {
        "universe_fetched_pct":     universe_pct,
        "min_history_depth":        min_depth,
        "fundamental_coverage_pct": dq.get("fundamental_coverage_pct"),
    }

    metrics: dict[str, dict] = {}
    breaches: list[str] = []
    n_abort = n_degraded = 0
    strategy_shift_ok = True
    for name, value in metrics_in.items():
        status, msg = _classify_metric(name, value)
        spec = _FLOORS[name]
        metrics[name] = {"value": value, "status": status,
                         "degraded_below": spec["degraded"], "abort_below": spec["abort"]}
        if msg:
            breaches.append(msg)
        if status == ABORT:
            n_abort += 1
        elif status == DEGRADED:
            n_degraded += 1
        if spec.get("blocks_strategy_shift") and status != OK:
            strategy_shift_ok = False

    # NaN/Inf scan → DEGRADED (never aborts; it's scrubbed downstream) (§15.2).
    nan_count = _scan_nan(snapshot)
    metrics["nan_inf_count"] = {"value": nan_count, "status": DEGRADED if nan_count else OK,
                                "degraded_below": None, "abort_below": None}
    if nan_count:
        breaches.append(f"nan_inf_count DEGRADED: {nan_count} non-finite numeric value(s)")
        n_degraded += 1

    # Valuation coverage is REPORTED for transparency but does NOT gate: it is
    # structurally FMP-capped (~35%) and can't reach 80% without a paid key, so
    # gating on it would make every run DEGRADED (Phase 2 decision — quality is the
    # gate, valuation is informational).
    metrics["valuation_coverage_pct"] = {
        "value": dq.get("valuation_coverage_pct"), "status": OK,
        "degraded_below": None, "abort_below": None, "informational": True}

    status = ABORT if n_abort else (DEGRADED if n_degraded else OK)
    score = max(0, min(100, 100 - _ABORT_PENALTY * n_abort - _DEGRADED_PENALTY * n_degraded))

    report = {
        "date":               snapshot.get("date"),
        "data_date":          snapshot.get("_data_date") or snapshot.get("date"),
        "generated_at":       datetime.now(timezone.utc).isoformat(),
        "status":             status,
        "data_quality_score": score,
        "strategy_shift_ok":  strategy_shift_ok,
        "expected_universe":  expected,
        "fetched_universe":   fetched,
        "metrics":            metrics,
        "breaches":           breaches,
        "cik_map_ok":         dq.get("cik_map_ok"),
        "price_outlier_count": dq.get("price_outlier_count", 0),
    }
    report["hash"] = report_hash(report)
    return report


def report_hash(report: dict) -> str:
    """Stable 16-hex fingerprint of the GATING content (metric values + status),
    excluding volatile fields (generated_at, hash). Stamped on forecasts/decisions
    so a run's exact data-quality context is reconstructable a year later."""
    payload = {
        "date":   report.get("data_date") or report.get("date"),
        "status": report.get("status"),
        "score":  report.get("data_quality_score"),
        "metrics": {k: v.get("value") for k, v in (report.get("metrics") or {}).items()},
    }
    return sha1(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


def provenance_stamp(report: dict | None) -> dict:
    """The compact provenance block to embed in a forecast row / decision envelope
    (§15.1). Empty-safe: a missing report yields a null-ish stamp, never a crash."""
    r = report or {}
    return {
        "data_quality_score":  r.get("data_quality_score"),
        "data_quality_status": r.get("status"),
        "data_quality_hash":   r.get("hash"),
    }


def write_report(report: dict, path: str = REPORT_FILE, history_path: str = HISTORY_FILE) -> dict:
    """Atomically write data_quality_report.json AND append a compact row to the
    data_quality_history.jsonl time series (§15.2). The history append is
    best-effort — it must never fail the run or the authoritative report write."""
    tmp = path + ".tmp"
    Path(tmp).write_text(json.dumps(report, indent=2, default=str))
    Path(tmp).replace(path)
    try:
        m = report.get("metrics", {})
        row = {
            "date":               report.get("data_date") or report.get("date"),
            "generated_at":       report.get("generated_at"),
            "status":             report.get("status"),
            "data_quality_score": report.get("data_quality_score"),
            "coverage_pct":       (m.get("fundamental_coverage_pct") or {}).get("value"),
            "universe_pct":       (m.get("universe_fetched_pct") or {}).get("value"),
            "min_depth":          (m.get("min_history_depth") or {}).get("value"),
            "nan_count":          (m.get("nan_inf_count") or {}).get("value"),
            "hash":               report.get("hash"),
        }
        with open(history_path, "a") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception:
        pass
    return report


def load_report(path: str = REPORT_FILE) -> dict:
    """Most recent data_quality_report.json, or {} if none / unreadable."""
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}


def merge_event_digest_into_report(stats: dict, path: str = REPORT_FILE,
                                   min_parse_rate: float = 0.8) -> dict:
    """Fold the Step-4 Haiku event-digest stats into the ALREADY-written
    data_quality_report.json so a digest failure is not silent (§15.2 lists "Haiku
    digest parse-success rate | 80% → DEGRADED"). A parse rate below `min_parse_rate`
    over ≥1 chunk adds a breach and floors the report status at DEGRADED, so the cloud
    routine's `data_quality` health check surfaces it to alert.yml.

    Patches the REPORT ONLY (not the history row — that was already appended by
    write_report); best-effort, never raises into the fetch job."""
    try:
        report = load_report(path)
        if not report:
            return {}
        report["event_digest"] = stats
        rate = stats.get("parse_success_rate", 1.0)
        # Record EACH condition independently (a busy news day can be both low-parse AND
        # budget-capped) — a single escalation to DEGRADED, but both facts surfaced so the
        # operator sees the full picture (raise the cap / investigate API health).
        new_breaches = []
        if stats.get("chunks", 0) and rate < min_parse_rate:
            new_breaches.append(f"event_digest DEGRADED: parse_success_rate {rate} < {min_parse_rate}")
        if stats.get("capped"):
            new_breaches.append(
                f"event_digest budget cap: processed {stats.get('max_chunks')} of "
                f"{stats.get('chunks_available')} chunks — news under-covered")
        if new_breaches:
            report.setdefault("breaches", []).extend(new_breaches)
            if _SEVERITY.get(report.get("status"), 0) < _SEVERITY[DEGRADED]:
                report["status"] = DEGRADED
                report["data_quality_score"] = min(report.get("data_quality_score", 100), 85)
            report["hash"] = report_hash(report)
        tmp = path + ".tmp"
        Path(tmp).write_text(json.dumps(report, indent=2, default=str))
        Path(tmp).replace(path)
        return report
    except Exception:
        return {}
