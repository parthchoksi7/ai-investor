# Investment Policy Statement (IPS) — AI Investor

> **This is the foundational governance artifact and the single source of truth for
> every investment constraint, limit, and policy in the system.** Where any prompt,
> code path (`guardrails.py`), or design doc disagrees with this document, **this
> document governs** and the other must be corrected. The machine-readable parameter
> table in **Appendix A** is authoritative; code and prompts SHOULD derive their
> limits from it (via `policy.yaml`) rather than hard-coding, to eliminate drift.
>
> Drafted to institutional best practice (CFA Institute IPS framework; Three Lines of
> Defense; SR 11-7 model risk governance). Adapted for a fully-automated, AI-operated,
> single-account book.
>
> **Release pod / related documents:** [STRATEGY_REDESIGN_PLAN.md](STRATEGY_REDESIGN_PLAN.md)
> (design & build plan; governance design in §18) · [MODEL_REGISTER.md](MODEL_REGISTER.md)
> (the SR 11-7 model inventory) · `policy.yaml` (machine-readable mirror of Appendix A,
> to build).
>
> | | |
> |---|---|
> | **Version** | 1.0 |
> | **Effective date** | 2026-06-27 |
> | **Owner / fiduciary** | Account owner (Parth Choksi) |
> | **Review cadence** | Quarterly, or on any material change (see §12) |
> | **Status** | ADOPTED — all changes governed by §12 change-control |

---

## 1. Purpose & Scope

### 1.1 Purpose
AI Investor is a fully-automated, long-only US equity program operated by a multi-agent
LLM pipeline plus a deterministic quantitative layer. **Its current purpose is a
research and learning platform, NOT a profit vehicle.** The explicit objective is to
determine — under honest, pre-registered measurement — whether the AI-driven process
can beat a passive ETF allocation *after tax and costs*, and thereby whether the owner
should allocate additional capital to it versus simply holding SPY/QQQ.

### 1.2 A valid outcome of this program is "do not trade — hold the ETF."
The measurement harness is explicitly permitted, and expected as a plausible result, to
conclude that the active process does not beat a passive benchmark after tax. Concluding
"just hold SPY/QQQ" is a **successful** outcome of the research platform, not a failure.

### 1.3 Scope
This IPS governs the dedicated agentic brokerage account only (`ACCOUNT_NUMBER` —
redacted; cash, individual, `agentic_allowed=true`). No other account is in scope. All
other accounts (`agentic_allowed=false`) are never touched by this system.

---

## 2. Governance & Roles (Three Lines of Defense)

Governance follows the institutional **Three Lines of Defense** model, adapted for an
automated system. Independence between lines is preserved to avoid correlated failure.

| Line | Function | Owned by | Mandate |
|------|----------|----------|---------|
| **1st — Risk ownership** | Generate research & decisions | Agents 1–6 (Regime, Research, Earnings, Devil's Advocate, Position Review, Portfolio Manager) + the quant engine | Own the risk in every recommendation; produce a documented thesis |
| **2nd — Oversight & control** | Independent risk veto + mechanical controls | Chief Risk Officer (Agent 7) + `guardrails.py` | Veto/clamp any decision that breaches this IPS; **must be decorrelated from the 1st line** (see §2.1) |
| **3rd — Independent assurance** | Audit that the process was followed and works | The measurement harness (`calibration.py`), reconciliation (`reconcile.py`), the data-quality gate, and the quarterly review | Independently verify decisions, reconcile fills, validate model performance, and report — answers to the owner, not the PM |

### 2.1 Decorrelation requirement (2nd line independence)
Because the CRO is an LLM and may share blind spots with the PM (an LLM can agree with
itself and still be wrong), the 2nd line's **binding controls are deterministic**: every
hard limit in this IPS is enforced in code (`guardrails.py`), not left to LLM judgment.
The CRO's qualitative veto is *additive* risk reduction, never the sole control. Where
feasible, the CRO should run on a different model than the PM to further decorrelate.

### 2.2 Decision authority
- **Routine weekly rebalance and daily risk-watch:** fully automated within this IPS.
- **Any deviation from a hard limit (Appendix A):** prohibited — the system must abort
  or clamp, never override.
- **Any change to this IPS or a parameter:** owner approval via §12 change-control only.

### 2.3 Separation of duties
The decision, the execution, the reconciliation, and the performance reporting are
performed by **distinct, independently-auditable components** with append-only logs, so
no single component both acts and grades itself. The 3rd line is the auditor of record.

---

## 3. Investment Objectives

### 3.1 Primary objective
Maximize **after-tax, after-cost, risk-adjusted total return** over the holding horizon,
**measured against passive benchmarks**, subject to all constraints in §5–§7.

### 3.2 The default action is HOLD
The system trades only when a trade improves the portfolio's expected value net of tax,
cost, and added risk. A no-trade week is a legitimate decision, not an absence of one.

### 3.3 Pre-registered success / kill criteria (the platform's terminal test)
Fixed in advance so it cannot be rationalized post-hoc:

- **Evaluation window:** 12 months from the first clean run under this IPS.
- **FUND IT (allocate additional capital)** only if, over the window, the **after-tax**
  book beats **SPY AND QQQ on a risk-adjusted basis** (Sharpe / information ratio — *not*
  raw return; beating the benchmark's return while running materially higher volatility
  is not a win) **with statistical significance, AND** beats the **quant-only shadow**
  book after tax (else the LLM layer adds cost, not value — run the cheaper deterministic
  quant).
- **ABANDON / hold ETFs** if it underperforms a SPY/QQQ hold after tax past the window.
- **N/A (no verdict)** if the harness exclusion rate (runs below the data-quality floor,
  §11) is material — fix the pipeline and restart the clock. A confounded result is not
  a verdict.
- **Significance & power:** the harness owns the significance test; the
  `NOT_SIGNIFICANT` flag governs until the bar is cleared. The live $507 book is too
  small to be significant alone, so the test runs primarily on the **paper / 100×-shadow
  modeled book**; the live book proves execution plumbing.

### 3.4 Operating model during validation (owner decision, Jun 27)

The live Robinhood book trades the **full pipeline (LLM agents included) with real
capital from day one.** The capital is intentionally insignificant, so the system does
**not** wait for model validation to go live — it trades the complete process and learns
in production. **Validation status therefore governs the decision to allocate
*additional, significant* capital, not whether the live book trades.**

Two paper arms carry the high-value measurement (they answer *different* questions —
both are required):

- **100× shadow** — a linear ×100 scale of the *same* live decisions (existing
  `SHADOW_MULTIPLIER`). The **magnitude** lens ("what would ~$50k have done at these
  prices") and the high-value performance view. Caveat: linear, zero market-impact — a
  modelling lens, not a real $50k book.
- **Quant-only shadow** — a genuinely *different* decision set (LLM removed). The
  **attribution** lens: does the 7-agent layer beat the deterministic floor after tax?
  This is the comparison that decides whether the LLM earns its keep.

The §3.3 fund/abandon verdict is read off the paper arms (magnitude + attribution),
since the live book is too small to be statistically significant on its own.

### 3.5 Capital-graduation ladder

Capital is added in **governed rungs, never as a binary switch.** The live book starts
and remains at insignificant capital through the evaluation window; scaling beyond it
requires clearing the §3.3 bar on the paper arms. Each rung change is an owner decision
recorded as an IPS amendment (§12).

| Rung | Trigger | Capital | Live behaviour |
|------|---------|---------|----------------|
| **0 — Research (now)** | adoption | insignificant (~$500) | full pipeline, real $; paper arms measure |
| **1 — Quant floor proven** | quant-only shadow beats SPY/QQQ risk-adjusted after tax, significant | small (owner-set) | continue full pipeline |
| **2 — LLM proven** | 7-agent book beats quant-only **and** benchmarks, risk-adjusted after tax, significant | scaled (owner-set) | continue full pipeline |
| **Abandon** | underperforms a benchmark hold after tax past the window | → 0 / passive | terminal action, §3.6 |

### 3.6 Terminal action (Abandon outcome)

On an Abandon verdict the program's honest conclusion is *"hold the ETF."* The live book
is **rotated into the benchmark (SPY/QQQ) and active trading halts.** This is an
**explicit owner-confirmed step, not auto-executed** — the system surfaces the verdict
and the recommended rotation; the owner approves it as an IPS amendment. Until confirmed,
the system holds (no new BUYs) rather than continuing to trade against its own verdict.

---

## 4. Risk Tolerance & Time Horizon

| Dimension | Policy |
|-----------|--------|
| **Time horizon** | 9–12 months primary holding horizon; positions underwritten on a 9–12 month fundamental thesis |
| **Drawdown tolerance** | Hard kill switch at **−20% portfolio drawdown from peak** → blocks all new BUYs (SELLs still permitted). Graduated de-risking optional (§7.6) |
| **Single-name loss limit** | Hard stop-loss at **−25% from entry** (cost basis, daily-close, no trailing) — a catastrophe brake, not a thesis tool |
| **Volatility** | No explicit vol target; risk is controlled via position/sector caps and the kill switch. Risk *contribution* is monitored and reported (§7.5) even though not hard-enforced |
| **Liquidity** | Universe restricted to liquid names (Appendix A admission floor); the book must be fully liquidatable within one trading day at negligible impact |
| **Tax sensitivity** | **CA top-bracket taxable account** — short-term gains taxed ~54%, long-term ~37%. After-tax return is the only return that counts; turnover is treated as a primary risk |

---

## 5. Eligible Universe

| Rule | Policy |
|------|--------|
| **Instruments** | Long-only common stock and ADRs of publicly-traded companies |
| **Prohibited** | Shorts, options, futures, leverage, margin, crypto, derivatives of any kind |
| **Universe size** | Target ~400 names (expandable from 100), **gated on fundamental-coverage ≥ 80%** |
| **Admission floor** | Market cap ≥ **$10B** AND ~30-day average daily dollar volume ≥ **$50M**, US-listed |
| **Exclusions** | `BLOCKED_TICKERS` (e.g., TSLA per existing policy); names below the admission floor; names inside their 90-day re-entry block |
| **Data requirement** | A name is candidate-eligible only with sufficient history (≥ 22 bars) and, for the post-shift fundamental strategy, real fundamental coverage |

---

## 6. Portfolio Construction Parameters

| Parameter | Limit | Control |
|-----------|-------|---------|
| **Holdings count** | 8–15 positions | PM target; monitored |
| **Max position** | **10%** of portfolio | `guardrails.py` clamp, qty recomputed |
| **Max sector** | **25%** of portfolio | `enforce_sector_limits` (SELLs applied before BUYs) |
| **Cash target** | **0–10%** (discipline flag > 15% idle) | Observability only — cash is a position; never force deployment |
| **Concentration** | Correlation-aware — five correlated names is one bet; CRO receives the return-correlation matrix | CRO review |

---

## 7. Trading & Rebalancing Policy

### 7.1 Cadence
- **Rebalance:** weekly, fixed **Wednesday** (Thu/Fri catch-up if data is stale), once
  per ISO week, full 7-agent pipeline. *Start weekly; revisit monthly once the harness
  shows whether weekly over-trades.*
- **Risk-watch:** every other weekday — deterministic, **SELL/HOLD only, never a BUY,
  never the LLM** — on the same execution envelope.

### 7.2 Minimum holding period
**~30 trading days** soft floor before a discretionary SELL. Risk exits (stop-loss,
tripped invalidation, kill switch) are **exempt**.

### 7.3 Wash-sale / re-entry discipline
No re-buy of a name sold within **30 calendar days**. Exited names carry a **90-day**
recall window for re-entry context.

### 7.4 Tax-aware hold (long-term boundary)
When a position is in gain and within **~30 trading days of its 1-year mark** (per-lot,
FIFO), the PM strongly prefers HOLD over a discretionary trim, to convert a ~54%
short-term gain into a ~37% long-term gain. Risk exits remain exempt.

### 7.5 Exit discipline
A SELL requires a **tripped invalidation OR a real measured change OR a risk trigger** —
**never** a daily alpha re-rank, "a superior opportunity," or a price blip. Exit
conditions are pre-committed at entry and evaluated against entry, not against last week.

### 7.6 Risk triggers (risk-watch, objective, no LLM)
Kill switch active (>20% drawdown); hard stop-loss breached (−25% from entry); a tripped
*quantitative, machine-checkable* `invalidates_if`. Nothing qualitative is a risk-watch
trigger.

---

## 8. Benchmark & Performance Measurement

| Item | Policy |
|------|--------|
| **Primary benchmark** | SPY (total return), measured **after tax** |
| **Required secondary** | QQQ (total return) — the book is tech/mega-cap-tilted, so beating SPY while losing to QQQ is not real alpha |
| **Additional baselines** | equal-weight top-N, random-N, buy-and-hold the initial book, and the **quant-only shadow** (LLM removed) |
| **Attribution** | Brinson allocation/selection/interaction + factor attribution — returns must be explained, not just measured |
| **Counterfactual** | Forward performance of **rejected/vetoed names** is tracked — the CRO and Devil's Advocate must be measurably right to be load-bearing |
| **Risk-adjusted** | Sharpe, Sortino, and information ratio vs the benchmark — **return alone is insufficient** (beating SPY's return at 2× its vol is not a win) |
| **Return methodology** | **Time-weighted return (TWR)** to neutralize deposits/withdrawals (resolves the "deposits inflate the peak" distortion); money-weighted IRR reported alongside for the owner's actual $ experience. GIPS-style discipline |
| **Tax reconciliation** | The after-tax figure (`cost_model`/`tax_lots`) is an *estimate*; the **broker's realized P&L / 1099 is authoritative.** Reconcile quarterly and at year-end — the headline must tie to broker-reported gains |
| **Benchmark rigor** | SPY/QQQ are the headline; also report vs a **risk-matched / blended** benchmark reflecting the book's actual exposure, so outperformance isn't just factor beta |
| **Breadth ceiling** | Per Grinold's Fundamental Law (IR ≈ IC × √breadth), low breadth structurally caps achievable risk-adjusted outperformance **even with genuine skill** — disclosed in the verdict (plan §7.6) |
| **Honesty** | After-tax, after-cost, vs the right benchmark, with a `NOT_SIGNIFICANT` flag below the power bar; survivorship and cash-drag caveats disclosed |

---

## 9. Risk Management & Stress Response

- **Kill switch:** −20% drawdown from peak blocks new BUYs (see §4). After a deposit,
  reset `portfolio_peak.json` to the post-deposit value (deposits inflate raw value).
- **Single-name stop:** −25% from entry (§7.6).
- **Market-wide crisis safe-mode:** on a defined market stress signal (e.g., index down
  > X% intraday, trading halt, or a VIX-level breach), the system enters **safe-mode** —
  halt all new BUYs, allow only risk-driven SELLs, and alert the owner. Thresholds in
  Appendix A.
- **Data-quality gate:** no decision is made on data below the floor (§11 / plan §15).

---

## 10. Model Governance (the agents are models)

Because the decision-makers are AI models, this program maintains a **Model Risk
Management** regime per SR 11-7 principles: a model register, validation status, ongoing
performance monitoring, **model/prompt version recording**, and a decommission criterion
for every model (the 7 agents + the quant composite). Unvalidated models **do** run in
the trivial live book (§3.4); validation governs whether they are relied upon for a
*capital-scaling* decision, not whether they trade. The underlying LLM version and each
agent's prompt version are **recorded on every decision** and changed only by **governed
adoption — A/B in shadow, never a silent swap** (§12 / plan §18.4) — so a provider model
update captures genuine improvement without silently confounding the measurement or
introducing a task-specific regression. The living inventory is
**[MODEL_REGISTER.md](MODEL_REGISTER.md)**; the full design regime is in
[STRATEGY_REDESIGN_PLAN.md](STRATEGY_REDESIGN_PLAN.md) §18.3.

---

## 11. Monitoring, Reporting & Review

- **Every run** writes a data-quality report and a health record; any below-floor metric
  raises an alert (plan §15). Silence is treated as failure (heartbeat monitor).
- **Weekly** pipeline-integrity digest.
- **Quarterly** investment review: realized after-tax vs benchmarks, attribution,
  thesis-correct rate, counterfactual (rejected-name) performance, model-register status,
  and any IPS exceptions.
- **Reconciliation:** broker fills reconciled against intended orders every run; drift
  detected and corrected before it compounds. **Tax/P&L reconciliation** to the broker's
  realized gains / 1099 quarterly and at year-end (§8 — the broker is authoritative).
- **Incident management:** detect → triage (P0–P3) → resolve → **blameless post-mortem**
  → preventive action, with a tracked incident log (plan §18.9).
- **Vendor-risk register** (Polygon, SEC/FMP, Robinhood MCP, Anthropic, Supabase)
  reviewed each quarter for deprecation/outage exposure (plan §18.8–18.9).

---

## 12. Amendment & Change Control

- This IPS and every parameter in Appendix A are **version-controlled**. No limit may be
  changed by code or prompt — only by amending this document.
- Each amendment records: effective date, rationale, approver (owner), and the prior
  value. Performance is **segmented by parameter regime** so a change never silently
  contaminates the measurement (plan §18.4).
- Strategy/parameter changes are stamped with a `policy_version` that the harness
  consumes; no backtest or forward comparison spans a version boundary unmarked.
- Material changes restart the relevant evaluation clock (§3.3).

---

## Appendix A — Authoritative parameter table (single source of truth)

> Code and prompts SHOULD derive from this (`policy.yaml`). Changing a value here is the
> only sanctioned way to change a limit.

```yaml
policy_version: "1.0"
effective_date: "2026-06-27"

account:
  type: cash_individual
  agentic_allowed: true

universe:
  size_target: 400
  expansion_gated_on_fundamental_coverage_pct: 80
  admission_market_cap_min_usd: 10_000_000_000
  admission_adv_min_usd: 50_000_000
  min_history_bars: 22
  blocked_tickers: ["TSLA"]

construction:
  holdings_min: 8
  holdings_max: 15
  max_position_pct: 10
  max_sector_pct: 25
  cash_target_pct: [0, 10]
  cash_discipline_flag_pct: 15

horizon:
  primary_months: [9, 12]

trading:
  rebalance_weekday: 2          # Wednesday; Thu/Fri catch-up
  rebalance_frequency: weekly   # revisit monthly
  min_holding_trading_days: 30  # risk exits exempt
  wash_sale_calendar_days: 30
  exited_recall_calendar_days: 90
  tax_aware_hold_window_trading_days: 30   # before 1-year mark, per-lot FIFO

risk:
  kill_switch_drawdown_pct: 20
  single_name_stop_pct: 25       # from entry, daily-close, no trailing
  safe_mode_index_intraday_drop_pct: 7     # market-wide circuit breaker (tunable)
  safe_mode_vix_level: 40                   # tunable

benchmark:
  primary: SPY_total_return_after_tax
  required_secondary: QQQ_total_return
  baselines: [equal_weight_topn, random_n, buy_and_hold_initial, quant_only_shadow]

evaluation:
  window_months: 12
  risk_adjusted_required: true        # Sharpe / information ratio, not raw return
  return_methodology: time_weighted   # money_weighted (IRR) reported alongside
  tax_source_of_truth: broker_1099    # internal model reconciled to it
  fund_if: "after_tax risk_adjusted beats SPY AND QQQ (significant) AND beats quant_only_shadow"
  abandon_if: "underperforms SPY/QQQ hold after tax past window"

capital_ladder:
  rung0_capital: insignificant        # ~$500, now
  scaling: owner_decision_via_amendment
  terminal_action_on_abandon: rotate_to_benchmark_owner_confirmed

resilience:
  scenario_stress: [gfc_2008, covid_2020, rate_shock_2022]
  vendor_risk_register: true
  incident_severity_levels: [P0, P1, P2, P3]
  price_outlier_quarantine_pct: 35    # >35% 1-day move w/o corporate action = suspect print

data_quality_floors:
  universe_fetch_pct_degraded: 95
  universe_fetch_pct_abort: 80
  fundamentals_coverage_pct_floor: 80
  factor_coverage_pct_floor: 80
  forecast_feed_max_age_runs: 3
  dossier_built_from_days_min: 2

tax:
  jurisdiction: CA_top_bracket
  short_term_rate_pct: 54
  long_term_rate_pct: 37
```

---

## Appendix B — Amendment log

| Version | Date | Change | Rationale | Approver |
|---------|------|--------|-----------|----------|
| 1.0 | 2026-06-27 | Initial adoption | Codify the Rev 3 strategy redesign decisions into a single governed mandate | Owner |
