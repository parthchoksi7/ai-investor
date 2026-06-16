# Daily Trading Cycle — Routine Prompt (canonical reference)

This is the authoritative copy of the **Daily Trading Cycle** Anthropic routine prompt
(`YOUR_ROUTINE_ID_DAILY`). The live prompt lives in Anthropic's systems; keep this
file in sync when you change it.

- **Schedule (cron):** `45 13,14,15,16 * * 1-5` — 9:45 / 10:45 / 11:45 / 12:45 AM **EDT**, Mon–Fri
  (initial attempt + 3 hourly retries to survive GitHub Actions scheduled-cron delay).
  - **Winter (EST, set in November):** `45 14,15,16,17 * * 1-5`. Revert in March.
- **Secrets are redacted below** (`<…>`). The live routine holds the real
  `POLYGON_API_KEY` and `SUPABASE_SERVICE_KEY`; never commit those to this repo.
- The `STEP 0` gate + the `pending_decisions.json` idempotency envelope together guarantee the
  pipeline executes **at most once per day**, only on the first attempt that sees fresh data.
  See `preflight_gate.py` and the "Daily Trading Cycle" section of `CLAUDE.md`.

---

```
Run the daily AI Investor V3 cycle.

═══════════════════════════════════════════════
STEP 0 — Pre-flight gate (run FIRST, on EVERY attempt)
═══════════════════════════════════════════════

This routine fires up to 4 times each morning (9:45 / 10:45 / 11:45 / 12:45 ET) to survive
GitHub Actions scheduled-cron delays. This gate decides whether THIS attempt should run at all.
Running the pipeline against stale data is pointless — main.py would just abort at preflight and
waste agent tokens. The gate is idempotent across the 4 attempts.

Pull the latest code AND the committed data files (market_snapshot.json, pending_decisions.json):
git pull --ff-only

Run the gate and capture its exit code:
python preflight_gate.py
GATE_EXIT=$?

Branch on GATE_EXIT — do not skip this:
  • 0  (PROCEED)    → Fresh market_snapshot.json for today is present AND today's pipeline has
                      not executed yet. Continue to STEP 1.
  • 10 (SKIP/RETRY) → market_snapshot.json is missing or not dated today. The GitHub Actions
                      market_data job hasn't landed fresh data yet. STOP now — do NOT fetch the
                      portfolio, run main.py, or place any orders. The next scheduled attempt
                      (+60 min) will re-check. If all 4 attempts see stale data, the day is
                      intentionally skipped (no trades — correct behavior).
  • 20 (SKIP/DONE)  → Today's pipeline already executed. STOP now — re-running would risk
                      double-execution.

Only continue past this step when GATE_EXIT == 0.
(preflight_gate.py uses only the Python standard library, so it runs before pip install.)

═══════════════════════════════════════════════
STEP 1 — Fetch portfolio via Robinhood MCP
═══════════════════════════════════════════════

Call in order:
1. get_accounts() — confirm account YOUR_ACCOUNT_NUMBER is present with agentic_allowed=true.
   If not found or agentic_allowed=false, STOP immediately.
2. get_portfolio(account_number='YOUR_ACCOUNT_NUMBER') — for cash and total_value
3. get_equity_positions(account_number='YOUR_ACCOUNT_NUMBER') — for holdings

Write mcp_portfolio.json with this exact structure:
{
  "as_of": "<ISO-8601 timestamp, US/Eastern, e.g. 2026-06-12T09:46:00-04:00>",
  "cash": <float>,
  "total_value": <float>,
  "positions": [
    {
      "symbol": "TICKER",
      "qty": <float>,
      "available_qty": <float>,
      "avg_price": <float>,
      "current_price": <float>,
      "market_value": <float>,
      "unrealized_pnl": <float>
    }
  ]
}

as_of MUST be the current timestamp (ET) at which you fetched this portfolio. execute.py
get_portfolio_summary() raises StalePortfolioError if as_of is missing or not dated today (ET),
and main.py then aborts before sizing any orders — every order is sized from this file, so a
stale copy would size today's trades against a prior day's cash/positions. Write it fresh every run.

available_qty = shares_available_for_sells from get_equity_positions (falls back to qty if the
field is absent). execute.py:_compute_qty caps SELL orders to available_qty to prevent oversell
when shares are held for options events or pending transfers.

═══════════════════════════════════════════════
STEP 2 — Set up environment
═══════════════════════════════════════════════

(Code was already pulled in STEP 0 — do not pull again.)

Create .env:
POLYGON_API_KEY=<POLYGON_API_KEY>
DRY_RUN=true
SUPABASE_URL=<SUPABASE_URL>
SUPABASE_SERVICE_KEY=<SUPABASE_SERVICE_KEY>

IMPORTANT: DRY_RUN=true is correct and intentional. robin_stocks (the Python Robinhood library)
is blocked in this cloud environment. Setting DRY_RUN=true prevents main.py from calling it and
from prematurely stamping executed_at. Real orders are placed via Robinhood MCP in STEP 4.

Install dependencies:
pip install -r requirements.txt -q

═══════════════════════════════════════════════
STEP 3 — Run the full pipeline
═══════════════════════════════════════════════

Run the pipeline and CAPTURE its exit code — do NOT discard it:
python main.py; MAIN_EXIT=$?

This runs the 7-agent pipeline (Regime → Research → Earnings → Devil's Advocate →
Position Review → Portfolio Manager → Chief Risk Officer), pre-computes fractional qty
for each decision, writes pending_decisions.json, logs to trades.csv / decision_journal.json /
agent_log.json / transactions.json, and publishes a snapshot to Supabase.

ALWAYS PUSH HEALTH FIRST — before validating anything, regardless of how main.py exited.
system_health.json is the ONLY trigger for alert.yml. main.py writes it on its abort paths
(stale data, zero-value portfolio) AND on success; if you stop the routine WITHOUT pushing it,
an ABORTED/FAILED day fires NO alert and fails silently. (A mid-pipeline crash may leave the
file stale because main.py never reached its own health-write — push whatever exists so the
last-known state + any partial agent_log reach the monitor.)
git config user.email 'ai-investor-bot@users.noreply.github.com'
git config user.name 'AI Investor Bot'
git add system_health.json agent_log.json
git diff --staged --quiet || (git commit -m 'chore: health' && (git push || (git pull --rebase && git push)))

CRASH GUARD — if main.py did not exit cleanly, STOP the routine here. Do NOT run STEP 4:
if [ "$MAIN_EXIT" -ne 0 ]; then
  echo "main.py exited $MAIN_EXIT — pipeline crashed mid-run. Health pushed (alert will fire). Not executing."
  # STOP NOW. pending_decisions.json may be absent or partial; proceeding would size or place
  # orders against an incomplete plan. Do not continue to STEP 4 under any circumstances.
  exit 0
fi

Only when MAIN_EXIT == 0, validate the output (guard against a missing/partial file —
on a first-ever run or a crash before the plan is written, the file may not exist):
python - <<'PY'
import json, os, sys
from datetime import datetime
from zoneinfo import ZoneInfo
if not os.path.exists('pending_decisions.json'):
    print("ERROR: pending_decisions.json was never written — main.py produced no plan.")
    sys.exit(1)
try:
    p = json.load(open('pending_decisions.json'))
except (OSError, json.JSONDecodeError) as e:
    print(f"ERROR: pending_decisions.json is unreadable ({e}) — treating as no plan.")
    sys.exit(1)
today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
if p.get('date') != today:
    print(f"ERROR: pending_decisions.json is dated {p.get('date')!r}, expected {today!r}")
    print("main.py exited early — market snapshot for today was not available.")
    sys.exit(1)
print(f"Pipeline OK: {len(p['decisions'])} decision(s) for {today}")
PY

If this check fails, STOP. Do not attempt order execution. (Health was already pushed above,
so the abort is visible to alert.yml.)

═══════════════════════════════════════════════
STEP 4 — Execute trades via Robinhood MCP
═══════════════════════════════════════════════

Read pending_decisions.json. It is a JSON envelope object — not a bare list.

GUARD 1 — Freshness:
  If pending_decisions["date"] ≠ today's date (YYYY-MM-DD, US/Eastern), STOP.
  Log: "Stale pending_decisions.json — skipping execution."

GUARD 2 — Idempotency:
  If pending_decisions["executed_at"] is not null, STOP immediately.
  This run was already executed. Do not place any orders. (Belt-and-suspenders with the
  STEP 0 gate's SKIP/DONE — across the 4 daily attempts this prevents double-execution.)

GUARD 3 — Execution claim (cross-attempt partial-fill protection):
  If pending_decisions["execution_started_at"] is not null, STOP immediately.
  A prior attempt STARTED placing orders and crashed before stamping executed_at.
  Orders may have been placed. Re-running would risk double-fills. Recover via the
  automated reconciler FIRST (DEPLOYMENT.md §9.3): `python reconcile.py` diffs live
  broker positions against the intended orders and classifies NONE_FILLED /
  ALL_FILLED / MANUAL_REQUIRED; `python reconcile.py --apply` then stamps (ALL_FILLED)
  or clears the stale claim (NONE_FILLED). Only fall back to the manual position diff
  / emergency stamp (DEPLOYMENT.md §9.4) if it returns MANUAL_REQUIRED.

Read decisions from pending_decisions["decisions"] (the nested array, not the root object).

TSLA HARD BLOCK: NEVER place any order for TSLA under any circumstances. Skip it entirely.

CLAIM THE RUN (only if decisions is non-empty — an empty list has nothing to protect):
BEFORE placing the FIRST order, stamp execution_started_at and push it so the claim
survives this environment dying mid-execution:

python - <<'PY'
import json
from journal import mark_execution_started
p = json.load(open('pending_decisions.json'))
mark_execution_started(p['run_id'])
PY
git config user.email 'ai-investor-bot@users.noreply.github.com'
git config user.name 'AI Investor Bot'
git add pending_decisions.json system_health.json trades.csv decision_journal.json agent_log.json transactions.json mcp_portfolio.json portfolio_snapshot.json fundamentals_cache.json portfolio_peak.json
git commit -m 'chore: execution claim'
git push || (git pull --rebase && git push)

If the push fails even after the rebase retry, STOP — do NOT place any orders.
Without a durable claim, a crash mid-execution re-opens the double-fill window.
Failing toward missed trades is correct; failing toward duplicate trades is not.
(The claim commit carries the full artifact set so the audit trail of what was
*intended* survives even if this attempt dies during order placement.)

Maintain a fills accumulator as you place orders — a dict you will write to fills.json:
  fills = {}   # ticker -> {"order_id": <str>, "price": <float or null>}

For each decision where action is BUY or SELL:

  1. QUANTITY — read decision["qty"] directly.
     This is the pre-computed fractional share count written by main.py (e.g., 0.648382).
     DO NOT recompute it. DO NOT round it to a whole number.
     Fallback (only if decision["qty"] is missing or null):
       fetch current price via get_equity_quotes, then compute:
       round(decision["target_weight"] × mcp_portfolio["total_value"] ÷ current_price, 6)

  2. SKIP CONDITION — skip this decision only if qty <= 0.
     qty = 0.648 is a valid fractional order. Place it.
     Robinhood supports fractional shares down to 0.000001.

  3. PLACE ORDER — generate a fresh UUID ref_id per order for broker-side idempotency
     (re-send the SAME ref_id only if you retry the SAME order after a transport failure;
     never reuse it for a different order):
     place_equity_order(
       account_number='YOUR_ACCOUNT_NUMBER',
       symbol=<ticker>,
       side='buy' or 'sell',
       quantity=<qty from step 1>,
       type='market',
       time_in_force='gfd',
       ref_id=<fresh uuid4>
     )

  4. RECORD THE RESULT:
     • If the response carries an order id, the broker ACCEPTED the order. Record it:
         fills[<ticker>] = {"order_id": <response id>, "price": <average/executed price or null>}
       (If the response has no fill price yet for a market order, leave price null — the
        decision-time quote in transactions.json is then kept.)
     • If the response is an error / rejection (no id), DO NOT add it to fills. Log the error
       to the session output. It will correctly remain dry_run=True (never published as a fill).
     • If place_equity_order itself ERRORS / throws (timeout, transport failure), treat it
       exactly like a rejection: record the error, DO NOT add it to fills, and CONTINUE to the
       next order. One order's exception must never abort the remaining orders — with SELLs
       placed before BUYs, aborting would strand capital in cash with the BUYs never attempted.

  5. LOG the result (ticker, qty, order id or error) to the session output.

Skip HOLD decisions entirely.
If pending_decisions["decisions"] is [], skip order placement (fills stays {}) and go to the stamp step.

Write the accumulator to fills.json so the stamp step can read it:
  Write fills.json = the fills dict you built above (e.g. {"BAC": {"order_id": "...", "price": 55.52}, ...}).

AFTER ALL ORDERS ARE PLACED (or if decisions was empty), stamp the execution —
BUT only if the CRO made a genuine decision. If the CRO itself failed due to API
error (api_failed=True in system_health.json), do NOT stamp: the next retry should
get a fresh run when the API recovers.

python - <<'PY'
import json, os
from journal import mark_pending_executed, mark_transactions_live
p = json.load(open('pending_decisions.json'))
h = json.load(open('system_health.json')) if os.path.exists('system_health.json') else {}
fills = json.load(open('fills.json')) if os.path.exists('fills.json') else {}
cro_api_failed = h.get('checks', {}).get('agent_7_cro', {}).get('api_failed', False)
if p['decisions'] or not cro_api_failed:
    mark_pending_executed(p['run_id'])
    # Reconcile ALL logs (transactions.json, trades.csv, decision_journal.json)
    # against broker fills: ONLY accepted orders go live; rejections stay
    # dry_run=True / status="rejected" and are never published or fed back to
    # the agents as phantom positions. fills maps ticker -> {"order_id": str,
    # "price": float|None}. If decisions existed but fills is empty, the
    # reconciler records reconciliation=FAILED on system_health.json (paging).
    # NOTE: never call mark_transactions_live(run_id) with no fills arg — it
    # now RAISES (a bare flip-all silently re-created the phantom-fill bug).
    mark_transactions_live(p['run_id'], fills)
    print(f"Execution stamped: run_id={p['run_id']} ({len(fills)} fill(s) reconciled)")
else:
    print("Skipping execution stamp — CRO blocked trades due to API error, not a risk decision. Next retry will re-run.")
PY

═══════════════════════════════════════════════
STEP 5 — Commit artifacts
═══════════════════════════════════════════════

git config user.email 'ai-investor-bot@users.noreply.github.com'
git config user.name 'AI Investor Bot'
git add portfolio_snapshot.json system_health.json mcp_portfolio.json trades.csv decision_journal.json fundamentals_cache.json portfolio_peak.json pending_decisions.json agent_log.json transactions.json fills.json
git diff --staged --quiet || git commit -m 'chore: daily cycle'

# Push WITH a rebase retry. Orders are already LIVE — this push carries the executed_at stamp,
# the reconciled logs, fills.json, and the post-trade snapshot that triggers publish.yml. A
# silent failure here means the website never updates AND today's durable fill record never
# reaches the remote while real orders exist. (STEP 4's claim push retries; STEP 5 must too.)
if git push || (git pull --rebase && git push); then
  echo "Artifacts pushed — executed_at + fills + snapshot are durable; publish.yml will update Supabase."
else
  # Still failing after the rebase retry — do NOT end silently. Force the failure into the
  # monitor so alert.yml pages you, then make a best-effort push of that alert.
  echo "CRITICAL: STEP 5 push failed after rebase retry. Trades are LIVE but not recorded remotely."
  python - <<'PY'
from health import append_check, FAILED
append_check("artifact_push", FAILED,
             message="STEP 5 push failed after rebase retry — trades LIVE but executed_at/fills/snapshot did not reach remote. Manual `git push` required; verify Supabase and run reconcile.py.")
PY
  git add system_health.json && git commit -m 'chore: artifact push failure' \
    && (git push || (git pull --rebase && git push)) \
    || echo "Could not reach remote at all — MANUAL intervention required NOW (push by hand, verify fills via get_equity_orders)."
fi
```

---

## What changed — Jun 10 2026 (commits `7652b9d`, `8f0b2e9`)

1. **STEP 1 — `available_qty` field added** to `mcp_portfolio.json`. Write
   `shares_available_for_sells` from `get_equity_positions` as `available_qty` alongside `qty`.
   `execute.py:_compute_qty` now caps SELL orders to `available_qty` to prevent oversell when
   shares are held for options events or pending transfers.
2. **STEP 3 validation** now reads `system_health.json` and reports any FAILED/DEGRADED agent
   checks as informational warnings (not hard stops). Identifies 529 API overload failures vs.
   genuine data problems.
3. **Code changes in `analysis.py`** (pulled automatically via `git pull` in STEP 0):
   - All 7 agents now retry up to 2× on failure (3 total attempts).
   - 529/overloaded errors use 30s/60s backoff instead of 1s/2s.
   - CRO default adds `api_failed: True` flag; health records DEGRADED (not OK) when CRO API fails.
4. **Code changes in `main.py`** (pulled automatically):
   - Circuit breaker: halts execution if SELL notional > 50% of portfolio value.
   - All `date` stamps now use `America/New_York` explicitly (was UTC).
5. **Code changes in `journal.py`** (pulled automatically):
   - All JSON writes are now atomic (`.tmp` + `os.replace()`).
   - `agent_log.json` capped at 90 entries.

## What changed vs. the previous prompt (Jun 9 2026)

1. **Added STEP 0 pre-flight gate** + moved `git pull --ff-only` to STEP 0 (was in STEP 2). The
   routine now no-ops cheaply on stale-data or already-run attempts instead of running the full
   pipeline and aborting.
2. **Schedule → 4 fire times** (`45 13,14,15,16 * * 1-5`) so a delayed/ skipped 8 AM
   `market_data.yml` run is caught by a later retry.
3. **Fixed STEP 3 validation bugs**: replaced `python -c "…"` (which had a `{p.get("date")}`
   nested-double-quote shell bug) with a `python - <<'PY'` heredoc, and switched `date.today()`
   (UTC in cloud) → US/Eastern to match the gate and `pending_decisions["date"]`.
4. **STEP 4 stamp** also converted to a heredoc for paste-safety.

## What changed — Jun 12 2026 evening (remediation batch)

1. **STEP 4 — stamp-first execution claim** (closes the cross-attempt double-fill window that
   was previously documented here as an unresolved caveat): `execution_started_at` is stamped,
   committed, and pushed BEFORE the first order is placed. GUARD 3 + the STEP 0 gate treat a
   non-null claim dated today as SKIP/DONE. If the claim push fails after one rebase retry, the
   attempt STOPS without placing orders — the system now fails toward missed trades, never
   duplicate trades. Crash-mid-execution recovery is Scenario B (position diff), never re-run.

## What changed — Jun 15 2026

1. **GUARD 3 recovery pointer → automated reconciler.** Now that A7 shipped
   `reconcile.py`, GUARD 3 points at the automated crash-state recovery (DEPLOYMENT.md
   §9.3: `reconcile.py` / `--apply`) as the first step, with the manual position-diff /
   emergency stamp (§9.4) reserved for the `MANUAL_REQUIRED` case. Cosmetic doc-sync —
   GUARD 3's functional job is unchanged (STOP on a non-null `execution_started_at`).

## What changed — Jun 16 2026 (observability + crash-handling hardening)

Closes the gaps where a failure was invisible or could cascade into bad execution.
All four are prompt-only (no code change). Verified against `main.py`'s actual control
flow (`run_daily_cycle()` has no top-level try/except, and its abort paths `return`
rather than `sys.exit`, so the routine — not main.py — owns making failure observable).

1. **STEP 3 — always push `system_health.json` before validating.** `alert.yml` fires
   ONLY on a `system_health.json` push. Previously the routine reached the STEP 5 push
   only on the happy path, so an ABORTED/FAILED day stopped before STEP 5 and the health
   record never left the box — a silent no-trade day with no alert. Health is now pushed
   immediately after `main.py`, on every path.
2. **STEP 3 — capture `main.py`'s exit code + CRASH GUARD.** `python main.py; MAIN_EXIT=$?`;
   on a non-zero exit the routine pushes health and STOPS — it does NOT proceed to STEP 4.
   Prevents sizing/placing orders against a partial or missing `pending_decisions.json`
   after an unhandled mid-pipeline exception.
3. **STEP 3 — validation heredoc guards a missing/unreadable file** (first-ever run, or a
   crash before the plan is written) instead of throwing `FileNotFoundError`.
4. **STEP 5 — push with a rebase retry + escalation.** Was `git push || echo WARNING`
   (orders live, push silently lost → no Supabase update, no durable fill record). Now
   retries via `git pull --rebase`, and on a persistent failure writes an `artifact_push`
   FAILED health check and best-effort pushes that so `alert.yml` pages.

> **Known follow-ups (code changes, tracked separately — NOT fixed by this prompt edit):**
> (a) `preflight_gate._check_api_health()` calls bare `anthropic.Anthropic()` when
> `ANTHROPIC_API_KEY` is unset, but the cloud authenticates via
> `CLAUDE_SESSION_INGRESS_TOKEN_FILE` (`auth_token=`), as `analysis.py:_get_client()` does
> — so the 529 canary cannot authenticate in the cloud and silently returns "healthy",
> disabling the overload protection on the live path. (b) STEP 4 records an order as filled
> on broker ACCEPTANCE without polling `get_equity_orders` to confirm the fill or capture
> the real fill price — an accepted-then-rejected market order (halt, unsettled cash on the
> cash account) is logged as a fill at the decision-time quote.
