# Release Notes — AI Investor

User-facing log of what shipped, when. **Update the `[Unreleased]` section in the
same PR as the change, and on deploy move it to a new dated release block** (see
DEPLOYMENT.md §7.0). Newest first.

> Timestamps are **Pacific Time** (reconstructed from git commit offsets `-0700`
> for pre-existing releases; approximate to the commit, not the live cloud run).
> The system trades a dedicated Robinhood agentic account; inception ≈ **2026-06-04**
> with **$500** starting capital.

---

## [Unreleased]

_Nothing pending — see the dated release below._

---

## [2026-06-14] — P1: quant backtest harness + cost_model spine + QA hardening  ·  ~22:15 PT  ·  PR #10

### Added
- **`cost_model.py`** — shared cost & tax spine (P1 foundation for the backtest
  #3 and the future net-edge gate #6). Holds the CA top-bracket tax rates +
  IRS-style ST/LT netting (`tax_on_realized`), a round-trip cost/slippage
  estimate (`round_trip_cost`), and `net_edge()` (gross − cost − CA tax).
  `performance.py` now imports the rates + netting from it (single source of
  truth), so simulated and live economics can't drift. (+9 tests; suite **245**.)
- **`backtest/` — quant-only backtest harness (#3 / P1).** Event loop over the
  `market_snapshot.json` history that reuses `quant_engine.score_all_tickers`
  unchanged (scores exactly what live scores), fills at next-day open (no
  look-ahead), and imports `cost_model` for after-cost/after-tax economics.
  Includes a momentum/inverse-vol strategy, an after-tax-vs-SPY report
  (CAGR/vol/Sharpe/max-DD/turnover, gross & net-of-tax), and `python -m backtest`.
  **No LLM in the backtest** (a frozen model knows the future — the LLM layer is
  forward-tested, not backtested). (+8 tests; suite **253**.)

  > **First result (honest):** the quant-only momentum/vol strategy returned
  > **−0.03%** over ~10 months vs SPY **+8.77%** — gross alpha **−8.8%**, 23.6×
  > annual turnover. The deterministic layer has **no demonstrated edge** at this
  > config; this is exactly the validation P1 exists to provide.

### Fixed (QA hardening — two independent review passes, all personas)
- **Timezone-flaky test** — `TestPublishSpyDataSource` computed "today" with local
  `date.today()` (Pacific) while the production function uses ET, so it failed ~3
  hours **every day** (the midnight-ET-to-midnight-PT window). Now uses ET to match.
- **Survivorship-bias caveat** — the backtest report now discloses that the universe
  is only tickers in today's snapshot (no delisted names) and fixed over the window,
  so absolute returns are biased upward.
- **+8 edge-case regression tests** — degenerate backtests (no SPY / empty history /
  warmup overflow / no-leverage / no-look-ahead), guard boundaries (exactly-5-day
  hold and exactly-30-day wash-sale both correctly allowed), survivorship disclosure.
  Suite **261**.

> **QA insight (Portfolio Manager lens):** monthly rebalancing yields **+$4,185
> realized vs −$242 for daily** (86 vs 1,360 trades) — churn is value-destructive,
> empirically backing the turnover/tax guards shipped in PR #9.

---

## [2026-06-14] — Paper-shadow 100× + after-tax scorecard + turnover discipline  ·  ~17:30 PT

Edge-upgrade batch P0/P0.5. Three shipped changes + two planning docs.

### Added
- **Paper-shadow 100× columns on `trades.csv`** (`qty_100x`, `total_value_100x`,
  `portfolio_value_100x`) — models the same trades on a hypothetical $50,000 book
  (same price; qty and dollar value ×100). Existing rows backfilled; new rows and
  broker reconciliation keep the twin in sync. `SHADOW_MULTIPLIER`/`_scaled` in
  `execute.py`. *Caveat: a linear projection (zero market impact), not proof of scale.*
- **After-tax scorecard** (`performance.py`) — net return after **California
  top-bracket** tax (short-term ≈54%, long-term ≈37.1%) vs holding SPY in the same
  account. Tracks **realized gain and after-tax realized gain separately**, via
  FIFO lot-matching with ST/LT classification. SELLs with no in-log cost basis are
  reported as "uncovered," never assigned a guessed basis. Flags
  `not_significant` below 60 trading days.
- **Turnover / tax guardrails** (`guardrails.py`, wired in `main.py`):
  `enforce_min_holding_period` (block SELLs of names bought < 5 trading days ago;
  risk exits exempt) and `enforce_wash_sale_reentry` (block BUYs of names sold
  within 30 calendar days — hardens the soft 10-day re-entry warning into a control).
  Folded into the `decision_validation` health check.

### Docs
- `SOLUTION_PLANS.md` — expert-panel designs for improvements #1/#2/#3/#5/#6/#9.
- `FINAL_PLAN.md` — phased roadmap (P0–P6) + California tax recalibration + a
  Shreyas-Doshi-style expert pre-mortem.

### Fixed (pre-deploy expert code review)
- **Tax netting** — `realized_summary` now nets ST/LT gains and losses per IRS
  ordering (a term loss offsets the other term's gain before tax) instead of
  taxing gains in full while crediting losses fully against after-tax.
- **Guard ordering** — turnover guards now run **before** the sector cap in
  `main.py`, so the cap projects against the SELL set that will actually execute
  (a SELL dropped after the cap freed its budget could otherwise let a
  same-sector BUY breach 25%).
- **Single transactions read** — `validate_decisions` + both turnover guards now
  share one `transactions.json` read (one consistent view, not three).
- DRY: `_last_live_buy_date`/`_last_live_sell_date` share `_last_live_trade_date`;
  min-holding skips the file read when the kill switch short-circuits.

### Tests
- +31: `TestPaperShadowColumns`, `TestRealizedLots`, `TestRealizedSummary` (incl.
  ST/LT netting), `TestAfterTaxScorecard` (incl. the "beats SPY pre-tax, loses
  after CA tax" case), `TestMinHoldingPeriod`, `TestWashSaleReentry`,
  `TestReleaseNotes`. Suite: **236 passing**.

---

## [2026-06-13] — IMPROVEMENTS_SPEC batch  ·  ~12:18 PT  ·  `722539a`, merge `26ded5f`

Critical evaluation + selective implementation of `IMPROVEMENTS_SPEC.md` (6 of 9
phases implemented, 3 rejected on inspection).

- **Sector cap (25%) in code** — `guardrails.enforce_sector_limits` + static
  `SECTOR_MAP`; the limit previously lived only in the PM prompt (not a control).
- **Outcome feedback loop** — `journal.close_position` populates
  `actual_return`/`thesis_correct`/`exits` on the matching open BUY when sold.
- **Agent memory** — `get_ticker_history` + `recently_exited` fed to the Research
  Analyst (prior outcomes) and Portfolio Manager (re-entry warning).
- **Honest quant composite** — sub-scores carry `*_available`; weights renormalize
  over real factors; `N/A` instead of a fake 50.
- **CRO real correlation** — pairwise 120d return correlation + sector
  concentration injected into the CRO prompt.
- **`performance.py`** — local portfolio-vs-SPY report (price-return) with drawdown/
  vol/Sharpe.

## [2026-06-12] — Senior code-review remediation (6 phases)  ·  ~19:12 PT  ·  `5f2144d`…`8acff18`

All P0 unless noted; each phase an independent commit.

- **Deterministic guardrails gate** (`5f2144d`) — action whitelist, BLOCKED_TICKERS,
  candidate-membership, BUY+SELL conflict rejection, weight clamp + qty recompute,
  notional cap, $5 min, GFV guard.
- **Stamp-first idempotency** (`7cc2e01`) — `execution_started_at` set + pushed
  before the first order; closes the cross-attempt double-fill window.
- **Authoritative fill reconciliation** (`b3739d9`) — `mark_transactions_live`
  reconciles all three logs against broker fills; `fills=None` now raises.
- **Portfolio freshness** (`02bc9a0`) — `get_portfolio_summary` raises
  `StalePortfolioError` unless `mcp_portfolio.json` `as_of` is today (ET).
- **Per-order failure isolation** (`8483759`) — one order exception can't strand
  the rest (SELL-before-BUY otherwise stranded capital in cash).
- **Hygiene** (`d580c7c`) — ET timestamps, single-lookup `_compute_qty`,
  health_check 1:15 PM ET.

## [2026-06-12] — Cloud trade reconciliation + EOD publish fixes  ·  ~07:26–07:55 PT  ·  `0e17c7d`…`0e502cf`

- **Mark cloud trades live** (`0e17c7d`) — cloud `main.py` runs `DRY_RUN=true`, so
  MCP trades were stamped `dry_run=True` and never reached the website; reconcile
  flips broker-accepted trades live.
- **Fill-aware reconcile + `close_value` immutability** (`67b0bf8`, `0e502cf`).
- **EOD publish** (`9f1ad2e`) — stage `portfolio_snapshot.json` (not just
  `mcp_portfolio.json`) and drop `[skip ci]`, so the 4 PM `close_value` auto-publishes.
- **Node 24** for all GitHub Actions (`e6185fe`).

## [2026-06-11] — Publish / SPY source fix  ·  ~12:24 PT  ·  `a35323c`, `dde9b84`

- Read SPY price from `market_snapshot.json` (today's live price) instead of
  Polygon "prev" (yesterday's close) → no duplicate chart rows; guard `is_close`
  inheritance.
- Remove `[skip ci]` from routine commits + add `workflow_dispatch` to `publish.yml`.

## [2026-06-11] — Pipeline resilience + richer news + CI  ·  ~06:51–11:44 PT  ·  `2b21c7f`…`57cd8d7`

- **Broker order verification + SELL-before-BUY + trades.csv schema migration**
  (`fd9d56a`) — rejected orders were being logged as fills.
- **max_tokens increase + JSON truncation recovery** (`61ab95a`) — verbose Haiku
  responses were truncating mid-JSON → all agents returned defaults → 0 trades.
- **Richer news feed** (`ac69e20`) + **news-before-history** ordering (`e2a18b3`)
  to survive the 5-calls/min free tier.
- **Redundant market-data crons** (`2b21c7f`) to survive GitHub silent skips.
- `{}`-shaped journal guard (`b8ec88d`); fetch cache purge (`a3ffe86`, `57cd8d7`).

## [2026-06-10] — 529 resilience + integrity + parallelization  ·  ~07:31–18:08 PT  ·  `7652b9d`…`736038f`

- **Retry all agents on Anthropic 529 overload** with exponential backoff
  (`7652b9d`); retry when a response parses to default (`a39f338`); canary 529
  gate (`53ba8e9`).
- **Integrity** (`8f0b2e9`) — atomic JSON writes, ET timezone everywhere, SELL cap
  vs `available_qty`, 50% daily-turnover circuit breaker.
- **Parallelize agents 2–5** (`736038f`); atomic fundamentals cache write.
- Publish source-of-truth fixes (`6531571`, `da7a180`, `ab844cc`).

## [2026-06-09 and earlier] — Foundation (see CLAUDE.md changelog for full detail)

- `alert.yml` block-scalar fix (health alerting had been silently dead).
- Route Supabase publish through GitHub Actions (Anthropic cloud blocks Supabase).
- Health tracking, pre-flight abort, failure alerting.
- Idempotent `mark_pending_executed`; stale market-data guard.
- Initial 7-agent pipeline, quant engine, Robinhood MCP execution, website.
