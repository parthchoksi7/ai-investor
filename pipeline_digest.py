"""
pipeline_digest.py — the weekly pipeline-integrity digest (§15.5).

One glance answers: "is the machine that produces my year-end verdict healthy?"
Summarizes the week's data_quality_history.jsonl + health_history.jsonl — coverage
trend, data-quality score, DEGRADED/ABORT runs, and the abort rate — into
pipeline_digest.md (committed Friday). Slow drift (coverage creeping 85% → 60%
over a month) is meant to be visible HERE, before it is a crisis.

Pure/read-only: no network, no side effects beyond writing the digest file.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

DQ_HISTORY     = "data_quality_history.jsonl"
HEALTH_HISTORY = "health_history.jsonl"
DIGEST_FILE    = "pipeline_digest.md"


def _read_jsonl(path: str) -> list[dict]:
    rows = []
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if isinstance(d, dict):
                    rows.append(d)
            except Exception:
                continue
    except Exception:
        pass
    return rows


def _within(row_date: str | None, since_iso: str) -> bool:
    return bool(row_date) and str(row_date)[:10] >= since_iso


def build_digest(as_of: "str | date | None" = None, window_days: int = 7,
                 dq_path: str = DQ_HISTORY, health_path: str = HEALTH_HISTORY) -> dict:
    """Summarize the last `window_days` of the pipeline time series."""
    if as_of is None:
        as_of = date.today()
    as_of_d = as_of if isinstance(as_of, date) else datetime.strptime(as_of, "%Y-%m-%d").date()
    since_iso = (as_of_d - timedelta(days=window_days - 1)).strftime("%Y-%m-%d")
    as_of_iso = as_of_d.strftime("%Y-%m-%d")

    dq = [r for r in _read_jsonl(dq_path) if _within(r.get("date"), since_iso)]
    hh = [r for r in _read_jsonl(health_path) if _within(r.get("date"), since_iso)]

    covs   = [r["coverage_pct"] for r in dq if isinstance(r.get("coverage_pct"), (int, float))]
    scores = [r["data_quality_score"] for r in dq if isinstance(r.get("data_quality_score"), (int, float))]
    dq_status_counts: dict[str, int] = {}
    for r in dq:
        dq_status_counts[r.get("status", "?")] = dq_status_counts.get(r.get("status", "?"), 0) + 1
    health_status_counts: dict[str, int] = {}
    for r in hh:
        health_status_counts[r.get("overall_status", "?")] = \
            health_status_counts.get(r.get("overall_status", "?"), 0) + 1

    n_health = len(hh)
    n_abort_or_fail = sum(v for k, v in health_status_counts.items() if k in ("ABORTED", "FAILED"))

    return {
        "as_of":        as_of_iso,
        "window":       f"{since_iso} .. {as_of_iso}",
        "dq_runs":      len(dq),
        "coverage_min": min(covs) if covs else None,
        "coverage_max": max(covs) if covs else None,
        "coverage_last": dq[-1].get("coverage_pct") if dq else None,
        "score_min":    min(scores) if scores else None,
        "score_avg":    round(sum(scores) / len(scores), 1) if scores else None,
        "dq_status_counts":     dq_status_counts,
        "health_runs":          n_health,
        "health_status_counts": health_status_counts,
        "abort_fail_rate":      round(100.0 * n_abort_or_fail / n_health, 1) if n_health else None,
        "degraded_or_abort_days": [
            {"date": r.get("date"), "status": r.get("status"), "coverage": r.get("coverage_pct")}
            for r in dq if r.get("status") in ("DEGRADED", "ABORT")
        ],
    }


def render_markdown(d: dict) -> str:
    def _fmt(v, suffix=""):
        return f"{v}{suffix}" if v is not None else "—"

    lines = [
        f"# Pipeline-Integrity Digest — week ending {d['as_of']}",
        "",
        f"_Window: {d['window']} · §15.5 weekly summary_",
        "",
        "## Data quality",
        f"- **Runs logged:** {d['dq_runs']}",
        f"- **Fundamental coverage:** last {_fmt(d['coverage_last'], '%')} "
        f"(min {_fmt(d['coverage_min'], '%')}, max {_fmt(d['coverage_max'], '%')})",
        f"- **Data-quality score:** avg {_fmt(d['score_avg'])} (min {_fmt(d['score_min'])})",
        f"- **Status mix:** {d['dq_status_counts'] or '—'}",
        "",
        "## Run health",
        f"- **Health rows:** {d['health_runs']}",
        f"- **Status mix:** {d['health_status_counts'] or '—'}",
        f"- **Abort/Fail rate:** {_fmt(d['abort_fail_rate'], '%')}",
        "",
    ]
    if d["degraded_or_abort_days"]:
        lines.append("## ⚠ Degraded / aborted data-quality days")
        for x in d["degraded_or_abort_days"]:
            lines.append(f"- {x['date']}: **{x['status']}** (coverage {_fmt(x['coverage'], '%')})")
    else:
        lines.append("## ✅ No degraded/aborted data-quality days this window")
    lines.append("")

    # Weekly rebalance status (Phase 5, §6.5) — did this ISO week get its rebalance?
    try:
        from market_calendar import iso_week_of
        lr = json.loads(Path("last_rebalance.json").read_text())
        this_week = iso_week_of(d["as_of"])
        if lr.get("iso_week") == this_week and (lr.get("executed_at") or lr.get("execution_started_at")):
            state = "✅ executed" if lr.get("executed_at") else "⚠ CLAIMED but never stamped (Scenario B?)"
            lines.append(f"## Weekly rebalance ({this_week}): {state} "
                         f"on {lr.get('date')} — {len(lr.get('tickers', []))} ticker(s) traded")
        else:
            lines.append(f"## 🚨 Weekly rebalance ({this_week}): NONE recorded "
                         f"(last: {lr.get('iso_week')} on {lr.get('date')}) — "
                         "the heartbeat's Friday check should have alerted")
        lines.append("")
    except Exception:
        pass  # pre-Phase-5 history or no rebalance yet — nothing to report

    # Stage C readiness — is the evidence clock decidable yet? (surfaced so the go/no-go
    # is watched passively, not by eyeballing agent_scorecards.json.)
    try:
        from stage_c_readiness import assess_readiness, load_scorecard
        a = assess_readiness(load_scorecard())
        lines.append("## Stage C readiness")
        lines.append(f"- **{a['verdict']}**")
        for name, p in a["signals"].items():
            if not p.get("present"):
                lines.append(f"  - `{name}` — not scored yet")
            else:
                mark = "✅" if p["decidable"] else "⏳"
                lines.append(f"  - {mark} `{name}` — ic={p['ic']} ci=±{p['ci_halfwidth']} "
                             f"n_eff={p['n_effective']}")
        lines.append("")
    except Exception:
        pass

    lines.append(f"_Generated {datetime.now().isoformat(timespec='seconds')}_")
    return "\n".join(lines)


def main() -> int:
    d = build_digest()
    md = render_markdown(d)
    Path(DIGEST_FILE).write_text(md + "\n")
    print(md)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
