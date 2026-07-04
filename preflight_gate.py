"""
preflight_gate.py — Decide whether (and in WHICH MODE) the routine should run.

Phase 5 (weekly cadence, §6): the cron still fires every weekday morning
(9:45 / 10:45 / 11:45 / 12:45 ET — initial attempt + 3 hourly retries), but the
GATE decides the mode:

  • REBALANCE (Wednesdays, or Thu/Fri catch-up when the week has none) — the full
    7-agent pipeline (main.py) + order execution. Needs a fresh market_snapshot.json
    AND a fresh, schema-valid research_dossier.json (the Wednesday agent input) AND
    a healthy Anthropic API.
  • RISK-WATCH (every other trading day) — the deterministic SELL-only safety net
    (risk_watch.py). Uses live Robinhood MCP portfolio data fetched by the routine,
    NOT the snapshot — so a late GitHub Actions cron never disables the daily
    stop-loss check (§6.7 / P1-7: price triggers always fire).

Exit codes (the routine MUST branch on these):
  0  → PROCEED/REBALANCE:  rebalance day, fresh data + dossier, API healthy, not yet
       executed. Run ``main.py`` then execute via MCP.
  30 → PROCEED/RISK-WATCH: any other trading day, not yet executed today. Run
       ``risk_watch.py`` then execute its (SELL-only) envelope via MCP.
  10 → SKIP/RETRY: market closed today (weekend / NYSE holiday) OR — on a rebalance
       day — data/dossier not fresh OR Anthropic API overloaded (529). Do NOT run.
       The next scheduled attempt (+60 min) re-checks. If all attempts on a
       rebalance day see stale data, the Thu/Fri catch-up picks the week up; if the
       whole week misses, the heartbeat's missed-week check alerts (§15.3).
  20 → SKIP/DONE: today's pipeline (either mode) already executed or claimed
       (idempotency). Do NOT run again — re-running risks double-execution.

Once-per-ISO-week rebalance lock (§6.5): a rebalance CLAIM or EXECUTION is mirrored
by journal.py into ``last_rebalance.json`` — durable across the daily risk_watch
overwrites of pending_decisions.json. The Thu/Fri catch-up fires only when NO
rebalance was attempted this ISO week (a Wednesday crash-mid-execution counts as
attempted: orders may exist — recover via Scenario B, never re-run).

Usage in the routine (after ``git checkout -B main origin/main``):
    python preflight_gate.py
    # exit 0  → main.py rebalance path   exit 30 → risk_watch.py path
    # exit 10/20 → stop cleanly, do NOT trade
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

PROCEED, SKIP_RETRY, SKIP_DONE, PROCEED_RISK_WATCH = 0, 10, 20, 30

# The rebalance weekday (0=Mon … 2=Wed) is policy, not code (IPS Appendix A).
# policy.py degrades to its built-in v2.0 defaults when PyYAML isn't installed
# yet (this gate runs BEFORE pip install), so the import is stdlib-safe.
from policy import get as _policy_get  # noqa: E402
REBALANCE_WEEKDAY = int(_policy_get("rebalance_weekday", 2))

# NYSE full-day market closures (weekends are handled separately). On a closed
# market the broker still ACCEPTS GFD orders but they sit `queued` and never fill,
# expiring at the (nonexistent) close — so the routine must not run. This was a
# live incident on Juneteenth 2026-06-19: the snapshot was dated "today", the gate
# proceeded, and 4 orders were placed that could never fill.
#
# The calendar lives in market_calendar.py (single source — the heartbeat and the
# ISO-week lock also need it). Re-exported here so existing importers/tests that
# reference preflight_gate.NYSE_HOLIDAYS keep working.
from market_calendar import NYSE_HOLIDAYS, iso_week_of  # noqa: E402


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


def _rebalance_attempted_this_week(pending) -> tuple[bool, str]:
    """(attempted, detail) — was a REBALANCE claimed or executed in the current ISO week?

    Primary source: last_rebalance.json (the durable mirror journal.py writes on the
    claim/executed stamps — survives risk_watch's daily overwrite of the envelope).
    Fallback: the pending envelope itself, when it IS a rebalance one dated this week
    (covers pre-mirror legacy envelopes and the window before any mirror exists).
    A CLAIM without executed_at still counts — orders may exist (Scenario B); the
    catch-up must fail toward a missed rebalance, never a duplicate one.
    """
    this_week = iso_week_of(TODAY)

    lr = _read_json("last_rebalance.json")
    if isinstance(lr, dict) and lr.get("iso_week") == this_week \
            and (lr.get("executed_at") or lr.get("execution_started_at")):
        return True, (f"last_rebalance.json: {lr.get('date')} run_id={lr.get('run_id')} "
                      f"executed_at={lr.get('executed_at')}")

    if isinstance(pending, dict) and pending.get("mode", "rebalance") == "rebalance" \
            and (pending.get("executed_at") or pending.get("execution_started_at")):
        try:
            if iso_week_of(pending.get("date")) == this_week:
                return True, (f"pending envelope: {pending.get('date')} "
                              f"run_id={pending.get('run_id')}")
        except Exception:
            pass  # undateable envelope — cannot prove this week; fall through

    return False, ""


def _dossier_fresh() -> tuple[bool, str]:
    """Minimal, stdlib-only freshness check on research_dossier.json (§11.4/P1-5).

    The full schema validation lives in build_dossier.validate_dossier (main.py
    re-runs it before the agents); this gate check is deliberately dependency-free
    so it runs before pip install. Requires: as_of == TODAY, built_from_days ≥ 2
    with the newest day == TODAY, and a non-empty tickers map. A rebalance must
    never run against a stale or empty dossier — the agents would read yesterday's
    (or nobody's) research as if it were today's.
    """
    d = _read_json("research_dossier.json")
    if not isinstance(d, dict) or not d:
        return False, "research_dossier.json missing or unreadable"
    if d.get("as_of") != TODAY:
        return False, f"dossier as_of={d.get('as_of')!r}, expected {TODAY}"
    bfd = d.get("built_from_days") or []
    if len(bfd) < 2:
        return False, f"dossier built_from_days={bfd} (< 2 — insufficient history)"
    if max(str(x) for x in bfd) != TODAY:
        return False, (f"dossier factors stale: newest built_from_day="
                       f"{max(str(x) for x in bfd)}, expected {TODAY}")
    if not d.get("tickers"):
        return False, "dossier has no tickers"
    return True, f"fresh ({d.get('n_tickers', len(d.get('tickers', {})))} tickers)"


def _check_api_health() -> tuple[bool, str]:
    """Make a minimal Anthropic API call to verify the API is not overloaded.
    Returns (healthy: bool, message: str). Rebalance mode only — risk_watch is
    deterministic (zero LLM) and must never be blocked by an API outage.
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

    # 1. Daily idempotency — has today's pipeline (either mode) already executed?
    pending = _read_json("pending_decisions.json")
    if pending and pending.get("date") == TODAY and pending.get("executed_at"):
        print(
            f"SKIP/DONE: pipeline already executed today ({TODAY}, "
            f"mode={pending.get('mode', 'rebalance')}), "
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
            f"mode={pending.get('mode', 'rebalance')}, "
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

    # 2. Mode routing (§6.3) — rebalance on the fixed weekday, or Thu/Fri catch-up
    #    when this ISO week has no rebalance attempt yet; risk_watch otherwise.
    weekday = _NOW_ET.weekday()
    attempted, attempt_detail = _rebalance_attempted_this_week(pending)
    is_rebalance_day = (weekday >= REBALANCE_WEEKDAY) and not attempted

    if not is_rebalance_day:
        why = (f"rebalance already attempted this ISO week ({attempt_detail})"
               if attempted else
               f"weekday {_NOW_ET.strftime('%A')} is before the rebalance day")
        print(
            f"PROCEED/RISK-WATCH: {TODAY} is a risk-watch day — {why}. "
            "Run risk_watch.py (SELL-only safety net; live MCP data; no LLM). "
            "Do NOT run main.py."
        )
        return PROCEED_RISK_WATCH

    catch_up = weekday > REBALANCE_WEEKDAY
    if catch_up:
        print(f"⏩ Catch-up rebalance: no rebalance attempted in ISO week "
              f"{iso_week_of(TODAY)} and today ({_NOW_ET.strftime('%A')}) is past "
              f"the scheduled weekday — running the weekly rebalance today (§6.5.2).")

    # 3. Freshness — is market_snapshot.json dated today with enough history?
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

    # 3b. Dossier freshness (§11.4 / P1-5) — the rebalance agents read the dossier;
    #     a stale/invalid one must SKIP (the Thu/Fri catch-up or next attempt
    #     re-checks), never be silently traded on.
    dossier_ok, dossier_msg = _dossier_fresh()
    if not dossier_ok:
        print(
            f"SKIP/RETRY: research dossier not ready — {dossier_msg}. "
            "The rebalance must not run against a stale/invalid dossier. "
            "Not running; the next scheduled attempt (+60 min) will re-check."
        )
        return SKIP_RETRY

    # 4. API health — canary call to catch Anthropic 529 overloads before burning
    #    the full pipeline against a degraded API.
    api_ok, api_msg = _check_api_health()
    if not api_ok:
        print(
            f"SKIP/RETRY: {api_msg}. "
            "Not running; the next scheduled attempt (+60 min) will re-check."
        )
        return SKIP_RETRY

    print(
        f"PROCEED/REBALANCE: fresh market_snapshot.json (date={TODAY}, "
        f"{len(snap.get('prices', {}))} tickers, min_depth={min_depth}), "
        f"dossier {dossier_msg}, API healthy, and no rebalance this ISO week"
        f"{' (catch-up)' if catch_up else ''}. Run main.py."
    )
    return PROCEED


if __name__ == "__main__":
    sys.exit(main())
