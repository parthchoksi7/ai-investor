# Daily Trading Cycle — Routine Prompt (canonical reference)

This is the authoritative copy of the **Daily Trading Cycle** Anthropic routine prompt
(`YOUR_ROUTINE_ID_DAILY`). The live prompt lives in Anthropic's systems; keep this
file in sync when you change it.

- **Schedule (cron):** `45 13,14,15,16 * * 1-5` — 9:45 / 10:45 / 11:45 / 12:45 AM **EDT**, Mon–Fri
  (initial attempt + 3 hourly retries to survive GitHub Actions scheduled-cron delay).
  - **Winter (EST, set in November):** `45 14,15,16,17 * * 1-5`. Revert in March.
- **Phase 5 (weekly cadence):** the cron still fires every weekday, but the STEP 0 gate
  routes the MODE: **REBALANCE** (exit 0 — Wednesdays, or Thu/Fri catch-up) runs the full
  7-agent pipeline; **RISK-WATCH** (exit 30 — every other trading day) runs the
  deterministic SELL-only safety net (`risk_watch.py`, no LLM, no BUYs). Both modes
  execute through the SAME envelope/claim/stamp machinery below.
- **No API secrets belong in this prompt.** The cloud plane can't reach Polygon or Supabase
  (both 403), so `POLYGON_API_KEY` / `SUPABASE_*` are unused here (STEP 2 explains why). The
  real Supabase write runs in GitHub Actions with the GitHub secret store. Keep the routine
  prompt secret-free; the only broker access is the Robinhood MCP connector (no creds stored).
- The `STEP 0` gate + the `pending_decisions.json` idempotency envelope together guarantee
  **at most one execution per day** and **at most one rebalance per ISO week** (the
  `last_rebalance.json` mirror survives risk-watch's daily envelope overwrite).
  See `preflight_gate.py` and the "Daily Trading Cycle" section of `CLAUDE.md`.

---

```
Run the daily AI Investor V3 cycle.

═══════════════════════════════════════════════
STEP 0 — Pre-flight gate (run FIRST, on EVERY attempt)
═══════════════════════════════════════════════

This routine fires up to 4 times each morning (9:45 / 10:45 / 11:45 / 12:45 ET) to survive
GitHub Actions scheduled-cron delays. The gate decides whether THIS attempt runs at all, and
in WHICH MODE. The gate is idempotent across the 4 attempts.

OPERATE ON `main` — this is mandatory and must be the FIRST thing you do.
This routine runs in a fresh worktree that may start on an arbitrary branch
(e.g. `claude/…`). A bare `git push` pushes to the CURRENT branch, so if you do
not switch to `main` first:
  • the gate reads a STALE `pending_decisions.json` / `last_rebalance.json` (main's,
    not this branch's) — on a retry it sees "not executed" and RE-RUNS the pipeline,
    which with real trades is a DOUBLE-FILL (the idempotency envelope is defeated);
  • `system_health.json` / `portfolio_snapshot.json` never reach `main`, so
    `alert.yml` (main-scoped) fires NO alert and the canonical state diverges.
Force the working tree onto the latest `main` so the gate reads the canonical
envelope and every later `git push` lands on `main`:

git fetch origin main
git checkout -B main origin/main      # reset local main to origin/main and switch to it

Run the gate and capture its exit code:
python preflight_gate.py
GATE_EXIT=$?

Branch on GATE_EXIT — do not skip this:
  • 0  (PROCEED/REBALANCE)  → Wednesday (or Thu/Fri catch-up for a missed week), fresh
                              market_snapshot.json AND fresh research_dossier.json, no
                              rebalance attempted this ISO week. Set MODE=rebalance and
                              continue to STEP 1.
  • 30 (PROCEED/RISK-WATCH) → any other trading day. Set MODE=risk_watch and continue to
                              STEP 1. You will run risk_watch.py (SELL-only, zero LLM) in
                              STEP 3 — NEVER main.py in this mode.
  • 10 (SKIP/RETRY)         → market closed, or (on a rebalance day) snapshot/dossier not
                              fresh or API overloaded. STOP now — do NOT fetch the
                              portfolio, run any pipeline, or place any orders. The next
                              scheduled attempt (+60 min) re-checks.
  • 20 (SKIP/DONE)          → Today's pipeline (either mode) already executed or claimed.
                              STOP now — re-running would risk double-execution.

Only continue past this step when GATE_EXIT == 0 or GATE_EXIT == 30.
(preflight_gate.py uses only the Python standard library, so it runs before pip install.)

═══════════════════════════════════════════════
STEP 1 — Fetch portfolio via Robinhood MCP  (both modes)
═══════════════════════════════════════════════

Call in order:
1. get_accounts() — confirm account YOUR_ACCOUNT_NUMBER is present with agentic_allowed=true.
   If not found or agentic_allowed=false, STOP immediately.
2. get_portfolio(account_number='YOUR_ACCOUNT_NUMBER') — for cash and total_value
3. get_equity_positions(account_number='YOUR_ACCOUNT_NUMBER') — for holdings

Write mcp_portfolio.json with this exact structure:
{
  "as_of": "<ISO-8601 timestamp, US/Eastern, e.g. 2026-07-06T09:46:00-04:00>",
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
and the pipeline then aborts before sizing any orders — every order is sized from this file, so a
stale copy would size today's trades against a prior day's cash/positions. Write it fresh every run.

available_qty = shares_available_for_sells from get_equity_positions (falls back to qty if the
field is absent). SELL orders are capped to available_qty to prevent oversell. In RISK-WATCH mode
avg_price and current_price are ALSO load-bearing: the −25% stop-loss trigger is evaluated as
current_price vs avg_price (cost basis) — write both accurately for every position.

═══════════════════════════════════════════════
STEP 2 — Set up environment  (both modes)
═══════════════════════════════════════════════

(Code was already pulled in STEP 0 — do not pull again.)

Create .env:
DRY_RUN=true

NO API SECRETS ARE NEEDED HERE — and none should be pasted into this prompt. This cloud
plane cannot reach Polygon or Supabase (both 403), so:
  • POLYGON_API_KEY is unused — market_data.py reads the committed market_snapshot.json
    (get_market_snapshot() checks the local file first); every Polygon call is guarded by
    `if POLYGON_KEY`, so its absence is a no-op.
  • SUPABASE_URL / SUPABASE_SERVICE_KEY are unused — publish.py writes portfolio_snapshot.json
    and then, with no keys, prints "Supabase not configured — skipping" and returns cleanly.
    The committed portfolio_snapshot.json push triggers publish.yml in GitHub Actions, which
    does the REAL Supabase write using the GitHub Actions secret store. Secrets live there,
    not in this prompt.

IMPORTANT: DRY_RUN=true is correct and intentional. robin_stocks (the Python Robinhood library)
is blocked in this cloud environment. Setting DRY_RUN=true prevents the pipeline from calling it
and from prematurely stamping executed_at. Real orders are placed via Robinhood MCP in STEP 4.

Install dependencies. The base image ships some Debian-managed packages (PyJWT,
cryptography) that a bare `pip install` cannot uninstall — the install then ABORTS
before reaching anthropic / robin_stocks, and the pipeline fails deep in STEP 3
with `No module named 'anthropic'` (this happened live on Jul 8 2026). Install,
then VERIFY the imports actually resolve; fall back to --ignore-installed; if the
imports STILL fail, STOP cleanly (a skipped attempt is the designed failure
direction — the next cron retries; a half-installed pipeline is not):

pip install -r requirements.txt -q 2>&1 | tail -2 || true
if ! python -c "import anthropic, robin_stocks, pyotp, dotenv, yaml" 2>/dev/null; then
  echo "deps incomplete after first install — retrying with --ignore-installed"
  pip install -r requirements.txt -q --ignore-installed 2>&1 | tail -2 || true
fi
python -c "import anthropic, robin_stocks, pyotp, dotenv, yaml" || {
  echo "dependencies unavailable — STOP. Push nothing, place no orders. The next scheduled attempt (+60 min) retries."
  exit 0
}

═══════════════════════════════════════════════
STEP 3 — Run the pipeline for this MODE
═══════════════════════════════════════════════

⛔ NEVER edit, debug, or commit any .py source file — not to work around an
import error, a crash, or a traceback, not under any time pressure. The routine
executes the committed code; it does not author it. A source change committed
here bypasses the mandatory `/code-review` + test gates (DEPLOYMENT §7.0) and
lands unreviewed on the live order path (this happened on Jul 8 2026 — a routine
hot-fixed main.py mid-run and committed it to main). If the pipeline crashes on
a code bug, that is a MISSED rebalance — the designed, safe failure direction:
push system_health.json (below) so alert.yml pages the owner, then STOP. The
owner fixes the code through the normal review gates; Thu/Fri catch-up + Friday's
missed-week heartbeat cover the week. Fixing code toward a completed trade is
never worth an unreviewed change to the capital path.

If MODE == rebalance:   python main.py; MAIN_EXIT=$?
If MODE == risk_watch:  python risk_watch.py; MAIN_EXIT=$?

  • main.py (Wednesdays / catch-up) runs the 7-agent pipeline (Regime → Research → Earnings →
    Devil's Advocate → Position Review → Portfolio Manager → CRO) reading the committed
    research_dossier.json, pre-computes fractional qty, and writes pending_decisions.json
    with mode="rebalance".
  • risk_watch.py (all other trading days) evaluates the deterministic SELL-only trigger set
    (−25% stop from cost basis on live MCP prices; kill-switch health; cross-mode interlock)
    and writes pending_decisions.json with mode="risk_watch". It NEVER emits a BUY and NEVER
    calls an LLM. Most days its decision list is [] — that is the expected outcome.

ALWAYS PUSH HEALTH FIRST — before validating anything, regardless of how the pipeline exited.
system_health.json is the ONLY trigger for alert.yml. Both pipelines write it on their abort
paths AND on success; if you stop the routine WITHOUT pushing it, an ABORTED/FAILED day fires
NO alert and fails silently. (A mid-pipeline crash may leave the file stale — push whatever
exists so the last-known state + any partial agent_log reach the monitor.)
git config user.email 'ai-investor-bot@users.noreply.github.com'
git config user.name 'AI Investor Bot'
git add system_health.json agent_log.json
git diff --staged --quiet || (git commit -m 'chore: health' && (git push || (git pull --rebase && git push)))

CRASH GUARD — if the pipeline did not exit cleanly, STOP the routine here. Do NOT run STEP 4:
if [ "$MAIN_EXIT" -ne 0 ]; then
  echo "pipeline exited $MAIN_EXIT — crashed mid-run. Health pushed (alert will fire). Not executing."
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
    print("ERROR: pending_decisions.json was never written — the pipeline produced no plan.")
    sys.exit(1)
try:
    p = json.load(open('pending_decisions.json'))
except (OSError, json.JSONDecodeError) as e:
    print(f"ERROR: pending_decisions.json is unreadable ({e}) — treating as no plan.")
    sys.exit(1)
today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
if p.get('date') != today:
    print(f"ERROR: pending_decisions.json is dated {p.get('date')!r}, expected {today!r}")
    print("The pipeline exited early — today's data was not available.")
    sys.exit(1)
mode = p.get('mode', 'rebalance')
if mode == 'risk_watch':
    bad = [d for d in p['decisions'] if str(d.get('action','')).upper() != 'SELL']
    if bad:
        print(f"ERROR: risk_watch envelope contains NON-SELL decisions: {bad} — refusing to execute.")
        sys.exit(1)
print(f"Pipeline OK: mode={mode}, {len(p['decisions'])} decision(s) for {today}")
PY

If this check fails, STOP. Do not attempt order execution. (Health was already pushed above,
so the abort is visible to alert.yml.)

═══════════════════════════════════════════════
STEP 4 — Execute trades via Robinhood MCP  (both modes — same machinery)
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

GUARD 4 — Mode integrity (risk_watch only):
  If pending_decisions["mode"] == "risk_watch" and ANY decision has action ≠ "SELL",
  STOP — the SELL-only safety net must never place a BUY. (STEP 3 already validated
  this; this is defense in depth at the order boundary.)

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
# Stage ONLY these named data artifacts — NEVER a .py file (see STEP 3). If `git
# status` shows a modified .py, do not stage it and do not commit it; that is a
# code change that must go through review, not the routine.
git add pending_decisions.json last_rebalance.json system_health.json trades.csv decision_journal.json agent_log.json transactions.json mcp_portfolio.json portfolio_snapshot.json fundamentals_cache.json portfolio_peak.json forecasts.jsonl forecasts_scored.jsonl agent_scorecards.json decisions_ledger.jsonl decisions_scored.jsonl counterfactual.json
git commit -m 'chore: execution claim'
git push || (git pull --rebase && git push)

(last_rebalance.json is the durable once-per-ISO-week rebalance lock — it MUST reach main
with the claim, or a Thu/Fri catch-up could re-run a crashed Wednesday rebalance.)

If the push fails even after the rebase retry, STOP — do NOT place any orders.
Without a durable claim, a crash mid-execution re-opens the double-fill window.
Failing toward missed trades is correct; failing toward duplicate trades is not.
(The claim commit carries the full artifact set so the audit trail of what was
*intended* survives even if this attempt dies during order placement.)

Maintain a fills accumulator as you place orders — a dict you will write to fills.json:
  fills = {}   # ticker -> {"order_id": <str>, "price": <float or null>}

For each decision where action is BUY or SELL:

  1. QUANTITY — read decision["qty"] directly.
     This is the pre-computed fractional share count (e.g., 0.648382).
     DO NOT recompute it. DO NOT round it to a whole number.
     STALE-PRICE RE-QUOTE (P0-1): if decision["price_as_of"] is present and ≠ today,
     the qty was sized on a stale slice price — fetch the live quote via
     get_equity_quotes and recompute:
       round(decision["target_weight"] × mcp_portfolio["total_value"] ÷ live_price, 6)
     Fallback (only if decision["qty"] is missing or null): same live-quote computation.

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
BUT (rebalance mode only) only if the CRO made a genuine decision. If the CRO itself
failed due to API error (api_failed=True in system_health.json), do NOT stamp: the next
retry should get a fresh run when the API recovers. (risk_watch has no CRO — always stamp.)

python - <<'PY'
import json, os
from journal import mark_pending_executed, mark_transactions_live
p = json.load(open('pending_decisions.json'))
h = json.load(open('system_health.json')) if os.path.exists('system_health.json') else {}
fills = json.load(open('fills.json')) if os.path.exists('fills.json') else {}
cro_api_failed = h.get('checks', {}).get('agent_7_cro', {}).get('api_failed', False)
if p.get('mode', 'rebalance') == 'risk_watch' or p['decisions'] or not cro_api_failed:
    mark_pending_executed(p['run_id'])
    # Reconcile ALL logs (transactions.json, trades.csv, decision_journal.json)
    # against broker fills: ONLY accepted orders go live; rejections stay
    # dry_run=True / status="rejected" and are never published or fed back to
    # the agents as phantom positions. fills maps ticker -> {"order_id": str,
    # "price": float|None}. If decisions existed but fills is empty, the
    # reconciler records reconciliation=FAILED on system_health.json (paging).
    # NOTE: never call mark_transactions_live(run_id) with no fills arg — it
    # RAISES (a bare flip-all silently re-created the phantom-fill bug).
    mark_transactions_live(p['run_id'], fills)
    print(f"Execution stamped: run_id={p['run_id']} mode={p.get('mode')} ({len(fills)} fill(s) reconciled)")
else:
    print("Skipping execution stamp — CRO blocked trades due to API error, not a risk decision. Next retry will re-run.")
PY

═══════════════════════════════════════════════
STEP 5 — Commit artifacts  (both modes)
═══════════════════════════════════════════════

git config user.email 'ai-investor-bot@users.noreply.github.com'
git config user.name 'AI Investor Bot'
git add portfolio_snapshot.json system_health.json mcp_portfolio.json trades.csv decision_journal.json fundamentals_cache.json portfolio_peak.json pending_decisions.json last_rebalance.json agent_log.json transactions.json fills.json forecasts.jsonl forecasts_scored.jsonl agent_scorecards.json decisions_ledger.jsonl decisions_scored.jsonl counterfactual.json
git diff --staged --quiet || git commit -m 'chore: daily cycle'

# Push WITH a rebase retry. Orders are already LIVE — this push carries the executed_at stamp,
# the ISO-week rebalance lock, the reconciled logs, fills.json, and the post-trade snapshot that
# triggers publish.yml. A silent failure here means the website never updates AND today's durable
# fill record never reaches the remote while real orders exist. (STEP 4's claim push retries;
# STEP 5 must too.)
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

## What changed — Jul 9 2026 (Jul 8 rebalance post-mortem — dep-verify + no-source-edit rule — REQUIRES live-routine sync)

The Jul 8 2026 rebalance survived only because the cloud agent improvised past two
failures: a broken `pip install` (missing `anthropic`/`robin_stocks`) and a real code
bug (`load_dossier` UnboundLocalError), the latter fixed by editing and committing
`main.py` mid-run — an unreviewed change to the live order path. Both gaps are closed
in the prompt itself, not just the code:

1. **STEP 2 — dependency verification, not just `pip install -q`.** The base image
   ships Debian-managed packages (PyJWT, cryptography) a bare install can't uninstall,
   so the install silently aborts before reaching `anthropic`/`robin_stocks`. STEP 2 now
   verifies the imports actually resolve, retries with `--ignore-installed`, and — if
   still broken — **STOPs cleanly** (push nothing, place no orders; the next scheduled
   attempt retries). A skipped attempt is the designed failure direction; a half-installed
   pipeline is not.
2. **STEP 3 — hard rule: never edit, debug, or commit a `.py` source file**, under any
   circumstances, even to unblock a crash. The routine executes committed code; it does
   not author it. A pipeline crash is a **missed rebalance** — push `system_health.json`
   (below) so `alert.yml` pages the owner, then STOP. Thu/Fri catch-up + Friday's
   missed-week heartbeat cover the week; the owner fixes the bug through the normal
   `/code-review` + test gates (DEPLOYMENT §7.0), never via an unreviewed live-routine edit.
3. **STEP 4/5 `git add` lists** now carry an explicit comment: stage only the named data
   artifacts, never a `.py` file — if `git status` shows a modified source file, leave it
   unstaged.

(The underlying bug itself — the `main.py` shadowing import — and the sector-cap /
capital-dependency guard fixes that made Jul 8's orphaned BUYs impossible are code-only
changes; see CLAUDE.md's Jul 9 2026 changelog. This routine-prompt change is purely the
process guardrail: verify deps, never patch source live.)

## What changed — Jul 4 2026 (Phase 5: weekly cadence + risk_watch — REQUIRES live-routine sync)

The entire prompt gained MODE routing. Summary of the deltas vs the daily-cycle prompt:

1. **STEP 0** — the gate now returns **four** codes; `30` (PROCEED/RISK-WATCH) is new.
   Set a MODE variable and branch. `0` now also requires a **fresh `research_dossier.json`**
   and fires only on Wednesday (or Thu/Fri catch-up for a rebalance-less ISO week).
2. **STEP 3** — runs `main.py` (rebalance) **or** `risk_watch.py` (risk-watch). The
   validation heredoc gained the mode line + a **SELL-only assertion** for risk_watch
   envelopes.
3. **STEP 4** — new **GUARD 4** (mode integrity: a risk_watch envelope with a BUY is never
   executed); the claim commit and STEP 5 both `git add last_rebalance.json` (the durable
   once-per-ISO-week rebalance lock); the qty step gained the **P0-1 stale-price re-quote**
   (re-size via `get_equity_quotes` when `decision["price_as_of"] ≠ today`); the stamp
   heredoc always stamps risk_watch envelopes (no CRO in that mode).
4. **STEP 1** — unchanged structurally, but `avg_price` / `current_price` are now
   load-bearing (the −25% stop trigger); the note says so.

## What changed — Jun 16 2026 (observability + crash-handling hardening)

1. **STEP 3 — always push `system_health.json` before validating.** `alert.yml` fires
   ONLY on a `system_health.json` push; an ABORTED/FAILED day must not stop silently.
2. **STEP 3 — capture the pipeline exit code + CRASH GUARD** (no STEP 4 on a non-zero exit).
3. **STEP 3 — validation heredoc guards a missing/unreadable file.**
4. **STEP 5 — push with a rebase retry + `artifact_push` FAILED escalation.**

## What changed — Jun 12 2026 evening (remediation batch)

1. **STEP 4 — stamp-first execution claim**: `execution_started_at` is stamped, committed,
   and pushed BEFORE the first order is placed; a claim-push failure STOPS the attempt
   without placing orders (fail toward missed trades, never duplicates).
