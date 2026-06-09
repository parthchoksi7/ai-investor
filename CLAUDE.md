# AI Investor — Claude Context

## What This Is

A fully automated daily equity trading system. Every weekday at 9:45 AM ET, a scheduled Claude Code routine runs a 7-agent investment pipeline, executes trades on a dedicated Robinhood Agentic account, and commits the trade log to GitHub. No human input required.

## Architecture

```
Scheduled Routine (Anthropic Cloud, 9:45 AM ET weekdays)
    │
    ├── market_data.py     → Polygon API: 210-day OHLCV + fundamentals (weekly cached)
    ├── quant_engine.py    → Deterministic scoring: momentum / quality / valuation / risk
    ├── analysis.py        → 7-agent Claude pipeline (see below)
    ├── execute.py         → Robinhood MCP: place orders on agentic account only
    ├── journal.py         → Decision journal + kill switch (20% drawdown)
    └── main.py            → Orchestrates all of the above
```

## The 7-Agent Pipeline (analysis.py)

| # | Agent | Model | Scope |
|---|-------|-------|-------|
| 1 | Market Regime Strategist | Sonnet | Portfolio-level: Risk-On / Neutral / Risk-Off |
| 2 | Research Analyst | Haiku (cached) | Per-ticker: variant perception, catalysts |
| 3 | Earnings & Catalyst Analyst | Haiku (cached) | Per-ticker: 90-day events |
| 4 | Devil's Advocate | Haiku (cached) | Per-ticker: bear case, reject flag |
| 5 | Position Review Analyst | Haiku (cached) | Per-holding: hold/reduce/exit |
| 6 | Portfolio Manager | Sonnet | Capital allocation, final trade list |
| 7 | Chief Risk Officer | Sonnet | Veto power — can reject all trades or specific tickers |

Haiku runs for each of up to 20 candidates. Sonnet runs 3 times total. Prompt caching is applied to all Haiku system prompts.

## Key Files

| File | Purpose |
|------|---------|
| `main.py` | Entry point — 9-step orchestration with full health tracking |
| `market_data.py` | Polygon.io: prices, 210-day history, fundamentals, news |
| `quant_engine.py` | Pure Python scoring (no LLM) |
| `analysis.py` | 7-agent pipeline |
| `execute.py` | Robinhood order execution via `robin_stocks` |
| `journal.py` | `decision_journal.json` + kill switch |
| `health.py` | `HealthTracker` — records every pipeline step to `system_health.json` |
| `fetch_snapshot.py` | Run by GitHub Actions to pre-fetch and commit market data |
| `trades.csv` | Trade log (committed to GitHub after each run) |
| `decision_journal.json` | Full thesis + invalidation conditions per trade |
| `system_health.json` | Written every run; push triggers `alert.yml` |
| `fundamentals_cache.json` | Weekly fundamentals cache (avoid re-fetching daily) |
| `portfolio_peak.json` | Tracks portfolio peak for drawdown kill switch |

## Robinhood Account

- **Agentic account number:** `994046696`
- **Account type:** Cash, individual, `agentic_allowed=true`
- **Starting capital:** $500
- All other accounts (`agentic_allowed=false`) are never touched by this system.

## Automated Execution

Two scheduled routines run every weekday. Both use the **Robinhood Trading MCP connector** (UUID: `13b51fe0-3004-4fa1-ae70-f3535d95ab6f`) — no Robinhood credentials stored anywhere.

### Daily Trading Cycle

- **Routine ID:** `trig_01Avvj5aBf3sXbDqUB3g4rTm`
- **Schedule:** `45 13 * * 1-5` (9:45 AM EDT, Mon–Fri)
- **View/manage:** https://claude.ai/code/routines/trig_01Avvj5aBf3sXbDqUB3g4rTm
- **DST note:** In winter (EST = UTC-5), update to `45 14 * * 1-5` in November, back to `45 13 * * 1-5` in March.

The routine runs the **full Python pipeline** (`main.py`) with `DRY_RUN=false`, executes trades via the Robinhood MCP, then commits and pushes updated files to GitHub.

Portfolio data is injected via `mcp_portfolio.json` (written by the routine from MCP data), so `execute.py` never needs to call `robin_stocks` in the cloud.

`POLYGON_API_KEY` is embedded in the routine prompt (stored privately in Anthropic's systems). `ANTHROPIC_API_KEY` is expected to be auto-injected by Anthropic's cloud environment.

**After `main.py` completes**, the routine MUST commit and push the following files:
```
git config user.name "AI Investor Bot"
git config user.email "ai-investor-bot@users.noreply.github.com"
git add portfolio_snapshot.json system_health.json pending_decisions.json agent_log.json trades.csv decision_journal.json transactions.json
git commit -m "chore: daily cycle [skip ci]"
git push
```
The push of `portfolio_snapshot.json` triggers `publish.yml` in GitHub Actions, which runs `publish.py` with Supabase access (Supabase is blocked in the Anthropic cloud — 403).

### EOD Close Snapshot

- **Routine ID:** `trig_01GtedgrYMGHYCJVLLHXZTCq`
- **Schedule:** `0 20 * * 1-5` (4:00 PM EDT, Mon–Fri)
- **View/manage:** https://claude.ai/code/routines/trig_01GtedgrYMGHYCJVLLHXZTCq
- **DST note:** In winter (EST = UTC-5), update to `0 21 * * 1-5` in November, back to `0 20 * * 1-5` in March.

This routine does **not** run the trading pipeline and places **no orders**. It:
1. Fetches portfolio state from Robinhood MCP
2. Writes `mcp_portfolio.json`
3. Runs `python publish.py --close`
   - `publish.py` writes `portfolio_snapshot.json` (with `is_close: true`) before attempting Supabase
   - Supabase call fails (403 — blocked in cloud), but the file is written
4. Commits and pushes `portfolio_snapshot.json`:
   ```
   git config user.name "AI Investor Bot"
   git config user.email "ai-investor-bot@users.noreply.github.com"
   git add portfolio_snapshot.json
   git commit -m "chore: eod portfolio snapshot [skip ci]"
   git push
   ```
   This push triggers `publish.yml` in GitHub Actions, which runs `publish.py` with Supabase access and writes `close_value` to the `portfolio_snapshots` table.

The `--close` flag writes both `total_value` (latest) **and** `close_value` + `close_at` (the official 4:00 PM closing price). `close_value` is immutable once written — it is the authoritative daily close used for the performance chart on the website.

## Running Locally

```bash
source venv/bin/activate
python main.py
```

Requires a `.env` file (gitignored) with:
```
ANTHROPIC_API_KEY=...
POLYGON_API_KEY=...
ROBINHOOD_USERNAME=...
ROBINHOOD_PASSWORD=...
ROBINHOOD_MFA_SECRET=...    # TOTP secret from authenticator app
ROBINHOOD_ACCOUNT_NUMBER=994046696
DRY_RUN=true                # set false to actually execute
```

## Investment Rules (enforced by agents)

- **Long-only:** no shorts, options, leverage, crypto, derivatives
- **Universe:** publicly traded common stocks, ADRs
- **Holdings:** 8–15 positions
- **Max position:** 10% of portfolio
- **Max sector:** 25% of portfolio
- **Cash target:** 0–10%
- **Horizon:** 1–3 months primary, up to 6 months
- **Default action:** HOLD — only trade when it improves portfolio expected value
- **Kill switch:** blocks new BUYs when portfolio drawdown exceeds 20% from peak

## Trade Decision Format

The Portfolio Manager outputs `target_weight` (0.0–0.10) rather than share counts. `execute.py._compute_qty()` converts weight to shares at execution time.

```python
{
  "ticker": "NVDA",
  "action": "BUY",
  "target_weight": 0.08,
  "source_of_capital": "cash",
  "rationale": "..."
}
```

## `pending_decisions.json` — Idempotency Envelope

`main.py` wraps decisions in a metadata envelope to prevent double-execution on retry:

```json
{
  "run_id": "20260608-145656",
  "date": "2026-06-08",
  "generated_at": "2026-06-08T13:56:00Z",
  "executed_at": null,
  "decisions": [ ... ]
}
```

**Cloud routine MUST follow this protocol every run:**

1. **Read decisions** from `pending_decisions["decisions"]` (not the root — it's no longer a bare list).
2. **Verify freshness** — check `pending_decisions["date"] == today`. If it's yesterday's file, DO NOT execute. Stop and log a warning.
3. **Check idempotency** — if `pending_decisions["executed_at"]` is not `null`, this run was already executed. DO NOT place orders again. Stop immediately.
4. **Execute orders** via Robinhood MCP. Each decision includes a pre-computed `qty` (fractional shares) — **use it directly, do NOT round to whole shares**. Robinhood supports fractional orders. Skip a decision only if `qty == 0`; a qty of 0.648 is a valid, placeable order.
5. **Stamp execution** after all MCP orders are placed:
   ```
   python -c "from journal import mark_pending_executed; mark_pending_executed('RUN_ID')"
   ```
   Replace `RUN_ID` with the value from `pending_decisions["run_id"]`.

Steps 3 and 5 together prevent double-execution if the routine retries after a partial failure.

## Cloud Environment (Scheduled Routine)

Anthropic's cloud environment blocks all external HTTP except Robinhood MCP and the Anthropic API. Specifically:
- `api.polygon.io` → **blocked** (HTTP 403)
- `query1.finance.yahoo.com` (yfinance) → **blocked** (HTTP 403)

As a result, the cloud routine uses a different data path than local:

| | Local (`python main.py`) | Cloud (scheduled routine) |
|---|---|---|
| Portfolio | `robin_stocks` | Robinhood MCP |
| Market data | Polygon (210-day OHLCV) | Robinhood MCP `get_equity_quotes` → `mcp_market_data.json` |
| Fundamentals | Polygon financials | Not available (quant scores default to 50) |
| News | Polygon news API | Not available |
| Auth | `ANTHROPIC_API_KEY` | `CLAUDE_SESSION_INGRESS_TOKEN_FILE` (OAuth token, auto-injected) |

**Quant scores in cloud:** All scores default to 50 (neutral) because no historical data is available. The 7 LLM agents still run fully and can apply their training knowledge about each company.

**`mcp_market_data.json`:** Written by the routine from Robinhood MCP quotes. `get_market_snapshot()` loads it as a fallback when both Polygon and yfinance return empty.

**`mcp_portfolio.json`:** Written by the routine from Robinhood MCP. `get_portfolio_summary()` reads it instead of calling `robin_stocks` when the file exists.

## Website (parth-choksi.com)

The portfolio dashboard at `https://www.parth-choksi.com/work/ai-investor` reads from Supabase. It does **not** fetch live prices on demand.

### Data freshness
Portfolio value on the website updates at two points each weekday:
- **9:45 AM ET** — daily trading cycle → `main.py` → `portfolio_snapshot.json` committed → `publish.yml` (GH Actions) → Supabase
- **4:00 PM ET** — EOD routine → `publish.py --close` → `portfolio_snapshot.json` committed → `publish.yml` (GH Actions) → Supabase (`close_value`)

The refresh button (↻) on the page re-reads the latest Supabase snapshot. It does not call any external price API. The timestamp shown ("Data as of …") reflects when `publish.py` last ran, not when the user clicked refresh.

### Why no live prices
The Polygon free tier rate-limits at 5 API calls/minute. With 12+ positions, fetching all prices in a single web request would take 2+ minutes — not viable. The Robinhood MCP is only available in Claude Code sessions, not from Vercel serverless functions. Yahoo Finance now requires authentication (HTTP 401).

### Supabase tables written by publish.py
| Table | Written by | Contents |
|-------|-----------|----------|
| `portfolio_snapshots` | Both routines | `total_value`, `cumulative_return_pct`, `updated_at`, `close_value`, `close_at` |
| `positions` | Daily cycle only | Per-ticker: `ticker`, `quantity`, `avg_cost`, `current_price`, `unrealized_pct` |
| `trades` | Daily cycle only | Executed trade log |

## Health Monitoring & Alerting

Every run — including aborted runs — writes `system_health.json` to the repo. Pushing this file triggers `.github/workflows/alert.yml`, which opens or updates a GitHub Issue (label: `health-alert`) when any check is non-OK, and auto-closes it on recovery.

### Status levels

| Status | Meaning |
|--------|---------|
| `OK` | Step completed normally |
| `DEGRADED` | Step completed with reduced quality (e.g., shallow history, low confidence) |
| `FAILED` | Step failed but pipeline continued |
| `ABORTED` | Pipeline halted before running agents |

### Checks recorded each run

| Check key | Fails when |
|-----------|-----------|
| `portfolio` | Robinhood MCP returned zero total value |
| `kill_switch` | Drawdown > 20% (DEGRADED — still runs SELLs) |
| `market_data` | `data_date != today` OR `min_depth < 22 bars` → **ABORTED** |
| `quant_scores` | All scores are 50.0 (no real history reached the engine) |
| `agent_1_regime` | No output, or confidence < 25 |
| `agent_2_research` | All or some empty thesis responses |
| `agent_3_earnings` | All or most default responses (score=5, empty catalysts) |
| `agent_4_devils_advocate` | All or some empty bear_case responses |
| `agent_5_position_review` | Open positions exist but no reviews returned |
| `agent_6_portfolio_manager` | 0 trades proposed despite REDUCE/EXIT signals (data starvation) |
| `agent_7_cro` | No CRO output returned |
| `execution` | Trade execution raised an exception |
| `supabase_publish` | Supabase publish failed |

### Pre-flight abort

The pipeline aborts before running any agents if either condition is true:
1. `market_data["_data_date"] != today` — snapshot is from a prior day
2. `min(history_depths) < 22` — not enough bars for any quant calculation

This prevents the silent all-50 quant score failure mode where agents run but produce no trades because they have no quantitative signal. When aborted, `system_health.json` is written immediately and the alert fires.

The `_data_date` field is set by `market_data.py` to reflect the actual source date, not `date.today()`, so stale snapshots are detectable even if the file is present.

## Known Limitations

- Cloud quant scores are always 50 if `market_snapshot.json` isn't available (GitHub Actions job delayed or failed). The pre-flight abort prevents silent no-trade runs.
- `market_data.py` makes ~90 API calls per run locally (one per ticker for 210-day history). Can be slow (~2–3 min) and may hit Polygon free-tier rate limits.
- DST: GitHub Actions crons (`market_data.yml`, `health_check.yml`) are auto-updated by `.github/workflows/update_dst.yml` on March 1 and November 1. Both Anthropic routine crons require manual updates — see the EOD note and Daily Cycle note under Automated Execution above.
- Website shows no live intraday prices. Value is current as of the last routine run (9:45 AM or 4:00 PM ET).

## Testing

Unit tests cover the deterministic parts of the pipeline (no API keys required):

```bash
pip install pytest
pytest test_pipeline.py -v
```

| Test class | Module | What it covers |
|------------|--------|----------------|
| `TestHealthTracker` | `health.py` | Status aggregation, severity ordering, alert list, JSON persistence |
| `TestPctReturn` | `quant_engine.py` | Percentage return calculation edge cases |
| `TestMomentumScore` | `quant_engine.py` | Uptrend/downtrend scoring, DMA detection, clamping |
| `TestQualityScore` | `quant_engine.py` | Margin tier thresholds, partial fundamentals |
| `TestValuationScore` | `quant_engine.py` | PE / FCF yield / EV-EBITDA thresholds, negative PE guard |
| `TestRiskMetrics` | `quant_engine.py` | Volatility scoring, beta computation, insufficient data defaults |
| `TestScoreAllTickers` | `quant_engine.py` | Composite weight formula, missing SPY, fundamentals integration |
| `TestKillSwitches` | `journal.py` | 20% drawdown threshold, peak tracking, zero-value guard |
| `TestMarkPendingExecuted` | `journal.py` | Idempotency, run_id mismatch guard, missing file safety |
| `TestPreflightAbortConditions` | `main.py` logic | Stale date guard, min-depth guard, MCP 2-bar scenario |

## Manual Execution Runbook

Use this when the scheduled routine fails or you need to intervene.

### Scenario A — Routine failed before placing orders

`pending_decisions.json` exists, `executed_at` is `null`.

1. Check `pending_decisions["date"] == today`. If stale (yesterday's file), **stop** — wait for tomorrow's run.
2. Read `pending_decisions["decisions"]`. If `[]`, nothing to execute.
3. For each BUY or SELL decision (**never TSLA**):
   - Use `decision["qty"]` directly — it is pre-computed fractional shares. **Do NOT round to whole shares.**
   - If `qty` is missing (old file), fall back to: `round(target_weight × total_value / current_price, 6)` — `total_value` from `mcp_portfolio.json`
   - Place: `place_equity_order(account_number='994046696', symbol=ticker, side='buy'|'sell', quantity=qty, type='market', time_in_force='gfd')`
   - Skip HOLD decisions entirely. Skip a trade only if `qty == 0`. A qty of 0.648 is a valid fractional order — place it.
4. After **all** orders are placed, stamp execution:
   ```
   python -c "from journal import mark_pending_executed; mark_pending_executed('<run_id>')"
   ```
   Replace `<run_id>` with `pending_decisions["run_id"]`.

### Scenario B — Routine failed after partial orders

`executed_at` is already set (not `null`).

1. **Do NOT re-run** `pending_decisions.json` — it is marked executed. Placing orders again would double-fill.
2. Fetch actual positions via `get_equity_positions`. Compare to expected target weights.
3. Place any missing orders individually via `place_equity_order` (same params as Scenario A).
4. Do **not** call `mark_pending_executed` again.

### Scenario C — Kill switch is active (drawdown > 20%)

1. Confirm: open `portfolio_peak.json` and compare `peak` vs current `total_value`.
2. Only SELLs are allowed — the pipeline blocks all BUYs automatically.
3. To resume after recovery: edit `portfolio_peak.json` and set `"peak"` to current portfolio value. The next run will then allow BUYs again.

### Scenario D — Full re-run needed (e.g., pipeline error before pending_decisions.json was written)

1. Ensure `pending_decisions.json` either doesn't exist or has `executed_at = null` and `date = today`.
2. Run the pipeline: `DRY_RUN=true python main.py`
3. Follow Scenario A.
