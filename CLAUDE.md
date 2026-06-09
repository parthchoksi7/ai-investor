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
| `main.py` | Entry point — 7-step orchestration |
| `market_data.py` | Polygon.io: prices, 210-day history, fundamentals, news |
| `quant_engine.py` | Pure Python scoring (no LLM) |
| `analysis.py` | 7-agent pipeline |
| `execute.py` | Robinhood order execution via `robin_stocks` |
| `journal.py` | `decision_journal.json` + kill switch |
| `trades.csv` | Trade log (committed to GitHub after each run) |
| `decision_journal.json` | Full thesis + invalidation conditions per trade |
| `fundamentals_cache.json` | Weekly fundamentals cache (avoid re-fetching daily) |
| `portfolio_peak.json` | Tracks portfolio peak for drawdown kill switch |

## Robinhood Account

- **Agentic account number:** `YOUR_ACCOUNT_NUMBER`
- **Account type:** Cash, individual, `agentic_allowed=true`
- **Starting capital:** $500
- All other accounts (`agentic_allowed=false`) are never touched by this system.

## Automated Execution

Two scheduled routines run every weekday. Both use the **Robinhood Trading MCP connector** (UUID: `YOUR_MCP_CONNECTOR_UUID`) — no Robinhood credentials stored anywhere.

### Daily Trading Cycle

- **Routine ID:** `YOUR_ROUTINE_ID_DAILY`
- **Schedule:** `45 13 * * 1-5` (9:45 AM EDT, Mon–Fri)
- **View/manage:** https://claude.ai/code/routines/YOUR_ROUTINE_ID_DAILY
- **DST note:** In winter (EST = UTC-5), update to `45 14 * * 1-5` in November, back to `45 13 * * 1-5` in March.

The routine runs the **full Python pipeline** (`main.py`) with `DRY_RUN=false`, executes trades via the Robinhood MCP, and publishes a portfolio snapshot to Supabase.

Portfolio data is injected via `mcp_portfolio.json` (written by the routine from MCP data), so `execute.py` never needs to call `robin_stocks` in the cloud.

`POLYGON_API_KEY` is embedded in the routine prompt (stored privately in Anthropic's systems). `ANTHROPIC_API_KEY` is expected to be auto-injected by Anthropic's cloud environment.

### EOD Close Snapshot

- **Routine ID:** `YOUR_ROUTINE_ID_EOD`
- **Schedule:** `0 20 * * 1-5` (4:00 PM EDT, Mon–Fri)
- **View/manage:** https://claude.ai/code/routines/YOUR_ROUTINE_ID_EOD
- **DST note:** In winter (EST = UTC-5), update to `0 21 * * 1-5` in November, back to `0 20 * * 1-5` in March.

This routine does **not** run the trading pipeline and places **no orders**. It:
1. Fetches portfolio state from Robinhood MCP
2. Writes `mcp_portfolio.json`
3. Runs `python publish.py --close`

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
ROBINHOOD_ACCOUNT_NUMBER=YOUR_ACCOUNT_NUMBER
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
4. **Execute orders** via Robinhood MCP.
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
- **9:45 AM ET** — daily trading cycle (`publish.py` called by `main.py`)
- **4:00 PM ET** — EOD Close Snapshot routine (`publish.py --close`)

The refresh button (↻) on the page re-reads the latest Supabase snapshot. It does not call any external price API. The timestamp shown ("Data as of …") reflects when `publish.py` last ran, not when the user clicked refresh.

### Why no live prices
The Polygon free tier rate-limits at 5 API calls/minute. With 12+ positions, fetching all prices in a single web request would take 2+ minutes — not viable. The Robinhood MCP is only available in Claude Code sessions, not from Vercel serverless functions. Yahoo Finance now requires authentication (HTTP 401).

### Supabase tables written by publish.py
| Table | Written by | Contents |
|-------|-----------|----------|
| `portfolio_snapshots` | Both routines | `total_value`, `cumulative_return_pct`, `updated_at`, `close_value`, `close_at` |
| `positions` | Daily cycle only | Per-ticker: `ticker`, `quantity`, `avg_cost`, `current_price`, `unrealized_pct` |
| `trades` | Daily cycle only | Executed trade log |

## Known Limitations

- Cloud quant scores are always 50 (no historical data). Agents work from LLM knowledge only.
- `market_data.py` makes ~90 API calls per run locally (one per ticker for 210-day history). Can be slow (~2–3 min) and may hit Polygon free-tier rate limits.
- DST: GitHub Actions crons (`market_data.yml`, `health_check.yml`) are auto-updated by `.github/workflows/update_dst.yml` on March 1 and November 1. Both Anthropic routine crons require manual updates — see the EOD note and Daily Cycle note under Automated Execution above.
- Website shows no live intraday prices. Value is current as of the last routine run (9:45 AM or 4:00 PM ET).

## Manual Execution Runbook

Use this when the scheduled routine fails or you need to intervene.

### Scenario A — Routine failed before placing orders

`pending_decisions.json` exists, `executed_at` is `null`.

1. Check `pending_decisions["date"] == today`. If stale (yesterday's file), **stop** — wait for tomorrow's run.
2. Read `pending_decisions["decisions"]`. If `[]`, nothing to execute.
3. For each BUY or SELL decision (**never TSLA**):
   - Get current price: `get_equity_quotes` for the ticker
   - Compute qty: `round(target_weight × total_value / current_price, 6)` — `total_value` from `mcp_portfolio.json`
   - Place: `place_equity_order(account_number='YOUR_ACCOUNT_NUMBER', symbol=ticker, side='buy'|'sell', quantity=qty, type='market', time_in_force='gfd')`
   - Skip HOLD decisions entirely. Do not skip if qty < 1 (Robinhood supports fractional shares); only skip if qty == 0.
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
