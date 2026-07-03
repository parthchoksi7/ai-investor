"""
heartbeat_check.py — the dead-man's switch (§15.4).

The worst failures produce NO ERROR AT ALL — the Jun-11 silently-skipped cron, the
Jun-18 dead forecast feed. A flow that never ran writes no failure signal, so the
per-flow health checks can't catch it. This standalone check runs late each weekday
and asserts that every expected daily artifact EXISTS and is DATED TODAY. Any missing
artifact → the heartbeat.yml workflow opens a `heartbeat-alert` GitHub Issue.

Two tiers, so a legitimately-skipped day never false-alarms:

  • DATA plane (GitHub Actions, ~morning): market_snapshot.json, data_quality_report
    .json, factor_history.jsonl. If these are stale/missing, the upstream fetch failed
    (cron skipped / dead feed) — alert.
  • COMPUTE plane (the cloud routine, ~midday): system_health.json (the day's health
    row). Required ONLY when the data plane is fresh — fresh data means the routine
    SHOULD have run, so a missing health row is the silent-skip failure. If the data
    plane itself is stale, the routine correctly did not run, so we do NOT also alert
    on its absent artifacts (that would be a cascading false alarm).

Non-trading days (weekends / NYSE holidays) are skipped — no artifacts are expected.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

from market_calendar import is_trading_day, today_et

REPORT_FILE = "heartbeat_report.json"


def _json_date(path: str, *fields: str) -> str | None:
    """Read the first present of `fields` from a JSON object file, or None."""
    try:
        d = json.loads(Path(path).read_text())
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    for f in fields:
        v = d.get(f)
        if v:
            return str(v)[:10]
    return None


def _jsonl_last_date(path: str, field: str = "date") -> str | None:
    """Date of the LAST row in a JSONL ledger (the most recent append), or None.
    Reads only the tail — the ledger can be large (factor_history over the universe)."""
    try:
        p = Path(path)
        if not p.is_file():
            return None
        # Read the last non-empty line without loading the whole file.
        with p.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, 65536)
            f.seek(size - chunk)
            tail = f.read().decode(errors="ignore").splitlines()
        for line in reversed(tail):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            v = row.get(field)
            if v:
                return str(v)[:10]
    except Exception:
        return None
    return None


# (name, kind, path, tier). kind: "json_date" | "jsonl_date". tier: "data" | "compute".
_ARTIFACTS = [
    ("market_snapshot",      "json_date",  "market_snapshot.json",     "data"),
    ("data_quality_report",  "json_date",  "data_quality_report.json", "data"),
    ("factor_history",       "jsonl_date", "factor_history.jsonl",     "data"),
    # research_dossier is the Phase-4 producer's output + the (future) Wednesday agent
    # input. build_dossier only WRITES a valid, today-dated dossier (it refuses to
    # overwrite a good one with a stale/invalid build), so a snapshot-fresh but
    # dossier-stale day means the build failed — catch it here before a consumer trades
    # on a silently-stale dossier (the devops-review blind spot).
    ("research_dossier",     "json_date",  "research_dossier.json",    "data"),
    ("system_health",        "json_date",  "system_health.json",       "compute"),
]
# Informational only — a legitimate 0-candidate day writes no forecast row, so a stale
# forecasts.jsonl is a WARNING here, not a heartbeat failure. The precise forecast-feed
# freeze alert (which knows the candidate count) lives in the routine/harness.
_WARN_ARTIFACTS = [
    ("forecasts", "jsonl_date", "forecasts.jsonl"),
]


def _artifact_date(kind: str, path: str, root: str) -> str | None:
    full = str(Path(root) / path)
    if kind == "json_date":
        # `as_of` covers research_dossier.json (its freshness key); the snapshot/report
        # use `date`/`data_date`. Checking all three is safe — the others lack `as_of`.
        return _json_date(full, "date", "data_date", "as_of")
    return _jsonl_last_date(full)


def check_heartbeat(as_of: "str | date | None" = None, root: str = ".") -> dict:
    """Assert every expected daily artifact is present and dated `as_of` (today ET).

    Returns a report dict: {ok, as_of, skipped, checks:[...], warnings:[...], missing:[...]}.
    `ok` is False iff a REQUIRED artifact is missing/stale on a trading day. Compute-plane
    artifacts are required only when the data plane is fresh (see module docstring).
    """
    if as_of is None:
        as_of = today_et()
    as_of_iso = as_of if isinstance(as_of, str) else as_of.strftime("%Y-%m-%d")

    if not is_trading_day(as_of_iso):
        return {"ok": True, "as_of": as_of_iso, "skipped": "non-trading day (weekend / NYSE holiday)",
                "checks": [], "warnings": [], "missing": []}

    checks = []
    for name, kind, path, tier in _ARTIFACTS:
        found = _artifact_date(kind, path, root)
        fresh = (found == as_of_iso)
        checks.append({"name": name, "tier": tier, "path": path,
                       "artifact_date": found, "fresh": fresh,
                       "status": "OK" if fresh else ("STALE" if found else "MISSING")})

    data_checks    = [c for c in checks if c["tier"] == "data"]
    compute_checks = [c for c in checks if c["tier"] == "compute"]
    data_fresh     = all(c["fresh"] for c in data_checks)

    # Data-plane failures always count. Compute-plane failures count only when the
    # data plane is fresh (the routine should have run). If the data plane is stale,
    # the routine correctly skipped — don't cascade the alarm to its artifacts.
    required_failed = [c for c in data_checks if not c["fresh"]]
    if data_fresh:
        required_failed += [c for c in compute_checks if not c["fresh"]]
    else:
        for c in compute_checks:
            if not c["fresh"]:
                c["status"] += " (not required — data plane stale, routine correctly skipped)"

    warnings = []
    for name, kind, path in _WARN_ARTIFACTS:
        found = _artifact_date(kind, path, root)
        if found != as_of_iso:
            warnings.append({"name": name, "path": path, "artifact_date": found,
                             "note": "stale/missing (informational — a 0-candidate day writes none)"})

    return {
        "ok":       not required_failed,
        "as_of":    as_of_iso,
        "skipped":  None,
        "data_plane_fresh": data_fresh,
        "checks":   checks,
        "warnings": warnings,
        "missing":  [c["name"] for c in required_failed],
    }


def main() -> int:
    report = check_heartbeat()
    Path(REPORT_FILE).write_text(json.dumps(report, indent=2))
    if report.get("skipped"):
        print(f"HEARTBEAT SKIP: {report['skipped']} ({report['as_of']})")
        return 0
    print(f"HEARTBEAT {report['as_of']} — {'OK' if report['ok'] else 'ALERT'} "
          f"(data_plane_fresh={report['data_plane_fresh']})")
    for c in report["checks"]:
        print(f"  [{c['status']:<8}] {c['name']} (date={c['artifact_date']})")
    for w in report["warnings"]:
        print(f"  [WARN    ] {w['name']} (date={w['artifact_date']}) — {w['note']}")
    if not report["ok"]:
        print(f"MISSING/STALE required artifacts: {', '.join(report['missing'])}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
