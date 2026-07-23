# Plan — Free full-universe valuation via SEC EDGAR (retire the FMP free-tier gap)

**Status:** proposed (2026-07-23). **Owner decision required** before build (see §10).
**Type:** deterministic signal-layer change → touches the live candidate-selection
composite → **`FORMULA_VERSION` bump + backtest-before-deploy** (evidence-gated).

## 1. Goal

Make the **valuation factor real for ~the full US universe, for free**, by *deriving*
P/E, FCF-yield, and EV/EBITDA from the SEC EDGAR `companyfacts` payload `SECProvider`
already downloads, combined with the Polygon price already in the snapshot — instead
of relying on FMP's free tier, which only returns them for ~mega-caps (**32 of 177
names, ~18%**, on the Jul 23 2026 snapshot).

This finishes the SEC migration: quality (margins/leverage) already moved to EDGAR at
~96% coverage; **valuation never did** — `SECProvider.fundamentals()` explicitly omits
it ("require price data ... use FMPProvider"). Valuation has been FMP-gated the whole
time. That is the residual gap this plan closes.

## 2. Why SEC EDGAR (vs other free options)

Every input the three ratios need is **already in the `companyfacts` payload we fetch**
(net income, diluted EPS, shares, operating cash flow, capex, cash, debt, D&A) — plus
we already have the price. No new key, no new dependency, ~100% US-equity coverage
(same as the quality factors).

Rejected free alternatives: **Finnhub** (60/min free but another key/dependency),
**Alpha Vantage** (25 req/day free — too throttled for 177 tickers), **Yahoo/yfinance**
(blocked, 401 auth — CLAUDE.md), **Polygon financials** (we have the key, but it's raw
SEC-derived statements — duplicates EDGAR with a tighter rate limit).

## 3. Current state (grounded in code)

| Piece | Where | Behavior today |
|---|---|---|
| SEC extraction | `data_providers.SECProvider.fundamentals()` | Pulls `Revenues`, `GrossProfit`, `OperatingIncomeLoss`, `StockholdersEquity`, `LongTermDebt` via `_latest_annual()`; returns `gross_margin` / `operating_margin` / `debt_to_equity` + `_as_of_filing`. **Omits valuation.** |
| Extraction helper | `SECProvider._latest_annual(*concepts)` | (value, filed_date) for the most-recent 10-K USD value of the first matching XBRL concept. **Reusable as-is** for the new concepts. |
| Enrichment/merge | `market_data._enrich_with_provider()` | Alternate-day 50/50 cache → `provider.fundamentals(t)` cached in `provider_cache.json`, merged into `snapshot["fundamentals"][t]`. **No price in scope here.** |
| Ratio scoring | `quant_engine.compute_valuation_score(fundamentals)` | Reads `pe_ratio` / `fcf_yield` / `ev_ebitda`; buckets each; `valuation_available = bool(scores)`. **No change needed.** |
| Composite | `quant_engine.score_all_tickers()` | Honest renormalization — valuation blended where present, dropped per-ticker where absent. **No change needed.** |
| Provenance key | `quant_engine.FORMULA_VERSION` = `"2.0-quality-tilt"` | Groups factor-persistence / IC. Must bump when the composite meaningfully changes. |

## 4. Design — where each piece lives

**The pivot:** valuation ratios need **today's price**, which is *not* in scope where
`provider.fundamentals()` is cached (and price moves daily, so the ratio isn't
cacheable). So split cleanly:

1. **`SECProvider` returns price-INDEPENDENT raw components** (cacheable, per-filing,
   no-look-ahead-stamped) alongside the existing margins/leverage. New keys, all
   underscore-prefixed intermediates (not consumed by the score directly):
   `_eps_diluted_annual`, `_shares_diluted`, `_fcf_annual`, `_total_debt`, `_cash`,
   `_ebitda_annual`. Cached in `provider_cache.json` exactly like the current fields.

2. **A new derive step combines components + price → ratios**, run AFTER the snapshot's
   prices AND fundamentals both exist (in `fetch_snapshot.py`, so the *committed*
   snapshot carries the ratios — consistent with how the quant scores already use the
   snapshot's close price). New pure function:
   `market_data.derive_valuation_ratios(prices, fundamentals) -> None` (mutates
   `fundamentals[t]`, writing `pe_ratio` / `fcf_yield` / `ev_ebitda` **only** when the
   components + a positive price are present and pass the guards, and **only when the
   ticker doesn't already have that ratio from FMP** — FMP-first, SEC fills the gap).

3. **`compute_valuation_score` unchanged** — it just sees `pe_ratio` / `fcf_yield` /
   `ev_ebitda` populated for far more names → `valuation_available` flips True → the
   honest composite blends the existing 0.25 valuation weight for those names. **The
   weights never change; only coverage of the existing valuation slot grows.** That is
   the "lights up coverage without a risky live re-weight" property.

```
SECProvider.fundamentals(t)         fetch_snapshot.py
  ├─ gross/operating_margin, d/e  ── (as today) ──┐
  └─ _eps, _shares, _fcf,          derive_valuation_ratios(prices, fundamentals)
     _total_debt, _cash, _ebitda ──────────────►  pe_ratio / fcf_yield / ev_ebitda
        (cached, no-look-ahead)                         │  (into snapshot["fundamentals"])
                                                        ▼
                                    quant_engine.compute_valuation_score  (UNCHANGED)
```

## 5. XBRL concept mappings (the bulk of the work — tag-fallback robustness)

Reuse `_latest_annual(g, *concepts)` (10-K, annual, no-look-ahead filed date). Fallback
lists mirror the existing `Revenues` pattern (companies tag the same concept differently):

| Component | us-gaap concept (first match wins) |
|---|---|
| Diluted EPS | `EarningsPerShareDiluted` → `EarningsPerShareBasic` |
| Net income (P/E via mktcap path / EPS cross-check) | `NetIncomeLoss` |
| Diluted shares | `WeightedAverageNumberOfDilutedSharesOutstanding` → `WeightedAverageNumberOfSharesOutstandingBasic` → `CommonStockSharesOutstanding` |
| Operating cash flow | `NetCashProvidedByUsedInOperatingActivities` → `NetCashProvidedByUsedInOperatingActivitiesContinuingOperations` |
| Capex | `PaymentsToAcquirePropertyPlantAndEquipment` → `PaymentsToAcquireProductiveAssets` |
| Cash & equivalents | `CashAndCashEquivalentsAtCarryingValue` → `CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents` |
| Short-term debt (add to LTD) | `LongTermDebtCurrent` → `DebtCurrent` → `ShortTermBorrowings` (0 if none) |
| D&A (for EBITDA) | `DepreciationDepletionAndAmortization` → `DepreciationAmortizationAndAccretionNet` → (`Depreciation` + `AmortizationOfIntangibleAssets`) |

`OperatingIncomeLoss` and `LongTermDebt` are already extracted — reuse them.

## 6. Ratio formulas + guards (units must match `compute_valuation_score`)

- `market_cap = price × _shares_diluted`
- `pe_ratio  = price / _eps_diluted_annual`   — **guard `_eps_diluted_annual > 0`** (negative/zero EPS → omit; the score also guards `pe > 0`)
- `_fcf_annual = _cfo_annual − _capex_annual`  (capex is reported positive → subtract)
- `fcf_yield = _fcf_annual / market_cap`       — market_cap > 0; fcf may be negative (score buckets ≤0 at 10)
- `ev = market_cap + _total_debt − _cash`
- `_ebitda_annual = OperatingIncomeLoss + _dna_annual`
- `ev_ebitda = ev / _ebitda_annual`            — **guard `_ebitda_annual > 0`**

Round to match FMP output (`pe`/`ev` 2dp, `fcf_yield` 4dp). Emit a ratio **only** when
its inputs are all present and pass guards — otherwise leave absent (honest N/A, never a
fake 50).

## 7. No-look-ahead & basis consistency

- **No-look-ahead:** each component carries its 10-K `filed` date via `_latest_annual`;
  `_as_of_filing` = max over the used inputs (existing mechanism). Price is current →
  no look-ahead. A historical replay must use the latest 10-K filed **≤ the replay
  date** + the price at that date — the `_as_of_filing` stamp already supports this;
  `build_dossier`'s `> as_of` drop already enforces it.
- **Basis:** SEC-derived uses **latest annual (10-K)**, matching the existing margins.
  FMP valuation is **TTM**. → §10 decision: (A) FMP-first, SEC fills gaps (mixed
  TTM/annual basis — coarse buckets rarely cross, but document it), or **(B, preferred)
  make SEC-derived the single valuation source for consistency** (annual everywhere,
  full coverage, one basis) and keep FMP **only for the earnings calendar** (SEC has
  none). B removes the mixed-basis wart and the FMP-valuation dependency entirely.

## 8. `FORMULA_VERSION` + evidence clock

Populating valuation for ~the full universe **materially changes the composite** for
many names → **bump `FORMULA_VERSION`** (e.g. `"2.1-valuation-live"`). This **resets the
factor-persistence / IC evidence clock** (P0-2 grouping) — *acceptable and expected*
here because the signal genuinely changed (unlike the P1 case, where a bump for zero
benefit was correctly declined). Update the policy-parity oracle if it asserts the
version string.

## 9. Validation — backtest before deploy (honest about limits)

1. **Unit tests** (`data_providers` + `market_data` + `quant_engine`):
   - SEC component extraction from a fixture companyfacts payload (each tag + fallbacks;
     missing-concept → omit; negative EPS → no `pe_ratio`; zero/neg EBITDA → no
     `ev_ebitda`).
   - `derive_valuation_ratios`: known components + price → exact ratios; missing price
     or component → ratio absent; FMP-present ticker not overwritten (option A) / basis
     rule (option B).
   - `compute_valuation_score` / `score_all_tickers`: `valuation_available` flips True;
     composite renormalizes with valuation blended; parity oracle at the new version.
   - No-look-ahead: a component whose `filed > as_of` is dropped in replay.
2. **Backtest** (`python -m backtest`): re-run momentum/quality/**now-live-valuation**
   composite after costs + CA tax vs SPY. **Honest caveat:** a *true* point-in-time
   valuation backtest needs **historical fundamentals** (per-date 10-K + price). We
   store composite scores in `factor_history.jsonl`, **not raw historical
   fundamentals** — so the harness can validate the *scoring mechanics* on current
   fundamentals, but a rigorous historical valuation-IC study needs either a
   fundamentals-history store or a vendor point-in-time set. State this in the report;
   do not claim an edge the data can't support. This is the same "measure, don't assert"
   discipline as the Stage-C readiness gate.
3. Coverage check: `derive_valuation_ratios` should lift valuation coverage from ~18%
   toward the SEC quality-coverage level (~90%+); log before/after in
   `data_quality_report`.

## 10. Decisions (owner-approved 2026-07-23)

1. **Valuation source (§7): DECIDED → Option B.** SEC-derived is the **single valuation
   source** (consistent annual basis, full free coverage); **FMP is kept only for the
   earnings calendar** it uniquely provides. Drops the FMP-valuation dependency
   entirely. → Phase 3 (§11) is now in-scope, not optional.
2. **Deploy gating: DECIDED → Ship.** Deploy on green unit tests + a scoring-mechanics
   backtest. The honest-composite bounds the risk (it only *adds* real data where we
   had N/A), so we deploy behind the `FORMULA_VERSION` bump and **watch** the IC via
   `stage_c_readiness` rather than blocking on a fundamentals-history study first.
3. **TTM basis: DECIDED → defer, but COMMITTED as the immediate next work.** Ship
   annual-basis first (simplest, matches the existing margins); **TTM (sum of the last 4
   10-Qs) begins as soon as this plan lands** — see Phase 4 (§11), which is a committed
   follow-up, not a maybe.

## 11. Rollout / phasing (each shippable, reversible)

- **Phase 1 — extraction (zero behavior change): ✅ SHIPPED (2026-07-23, commit
  `5ac70eb`).** Added the raw components to `SECProvider` (new underscore keys).
  Cached but unused → composite identical. Verified against live EDGAR (coverage,
  and the honest-N/A behavior for financials). `/code-review high` pre-commit found 5
  correctness bugs (a cross-field vintage-mismatch class the within-field
  `prefer_recent` fix didn't cover — e.g. combining `cfo`/`capex` or `ltd`/`std` from
  two different fiscal years into one metric; caught live in JPM, whose `LongTermDebt`
  tag is frozen at FY2013); 4 fixed same-commit (regression tests added), 1 documented
  as an accepted, near-zero-population edge case. See `RELEASE_NOTES.md`'s Phase-1
  entry for the full remediation writeup.
  - **Deferred cleanup (found by the same review pass, not applied — low priority, no
    correctness impact, revisit in a dedicated cleanup pass rather than blocking
    Phase 2):**
    - `data_providers.py:354-397` — the ~9 near-identical
      `_latest_annual_ex(g, concept-list, unit=..., prefer_recent=True)` calls in
      `fundamentals()` are written out longhand rather than iterating a small
      `(name, concepts, unit)` spec table.
    - `data_providers.py:266-282` (`_latest_annual_ex`'s `prefer_recent` branch) —
      hand-rolls a running max (`best = None` / `if best is None or key > best[0]`)
      instead of collecting per-concept candidates and calling
      `max(candidates, key=..., default=None)`.
    - `market_data.py:194-195` vs `data_providers.py`'s `_fcf_annual` — the
      Polygon-path `fcf_margin` (`ocf - abs(capex)`) and the new SEC-path
      `_fcf_annual` (`cfo - capex`) reimplement the same "operating cash flow minus
      capex" formula independently, with different capex-sign handling (`abs()` vs.
      relying on capex already being reported as a positive outflow) — worth a shared
      helper once Phase 2 makes both paths live simultaneously.
- **Phase 2 — derive + light up (the behavior change):** add
  `derive_valuation_ratios`, wire into `fetch_snapshot.py`, bump `FORMULA_VERSION`,
  backtest, deploy. Reversible by reverting the derive wiring (components go back to
  unused; version reverts).
- **Phase 3 — single-source (option B, in-scope per §10.1):** route valuation entirely
  through SEC-derived; keep FMP for the calendar only. Removes the mixed-basis wart and
  the FMP-valuation dependency.
- **Phase 4 — TTM basis (committed follow-up, per §10.3):** replace annual (10-K) inputs
  with trailing-twelve-months (sum of the last 4 10-Q `us-gaap` flow figures; balance
  items stay point-in-time latest). Flow concepts (revenue, net income, CFO, capex, D&A,
  operating income) become TTM; EPS becomes TTM diluted. Starts as soon as Phases 1–3
  land. Same discipline: `FORMULA_VERSION` bump (`2.2-valuation-ttm`) + backtest. No-
  look-ahead unchanged (each quarter carries its own `filed` date; TTM is available only
  once the 4th quarter is filed).

## 12. Risks & mitigations

| Risk | Mitigation |
|---|---|
| XBRL tag variability → sparse coverage for some names | Fallback lists (§5); honest N/A when absent (never fake); coverage logged in `data_quality`. |
| Mixed TTM (FMP) / annual (SEC) basis | Option B (single annual source) removes it; else document — coarse buckets rarely cross. |
| Evidence clock reset on the version bump | Expected for a real signal change; the persistence/IC partition (P0-2) handles it cleanly; monitor `stage_c_readiness`. |
| Backtest can't prove historical valuation edge (no fundamentals history) | State the limit honestly; the honest-composite means the change only *adds real data* where we had a neutral gap, so downside is bounded even pre-proof. |
| ADR / 20-F filers with thin us-gaap XBRL | Same as today's margins — they simply stay N/A for valuation; no regression. |
| `dei` shares namespace | Use us-gaap share concepts (§5) to avoid expanding `_get_us_gaap`; or extend it to also return `dei` if needed. |

## 13. Files touched (estimate)

`data_providers.py` (SECProvider extraction — most of the work), `market_data.py`
(`derive_valuation_ratios` + wire into the snapshot build), `fetch_snapshot.py` (call
the derive step), `quant_engine.py` (`FORMULA_VERSION` bump only), `test_pipeline.py`
(new tests), `backtest/` (re-run + report caveat), `RELEASE_NOTES.md` + CLAUDE.md
(coverage docs). **No order-placement / idempotency / envelope code.**

## 14. Additional SEC data — evaluation & factor roadmap

The question: *beyond valuation, what else does SEC give us for free that's worth
using?* A lot — and the marginal cost is low, because **Phases 1–4 already open up
`SECProvider` to parse the `companyfacts` payload**, so most of the signals below reuse
the exact same fetch + `_latest_annual` machinery. But this is a signal-layer change to a
live strategy, so the discipline holds: **each new factor is a hypothesis, added one
validated cluster at a time (`FORMULA_VERSION` bump + backtest/IC), never a laundry list
dumped into the composite at once** — correlated factors don't add breadth, they just
re-weight an existing bet and make attribution impossible.

### 14a. Same payload we're already parsing (near-free — the high-value adds)

Grouped by **orthogonal cluster** (correlation *within* a cluster is high, so pick the
strongest per cluster rather than stacking all):

| Signal | Formula (all from `companyfacts`) | Evidence | Cluster / orthogonality | Verdict |
|---|---|---|---|---|
| **Gross profitability** | `GrossProfit / Assets` | Novy-Marx 2013 — the most robust quality factor; often beats margins | Quality (we already have GrossProfit; need `Assets`) | **ADOPT** — fold into the QUALITY factor; strongest single add |
| **Asset growth** | ΔTotal `Assets` YoY | Cooper-Gulen-Schill 2008 — strong **negative** predictor | **NEW cluster: investment/anomaly** (orthogonal to mom/qual/val) | **ADOPT** — anchor of a new factor |
| **Net share issuance** | Δ diluted shares YoY (buyback = +, issuance = −) | Pontiff-Woodgate, Daniel-Titman — issuance → low returns | Investment/anomaly (shares already extracted for valuation) | **ADOPT** — nearly free |
| **Accruals (Sloan)** | `(NetIncome − CFO) / Assets` | Sloan 1996 — high accruals → low returns (earnings quality) | Investment/earnings-quality (NI, CFO already extracted) | **ADOPT** — nearly free |
| **Piotroski F-score** | 9 binary signals (profitability/leverage/efficiency) | Piotroski 2000 — works within value | Composite that *overlaps* the four above | **DEFER** — build after the individual factors; it re-bundles them |
| ROE / ROIC / rev & EPS growth | NI/equity; NOPAT/IC; ΔRev, ΔEPS | Quality/growth, but highly correlated with gross profitability + momentum | Redundant with existing quality + momentum | **REJECT (low marginal breadth)** |

**Recommended first factor batch (post-valuation):** (1) **gross profitability** into the
quality factor, and (2) a **new "investment" factor** = a small composite of *asset
growth + net issuance + accruals* (all annual, all orthogonal to mom/qual/val, all
free from the payload we're already parsing). That genuinely adds a *new dimension* of
breadth — the thing the composite most lacks — rather than thickening an existing bet.

### 14b. Separate EDGAR endpoints (real value, more effort — a distinct fetch/parse)

| Source | Signal | Evidence | Effort | Verdict |
|---|---|---|---|---|
| **Form 4** (`data.sec.gov`, insider transactions) | Insider buying/selling, esp. *opportunistic* cluster buys | Lakonishok-Lee 2001, Cohen-Malloy-Pomorski 2012 — insider purchases predict returns | New endpoint + Form-4 XML parse, dedup, aggregate per ticker | **ADOPT LATER** — the standout Tier-2 signal; orthogonal, catalyst-oriented, free |
| **8-K item codes** (submissions metadata) | Red-flag events: **Item 4.02** (restatement / non-reliance), going-concern, **Item 5.02** (exec departures) | Restatements/going-concern strongly predict underperformance | Parse submissions index + item codes | **ADOPT LATER as a RISK SCREEN** (an exclusion/penalty, not a return factor) |
| **13F** (institutional holdings) | Δ institutional ownership | Weak, 45-day lagged, noisy | High parse cost | **REJECT for now** |
| **13D/Schedule 13D** (activist >5% stakes) | Activist involvement (catalyst) | Event-study positive but sparse/lumpy | Moderate | **DEFER** — catalyst enrichment, not a systematic factor |

### 14c. Out of scope (for now)

Full-text NLP of 10-K MD&A / risk-factor *changes* / sentiment — high effort, low
reliability, and the current architecture has no NLP layer. Revisit only if a cheap,
validated signal emerges.

### 14d. Sequencing (each its own version bump + backtest; ship one cluster at a time)

1. **This plan** — valuation live (Phases 1–3), then **TTM** (Phase 4).
2. **Factor batch 1** — gross profitability → quality; new investment factor (asset
   growth + net issuance + accruals). `FORMULA_VERSION 2.3-quality-investment`.
3. **Factor batch 2** — Form 4 insider sentiment (new endpoint).
4. **Risk screen** — 8-K restatement / going-concern penalty (exclusion, not a factor).

Same honest-`N/A`, same no-look-ahead (`filed`-date gating), same measurement-not-faith
gate (watch each factor's forward IC on `factor_history` before trusting it) — and the
same backtest caveat from §9.2 (a rigorous historical IC study needs a
point-in-time **fundamentals-history store**, which becomes the natural infrastructure
prerequisite once more than one fundamental-derived factor is live).
