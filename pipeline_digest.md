# Pipeline-Integrity Digest — week ending 2026-10-31

_Window: 2026-10-25 .. 2026-10-31 · §15.5 weekly summary_

## Data quality
- **Runs logged:** 4
- **Fundamental coverage:** last 98.8% (min 97.2%, max 98.8%)
- **Data-quality score:** avg 100.0 (min 100)
- **Status mix:** {'OK': 4}

## Run health
- **Health rows:** 0
- **Status mix:** —
- **Abort/Fail rate:** —

## ✅ No degraded/aborted data-quality days this window

## Weekly rebalance (2026-W44): ✅ executed on 2026-10-28 — 4 ticker(s) traded

## Cash drag vs SPY (over-band cash)
- **Cumulative: $+9.66** (cost) · avg excess cash 26.9% above the 10% band · 32 periods

## Stage C readiness
- **ACCUMULATING — not enough evidence to decide yet**
  - ⏳ `quant.composite_score@21d` — ic=-0.166 ci=±0.274 n_eff=51
  - ✅ `persist_mean.composite_7d_mean@21d` — ic=-0.012 ci=±0.06 n_eff=1070
  - ✅ `event_present.flag@21d` — ic=0.04 ci=±0.06 n_eff=1070

## Scale-rule evidence
- **Gate 1a** (`3.0-beta-neutral`, full universe): 2 non-overlapping period(s), mean IC -0.0661, 95% CI [-0.6048, 0.4726], n_obs 196 → **NOT_YET** · sector-neutral mean IC -0.1223
- **Gate 1b** `pm_selected@21d`: demeaned gap -0.02634, q_bh 0.9085 (family of 15) → NOT_SIGNIFICANT
- **Gate 1b** `da_reject@21d`: demeaned gap 0.00597, q_bh 1.0 (family of 15) → NOT_SIGNIFICANT

_Generated 2026-10-31T01:40:14_
