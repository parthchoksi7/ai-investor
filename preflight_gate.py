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
  10 → SKIP/RETRY: data not fresh OR Anthropic API overloaded (529). Do NOT run
       the pipeline. Stop this attempt; the next scheduled attempt (+60 min)
       will re-check. If all attempts see stale data or a degraded API, the day
       is simply skipped.
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
ET = ZoneInfo("America/New_York")
TODAY = datetime.now(ET).strftime("%Y-%m-%d")

# Minimum history bars required for any quant calculation — mirrors the
# pre-flight abort threshold in main.py / fetch_snapshot.py.
MIN_BARS = 22

PROCEED, SKIP_RETRY, SKIP_DONE = 0, 10, 20


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

    api_key = os.getenv("ANTHROPIC_API_KEY")
    # In the cloud routine the key is auto-injected; locally it comes from .env.
    # If neither is available skip the check rather than blocking on a missing key.
    if not api_key:
        try:
            token_file = os.getenv("CLAUDE_SESSION_INGRESS_TOKEN_FILE")
            if not token_file:
                return True, "No API key available — skipping canary check"
        except Exception:
            return True, "No API key available — skipping canary check"

    try:
        client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
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
            "been placed. DO NOT re-run. Recover manually via Scenario B in CLAUDE.md."
        )
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
