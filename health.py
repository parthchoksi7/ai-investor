"""
health.py — System health tracking for every pipeline run.

Records all failure points to system_health.json after each run.
The alert.yml GitHub Actions workflow reads this file and creates
a GitHub Issue whenever overall_status is not OK.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

HEALTH_FILE = "system_health.json"

# Status levels (ordered by severity)
OK        = "OK"
DEGRADED  = "DEGRADED"
FAILED    = "FAILED"
ABORTED   = "ABORTED"

_SEVERITY = {OK: 0, DEGRADED: 1, FAILED: 2, ABORTED: 3}


class HealthTracker:
    def __init__(self, run_id: str, date: str):
        self.run_id = run_id
        self.date   = date
        self.checks: dict  = {}
        self.alerts: list  = []

    def record(self, name: str, status: str, message: str = "", **details):
        """
        Record a health check result.
          name    — logical check name, e.g. "market_data", "agent_research"
          status  — OK | DEGRADED | FAILED | ABORTED
          message — human-readable description (required when status != OK)
          details — arbitrary key/value metadata stored alongside the check
        """
        self.checks[name] = {
            "status":  status,
            "message": message,
            "ts":      datetime.now(timezone.utc).isoformat(),
            **details,
        }
        if status != OK:
            self.alerts.append(f"[{status}] {name}: {message}")

    @property
    def overall_status(self) -> str:
        if not self.checks:
            return FAILED
        worst = max(
            (_SEVERITY.get(c["status"], 0) for c in self.checks.values()),
            default=0,
        )
        return next(s for s, v in _SEVERITY.items() if v == worst)

    def save(self) -> dict:
        data = {
            "run_id":         self.run_id,
            "date":           self.date,
            "timestamp":      datetime.now(timezone.utc).isoformat(),
            "overall_status": self.overall_status,
            "checks":         self.checks,
            "alerts":         self.alerts,
        }
        tmp = HEALTH_FILE + ".tmp"
        Path(tmp).write_text(json.dumps(data, indent=2))
        Path(tmp).replace(HEALTH_FILE)  # atomic
        return data


def load_last_health() -> dict:
    """Return the most recent health report, or empty dict if none exists."""
    try:
        return json.loads(Path(HEALTH_FILE).read_text())
    except Exception:
        return {}


def append_check(name: str, status: str, message: str = "", **details) -> dict:
    """Add/overwrite one check on the EXISTING system_health.json.

    For steps that run AFTER main.py has already written the health file —
    e.g. fill reconciliation in routine STEP 4. Recomputes overall_status and
    rebuilds the alerts list: alert.yml keys off overall status, so inserting
    a FAILED check without recomputing would hide it under a stale OK.
    """
    data = load_last_health()
    if not data:
        data = {"run_id": None, "date": None, "checks": {}, "alerts": []}
    checks = data.setdefault("checks", {})
    checks[name] = {
        "status":  status,
        "message": message,
        "ts":      datetime.now(timezone.utc).isoformat(),
        **details,
    }
    data["alerts"] = [
        f"[{c.get('status')}] {n}: {c.get('message', '')}"
        for n, c in checks.items() if c.get("status") != OK
    ]
    worst = max((_SEVERITY.get(c.get("status"), 0) for c in checks.values()), default=0)
    data["overall_status"] = next(s for s, v in _SEVERITY.items() if v == worst)
    data["timestamp"] = datetime.now(timezone.utc).isoformat()

    tmp = HEALTH_FILE + ".tmp"
    Path(tmp).write_text(json.dumps(data, indent=2))
    Path(tmp).replace(HEALTH_FILE)  # atomic
    return data
