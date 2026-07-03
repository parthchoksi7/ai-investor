"""
preflight_gate.py — Decide whether the Claude trading routine should run *at all*.

The routine depends on a fresh ``market_snapshot.json`` pushed by the
``market_data.yml`` GitHub Actions job (~8:00 AM ET). GitHub's scheduled crons
are best-effort and can be delayed by hours or skipped entirely, so the routine
fires several times across the morning (9:45 / 10:45 / 11:45 / 12:45 ET =
initial attempt + 3 hourly retries) and runs THIS gate first on every attempt.

Running the full pipeline against stale data is pointless — ``main.py`` would
just abort at preflight (``_data_date != today``) and produce zero trades while
still burning agent tokens. This gate stops that: it cheaply decides whether to
proceed, skip-and-retry-later, or skip-because-already-done.

The gate is idempotent across the multiple daily attempts: it guarantees the
pipeline executes at most once per day, only when data is fresh.

Exit codes (the routine MUST branch on these):
  0  → PROCEED:  fresh data, API healthy, not yet executed today. Run ``main.py``.
  10 → SKIP/RETRY: market closed today (weekend / NYSE holiday) OR data not fresh
       OR Anthropic API overloaded (529). Do NOT run the pipeline. Stop this
       attempt; the next scheduled attempt (+60 min) will re-check. If all
       attempts see a closed market / stale data / degraded API, the day is
       simply skipped.
  20 → SKIP/DONE: today's pipeline already executed (idempotency). Do NOT run
       again — re-running would risk double-execution.

Usage in the routine (after ``git pull`` to get the latest pushed snapshot):
    python preflight_gate.py
    # exit 0  → proceed to main.py
    # exit !=0 → stop cleanly, do NOT trade
"""

import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

# Market-day date in US/Eastern (the cloud runner is UTC; in the early-morning
# window ET and UTC share a calendar date, but compute ET explicitly to be safe).
# PREFLIGHT_DATE_OVERRIDE ("YYYY-MM-DD") forces the effective date for both the
# freshness comparison and the weekend/holiday calendar — used by tests (so the
# suite is deterministic regardless of the wall-clock day) and available as a
# manual override. It drives the weekday too, so it must be a real calendar date.
ET = ZoneInfo("America/New_York")
_OVERRIDE = os.getenv("PREFLIGHT_DATE_OVERRIDE")
_NOW_ET = (datetime.strptime(_OVERRIDE, "%Y-%m-%d").replace(tzinfo=ET)
           if _OVERRIDE else datetime.now(ET))
TODAY = _NOW_ET.strftime("%Y-%m-%d")

# Minimum history bars required for any quant calculation — mirrors the
# pre-flight abort threshold in main.py / fetch_snapshot.py.
MIN_BARS = 22

PROCEED, SKIP_RETRY, SKIP_DONE = 0, 10, 20

# NYSE full-day market closures (weekends are handled separately). On a closed
# market the broker still ACCEPTS GFD orders but they sit `queued` and never fill,
# expiring at the (nonexistent) close — so the routine must not run. This was a
# live incident on Juneteenth 2026-06-19: the snapshot was dated "today", the gate
# proceeded, and 4 orders were placed that could never fill.
#
# The calendar now lives in market_calendar.py (single source — the Phase 3
# heartbeat also needs it). Re-exported here so existing importers/tests that
# reference preflight_gate.NYSE_HOLIDAYS keep working.
from market_calendar import NYSE_HOLIDAYS  # noqa: E402


def _market_closed_today() -> tuple[bool, str]:
    """Return (closed, reason) for TODAY in US/Eastern. Covers weekends and the
    NYSE_HOLIDAYS calendar. Does NOT cover early-close (half) days — those still
    trade, so the routine should run on them."""
    dt = _NOW_ET
    if dt.weekday() >= 5:  # 5=Sat, 6=Sun
        return True, f"weekend ({dt.strftime('%A')})"
    if TODAY in NYSE_HOLIDAYS:
        return True, "NYSE holiday"
    return False, ""


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _check_api_health() -> tuple[bool, str]:
    """Make a minimal 1-token Anthropic API call to verify the API is not overloaded.
    Returns (healthy: bool, message: str).
    """
    try:
        import anthropic
    except ImportError:
        return True, "anthropic package not installed — skipping canary check"

    # Build the client the SAME way analysis.py:_get_client() does, so the canary
    # authenticates identically to the real agents:
    #   • ANTHROPIC_API_KEY when present (local / .env), else
    #   • the OAuth token file the cloud injects, via auth_token= — this is how
    #     every scheduled-routine agent call authenticates. A bare Anthropic() does
    #     NOT pick this token up, so the old canary failed auth in the cloud, fell
    #     through to the non-529 "proceed" branch, and silently disabled 529
    #     overload protection on the exact (live) path it was built to guard.
    # If neither credential is available, skip the check rather than block on it.
    api_key    = os.getenv("ANTHROPIC_API_KEY")
    token_file = os.getenv("CLAUDE_SESSION_INGRESS_TOKEN_FILE", "")
    try:
        if api_key:
            client = anthropic.Anthropic(api_key=api_key)
        elif token_file and os.path.isfile(token_file):
            with open(token_file) as _tf:
                client = anthropic.Anthropic(auth_token=_tf.read().strip())
        else:
            return True, "No API key or OAuth token available — skipping canary check"
    except Exception as e:
        return True, f"Could not build Anthropic client (proceeding): {str(e)[:120]}"

    try:
        # Use 50 tokens (not 1) to simulate real agent load — under heavy API load,
        # short requests succeed while 600-token requests silently return empty.
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=50,
            messages=[{"role": "user", "content": 'Reply with exactly: {"status":"ok"}'}],
        )
        body = resp.content[0].text.strip() if resp.content else ""
        if not body:
            return False, "Anthropic API returned empty response under load — skipping run"
        return True, "API healthy"
    except Exception as e:
        err = str(e)
        if "529" in err or "overloaded" in err.lower():
            return False, f"Anthropic API overloaded (529) — {err[:120]}"
        # Non-529 errors (auth, network) — don't block on them; let main.py surface the real error.
        return True, f"Canary check non-529 error (proceeding): {err[:120]}"


def main() -> int:
    # 0. Market calendar — is the market even open today? A closed market (weekend
    #    or NYSE holiday) accepts GFD orders that can never fill, so skip outright.
    #    market_data.yml can still stamp a "today"-dated snapshot on a holiday, so
    #    the freshness check below does NOT catch this — gate on the calendar first.
    closed, reason = _market_closed_today()
    if closed:
        print(
            f"SKIP/RETRY: market is closed today ({TODAY} — {reason}). "
            "Orders placed now would sit queued and never fill. Not running."
        )
        return SKIP_RETRY

    # 1. Idempotency — has today's pipeline already executed?
    pending = _read_json("pending_decisions.json")
    if pending and pending.get("date") == TODAY and pending.get("executed_at"):
        print(
            f"SKIP/DONE: pipeline already executed today ({TODAY}), "
            f"run_id={pending.get('run_id')}. Not running again."
        )
        return SKIP_DONE

    # 1b. Execution claim — a prior attempt started placing orders but never
    # finished (crashed mid-execution before executed_at was stamped). Orders
    # MAY have been placed; re-running would risk double-fills. Fail toward
    # missed trades: stop, and recover via the Scenario B runbook in CLAUDE.md
    # (diff actual get_equity_positions against pending_decisions targets).
    if pending and pending.get("date") == TODAY and pending.get("execution_started_at"):
        print(
            f"SKIP/DONE: execution was STARTED today ({TODAY}, "
            f"run_id={pending.get('run_id')}, "
            f"started_at={pending.get('execution_started_at')}) but executed_at was "
            "never stamped — a prior attempt crashed mid-execution. Orders may have "
            "been placed. DO NOT re-run."
        )
        # A7: automatically diff LIVE broker positions against the intended orders
        # and emit a SPECIFIC, diff-driven recovery alert instead of a generic
        # "recover manually" note. Report-only here (apply=False): the gate stays
        # fail-safe and never mutates state on a retry-able check. A human (or a
        # dedicated recovery step) runs `python reconcile.py --apply` after review
        # to perform the provably-safe remediation, if any.
        try:
            from reconcile import reconcile_crash_state
            rep = reconcile_crash_state(apply=False)
            print(f"   Reconciliation: {rep.get('classification', '?').upper()} — "
                  f"{rep.get('recommended_action', '')}")
            print("   See reconciliation_report.json; run `python reconcile.py --apply` "
                  "to apply a provably-safe remediation, or recover via Scenario B in CLAUDE.md.")
        except Exception as e:
            print(f"   ⚠ Auto-reconciliation unavailable ({str(e)[:160]}). "
                  "Recover manually via Scenario B in CLAUDE.md.")
        return SKIP_DONE

    # 2. Freshness — is market_snapshot.json dated today with enough history?
    snap = _read_json("market_snapshot.json")
    if snap is None:
        print(
            "SKIP/RETRY: market_snapshot.json is missing or unreadable. "
            "The market_data.yml GitHub Actions job has not landed data yet. "
            "Not running; the next scheduled attempt (+60 min) will re-check."
        )
        return SKIP_RETRY

    snap_date = snap.get("date")
    depths = [len(h) for h in snap.get("history", {}).values()]
    min_depth = min(depths) if depths else 0

    if snap_date != TODAY:
        print(
            f"SKIP/RETRY: market_snapshot.json date={snap_date}, expected {TODAY}. "
            "GitHub Actions market_data job has not pushed fresh data yet. "
            "Not running; the next scheduled attempt (+60 min) will re-check. "
            "Did you `git pull` before running this gate?"
        )
        return SKIP_RETRY

    if min_depth < MIN_BARS:
        print(
            f"SKIP/RETRY: market_snapshot.json has only {min_depth} history bars "
            f"(need {MIN_BARS}+). Data is incomplete. Not running; the next "
            "scheduled attempt (+60 min) will re-check."
        )
        return SKIP_RETRY

    # 3. API health — canary call to catch Anthropic 529 overloads before burning
    #    the full pipeline against a degraded API.
    api_ok, api_msg = _check_api_health()
    if not api_ok:
        print(
            f"SKIP/RETRY: {api_msg}. "
            "Not running; the next scheduled attempt (+60 min) will re-check."
        )
        return SKIP_RETRY

    print(
        f"PROCEED: fresh market_snapshot.json (date={TODAY}, "
        f"{len(snap.get('prices', {}))} tickers, min_depth={min_depth}), "
        f"API healthy, and not yet executed today. Run main.py."
    )
    return PROCEED


if __name__ == "__main__":
    sys.exit(main())
