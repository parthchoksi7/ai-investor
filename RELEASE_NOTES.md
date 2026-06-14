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

### Added — SECProvider: free EDGAR fundamentals for the full universe
- **`data_providers.SECProvider`** — uses the SEC EDGAR company-facts XBRL API
  (`data.sec.gov/api/xbrl/companyfacts`) to source `gross_margin`,
  `operating_margin`, and `debt_to_equity` for ~100% of US-listed equities.
  Completely free, no API key, no rate-limit concerns. Powers the full **quality
  score** for every ticker in the watchlist (was all-N/A without `FMP_API_KEY`).
- **`get_provider()` factory** now returns `SECProvider` (not the inert `StubProvider`)
  when no `FMP_API_KEY` is present. Existing behaviour when FMP key is set is
  unchanged — `FMPProvider` still wins and supplies all 6 factors + earnings calendar.
- **`_enrich_with_provider()`** no longer requires `FMP_API_KEY` to proceed; the
  `StubProvider` check is kept as a test injection point.
- **Provider chain (priority order):**
  1. `FMP_API_KEY` set → `FMPProvider`: all 6 quant factors + earnings calendar + estimates
  2. No key → `SECProvider`: 3 quality factors (gross_margin / operating_margin / D/E); no earnings calendar
  3. Test fixtures → `StubProvider`: deterministic no-op

> **What EDGAR does NOT provide:** P/E, FCF yield, EV/EBITDA (price-dependent ratios), and
> the forward earnings calendar. Those still require `FMP_API_KEY`.

(+8 tests: `TestSECProvider` — ratio computation, most-recent annual, zero-equity guard,
HTTP error → None, CIK map loaded once, Protocol conformance.)

### Fixed — #1 FMP provider migrated to the stable API
- FMP deprecated the legacy `/api/v3` endpoints for keys issued after 2025-08-31
  (they 403 "Legacy Endpoint"), so `FMPProvider` was returning `None` even with a
  valid key (graceful no-op — no regression, just no data). Migrated to the
  `/stable` API with **live-validated** endpoints + field names:
  `ratios-ttm` + `key-metrics-ttm` (fundamentals), `earnings` (calendar),
  `analyst-estimates`. Confirmed against AAPL/NVDA/JPM — real margins, P/E,
  FCF yield, EV/EBITDA, and verified next-earnings dates now flow into the
  snapshot. **#1 is now active with `FMP_API_KEY` set.**
- **Alternate-day 50/50 enrichment cache** (`market_data._enrich_with_provider`,
  `provider_cache.json`) — the universe is hash-split into two groups; one refreshes
  each day, so ~50 tickers × 3 stable-API calls ≈ **150 FMP calls/day** (under the
  250/day free-tier limit), each ticker refreshes every ~2 days. **Coverage-aware
  backoff:** FMP free tier covers only **~35%** of the universe (the rest 402
  "premium only"); empty/premium tickers are re-checked every 30 days, not every 2,
  so the budget isn't wasted on them. Cache persisted via `actions/cache`.
- **`market_data.yml`** now passes `FMP_API_KEY` to the fetch step (was missing) and
  persists `provider_cache.json`.

> **Coverage reality (FMP free tier):** ~35/100 tickers return all 6 factors (mega-caps:
> AAPL/MSFT/NVDA/GOOGL/META/AMZN/JPM/BAC/GS/COST/NFLX…); the other ~65 get 3 quality
> factors from EDGAR + momentum+vol from Polygon. Full 6-factor coverage for all tickers
> needs a paid FMP tier (~$22/mo).

---

## [2026-06-14] — Edge batch: #1 real data + #6 net-edge gate + #2 forecast ledger  ·  ~23:20 PT  ·  PR #11/#12/#13

Deployed together after two persona test rounds (289 unit tests + 14 cross-feature
interaction probes, all green). 5.1 (structured output) deferred — needs a live
API dry-run. `FMP_API_KEY` still needed to activate #1's real data (no-op until then).

### Added — #1 real data (provider layer) + Phase 3.2 earnings gate
- **`data_providers.py`** — `MarketDataProvider` Protocol + `StubProvider`
  (testable without a key) + `FMPProvider` (Financial Modeling Prep) + `get_provider()`
  factory. Degrades gracefully: no `FMP_API_KEY` → stub → `None` → free-tier
  fallback, **zero regression**.
- **`market_data.get_market_snapshot()`** now overlays provider `fundamentals`
  (so quant quality/valuation go live — no `quant_engine` change) and a verified
  `earnings_calendar`. No-op without a key.
- **`analysis.run_earnings_catalyst_analyst`** — injects the **verified** earnings
  date with a **fabrication guard** (the calendar date overrides the model's
  guess; flags `earnings_date_corrected`), and **IMPROVEMENTS_SPEC Phase 3.2**:
  when a real calendar exists, **skips the LLM call** for names with no event in
  90 days (no token spend, no all-default noise to the PM). Falls back to current
  behavior when no calendar is available.

> ⚠️ **VENDOR KEY BLOCKER:** set `FMP_API_KEY` in `.env` (local) and as a GitHub
> Actions secret (for `market_data.yml`) to activate real data. Until then the
> stub path runs — identical to today's free-tier behavior. FMP field mappings
> are best-effort vs the v3 schema; validate against a live response.

(+11 tests: `TestDataProviders`, `TestEarningsGateAndFabrication`.)

### Added — #6 net-edge gate (tax-aware trade filter)
- **`tax_lots.py`** — read-only FIFO open-lot accounting (qty / cost basis /
  acquired date) derived from `transactions.json` on demand; persists nothing, so
  it stays out of the money/state path. Plus `holding_days()`.
- **`guardrails.enforce_net_edge`** (using `cost_model.net_edge`) — rejects a BUY
  whose expected return, after round-trip cost **and ~54% CA short-term tax**, is
  below `MIN_NET_EDGE`. **Conditional** on an explicit `expected_return`: a
  decision without one is passed through (no regression). **SELLs exempt** (exits
  never blocked). Wired into `main.py` after the turnover/sector guards; folded
  into the `decision_validation` health check.
- **PM now emits `expected_return`** (gross fraction over the 1–3 mo horizon) — so
  the gate has input, and the journal's feedback loop (`thesis_correct` threshold)
  gets a real expectation instead of 0.

> Makes "is this trade worth it after CA tax?" a **code-level control** rather than
> a hope. Mechanism (fewer marginal trades → less short-term tax) is consistent
> with the backtest finding that monthly rebalance (+$4,185) >> daily (−$242).
> `MIN_NET_EDGE` defaults to $0 (must be net-positive after tax+cost); tunable.

(+8 tests: `TestTaxLots`, `TestNetEdgeGate`.)

### Added — #2 forecast ledger (the learning clock)
- **`calibration.py`** — `log_forecasts()` appends each run's structured agent
  forecasts (quant composite, research confidence, earnings alpha, devil's-advocate
  risk, position hold score) to `forecasts.jsonl`, one row per (agent, ticker,
  field) with the entry price + horizon. **OBSERVATIONAL — logging only, wired
  after `record_run`, never affects a decision and never raises into the pipeline.**
- `score_matured()` joins matured forecasts to realized forward returns from the
  snapshot history (idempotent) → `forecasts_scored.jsonl`.
- `agent_scorecard()` — per-agent rank-IC + sign-hit-rate with **shrinkage toward a
  no-skill prior** (`ic_shrunk = ic·n/(n+k)`), so a lucky handful can't read as
  signal. Nothing sizes a trade on it yet — it's a scoreboard, gated behind sample
  size (future work).

> Scores the **full candidate universe** every run (not just executed trades), so
> it accrues hundreds of labeled forecasts/month — the only way to beat the
> small-sample problem on a $500 book. The ledger files are gitignored.

(+5 tests: `TestCalibrationLedger`.)

> **Integrated suite: 285 passing** (#1 + #6 + #2 together).

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
