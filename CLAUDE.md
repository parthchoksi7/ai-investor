# AI Investor — Claude Context

## What This Is

A fully automated daily equity trading system. Every weekday at 9:45 AM ET, a scheduled Claude Code routine runs a 7-agent investment pipeline, executes trades on a dedicated Robinhood Agentic account, and commits the trade log to GitHub. No human input required.

## Architecture

```
GitHub Actions (market_data.yml · 7:00 / 8:00 / 8:30 AM ET)
    ├── fetch_snapshot.py  → Polygon API: 210-day OHLCV + news
    ├── data_providers.py  → Provider enrichment (SEC EDGAR free, or FMP with key)
    │     SECProvider      →   gross_margin / operating_margin / debt_to_equity (all tickers)
    │     FMPProvider      →   + pe_ratio / fcf_yield / ev_ebitda + earnings calendar (key req.)
    └── market_snapshot.json committed to repo ← cloud routine reads this

Scheduled Routine (Anthropic Cloud, 9:45 AM ET weekdays)
    │
    ├── market_data.py     → reads committed snapshot (OHLCV + enriched fundamentals)
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
| `market_data.py` | Polygon.io: prices, 210-day history, fundamentals, news; calls `_enrich_with_provider()` |
| `data_providers.py` | Provider abstraction: `SECProvider` (EDGAR, free, no key) / `FMPProvider` (key req.) / `StubProvider` (tests); `get_provider()` factory |
| `quant_engine.py` | Pure Python scoring (no LLM) |
| `analysis.py` | 7-agent pipeline |
| `execute.py` | Robinhood order execution via `robin_stocks` |
| `journal.py` | `decision_journal.json` + kill switch |
| `health.py` | `HealthTracker` — records every pipeline step to `system_health.json` |
| `fetch_snapshot.py` | Run by GitHub Actions to pre-fetch and commit market data |
| `preflight_gate.py` | STEP 0 gate the routine runs first each attempt — PROCEED / SKIP-RETRY / SKIP-DONE (see below) |
| `ROUTINE_DAILY_CYCLE.md` | Canonical, version-controlled copy of the daily routine prompt (secrets redacted) |
| `ROUTINE_EOD_CLOSE.md` | Canonical, version-controlled copy of the EOD close routine prompt (secrets redacted) |
| `trades.csv` | Trade log (committed to GitHub after each run) |
| `decision_journal.json` | Full thesis + invalidation conditions per trade |
| `system_health.json` | Written every run; push triggers `alert.yml` |
| `fundamentals_cache.json` | Weekly fundamentals cache (avoid re-fetching daily) |
| `portfolio_peak.json` | Tracks portfolio peak for drawdown kill switch |
| `provider_cache.json` | Alternate-day 50/50 enrichment cache (EDGAR/FMP fundamentals); persisted via `actions/cache` |

## Robinhood Account

- **Agentic account number:** `YOUR_ACCOUNT_NUMBER`
- **Account type:** Cash, individual, `agentic_allowed=true`
- **Starting capital:** $500
- All other accounts (`agentic_allowed=false`) are never touched by this system.

## Automated Execution

Two scheduled routines run every weekday. Both use the **Robinhood Trading MCP connector** (UUID: `YOUR_MCP_CONNECTOR_UUID`) — no Robinhood credentials stored anywhere.

### Daily Trading Cycle

- **Routine ID:** `YOUR_ROUTINE_ID_DAILY`
- **Schedule:** `45 13,14,15,16 * * 1-5` (9:45 / 10:45 / 11:45 / 12:45 AM EDT, Mon–Fri) — **initial attempt + 3 hourly retries**.
- **View/manage:** https://claude.ai/code/routines/YOUR_ROUTINE_ID_DAILY
- **DST note:** In winter (EST = UTC-5), update to `45 14,15,16,17 * * 1-5` in November, back to `45 13,14,15,16 * * 1-5` in March.

#### Pre-flight gate (run FIRST, every attempt) — `preflight_gate.py`

The routine depends on a fresh `market_snapshot.json` from the `market_data.yml` GitHub Actions job. GitHub's scheduled crons can be delayed by hours or skipped, so the routine fires up to **4 times** across the morning and each attempt must gate itself. Running the pipeline against stale data does nothing useful (it just aborts at preflight and wastes tokens), so the routine **must not** run `main.py` unless the gate says PROCEED.

Protocol on **every** attempt:

```bash
# OPERATE ON main first — the worktree may start on a claude/* branch. A bare
# git push targets the CURRENT branch, so if you don't switch to main the gate
# reads a stale pending_decisions.json (→ retry re-runs the pipeline = double-fill)
# and system_health.json never reaches main (→ alert.yml fires nothing).
git fetch origin main
git checkout -B main origin/main   # canonical pending_decisions.json + pushes land on main
python preflight_gate.py     # decide whether to run
case $? in
  0)  : run main.py + execute (continue with the normal protocol below) ;;
  10) echo "stale data — skip this attempt; the next cron (+60 min) will retry"; exit 0 ;;
  20) echo "already executed today — skip"; exit 0 ;;
esac
```

- **Exit 0 (PROCEED):** fresh snapshot dated today (≥22 history bars) AND today's pipeline has not executed yet → run the full pipeline.
- **Exit 10 (SKIP/RETRY):** `market_snapshot.json` is missing or not dated today → **do not run**. Stop cleanly; the next scheduled attempt re-checks. If all 4 attempts see stale data, the day is intentionally skipped (no trades — correct behavior).
- **Exit 20 (SKIP/DONE):** `pending_decisions.json` shows today already executed, **or** today's `execution_started_at` is set without `executed_at` (a prior attempt crashed mid-execution — orders may exist; recover via Scenario B) → **do not run again**.

This is why the schedule has four fire times rather than one: the gate + the existing `pending_decisions` idempotency envelope guarantee the pipeline runs **at most once per day**, on the first attempt that sees fresh data. On Jun 9 the market_data job didn't land until 12:14 PM ET — the 12:45 retry would have caught it.

When the gate returns 0, the routine runs the **full Python pipeline** (`main.py`) with `DRY_RUN=false`, executes trades via the Robinhood MCP, then commits and pushes updated files to GitHub.

> 📄 The full, paste-ready routine prompt (with secrets redacted) is version-controlled at [`ROUTINE_DAILY_CYCLE.md`](ROUTINE_DAILY_CYCLE.md). Keep it in sync whenever you change the live routine. STEP 0 (gate) and the 4-fire schedule live there.

Portfolio data is injected via `mcp_portfolio.json` (written by the routine from MCP data), so `execute.py` never needs to call `robin_stocks` in the cloud.

`POLYGON_API_KEY` is embedded in the routine prompt (stored privately in Anthropic's systems). `ANTHROPIC_API_KEY` is expected to be auto-injected by Anthropic's cloud environment.

**After `main.py` completes**, the routine MUST commit and push the following files:
```
git config user.name "AI Investor Bot"
git config user.email "ai-investor-bot@users.noreply.github.com"
git add portfolio_snapshot.json system_health.json pending_decisions.json agent_log.json trades.csv decision_journal.json transactions.json forecasts.jsonl forecasts_scored.jsonl agent_scorecards.json
git commit -m "chore: daily cycle"
git push
```
The push of `portfolio_snapshot.json` triggers `publish.yml` in GitHub Actions, which runs `publish.py` with Supabase access (Supabase is blocked in the Anthropic cloud — 403).

### EOD Close Snapshot

- **Routine ID:** `YOUR_ROUTINE_ID_EOD`
- **Schedule:** `0 20 * * 1-5` (4:00 PM EDT, Mon–Fri)
- **View/manage:** https://claude.ai/code/routines/YOUR_ROUTINE_ID_EOD
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
   git add portfolio_snapshot.json mcp_portfolio.json
   git commit -m "chore: eod portfolio snapshot"
   git push
   ```
   This push triggers `publish.yml` in GitHub Actions, which runs `publish.py` with Supabase access and writes `close_value` to the `portfolio_snapshots` table.

The `--close` flag writes both `total_value` (latest) **and** `close_value` + `close_at` (the official 4:00 PM closing price). `close_value` is immutable once written — it is the authoritative daily close used for the performance chart on the website.

> 📄 The full, paste-ready EOD routine prompt (with secrets redacted) is version-controlled at [`ROUTINE_EOD_CLOSE.md`](ROUTINE_EOD_CLOSE.md). Keep it in sync whenever you change the live routine.
>
> ⚠️ **Two non-obvious requirements (both were live bugs, fixed Jun 12 2026):** STEP 4 **must** `git add portfolio_snapshot.json` (NOT just `mcp_portfolio.json`) and the commit message **must NOT** contain `[skip ci]`. `publish.yml` triggers *only* on a `portfolio_snapshot.json` push and is suppressed by `[skip ci]`; either mistake means `close_value` silently never reaches Supabase (the symptom: daily-close chart points appear only after a manual workflow dispatch).

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
  "execution_started_at": null,
  "executed_at": null,
  "decisions": [ ... ]
}
```

**Cloud routine MUST follow this protocol every run:**

1. **Read decisions** from `pending_decisions["decisions"]` (not the root — it's no longer a bare list).
2. **Verify freshness** — check `pending_decisions["date"] == today`. If it's yesterday's file, DO NOT execute. Stop and log a warning.
3. **Check idempotency** — if `pending_decisions["executed_at"]` is not `null`, this run was already executed. DO NOT place orders again. Stop immediately.
4. **Check the execution claim** — if `pending_decisions["execution_started_at"]` is not `null`, a prior attempt started placing orders and crashed before stamping `executed_at`. Orders may have been placed. DO NOT re-run — recover via **Scenario B** in the Manual Execution Runbook.
5. **Claim the run** (skip when `decisions` is empty): call `journal.mark_execution_started(run_id)`, then commit and push the full artifact set BEFORE placing the first order. If the push fails after one `git pull --rebase` retry, STOP without placing orders — without a durable claim, a crash mid-execution re-opens the cross-attempt double-fill window.
6. **Execute orders** via Robinhood MCP. Each decision includes a pre-computed `qty` (fractional shares) — **use it directly, do NOT round to whole shares**. Robinhood supports fractional orders. Skip a decision only if `qty == 0`; a qty of 0.648 is a valid, placeable order.
7. **Stamp execution** after all MCP orders are placed:
   ```
   python -c "from journal import mark_pending_executed; mark_pending_executed('RUN_ID')"
   ```
   Replace `RUN_ID` with the value from `pending_decisions["run_id"]`.

Steps 3–5 and 7 together guarantee the failure direction: a crash at any point yields **missed trades** (recoverable via Scenario B), never **duplicate trades**. Old envelopes without `execution_started_at` are read with `.get()` — a missing field behaves as `null`.

## Cloud Environment (Scheduled Routine)

Anthropic's cloud environment blocks all external HTTP except Robinhood MCP and the Anthropic API. Specifically:
- `api.polygon.io` → **blocked** (HTTP 403)
- `query1.finance.yahoo.com` (yfinance) → **blocked** (HTTP 403)

As a result, the cloud routine sources market data differently than local:

| | Local (`python main.py`) | Cloud (scheduled routine) |
|---|---|---|
| Portfolio | `robin_stocks` | Robinhood MCP → `mcp_portfolio.json` |
| Market data | Polygon (210-day OHLCV, live) | **`market_snapshot.json`** — full Polygon 210-day OHLCV, fetched & committed by `market_data.yml` (GH Actions), git-pulled by the routine |
| Fundamentals | Polygon financials | Whatever `market_snapshot.json` carries |
| News | Polygon news API | Whatever `market_snapshot.json` carries |
| Auth | `ANTHROPIC_API_KEY` | `CLAUDE_SESSION_INGRESS_TOKEN_FILE` (OAuth token, auto-injected) |

**Quant scores in cloud (normal path):** the committed `market_snapshot.json` contains the full 210-day history, so quant scores are **real**, identical to local. The `preflight_gate.py` STEP 0 check requires this snapshot dated today with ≥22 bars or it SKIP/RETRYs — so the pipeline only runs against real data. (`main.py` independently aborts if `_data_date != today` or `min_depth < 22`.)

**Degraded fallback (`mcp_market_data.json`):** if `market_snapshot.json` is missing, `get_market_snapshot()` falls back to MCP quotes written to `mcp_market_data.json`. This carries only ~2 history bars, so quant scores would default to 50 — but the preflight gate + the main.py abort prevent the pipeline from actually running in this state. It exists as a last-resort data source, not the normal path. The 7 LLM agents apply training knowledge regardless.

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
| `decision_validation` | Guardrails rejected/clamped/skipped any decision (DEGRADED) — see `guardrails.py` |
| `execution` | Any order rejected by the broker (no order id returned) → DEGRADED (partial fills) or FAILED (none filled), with per-ticker `failed_orders` detail; also any execution exception |
| `reconciliation` | Decisions existed but zero fills reconciled live (FAILED — orders may have been placed with fills.json missing/empty) or partial (DEGRADED). Written by `mark_transactions_live` after `main.py`, via `health.append_check` |
| `supabase_publish` | Supabase publish failed |

### Pre-flight abort

The pipeline aborts before running any agents if either condition is true:
1. `market_data["_data_date"] != today` — snapshot is from a prior day
2. `min(history_depths) < 22` — not enough bars for any quant calculation

This prevents the silent all-50 quant score failure mode where agents run but produce no trades because they have no quantitative signal. When aborted, `system_health.json` is written immediately and the alert fires.

The `_data_date` field is set by `market_data.py` to reflect the actual source date, not `date.today()`, so stale snapshots are detectable even if the file is present.

## Changelog — Jun 17 2026 (post-run gap audit: regime publish + alerting + PM auditability)

A critical multi-persona review of the Jun 17 run (which itself completed cleanly,
0 trades, RISK_ON). The run output looked healthy but the audit found the pipeline
was publishing wrong data and firing no alerts. P0 data + P1 observability; **no P0
execution-path code changed** (the touched `main.py` lines are arg-passing + health
checks, not order/qty/idempotency). Newest first.

| Change | Why it mattered |
|--------|-----------------|
| `fix(publish)` **live regime is now published** — `publish_to_supabase(regime=…)`; resolution is **explicit arg → today's `agent_log` → snapshot file (last resort)**, with an ET date guard so a prior-day agent_log is treated as stale too. `main.py` passes the live pipeline regime at both call-sites. | The published regime was read from the *previous* day's `portfolio_snapshot.json` first; any non-empty string is truthy, so the agent_log fallback never ran. The Jun 17 RISK_ON run was published to the live dashboard as **NEUTRAL** (the prior EOD value). Live data corruption, not just an internal log. |
| `fix(health)` **expected Supabase egress 403 → OK, not FAILED** — new `_record_supabase_health()` records the `Host not in allowlist`/egress block as OK ("deferred to GitHub Actions"); a genuine publish error (bad key/schema) is still FAILED. | `overall_status=FAILED` on **every** clean cloud run made the health signal pure noise AND blocked `alert.yml` from ever auto-closing a recovered issue (status never returned to OK). `health_check.yml` still verifies the row actually landed downstream. |
| `feat(analysis)` **PM parse-failure is distinguished from a deliberate no-trade** — `_safe_call(return_meta=True)` exposes `parsed_ok`; `agent_6` health → DEGRADED on a parse-failure `[]`; raw PM response logged to `agent_log.json` (`portfolio_manager_raw`). | `_safe_call` returned `[]` both when the PM legitimately held and when its JSON failed to parse — a mangled response masqueraded as "no trades" with no signal and no logged reasoning (unauditable no-trade day). Fails safe (no bad trades) but was a silent alpha/availability hole. |
| `feat(main)` **`cash_discipline` health signal** — DEGRADED when cash > `CASH_DISCIPLINE_PCT` (15%) AND the run places no BUYs (pure `cash_discipline_status()` helper). Observability only; never forces a trade. | The 0–10% cash target is enforced ONLY in the PM prompt and the LLM ignored it (Jun 17: 33.5% cash idle in RISK_ON, 0 trades). Auto-deploying would churn a CA top-bracket taxable account against the turnover/wash-sale guards, so we surface for human review instead of forcing buys. |
| `fix(gitignore)` **`fills.json` un-ignored**; `alert.yml` gains `workflow_dispatch`. | STEP 4 `git add fills.json` was silently ignored, so the broker-fill audit trail never reached the remote (a crash after STEP 4 couldn't be reconciled from it). `workflow_dispatch` lets the unverified `contents: read` alert fix be tested without a `main` push. |

> **QA:** full `pytest` green (**415**, +14: `TestSupabaseHealthClassification`, `TestSafeCallMeta`, `TestPmParseFailureSurfaced`, `TestCashDisciplineStatus`, `TestPublishRegimePriority`). Workflow YAML parses; DEPLOYMENT §3.1–3.7 regression scripts pass. Expert `/code-review high` run pre-commit per §7.0.2 — no correctness findings (changes are observability/arg-passing, not execution-path, so `high` not `ultra`). **Dry-run (§3.8 / §7.1) intentionally SKIPPED** — Jun 17 is a trading day and today's cycle already executed; a real `main.py` dry-run would overwrite `pending_decisions.json` and risk a double-fill (DEPLOYMENT §7.1 trading-day warning). `pending_decisions.json` was NOT staged.
>
> ### ✅ P0 (operational) FIXED in the routine docs — the routine now operates on `main`
>
> `gh run list` showed the daily routine landing on auto-generated Claude worktree branches (`claude/festive-hopper-p3rmxg` on 6/16, `claude/hopeful-brown-udsl1g` on 6/17), not `main`. **Confirmed live double-execution vector:** on 6/17 the 9:45 run executed on a branch (its `executed_at` never reached `main`), so the 12:45 retry pulled `main`, saw "not executed today," and **re-ran the entire pipeline** (`run 20260617-164755`). Both were 0-trade so harmless, but with real trades that is a DOUBLE-FILL — the `pending_decisions.json` idempotency envelope is defeated when it doesn't live on `main`. Also: `alert.yml` (main-scoped) fired nothing for the branch runs, and the stale-regime publish still reached the live site via `publish.yml` (no branch filter).
>
> **Fix (this batch):** `ROUTINE_DAILY_CYCLE.md` STEP 0 and `ROUTINE_EOD_CLOSE.md` STEP 0 now begin with `git fetch origin main && git checkout -B main origin/main`, so the routine operates ON `main` — the gate reads the canonical `pending_decisions.json` and every existing bare `git push` lands on `main`. Minimal change (only the checkout is added); all STEP 4/5 claim/push semantics are preserved verbatim. **⚠️ Requires live-routine sync** (both routines) via the routines UI before it takes effect — the code/doc change alone does not alter the live prompts.

## Changelog — Jun 16 2026 (NaN publish fix + canary auth + routine observability)

Triggered by the Jun 16 run, which surfaced a genuine (non-403) Supabase publish
break and two latent reliability gaps. P2-class (data layer); no P0 execution-path
code changed. **The daily routine prompt changed — re-sync the live routine** (see
the live-routine note below). Newest first.

| Change | Why it mattered |
|--------|-----------------|
| `fix(quant)` **`compute_risk_metrics` rejects degenerate price series** — a non-finite or ≤0 close now returns `volatility_available: False` (dropped from the honest composite) instead of producing a NaN annualized vol; belt-and-suspenders `math.isfinite(vol)` guard. | A NaN close in the snapshot (TXN/TJX/CAT showed `vol=nan%`) propagated into `ann_vol=NaN`, which broke the Supabase publish (`Out of range float values are not JSON compliant`) — the website went stale and **every** subsequent publish (incl. the GH Actions path) would keep failing until the NaN was stopped at the source. |
| `fix(publish)` **`_sanitize()` NaN/Inf → None scrub** at all four serialization boundaries (snapshot-file write + snapshot/positions/trades/quant upserts); snapshot write uses `allow_nan=False` to fail loud. | Last-line-of-defense so *any* future NaN/Inf (from any upstream calc) degrades a field to `null` rather than breaking the whole publish. |
| `fix(gate)` **canary authenticates via the OAuth token file** (`auth_token=`) like `analysis.py:_get_client()`, not a bare `anthropic.Anthropic()`. | In the cloud (no `ANTHROPIC_API_KEY`, OAuth token injected) the old canary failed auth, fell through to the non-529 "proceed" branch, and **silently disabled 529-overload protection on the exact live path it was built to guard**. |
| `docs(routine)` **`ROUTINE_DAILY_CYCLE.md` STEP 3/STEP 5 observability hardening** — always push `system_health.json` after `main.py` (fires `alert.yml` on aborted/failed days); capture `main.py` exit code + crash-guard (no STEP 4 on non-zero exit); guard a missing/unreadable `pending_decisions.json`; STEP 5 push gets a rebase retry + `artifact_push` FAILED escalation. | `alert.yml` fires only on a `system_health.json` push, but the routine reached the STEP 5 push only on the happy path — an aborted/failed day stopped earlier and **fired no alert** (silent no-trade day). `run_daily_cycle()` has no top-level try/except, so a mid-pipeline crash also left the routine with no exit-code handling. STEP 5's `git push || echo WARNING` could lose the executed_at stamp/fills/snapshot silently while orders were live. |

> **QA:** full `pytest` green (**401**, +19: `TestSanitizeNaN`, `TestCanaryAuth`, NaN-guard cases in `TestRiskMetrics`). Workflow YAML parses. Expert `/code-review` run pre-commit per the §7.0 gate. **Dry-run skipped** (DEPLOYMENT §7.1 — deployed on a trading day; a real `main.py` dry-run would overwrite `pending_decisions.json` and risk a double-fill) — validated via the suite + targeted integration checks.
>
> **Live routine sync required:** the daily routine (`YOUR_ROUTINE_ID_DAILY`) STEP 3/STEP 5 changed — paste the updated `ROUTINE_DAILY_CYCLE.md` block into the routines UI (substituting the real account number + secrets). Until synced, the live routine keeps the old STEP 3/5; the code fixes (quant/publish/gate) take effect immediately on the next `git pull`. EOD routine unchanged.
>
> **Known follow-up (not in this batch):** STEP 4 still records an order as filled on broker *acceptance* without polling `get_equity_orders` to confirm the fill / capture the real fill price — its own PR (#5).

## Changelog — Jun 14 2026 (P1 backtest harness + cost_model spine + QA hardening)

FINAL_PLAN P1 — the validation foundation. All **offline tooling** (not in the live
trade path). PR #10.

| Change | Why |
|--------|-----|
| `feat(cost_model)` **`cost_model.py`** — shared spine: CA ST/LT tax + IRS-style netting (`tax_on_realized`), `round_trip_cost`, `net_edge`. `performance.py` delegates tax to it (single source of truth). | The backtest (#3) and the future net-edge gate (#6) must compute identical economics; centralizing prevents drift. |
| `feat(backtest)` **`backtest/`** — event-loop harness reusing `quant_engine.score_all_tickers` unchanged, next-open fills (no look-ahead), `cost_model` costs, momentum/inverse-vol strategy, after-tax-vs-SPY report. `python -m backtest`. **No LLM in the backtest.** | First honest verdict: the quant-only strategy returns **−0.03% vs SPY +8.77%** (no edge yet) — surfaced instead of shipped on faith. |
| `fix(test)` **timezone-flaky test** — `TestPublishSpyDataSource` used local `date.today()` vs the function's ET, failing ~3 hrs/day. Now ET. | Found in QA; would have intermittently red-flagged CI / blocked deploys in the ET/PT straddle window. |
| `docs(backtest)` **survivorship-bias caveat** in the report. | The universe is current survivors only (no delisted names) → upward bias; must be disclosed. |

> **QA:** two independent review passes (all thread personas), edge-case battery, +25 tests over the batch → **261 passing**. Subagent `/code-review` was rate-limited; a thorough manual review covered look-ahead, leverage, tax netting, guard boundaries, degenerate inputs — no actionable correctness findings. **PM insight:** monthly rebalance (+$4,185 realized) vastly beats daily (−$242) — churn is value-destructive.

## Changelog — Jun 14 2026 (paper-shadow + after-tax + turnover discipline)

Edge-upgrade batch P0/P0.5 from the FINAL_PLAN roadmap. All changes are
code/data; no live routine prompt change required. The turnover guards run in the
`main.py` trade path (after the sector cap, inside the `decision_validation`
health check); the after-tax scorecard and 100× columns are reporting/logging
only. Newest first.

| Change | Why it mattered |
|--------|-----------------|
| `feat(perf)` **after-tax scorecard** (`performance.py`) — realized gain and after-tax realized gain tracked **separately** via FIFO lot-matching; CA top-bracket rates (ST ≈54%, LT ≈37.1%); SELLs with no in-log basis reported "uncovered" not guessed; vs SPY-buy-and-hold; `not_significant` flag < 60 days. | The pre-mortem's #1 unaddressed risk: we measured **pre-tax** return. For a CA top-bracket taxable account every churned gain is ~54% short-term — the only honest metric is after-tax-vs-hold-SPY. |
| `feat(guardrails)` **turnover/tax discipline** — `enforce_min_holding_period` (block SELL of a name bought < 5 trading days ago; risk exits exempt) + `enforce_wash_sale_reentry` (block BUY of a name sold < 30 calendar days ago). Wired in `main.py`, folded into `decision_validation`. | Weekly momentum rotation is tax-suicidal at ~54% ST. These cut the documented churn; `enforce_wash_sale_reentry` hardens the soft 10-day `recently_exited` warning into a 30-day control. |
| `feat(execute)` **paper-shadow 100× columns** on `trades.csv` (`qty_100x`, `total_value_100x`, `portfolio_value_100x`) — `SHADOW_MULTIPLIER`/`_scaled`; old rows backfilled; reconciliation keeps `total_value_100x` synced to the fill. | Models a $50,000 book at the same prices for a usable performance lens. **Caveat:** linear projection (zero market impact), not proof of scale — a real paper account is roadmap P4. |
| `docs(process)` **`RELEASE_NOTES.md`** + DEPLOYMENT.md **§7.0 pre-deploy gates** (update release notes; expert `/code-review` before commit) + CLAUDE.md "Deployment gates"; `TestReleaseNotes` guards the `[Unreleased]` section. | No user-facing release log existed, and deploys shipped without a mandated expert review step. |
| `docs(plan)` **`SOLUTION_PLANS.md`** + **`FINAL_PLAN.md`** — expert-panel designs for #1/#2/#3/#5/#6/#9, phased roadmap, CA tax recalibration, Shreyas-Doshi pre-mortem. | Decision-ready plan for the edge upgrades; spine principle: backtest deterministic layers, forward-test the LLM. |

> **Pre-deploy code review (§7.0.2) fixes folded in:** (1) `realized_summary` nets ST/LT gains/losses per IRS ordering (was taxing gains in full while crediting losses fully against after-tax); (2) turnover guards run BEFORE the sector cap in `main.py` so the cap sees the executable SELL set (a dropped SELL could otherwise let a same-sector BUY breach 25%); (3) `validate_decisions` + both turnover guards share a single `transactions.json` read; (4) `_last_live_*_date` DRY'd, min-holding skips the read under kill switch.
>
> **QA:** full `pytest` green (**236**, +31 new: `TestPaperShadowColumns`, `TestRealizedLots`, `TestRealizedSummary`, `TestAfterTaxScorecard`, `TestMinHoldingPeriod`, `TestWashSaleReentry`, `TestReleaseNotes`). Expert `/code-review` run pre-commit per the new §7.0 gate. A real `DRY_RUN main.py` was **not** run (the snapshot is from 6/12 → preflight-abort, and running it would overwrite `pending_decisions.json`) — validated via the test suite + targeted integration checks instead.

## Changelog — Jun 13 2026 (IMPROVEMENTS_SPEC evaluation batch)

Critical evaluation + selective implementation of `IMPROVEMENTS_SPEC.md` (a phased work order written from a partial read of the repo). Three of its phases were rejected on inspection; six were implemented. No live routine prompt change is required — all changes are code/data, and the cloud routine's STEP 4/5 protocol is unchanged. Newest first.

| Change | Phase | Why it mattered |
|--------|-------|-----------------|
| `feat(guardrails)` 25% **sector cap** in code (`enforce_sector_limits` + static `SECTOR_MAP`), wired in `main.py` after `validate_decisions`, folded into the `decision_validation` health check. | 0.2 | The sector limit lived **only** in the PM prompt — an LLM is not a control. Three 10% BUYs into one sector would size 30% concentration. SELLs applied first so a same-sector exit frees budget; existing holdings count. |
| `feat(journal)` **`close_position`** populates `actual_return` / `thesis_correct` / `exits` on the matching open BUY when a position is sold; wired into `main.py`'s executed-SELL loop. | 1 | The feedback loop was dead — entries were created with `actual_return=None` and nothing ever populated them, so the system could not tell a thesis that worked from one that blew up. Cloud caveat (speculative close under DRY_RUN, reconciliation won't re-open a wrongly-closed BUY) is documented in the function. |
| `feat(memory)` `get_ticker_history` + `recently_exited` (journal) fed into the **Research Analyst** (prior outcomes) and **Portfolio Manager** (re-entry warning). | 2 | Only Position Review saw prior theses, and only for current holdings. The thesis-builders for new/re-entered names got nothing → the "sold AAPL at \$292, rebuy at \$291" churn. |
| `fix(quant)` **honest composite** — each sub-score carries a `*_available` flag; `score_all_tickers` weights only real factors and renormalizes; `factors_used` recorded; `_fmt_scores` shows `N/A` not a fake 50. | 3.1 | All 100 fundamentals are `None` on free-tier Polygon, so quality/valuation were a constant 50 for every ticker — a "4-factor" composite that was really momentum+volatility. **No ranking/trade impact** (the 50s were constant, so they never affected ordering); this is a pure honesty fix. |
| `feat(cro)` **real correlation data** — `quant_engine.compute_return_correlations` (pairwise 120d return corr) + sector concentration injected into the CRO `user_msg`. | 4 | The CRO prompt asks it to assess "correlation risk" but was fed only per-ticker weight/vol/beta — its correlation judgment was fabricated. Block is omitted entirely when no matrix can be computed (no pretense). |
| `feat(perf)` new **`performance.py`** — local portfolio-vs-SPY report (cumulative, max drawdown, ann. vol, Sharpe) from `agent_log.json` + `market_snapshot.json`; writes `performance_report.json`. Supabase-independent. | 6 | No local way to tell if the strategy beats buy-and-hold. Honest caveats printed (price-return SPY, cash drag, too-few-days Sharpe). |
| `fix(gitignore)` split the malformed `market_snapshot.jsonfills.json` line → `fills.json` ignored; `market_snapshot.json` left tracked (the cloud data source `market_data.yml` commits); `performance_report.json` ignored. | — | The concatenated line ignored **neither** file. Found during QA. |

**Rejected after inspection** (the spec was written without seeing the current code/data):
- **0.1 (clamp oversized `target_weight`)** — already done, and better: `guardrails.validate_decisions` rule 5 already clamps weight to [0,0.10] **and recomputes qty** (the spec's version would drop the trade instead of capping it). Adding it would create two competing gates.
- **3.2 (gate the earnings agent on an upcoming-earnings signal)** — unbuildable: there is **no earnings-calendar data source** in the system. A heuristic gate would be fabrication. The real fix is to add a calendar feed (documented as future work).
- **5.1 (structured output, delete brace-recovery)** — high blast radius on the core parse path; the brace-recovery it deletes currently works. Deferred to its own PR with live dry-run validation.
- **5.2 (programmatic `invalidates_if` checks)** — the real stored conditions are **qualitative fundamental triggers** ("Mounjaro revenue growth below 25% YoY", "WTI below \$55/bbl", "KEYTRUDA below \$15B consensus"), not the stock's own price. A price/drawdown regex would misread a commodity price or consensus figure as a stock-price stop and feed false flags to the LLM. The right fix is an explicit structured `price_stop` field the Research agent populates — a data-model change, not regex mining.

> **QA:** full `pytest` suite green (**205**, +45 new). `DRY_RUN` integration was validated with a **stubbed** end-to-end `get_trade_decisions` run (today's snapshot is from 6/12 and would preflight-abort, so a real `main.py` dry-run can't reach the agents until Monday's fresh snapshot). Workflow YAML parses; DEPLOYMENT §3.1/§3.7 regression scripts pass. P0/P1/P2 change — solo-dev 24h cooling + a real dry-run on Monday's fresh data recommended before the change drives a live run.

## Changelog — Jun 12 2026 (evening — code-review remediation batch)

Six-phase remediation from a senior code review. Each phase is an independent commit; all P0 unless noted. Newest first. **Live-routine sync required before the next live run** — see the "Live routine sync" note at the end.

| Commit | Change | Why it mattered |
|--------|--------|-----------------|
| `chore(hygiene)` | (Fix 8) ET timestamps in `journal.check_kill_switches` + `publish.py`; `_compute_qty` single position lookup; `health_check.yml` 11:00 AM → 1:15 PM ET (+`update_dst.yml` regex fix for minute 15); CLAUDE.md Cloud Environment corrected (committed `market_snapshot.json` is the primary cloud source with real quant scores, not all-50); deposit-inflates-peak limitation documented. | `health_check` at 11:00 false-alarmed against legit 11:45/12:45 routine retries; docs claimed all-50 cloud scores as normal when the gate guarantees real data; naive `datetime.now()` could log the wrong date near midnight. |
| `fix(execute)` | (Fix 6) Per-order try/except in `execute_trades`; routine STEP 4 mirrors it for the MCP path. | One transient order exception aborted the whole loop — with SELL-before-BUY ordering, SELLs filled but BUYs never attempted → capital stranded in cash. |
| `fix(portfolio)` | (Fix 4) `get_portfolio_summary` raises `StalePortfolioError` if `mcp_portfolio.json` `as_of` is missing/not today (ET); `main.py` aborts before sizing orders. Routine STEP 1 (daily + EOD) writes `as_of`. | Every order was sized from this file with no freshness check; a stale committed copy would size today's trades against a prior day's cash/positions. |
| `fix(reconcile)` | (Fix 3+5) `mark_transactions_live` is authoritative for **all three** logs (transactions.json / trades.csv / decision_journal.json) against broker fills; `get_trade_history` excludes `dry_run` rows; `fills=None` now raises (was silent flip-all); `health.append_check`; one-time backfill of the Jun 10–12 `trades.csv`/journal rows. | trades.csv showed phantom fills fed back to agents via `get_trade_history`, and `decision_journal` kept "open" theses for rejected orders — agents were told they held positions the broker may have refused. |
| `fix(idempotency)` | (Fix 2) Stamp-first execution claim: `execution_started_at` stamped + pushed BEFORE the first order; gate + GUARD 3 treat a today claim as SKIP/DONE; STOP-without-orders if the claim push fails. | An attempt that placed some orders then crashed before stamping `executed_at` left the next hourly attempt to re-place everything (documented unresolved caveat). Now fails toward missed trades, never duplicates. |
| `feat(guardrails)` | (Fix 1+7) `guardrails.validate_decisions` in `main.py` after qty pre-compute: action whitelist, BLOCKED_TICKERS, ticker ∈ candidates ∪ holdings, same-ticker BUY+SELL rejection, weight clamp to [0,0.10] **with qty recompute**, BUY notional cap 12% (SELLs exempt), $5 min, cash-account GFV guard. `decision_validation` health check. | `target_weight` flowed straight into `_compute_qty` with no bounds check (DEPLOYMENT ex-§2.6 known gap); any positive-qty decision was placed regardless of whether the ticker was analyzed. |

> **Live routine sync required** (cannot be done from code — update the live Anthropic routine prompts to match the canonical MD files via the routines UI / `RemoteTrigger`): **daily** `YOUR_ROUTINE_ID_DAILY` — STEP 1 writes `as_of`; STEP 4 claim-commit-push before first order, per-order error handling, reconciler comment; STEP 5 adds `fills.json`. **EOD** `YOUR_ROUTINE_ID_EOD` — STEP 1 writes `as_of`. Until the daily routine is synced, Fix 4 makes Monday's run abort safely (no trades) rather than trade on an undated portfolio.
>
> **Dry-run status:** the full `pytest` suite (166) passes; the live `DRY_RUN=true python main.py` integration run is deferred (DEPLOYMENT §7.1 — it overwrites `pending_decisions.json`; run it on the weekend before syncing the routine, then discard the local `pending_decisions.json`).

## Changelog — Jun 12 2026

Every substantive fix that landed today, newest first:

| Commit | Change | Why it mattered |
|--------|--------|-----------------|
| `67b0bf8` | `journal.mark_transactions_live(run_id, fills)` is now **fill-aware**: only tickers the broker accepted (present in `fills`, keyed `ticker -> {order_id, price}`) flip `dry_run=False` and get their `broker_order_id` + actual fill price persisted; rejected/absent tickers stay `dry_run=True`. `publish.py` `close_value` is now **write-once** (guarded by a null-check on today's row). Both routine prompts updated: daily STEP 4 builds a fills accumulator → `fills.json` → passes to reconcile, and adds a per-order `ref_id` (UUID) for broker idempotency. `test_pipeline.py`: `TestMarkTransactionsLive` (5) + `TestCloseValueImmutability` (3). | Senior review caught that the first cut flipped **every** decision live regardless of actual fill — reintroducing the `fd9d56a` phantom-fill bug for the cloud path, with no `broker_order_id` ever persisted (no reconciliation). `close_value` overwrote on every is_close publish — unmasked once EOD actually triggered publish. |
| `9f1ad2e` | New `ROUTINE_EOD_CLOSE.md` (version-controlled EOD prompt); CLAUDE.md EOD section now flags the `portfolio_snapshot.json` trigger requirement. EOD routine fixed to `git add portfolio_snapshot.json` (was only `mcp_portfolio.json`) and drop `[skip ci]`. | EOD committed only `mcp_portfolio.json`, but `publish.yml` triggers solely on a `portfolio_snapshot.json` push → the official 4 PM `close_value` **never auto-published** (required manual dispatch / backfill commits). |
| `0e17c7d` | `journal.mark_transactions_live` added + called in daily STEP 4 stamp; daily STEP 5 commit message dropped `[skip ci]`; Jun 11/12 `transactions.json` backfilled `dry_run=False` (reconciled against Robinhood — all 7 orders confirmed filled). | Cloud `main.py` runs `DRY_RUN=true` (robin_stocks blocked), so `record_transaction` stamped every real MCP trade `dry_run=True`; `publish.py` filtered them out → no cloud trade ever reached the website. `[skip ci]` on the routine commit also suppressed `publish.yml`. |
| `6c46cc9` | `ROUTINE_DAILY_CYCLE.md` STEP 1: added `available_qty` to the `mcp_portfolio.json` spec. | `execute.py:_compute_qty` reads `available_qty` to cap SELLs, but the routine never wrote it (spec/code mismatch). |

> **Live routines updated** (via `RemoteTrigger`, schedules + Robinhood MCP preserved) to match the canonical MD files. Daily takes effect next proceeding run (today already executed); EOD's first live test is the 4 PM ET run.
>
> **Dry-run skipped (DEPLOYMENT §7.1/§14):** `DRY_RUN=true python main.py` was **not** run — it's market hours on a trading day and today's cycle already executed; running it would overwrite `pending_decisions.json` and risk a double-fill. Verification was limited to the full `pytest` suite + workflow YAML parse.

## Changelog — Jun 11 2026

Every substantive fix that landed today, newest first:

| Commit | Change | Why it mattered |
|--------|--------|-----------------|
| (this change) | `publish.py`: `_fetch_spy_from_snapshot()` reads SPY price from `market_snapshot.json` (committed daily, contains today's live price) instead of Polygon "prev" (previous day's close); `is_close` file-override now guarded by `GITHUB_ACTIONS` env var; `test_pipeline.py`: 9 new tests (`TestPublishSpyDataSource`, `TestIsCloseInheritance`); Supabase Jun 11 row repaired (cleared spurious `close_value`, updated SPY to 735.15 / -0.55%); website `performance.ts`: Jun 5 inception point + SPY always synced with portfolio | (1) Polygon "prev" at 9:45 AM returns yesterday's close — identical to what yesterday's EOD already stored → duplicate chart rows. (2) Morning run inherited `is_close=True` from previous day's EOD `portfolio_snapshot.json` → wrote spurious `close_value` for the new day, recording a 9:45 AM intraday price as the official 4 PM close |
| (prev change) | `test_pipeline.py`: 24 new tests (`TestLoadListGuards`, `TestTradeLogMigration`, `TestOrderExecuted`, `TestSellBeforeBuyOrdering`, `TestExecutionStampDecision`); `execute.order_executed()` extracted to module level so main.py and tests share the real classifier; docs updated | Locks in the `fd9d56a` behavior with regression coverage per DEPLOYMENT.md §12 — execution-path changes must ship with tests |
| `fd9d56a` | Broker order verification in `main.py` (an order counts as executed only if the broker returned an order id; rejections → health DEGRADED/FAILED with per-ticker detail, excluded from all logs); SELL-before-BUY ordering in `execute_trades` (cash account); `trades.csv` 12-column schema migration + Jun 10 backfill; `journal._load_list()` type guards on all list appenders; idempotency stamp now set when ANY order placed, withheld when NONE placed | Rejected orders (insufficient buying power, halted ticker) were logged as fills with `execution: OK`; BUYs funded by same-day SELLs could be rejected if placed first; trades.csv rows were misaligning under the stale 7-column header; a `{}`-shaped journal crashed Step 7 after orders were already placed |
| `b8ec88d` | `journal.py`: guard `decision_journal.json` being `{}` (empty dict) on first run — `isinstance` check resets to `[]` before `.append()` | Cloud routine crashed with `AttributeError: 'dict' object has no attribute 'append'` on the very first trade because the file contained `{}` not `[]`; pipeline produced 0 trades |
| `61ab95a` | `analysis.py`: `max_tokens` increased (Agent 1: 400→700, Agent 2: 600→1000, Agent 3: 400→600, Agent 4: 500→800); `_parse_json` truncation recovery (count open braces, strip trailing partial strings, append closing `}]`) | Richer news descriptions in prompts pushed verbose Haiku responses past the old ceiling; every parse failed mid-JSON → all 20 Research/DA/Earnings agents returned defaults → 0 trades |
| `e2a18b3` | `market_data.py`: `get_news_summary()` and `get_ticker_news()` called BEFORE the history loop (not after) | Polygon free tier = 5 calls/minute. History loop exhaust the budget in <1 second; news calls 40 seconds later hit a grey zone. Moving news first guarantees a fresh 5-call budget |
| `ac69e20` | `market_data.py`: news feed upgraded — 50 articles (was 20), `description` field (300 chars), `published_utc`; new `get_ticker_news()` for top-4 movers (|change_pct|>3%); `analysis.py`: `_fmt_news` + `_ticker_news` helpers pass richer context to Agents 1–5 | Agents previously saw only headlines; missed material context behind moves |
| `2b21c7f` | `market_data.yml`: 3 staggered cron triggers (7:00 / 8:00 / 8:30 AM EDT) instead of a single 8:00 AM cron; `update_dst.yml`: rewrote broken regex (matched minute 20 which never existed in the file) to do explicit block-string replacement of all 3 crons | Jun 11 market_data run silently skipped by GitHub scheduler — no error, no trace. Triple triggers mean one silent skip can't strand the 9:45 AM routine |

## Changelog — Jun 10 2026

Every substantive fix that landed today, newest first:

| Commit | Change | Why it mattered |
|--------|--------|-----------------|
| `8f0b2e9` | Atomic JSON writes (`journal.py`, `health.py`); ET timezone everywhere; SELL cap vs `available_qty`; 50% daily turnover circuit breaker (`main.py`) | Eliminates JSON corruption on process kill; UTC/ET date mismatch at year-end; oversell rejection from broker; full-portfolio churn on bad PM output |
| `7652b9d` | `_safe_call` retries: 0→2 default; 529-specific 30s/60s backoff; all 7 agents at retries=2; CRO `api_failed` flag + DEGRADED health status | Today's 529 Anthropic API overloads killed all per-ticker agents and blocked CRO → 0 trades. Retries with long backoff survive transient load spikes |
| `cc75b18` | Replace `statistics` stdlib with pure math (`quant_engine.py`); fix list-unwrap in `_parse_json` | Python 3.11 `statistics.stdev` `AttributeError` crash on non-Fraction float types; Devil's Advocate `list.get()` crash |

## Changelog — Jun 9 2026

Every substantive fix that landed in the prior day, newest first:

| Commit | Change | Why it mattered |
|--------|--------|-----------------|
| (this change) | `alert.yml` YAML block-scalar fix; CLAUDE.md failure-mode/QA docs | `alert.yml` was an invalid workflow → failed on every push (email flood) and health alerting was dead |
| `630584a` | Removed redundant `publish_eod.yml` | `publish.yml` already handles both daily and EOD on `portfolio_snapshot.json` push |
| `04f6a3f` | Fixed CRO transient failures + list-unwrap parsing bug (`analysis.py`) | Agent 7 (CRO) could crash/misparse and block the trade list |
| `f192019` | Route Supabase publish through GitHub Actions (`publish.py`, `publish.yml`, `main.py`) | Anthropic cloud blocks Supabase (403); GH Actions has access |
| `c57f9f4` | Fixed empty responses in agents 2–4; added fundamentals cache | Haiku agents were returning blank theses; cache cuts Polygon calls |
| `02fc33c` | Granted `contents: write` to `market_data.yml` | Fixed the `git push` 403 (verified pushing) |
| `6d52c05` | Health tracking, pre-flight abort, failure alerting | Prevents silent all-50 quant no-trade runs |
| `0b5c53e` | Made `mark_pending_executed` idempotent | Prevents double-execution on routine retry |
| `f1eaf4b` | Stale market-data guard + filter preferred-share news tickers | Stops stale snapshots and `JPMPC`-style yfinance 404 noise |
| `e1eec67` | Guard against negative qty reaching the broker | Capital-integrity: never send a malformed order |
| `05a0bf0` | Node.js 24 action compat + pre-computed fractional qty for cloud | Cloud routine places fractional orders directly from `qty` |

## Operational Failure Modes — Jun 9 2026 Incident Log

All observed failures, root cause, and verified status. Documented so the same symptoms are diagnosable on sight.

| # | Symptom (what you saw) | Root cause | Status | Type |
|---|------------------------|-----------|--------|------|
| 1 | `Invalid workflow file: .github/workflows/alert.yml#L61` — and `alert.yml` "failing" on **every** push (even commits that don't touch `system_health.json`) | The issue-body markdown was a multi-line JS template literal whose lines sat at **column 0**, breaking out of the YAML `script: |` block scalar. YAML read the leading `*` of `**Run ID:**` as an alias token. An invalid workflow file is recorded as a failed run on every push (GitHub can't parse it to even apply the path filter) → **this was the source of the email flood, and health alerting was silently dead the whole time.** | **FIXED** — body rebuilt as an array of indented string literals joined with `\n`; all lines stay inside the block scalar. Validated with `yaml.safe_load`. | Code |
| 2 | `remote: Write access to repository not granted` / `403` on the `git push` step (market_data.yml) | The pushing workflow lacked `permissions: contents: write`, so the built-in `GITHUB_TOKEN` was read-only. The repo default (`default_workflow_permissions`) is `read`, but a **per-workflow `permissions:` block overrides it** (verified). | **FIXED** by commit `02fc33c` adding `permissions: contents: write` to `market_data.yml`. **Verified working**: run `27227558840` pushed `8967ff3..1bf6ece main -> main`. `update_dst.yml` already has the same block. No repo-setting change required. | Code (already merged) |
| 3 | `{'message': 'Invalid API key', 'code': 401}` (fetch_snapshot.py / publish.py) | `SUPABASE_SERVICE_KEY` returned 401 earlier in the day. | **CURRENTLY HEALTHY** — `publish.yml` run on `e1ff540` succeeded, so the secret is valid now. `fetch_snapshot.py` also now wraps the upload in try/except so a bad key no longer crashes the market-data job (the committed file is the authoritative path for the routine). Monitor; if it recurs, rotate the **service-role** key under Settings → Secrets → Actions. | Secret (transient) |
| 4 | `❌ No portfolio snapshot for <date> … exit code 1` (health_check.yml) | Downstream symptom of #3: when Supabase publish hadn't written today's row, the 11:00 AM health check found nothing. Also fires if the routine/publish simply hasn't run by 11:00 AM. | Resolves once #3 is healthy. Note it is **timing-sensitive** — see the cron-delay risk below. | Symptom |

Noise (non-fatal, safe to ignore): `$JPMPC: possibly delisted` and similar yfinance 404s for preferred-share tickers. The Polygon news-discovery path already filters `^[A-Z]{2,5}P[A-Z]$` (commit `f1eaf4b`); remaining warnings are yfinance probing tickers that return empty and do not affect the run.

### ⚠️ The real standing risk: GitHub scheduled-cron delay

`market_data.yml` now fires at **three** staggered times — 7:00 AM, 8:00 AM, and 8:30 AM EDT (commit `2b21c7f`) — with an idempotency guard that skips the job if today's snapshot is already present. **GitHub Actions scheduled runs are best-effort and can be delayed by hours or skipped during high load** — even with three triggers, all three could be delayed simultaneously. On Jun 9 every run was late; on Jun 11 the job was silently skipped. If none of the three crons lands before 9:45 AM ET, `market_snapshot.json` is stale → routine **preflight-aborts** (`_data_date != today`) → zero trades.

Mitigations (in order of robustness):
1. **Safety dispatch** (still the highest-reliability option): trigger `market_data.yml` manually if no fresh `chore: market snapshot` commit has landed by ~9:15 AM ET: `gh workflow run market_data.yml --repo parthchoksi7/ai-investor` then confirm the commit is on `main`.
2. ~~Move the cron earlier~~ — **Done** (commit `2b21c7f`): earliest trigger is now 7:00 AM EDT, 165 min before the routine.
3. Longer term: have the routine itself dispatch `market_data.yml` and poll for the fresh commit before running `main.py`, rather than assuming the file is present.

### Repository settings — current state (verified, no change required)

- `default_workflow_permissions = read`. This is fine **because every workflow that pushes declares its own `permissions: contents: write`.** Flipping Settings → Actions → Workflow permissions to "Read and write" is optional defense-in-depth, not a fix.
- Branch protection on `main`: not enabled (private repo on the free plan). Bot accounts push directly without obstruction.
- Actions secrets present: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `POLYGON_API_KEY`, `ANTHROPIC_API_KEY`, `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`.

## Deployment gates (run before every deploy)

Two mandatory pre-commit gates, authoritative copy in DEPLOYMENT.md §7.0:
1. **Update `RELEASE_NOTES.md`** — describe the change under `[Unreleased]`, and on
   deploy move it into a new `## [YYYY-MM-DD] — … · ~HH:MM PT · <hash>` block.
   (`TestReleaseNotes` asserts an `[Unreleased]` section exists.)
2. **Expert code review** — `/code-review high` (or `/code-review ultra` for P0
   execution-path changes); resolve every finding before committing.

## Pre-Run QA Checklist (run before market open or after any CI change)

Fast verification that tomorrow's 9:45 AM cycle will succeed:

```bash
# 1. All workflow YAML parses (catches block-scalar / indentation bugs like alert.yml#L61)
python3 -c "import yaml,glob; [yaml.safe_load(open(f)) for f in glob.glob('.github/workflows/*.yml')]; print('workflows OK')"

# 2. Unit tests pass (deterministic pipeline logic — no API keys needed)
pytest test_pipeline.py -q

# 3. Local pipeline dry run (requires .env with API keys; never places orders)
DRY_RUN=true python main.py
```

Manual checks (GitHub UI / `gh`):
- [ ] **`market_data.yml` is green today AND its `chore: market snapshot` commit landed on `main` before 9:45 AM ET** ← the single highest-signal check; if stale, the routine aborts regardless of everything else (see cron-delay risk). Verify: `python3 -c "import json;d=json.load(open('market_snapshot.json'));print(d['date'])"` == today after `git pull`.
- [ ] **Actions secrets** `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` present and valid; last `Publish to Supabase` run green (failure modes #3/#4).
- [ ] No open `health-alert` GitHub Issue from a prior aborted run masking a new one.
- [ ] Both Anthropic routine crons match the current DST offset (EDT `45 13` / `0 20`; EST `45 14` / `0 21`).
- [ ] (Optional) Settings → Actions → Workflow permissions = "Read and write" for defense-in-depth — not required since pushing workflows declare their own write permission.

## Known Limitations

- Cloud quant scores are real (full 210-day history) because the committed `market_snapshot.json` is the primary source; they only degrade toward 50 in the `mcp_market_data.json` fallback, which the pre-flight gate + main.py abort prevent from running. So "no trades" from a missing snapshot is intended (the day is skipped), not a silent all-50 run.
- **Fundamentals coverage (quality + valuation factors):** `SECProvider` (no key required) sources gross_margin / operating_margin / debt_to_equity from SEC EDGAR for virtually all US-listed equities. P/E, FCF yield, and EV/EBITDA require `FMP_API_KEY`; without it those three fields remain N/A. Forward earnings calendar also requires `FMP_API_KEY`. See `data_providers.py` for the full provider chain.
- **FMP free-tier coverage:** only ~35% of the 100-ticker watchlist (mega-caps: AAPL, MSFT, NVDA, GOOGL, META, JPM, BAC, GS, etc.) returns data on the free plan; the rest 402 "premium only". The alternate-day 50/50 cache + 30-day backoff for empty tickers keeps usage under the 250/day free limit.
- **Drawdown peak does not account for deposits.** `portfolio_peak.json` tracks raw `total_value`. Funding the account (a deposit) inflates `total_value` → a new apparent peak, so the kill-switch drawdown math (`(peak − current) / peak`) measures from a peak that includes deposited cash, not investment performance. After any deposit/withdrawal, manually reset `portfolio_peak.json["peak"]` to the post-deposit value (see Manual Execution Runbook Scenario C).
- `market_data.py` makes ~90 API calls per run locally (one per ticker for 210-day history) plus up to 5 Polygon calls for news (1 summary + 4 per-ticker). Polygon free tier = 5 calls/minute — news calls are placed FIRST (before the history loop) to guarantee a fresh budget window.
- DST: GitHub Actions crons (`market_data.yml`, `health_check.yml`) are auto-updated by `.github/workflows/update_dst.yml` on March 15 and November 8. Both Anthropic routine crons require manual updates — see the EOD note and Daily Cycle note under Automated Execution above.
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
| `TestLoadListGuards` | `journal.py` | `_load_list` coercion; appenders survive a `{}`-shaped journal/transactions/agent-log file |
| `TestTradeLogMigration` | `execute.py` | `trades.csv` old-header rewrite, row preservation, aligned appends post-migration |
| `TestOrderExecuted` | `execute.py` | Broker result classification: order id / dry-run = fill; rejection / block / empty = not a fill |
| `TestSellBeforeBuyOrdering` | `execute.py` | SELLs placed before BUYs (stable within side); HOLD and qty-0 never placed |
| `TestExecutionStampDecision` | `main.py` logic | Idempotency stamp truth table: stamp on any fill, withhold when nothing placed |
| `TestEnforceSectorLimits` | `guardrails.py` | 25% sector cap; SELL frees budget before BUY; holdings count; order preserved; default `SECTOR_MAP` |
| `TestClosePosition` | `journal.py` | Realized-outcome feedback: full/partial exit, thesis-correct branches, no-op + zero-avg guards, survives reconciliation |
| `TestTickerHistoryHelpers` / `TestMemoryPromptBlocks` / `TestMemoryInjectedIntoAgents` | `journal.py` / `analysis.py` | Per-ticker history + recently-exited recall; prompt blocks; memory actually reaches Research/PM `user_msg` |
| `TestReturnCorrelations` / `TestCroCorrelationInjection` | `quant_engine.py` / `analysis.py` | Pairwise return correlation; CRO `user_msg` carries a real correlation block (none when no history) |
| `TestPerformanceReport` | `performance.py` | Metrics on known curves, epoch-ms SPY parse, as-of alignment, end-to-end report, missing-file safety |
| `TestScoreAllTickers` (updated) | `quant_engine.py` | Honest composite: all-4 factors when fundamentals present; drops + renormalizes momentum+volatility when absent; neutral when no factor has data |
| `TestDataProviders` | `data_providers.py` | StubProvider contract; FMPProvider no-key degradation; FMP stable-API field mapping; earnings soonest-future; factory returns SECProvider (no key) / FMPProvider (key) |
| `TestSECProvider` | `data_providers.py` | EDGAR ratio computation from 10-K; most-recent annual entry; unknown ticker → None; no earnings/estimates; CIK map loaded once; zero-equity guard; HTTP error → None; Protocol conformance |
| `TestProviderEnrichmentCache` | `market_data.py` | Group determinism (0/1); StubProvider → no-op; fetch on group day + cache; cache served on off-day; 30-day backoff for empty entries |
| `TestEarningsGateAndFabrication` | `analysis.py` | Phase 3.2: skip LLM when calendar shows no event in 90d; run when event within 90d; no-calendar falls back to prior behavior; fabrication guard overrides model's date guess |

## Manual Execution Runbook

Use this when the scheduled routine fails or you need to intervene.

### Scenario A — Routine failed before placing orders

`pending_decisions.json` exists, `executed_at` is `null`, **and `execution_started_at` is `null`**.

> ⚠️ If `execution_started_at` is **not** null, the failed attempt got as far as claiming the
> run — orders may have been placed even though `executed_at` was never stamped. That is
> **Scenario B**, not A. Never blind-execute a claimed file.

1. Check `pending_decisions["date"] == today`. If stale (yesterday's file), **stop** — wait for tomorrow's run.
2. Read `pending_decisions["decisions"]`. If `[]`, nothing to execute.
3. For each BUY or SELL decision (**never TSLA**):
   - Use `decision["qty"]` directly — it is pre-computed fractional shares. **Do NOT round to whole shares.**
   - If `qty` is missing (old file), fall back to: `round(target_weight × total_value / current_price, 6)` — `total_value` from `mcp_portfolio.json`
   - Place: `place_equity_order(account_number='YOUR_ACCOUNT_NUMBER', symbol=ticker, side='buy'|'sell', quantity=qty, type='market', time_in_force='gfd')`
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
