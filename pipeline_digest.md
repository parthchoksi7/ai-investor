# Pipeline-Integrity Digest — week ending 2026-10-10

_Window: 2026-10-04 .. 2026-10-10 · §15.5 weekly summary_

## Data quality
- **Runs logged:** 9
- **Fundamental coverage:** last 96.1% (min 95.6%, max 98.2%)
- **Data-quality score:** avg 100.0 (min 100)
- **Status mix:** {'OK': 9}

## Run health
- **Health rows:** 0
- **Status mix:** —
- **Abort/Fail rate:** —

## ✅ No degraded/aborted data-quality days this window

## Weekly rebalance (2026-W41): ✅ executed on 2026-10-07 — 5 ticker(s) traded

## Cash drag vs SPY (over-band cash)
- **Cumulative: $+10.72** (cost) · avg excess cash 29.6% above the 10% band · 29 periods

## Stage C readiness
- **ACCUMULATING — not enough evidence to decide yet**
  - ⏳ `quant.composite_score@21d` — ic=-0.075 ci=±0.364 n_eff=29
  - ✅ `persist_mean.composite_7d_mean@21d` — ic=-0.006 ci=±0.063 n_eff=964
  - ✅ `event_present.flag@21d` — ic=0.03 ci=±0.063 n_eff=964

## Scale-rule evidence
- **Gate 1a** (`3.0-beta-neutral`, full universe): 1 non-overlapping period(s), mean IC -0.1085, 95% CI None, n_obs 98 → **NOT_YET** · sector-neutral mean IC -0.1561
- **Gate 1b** `pm_selected@21d`: demeaned gap -0.04258, q_bh 0.575 (family of 10) → NOT_SIGNIFICANT
- **Gate 1b** `da_reject@21d`: demeaned gap 0.00251, q_bh 0.908 (family of 10) → NOT_SIGNIFICANT

_Generated 2026-10-10T01:49:10_
