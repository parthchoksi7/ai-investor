# AI Investor

A fully automated daily equity trading system. Every weekday at 9:45 AM ET, a scheduled Claude Code routine runs a 7-agent investment pipeline, executes trades on a dedicated Robinhood account, and commits the trade log to GitHub. No human input required.

## How it works

```
8:00 AM ET  — GitHub Actions fetches 210-day market snapshot (Polygon API)
              Commits market_snapshot.json to repo
9:45 AM ET  — Anthropic cloud routine runs the full pipeline:
              1. Kill-switch check (abort if drawdown > 20%)
              2. Fetch portfolio from Robinhood MCP
              3. Pre-flight: abort if market_snapshot.json isn't today's
              4. Compute quant scores (momentum / quality / valuation / risk)
              5. Run 7-agent Claude pipeline
              6. Execute trades via Robinhood MCP
              7. Log to CSV + decision journal
              8. Publish portfolio snapshot to Supabase
              9. Write system_health.json → triggers GitHub Issue on failure
4:00 PM ET  — EOD snapshot routine records official closing value
```

## The 7-Agent Pipeline

| # | Agent | Model | Role |
|---|-------|-------|------|
| 1 | Market Regime Strategist | Sonnet | Portfolio risk level: Risk-On / Neutral / Risk-Off |
| 2 | Research Analyst | Haiku | Per-ticker: variant perception, catalysts |
| 3 | Earnings & Catalyst Analyst | Haiku | Per-ticker: 90-day events |
| 4 | Devil's Advocate | Haiku | Per-ticker: bear case |
| 5 | Position Review Analyst | Haiku | Per-holding: hold / reduce / exit |
| 6 | Portfolio Manager | Sonnet | Capital allocation, final trade list |
| 7 | Chief Risk Officer | Sonnet | Veto power over all trades |

Agents 2–5 use Anthropic prompt caching and run in parallel per ticker.

## Investment Rules

- Long-only, no shorts, options, or leverage
- Universe: US common stocks and ADRs
- 8–15 positions, max 10% per position, max 25% per sector
- Default action: HOLD — only trade when it improves expected value
- Kill switch: blocks all BUYs when drawdown exceeds 20% from peak

## Local Setup

### Prerequisites

- Python 3.11+
- Polygon.io API key (free tier works)
- Anthropic API key
- Robinhood account with TOTP MFA enabled

### Install

```bash
git clone https://github.com/parthchoksi7/ai-investor.git
cd ai-investor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Configure

Create a `.env` file (gitignored):

```env
ANTHROPIC_API_KEY=sk-ant-...
POLYGON_API_KEY=...
ROBINHOOD_USERNAME=...
ROBINHOOD_PASSWORD=...
ROBINHOOD_MFA_SECRET=...        # TOTP secret from authenticator app
ROBINHOOD_ACCOUNT_NUMBER=994046696
DRY_RUN=true                    # set to false to place real orders
```

### Run

```bash
source venv/bin/activate
python main.py
```

Runs the full pipeline. With `DRY_RUN=true` it logs decisions but places no orders.

## Running Tests

```bash
pip install pytest
pytest test_pipeline.py -v
```

Tests cover the quant engine (pure functions), health tracker, kill switch logic, and idempotency envelope — no API keys needed.

## Key Files

| File | Purpose |
|------|---------|
| `main.py` | Pipeline orchestrator — 9-step daily cycle |
| `market_data.py` | Polygon + yfinance: prices, 210-day OHLCV, fundamentals, news |
| `quant_engine.py` | Deterministic scoring: momentum / quality / valuation / risk |
| `analysis.py` | 7-agent Claude pipeline |
| `execute.py` | Robinhood order execution via `robin_stocks` |
| `journal.py` | Decision journal + 20% drawdown kill switch |
| `health.py` | Health tracker — writes `system_health.json` after every run |
| `publish.py` | Publishes portfolio snapshots to Supabase |
| `fetch_snapshot.py` | Run by GitHub Actions to pre-fetch market data |

### Data files (gitignored in dev, committed in cloud)

| File | Written by | Read by |
|------|-----------|---------|
| `market_snapshot.json` | GitHub Actions (8 AM) | Cloud routine (9:45 AM) |
| `mcp_portfolio.json` | Cloud routine | `execute.py` |
| `mcp_market_data.json` | Cloud routine | `market_data.py` (fallback) |
| `system_health.json` | `main.py` | `alert.yml` GitHub Action |
| `pending_decisions.json` | `main.py` | Cloud routine (idempotency) |
| `decision_journal.json` | `journal.py` | `main.py` |
| `fundamentals_cache.json` | `market_data.py` | `market_data.py` (weekly cache) |

## GitHub Actions

| Workflow | Schedule | Purpose |
|----------|----------|---------|
| `market_data.yml` | 8:00 AM ET weekdays | Fetch snapshot, commit `market_snapshot.json` |
| `health_check.yml` | 11:00 AM ET weekdays | Verify Supabase has today's portfolio snapshot |
| `alert.yml` | On `system_health.json` push | Open/close GitHub Issues on pipeline failure |
| `update_dst.yml` | Mar 15, Nov 8 | Auto-update cron times for EDT↔EST |
| `keepalive.yml` | Sundays | Re-enable workflows (GitHub disables after 60 days) |

## Cloud vs Local

The Anthropic cloud environment blocks all outbound HTTP except the Robinhood MCP connector. This means Polygon, yfinance, and Supabase are unreachable from the routine.

| | Local | Cloud (scheduled routine) |
|---|---|---|
| Portfolio data | `robin_stocks` | Robinhood MCP → `mcp_portfolio.json` |
| Market data | Polygon (210-day OHLCV) | `market_snapshot.json` committed by GitHub Actions |
| Quant scores | Full (real OHLCV) | Full — same data, pre-fetched by GH Actions |
| Fallback if snapshot missing | yfinance | Abort — do not run with stale data |

## Health Monitoring

After every run, `main.py` writes `system_health.json` with the status of each pipeline step (OK / DEGRADED / FAILED / ABORTED). Pushing this file triggers `alert.yml`, which:

- Opens a GitHub Issue (label: `health-alert`) when status is not OK
- Adds a comment to the existing issue on subsequent failures
- Auto-closes the issue when the pipeline recovers

Example statuses tracked: `portfolio`, `kill_switch`, `market_data`, `quant_scores`, `agent_1_regime` through `agent_7_cro`, `execution`, `supabase_publish`.

## Automated Deployment

The trading routine runs on Anthropic's cloud scheduler — no server required.

- **Daily trading:** Routine `trig_01Avvj5aBf3sXbDqUB3g4rTm` at 9:45 AM EDT
- **EOD snapshot:** Routine `trig_01GtedgrYMGHYCJVLLHXZTCq` at 4:00 PM EDT

DST note: GitHub Actions crons update automatically via `update_dst.yml`. The two Anthropic routine crons require manual updates each March and November (see CLAUDE.md).

## Manual Intervention

See **Manual Execution Runbook** in `CLAUDE.md` for step-by-step instructions covering:

- **Scenario A** — Routine failed before placing orders
- **Scenario B** — Routine failed after partial orders (double-execution prevention)
- **Scenario C** — Kill switch active (drawdown > 20%)
- **Scenario D** — Full re-run needed
