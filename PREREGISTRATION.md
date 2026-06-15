# Pre-Registration — AI Investor Calibration Ledger Primary Metric

**Project:** Deliberative Multi-Agent LLM Architecture for Autonomous Equity
Portfolio Management
**Author:** Parth Choksi (Independent Researcher)
**Registered:** AsPredicted #296637 — https://aspredicted.org/zm7a2p.pdf
(public, immutable, timestamped 2026-06-15).
**Status:** committed before any per-agent skill number is reported publicly.
**Source of truth:** the values below are encoded in `calibration.py`
(`PRIMARY_METRIC`, `PRIMARY_HORIZON`, `BH_ALPHA`) and `performance.py`
(`MIN_SIGNIFICANT_DAYS`); this document and the code are intended not to drift.

---

## 1. Study type
Observational forecast-calibration study of an already-deployed system. No
manipulation; we score numeric agent/quant forecasts against realized forward
returns. This pre-registration fixes the **primary metric, horizon, benchmark,
and decision threshold before reporting**, to prevent data dredging across the
multiple (agent, field) series the ledger computes.

## 2. Primary hypothesis
The deterministic quant composite score has positive cross-sectional predictive
rank-association with realized forward returns over the stated horizon.

## 3. Primary metric (ONE, pre-committed)
The **block-sampled Spearman rank information coefficient (IC)** of
`quant.composite_score` against the realized forward return.

- **Entry (return base):** the **next-session open** after the signal date — the
  first executable price — derived at scoring time. The signal-day close is NOT
  used as the entry (it is non-executable; using it induces one-bar look-ahead).
- **Exit:** the close on/after entry + horizon.
- **Horizon:** 21 days.
- **Independence correction:** observations are block-sampled to be
  non-overlapping (≥ horizon spacing per ticker) so the ~21-day return windows do
  not overlap; all confidence intervals and p-values use this **effective N**, not
  the raw overlapping daily count.

## 4. Secondary (exploratory) metrics
ICs and sign-hit-rates for the other agents/fields (research confidence,
earnings-alpha score, Devil's-Advocate risk score, position hold score). These
are **exploratory**. Across all metrics a **Benjamini-Hochberg** correction is
applied (α = 0.05). No secondary metric will be reported as confirmatory.

## 5. Benchmark
- SPY on a **total-return** basis (dividend-inclusive), matching the portfolio's
  total-value curve — NOT price-return SPY.
- Plus factor ETFs for context: MTUM (momentum), USMV (low-vol), QUAL (quality),
  RSP (equal-weight).
- Average net exposure and realized beta are reported alongside, since the book
  holds cash and is not fully invested.

## 6. Sample-size / reporting threshold
No edge or per-agent-skill claim is published before **≥ 60 trading days** of
accrued, matured, non-overlapping observations. Below that threshold all outputs
are labeled "not statistically significant — plumbing, not proof." Strong
inference is expected to need 252+ trading days.

## 7. Decision / success rule
The primary hypothesis is supported only if the block-sampled primary IC is
**positive and significant after Benjamini-Hochberg adjustment (α = 0.05)** at or
beyond the 60-trading-day threshold. A null or negative primary IC is reported as
such. The primary metric, horizon, entry definition, and benchmark **will not be
changed** after observing results.

## 8. Known biases disclosed (not corrected by this registration)
Survivorship in the current-membership universe (historical ICs upward-biased);
restatement-contaminated EDGAR fundamentals; range restriction from scoring a
pre-filtered candidate set; LLM pre-training contamination of any backtest. These
are disclosed in the paper (§3.3, §3.7, §6.4–6.5) and bound the interpretation.

---

*Once registered, the OSF/AsPredicted URL is recorded in PAPER_DRAFT §4.4 and in
the calibration scorecard `_meta`.*
