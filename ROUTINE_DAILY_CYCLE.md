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
  "cash": <float>,
  "total_value": <float>,
  "positions": [
    {
      "symbol": "TICKER",
      "qty": <float>,
      "avg_price": <float>,
      "current_price": <float>,
      "market_value": <float>,
      "unrealized_pnl": <float>
    }
  ]
}

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

python main.py

This runs the 7-agent pipeline (Regime → Research → Earnings → Devil's Advocate →
Position Review → Portfolio Manager → Chief Risk Officer), pre-computes fractional qty
for each decision, writes pending_decisions.json, logs to trades.csv / decision_journal.json /
agent_log.json / transactions.json, and publishes a snapshot to Supabase.

After main.py completes, validate the output (heredoc avoids shell-quoting issues; uses ET date):
python - <<'PY'
import json, sys
from datetime import datetime
from zoneinfo import ZoneInfo
p = json.load(open('pending_decisions.json'))
today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
if p.get('date') != today:
    print(f"ERROR: pending_decisions.json is dated {p.get('date')!r}, expected {today!r}")
    print("main.py exited early — market snapshot for today was not available.")
    sys.exit(1)
h = json.load(open('system_health.json')) if __import__('os').path.exists('system_health.json') else {}
checks = h.get('checks', {})
agent_failures = [k for k,v in checks.items() if v.get('status') in ('FAILED','DEGRADED') and k.startswith('agent_')]
if agent_failures:
    print(f"WARNING: agent failures detected: {agent_failures}")
    print("analysis.py retries up to 3 attempts with 30s/60s backoff on 529 errors — these are transient Anthropic API overloads.")
print(f"Pipeline OK: {len(p['decisions'])} decision(s) for {today}")
PY

If this check fails (date mismatch), STOP. Do not attempt order execution.
Agent warnings (FAILED/DEGRADED) are informational — the pipeline handles them with retries.

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

Read decisions from pending_decisions["decisions"] (the nested array, not the root object).

TSLA HARD BLOCK: NEVER place any order for TSLA under any circumstances. Skip it entirely.

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

  3. PLACE ORDER:
     place_equity_order(
       account_number='YOUR_ACCOUNT_NUMBER',
       symbol=<ticker>,
       side='buy' or 'sell',
       quantity=<qty from step 1>,
       type='market',
       time_in_force='gfd'
     )

  4. LOG the result (ticker, qty, order id or error) to the session output.

Skip HOLD decisions entirely.
If pending_decisions["decisions"] is [], skip directly to the stamp step.

AFTER ALL ORDERS ARE PLACED (or if decisions was empty), stamp the execution:
python - <<'PY'
import json
from journal import mark_pending_executed
p = json.load(open('pending_decisions.json'))
mark_pending_executed(p['run_id'])
PY

═══════════════════════════════════════════════
STEP 5 — Commit artifacts
═══════════════════════════════════════════════

git config user.email 'ai-investor-bot@users.noreply.github.com'
git config user.name 'AI Investor Bot'
git add portfolio_snapshot.json system_health.json mcp_portfolio.json trades.csv decision_journal.json fundamentals_cache.json portfolio_peak.json pending_decisions.json agent_log.json transactions.json
git diff --staged --quiet || git commit -m 'chore: daily cycle [skip ci]'
git push || echo "WARNING: git push failed — trades executed but artifacts not committed to remote"
```

---

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

> ⚠️ **Partial-failure caveat (unchanged, pre-existing):** if an attempt places some orders but
> crashes before STEP 4 stamps `executed_at`, nothing is pushed, and the next attempt may re-run.
> GUARD 2 protects within an attempt; for cross-attempt partial fills, follow **Scenario B** in
> the `CLAUDE.md` Manual Execution Runbook (compare actual `get_equity_positions` to targets).
