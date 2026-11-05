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
       day — Anthropic API overloaded (529). Do NOT run. The next scheduled attempt
       (+60 min) re-checks.
  11 → SKIP/RETRY-STALE: on a rebalance day, the snapshot or dossier is not fresh
       (the data plane is late). Do NOT run; push data_request.json so
       market_data.yml starts now rather than on GitHub's deferred cron. If all
       attempts see stale data, the Thu/Fri catch-up picks the week up; if the
       whole week misses, the heartbeat's missed-week check alerts (§15.3).
       FINAL-ATTEMPT FALLBACK: from 12:40 ET (the routine's last scheduled attempt
       is 12:45), a blocked rebalance returns 30 instead of 10/11 — the stop-loss
       check must not be skipped all day because data or the API is late. The
       risk_watch envelope never consumes the week's rebalance.
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
    # exit 11 → push data_request.json, then stop   exit 10/20 → stop, do NOT trade
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
# PREFLIGHT_TIME_OVERRIDE ("HH:MM", ET) pins the time of day — tests use it to
# exercise the final-attempt fallback deterministically. With only the date
# override set, the effective time is 00:00 (never the final attempt).
_TIME_OVERRIDE = os.getenv("PREFLIGHT_TIME_OVERRIDE")
if _TIME_OVERRIDE:
    _hh, _mm = (int(x) for x in _TIME_OVERRIDE.split(":"))
    _NOW_ET = _NOW_ET.replace(hour=_hh, minute=_mm)
TODAY = _NOW_ET.strftime("%Y-%m-%d")

# Minimum history bars required for any quant calculation — mirrors the
# pre-flight abort threshold in main.py / fetch_snapshot.py.
MIN_BARS = 22

PROCEED, SKIP_RETRY, SKIP_DONE, PROCEED_RISK_WATCH = 0, 10, 20, 30
# SKIP/RETRY because the DATA PLANE is late (snapshot or dossier not fresh) — as
# opposed to 10 (market closed / API overloaded). The routine answers 11 by
# pushing data_request.json, which starts market_data.yml immediately instead of
# waiting on GitHub's deferred cron (Oct 2026: crons landing 10 AM–3:30 PM ET).
# An unsynced routine only continues on 0/30, so it treats 11 exactly like 10.
SKIP_RETRY_STALE = 11

# The routine's LAST scheduled attempt is 12:45 ET (crons 9:45/10:45/11:45/12:45).
# On a rebalance-eligible day, an attempt at/after this time whose rebalance is
# still blocked runs risk_watch instead of skipping — otherwise a late data plane
# disables the −25% stop for the whole day (Sep 23–25 and Sep 30–Oct 1 2026: five
# trading days with no stop check while GitHub Actions was billing-blocked).
# 12:40, not earlier: the 11:45 attempt would have to start 55 min late to be
# mistaken for the final one (which would consume the day with risk_watch). The ET
# times hold year-round only while the routine cron is DST-adjusted (CLAUDE.md DST
# note); if that update is missed the fallback simply never fires — no worse than
# before it existed.
FINAL_ATTEMPT_ET = (12, 40)


def _is_final_attempt() -> bool:
    return (_NOW_ET.hour, _NOW_ET.minute) >= FINAL_ATTEMPT_ET

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


def data_plane_ready() -> tuple[bool, str]:
    """The ONE definition of "today's data has landed": snapshot dated today with
    ≥ MIN_BARS bars everywhere AND a fresh, valid dossier. The gate uses it, and
    market_data.yml's on-demand `check` job imports it (stdlib-only) — so the two
    can never disagree about whether a data run is needed."""
    snap = _read_json("market_snapshot.json")
    if snap is None:
        return False, ("market_snapshot.json is missing or unreadable — the "
                       "market_data.yml GitHub Actions job has not landed data yet")
    if snap.get("date") != TODAY:
        return False, (f"market_snapshot.json date={snap.get('date')}, expected {TODAY} — "
                       "the market_data job has not pushed fresh data yet "
                       "(did you `git pull` before running this gate?)")
    depths = [len(h) for h in (snap.get("history") or {}).values()]
    min_depth = min(depths) if depths else 0
    if min_depth < MIN_BARS:
        return False, (f"market_snapshot.json has only {min_depth} history bars "
                       f"(need {MIN_BARS}+) — data is incomplete")
    ok, msg = _dossier_fresh()
    if not ok:
        return False, (f"research dossier not ready — {msg}; the rebalance must not run "
                       "against a stale/invalid dossier")
    return True, f"fresh (min_depth={min_depth}; dossier {msg})"


CANARY_MODEL = "claude-haiku-4-5-20251001"   # = analysis.MODEL_FAST
CANARY_URL   = "https://api.anthropic.com/v1/messages"   # default; ANTHROPIC_BASE_URL overrides


def _check_api_health() -> tuple[bool, str]:
    """Make one minimal model call with the SAME credential the agents will use.
    Returns (healthy, message). Rebalance mode only — risk_watch is deterministic
    (zero LLM) and is never gated on this.

    Standard library only (urllib), on purpose. The routine runs this gate in
    STEP 0, BEFORE `pip install` in STEP 2, so `import anthropic` fails here in the
    cloud. Until 2026-11-05 this function caught that ImportError and returned
    "skipping canary check" — healthy — so in the cloud the canary had never made
    a call, and the PROCEED line printed "API healthy" regardless.

    ANY failure blocks the rebalance (SKIP-RETRY; on the final attempt the gate
    falls back to risk_watch, so the −25% stop still runs and the week stays
    open for a Thu/Fri catch-up). Until 2026-11-05 only 529/overloaded blocked and
    auth/network errors "proceeded": on Wed 2026-11-04 every agent call then failed
    with 403 permission_error (the cloud session token stopped being accepted for
    direct API calls), the run produced an empty plan, and that empty plan was
    stamped as the week's rebalance. A credential the canary cannot use is one the
    agents cannot use either, so proceeding never helps.

    Credential and endpoint come from llm_credentials.py, the module
    analysis._get_client() uses too. No credential at all → block.
    """
    import urllib.error
    import urllib.request

    # Test seam (like PREFLIGHT_DATE_OVERRIDE): the subprocess gate tests pin the
    # canary result so the suite never makes a live call or depends on the
    # developer's credentials. "ok" → healthy, anything else → blocked.
    _canary_override = os.getenv("PREFLIGHT_CANARY_OVERRIDE")
    if _canary_override:
        if _canary_override == "ok":
            return True, "model API healthy (PREFLIGHT_CANARY_OVERRIDE)"
        return False, f"model API blocked (PREFLIGHT_CANARY_OVERRIDE={_canary_override})"

    # The SAME rule the agents use (llm_credentials.py — one definition).
    from llm_credentials import model_credential, base_url
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
    try:
        kind, secret, cred = model_credential()
    except OSError as e:
        return False, f"model credential unreadable ({e}) — rebalance cannot run"
    if kind == "api_key":
        headers["x-api-key"] = secret
    elif kind == "session_token":
        headers["authorization"] = f"Bearer {secret}"
    else:
        return False, ("no model credential (no ANTHROPIC_API_KEY / "
                       "AI_INVESTOR_ANTHROPIC_API_KEY, no session token) "
                       "— the agents cannot run")

    body = json.dumps({
        # 50 tokens, not 1: under heavy load short requests succeed while
        # 600-token requests come back empty.
        "model": CANARY_MODEL,
        "max_tokens": 50,
        "messages": [{"role": "user", "content": 'Reply with exactly: {"status":"ok"}'}],
    }).encode()
    # Same endpoint the agents' SDK client uses.
    url = base_url() + "/v1/messages"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:160]
        except Exception:
            detail = ""
        if e.code == 529 or "overloaded" in detail.lower():
            return False, f"model API overloaded (529) — {detail}"
        if e.code in (401, 403):
            return False, (f"model API rejected the {cred} (HTTP {e.code}) — "
                           f"the agents would fail the same way. {detail}")
        return False, f"model API error HTTP {e.code} ({cred}) — {detail}"
    except Exception as e:  # network, timeout, bad JSON
        return False, f"model API unreachable ({cred}) — {str(e)[:120]}"

    text = "".join(b.get("text", "") for b in (payload.get("content") or [])
                   if isinstance(b, dict)).strip()
    if not text:
        return False, "model API returned an empty response under load — skipping run"
    return True, f"model API healthy ({cred})"


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

    def _blocked(code: int, why: str) -> int:
        """A rebalance that cannot run today must not ALSO disable the daily
        safety net (P1-7). Before the final attempt: skip and let the next attempt
        re-check. On the final attempt: run risk_watch (SELL-only, live MCP data,
        no snapshot/dossier/LLM needed). Its envelope is mode="risk_watch", which
        never satisfies the ISO-week rebalance lock — so the Thu/Fri catch-up
        stays available."""
        if _is_final_attempt():
            print(
                f"PROCEED/RISK-WATCH: rebalance blocked on the FINAL attempt "
                f"({_NOW_ET.strftime('%H:%M')} ET) — {why}. Running risk_watch.py so "
                "the −25% stop and kill-switch check still happen today. The weekly "
                "rebalance is NOT consumed: a Thu/Fri catch-up remains available. "
                "Do NOT run main.py."
            )
            return PROCEED_RISK_WATCH
        label = "SKIP/RETRY-STALE" if code == SKIP_RETRY_STALE else "SKIP/RETRY"
        print(f"{label}: {why}. Not running; the next scheduled attempt (+60 min) "
              "will re-check.")
        return code

    # 3. Data plane — snapshot dated today with ≥ MIN_BARS AND a fresh, valid dossier
    #    (§11.4 / P1-5). Both come from the same market_data.yml run, so a data
    #    request fixes either; the on-demand check job uses this same function.
    data_ok, data_msg = data_plane_ready()
    if not data_ok:
        return _blocked(SKIP_RETRY_STALE, data_msg)
    snap = _read_json("market_snapshot.json") or {}
    min_depth = min((len(h) for h in (snap.get("history") or {}).values()), default=0)

    # 4. API health — one real model call with the agents' credential, so an
    #    overload OR a rejected/missing credential blocks the run before it burns
    #    the week on an empty plan (2026-11-04).
    api_ok, api_msg = _check_api_health()
    if not api_ok:
        return _blocked(SKIP_RETRY, api_msg)

    print(
        f"PROCEED/REBALANCE: fresh market_snapshot.json (date={TODAY}, "
        f"{len(snap.get('prices', {}))} tickers, min_depth={min_depth}), "
        f"data plane {data_msg}, {api_msg}, and no rebalance this ISO week"
        f"{' (catch-up)' if catch_up else ''}. Run main.py."
    )
    return PROCEED


if __name__ == "__main__":
    sys.exit(main())
