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

The daily cycle runs via a **Claude Code scheduled routine** (not GitHub Actions):

- **Routine ID:** `YOUR_ROUTINE_ID_DAILY`
- **Schedule:** `45 13 * * 1-5` (9:45 AM EDT, Mon–Fri)
- **View/manage:** https://claude.ai/code/routines/YOUR_ROUTINE_ID_DAILY
- **DST note:** Cron is UTC. In winter (EST = UTC-5), update to `45 14 * * 1-5` in November, back to `45 13 * * 1-5` in March.

The routine uses the **Robinhood Trading MCP connector** (connector UUID: `YOUR_MCP_CONNECTOR_UUID`) — no Robinhood credentials stored anywhere.

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

## Known Limitations

- `market_data.py` makes ~90 API calls per run (one per ticker for 210-day history). Can be slow (~2–3 min) and may hit Polygon free-tier rate limits.
- The scheduled routine uses `get_equity_quotes` via Robinhood MCP for market data (no Polygon key needed in cloud). The local `main.py` uses Polygon.
- DST: the cron doesn't auto-adjust. Update manually in November and March.
