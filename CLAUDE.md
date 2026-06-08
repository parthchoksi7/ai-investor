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

- **Agentic account number:** `994046696`
- **Account type:** Cash, individual, `agentic_allowed=true`
- **Starting capital:** $500
- All other accounts (`agentic_allowed=false`) are never touched by this system.

## Automated Execution

The daily cycle runs via a **Claude Code scheduled routine** (not GitHub Actions):

- **Routine ID:** `trig_01Avvj5aBf3sXbDqUB3g4rTm`
- **Schedule:** `45 13 * * 1-5` (9:45 AM EDT, Mon–Fri)
- **View/manage:** https://claude.ai/code/routines/trig_01Avvj5aBf3sXbDqUB3g4rTm
- **DST note:** Cron is UTC. In winter (EST = UTC-5), update to `45 14 * * 1-5` in November, back to `45 13 * * 1-5` in March.

The routine uses the **Robinhood Trading MCP connector** (connector UUID: `13b51fe0-3004-4fa1-ae70-f3535d95ab6f`) — no Robinhood credentials stored anywhere.

The routine runs the **full Python pipeline** (`main.py`) with `DRY_RUN=true` — this skips `robin_stocks` execution but runs all 7 agents and writes `pending_decisions.json`. The routine then reads that file and executes orders via the Robinhood MCP directly.

Portfolio data is injected via `mcp_portfolio.json` (written by the routine from MCP data), so `execute.py` never needs to call `robin_stocks` in the cloud.

`POLYGON_API_KEY` is embedded in the routine prompt (stored privately in Anthropic's systems). `ANTHROPIC_API_KEY` is expected to be auto-injected by Anthropic's cloud environment.

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

## Known Limitations

- Cloud quant scores are always 50 (no historical data). Agents work from LLM knowledge only.
- `market_data.py` makes ~90 API calls per run locally (one per ticker for 210-day history). Can be slow (~2–3 min) and may hit Polygon free-tier rate limits.
- DST: the cron doesn't auto-adjust. Update manually in November (`45 14 * * 1-5`) and March (`45 13 * * 1-5`).
