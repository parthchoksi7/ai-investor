# Pipeline-Integrity Digest — week ending 2026-10-24

_Window: 2026-10-18 .. 2026-10-24 · §15.5 weekly summary_

## Data quality
- **Runs logged:** 7
- **Fundamental coverage:** last 97.8% (min 95.2%, max 98.2%)
- **Data-quality score:** avg 100.0 (min 100)
- **Status mix:** {'OK': 7}

## Run health
- **Health rows:** 0
- **Status mix:** —
- **Abort/Fail rate:** —

## ✅ No degraded/aborted data-quality days this window

## Weekly rebalance (2026-W43): ✅ executed on 2026-10-21 — 0 ticker(s) traded

## Cash drag vs SPY (over-band cash)
- **Cumulative: $+9.66** (cost) · avg excess cash 27.7% above the 10% band · 31 periods

## Stage C readiness
- **ACCUMULATING — not enough evidence to decide yet**
  - ⏳ `quant.composite_score@21d` — ic=-0.075 ci=±0.364 n_eff=29
  - ✅ `persist_mean.composite_7d_mean@21d` — ic=-0.006 ci=±0.063 n_eff=964
  - ✅ `event_present.flag@21d` — ic=0.03 ci=±0.063 n_eff=964

## Scale-rule evidence
- **Gate 1a** (`3.0-beta-neutral`, full universe): 2 non-overlapping period(s), mean IC -0.0661, 95% CI [-0.6048, 0.4726], n_obs 196 → **NOT_YET** · sector-neutral mean IC -0.1223
- **Gate 1b** `pm_selected@21d`: demeaned gap -0.04371, q_bh 0.377 (family of 13) → NOT_SIGNIFICANT
- **Gate 1b** `da_reject@21d`: demeaned gap 0.00639, q_bh 0.9201 (family of 13) → NOT_SIGNIFICANT

_Generated 2026-10-24T00:50:07_
