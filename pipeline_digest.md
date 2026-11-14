# Pipeline-Integrity Digest — week ending 2026-11-14

_Window: 2026-11-08 .. 2026-11-14 · §15.5 weekly summary_

## Data quality
- **Runs logged:** 3
- **Fundamental coverage:** last 97.2% (min 97.2%, max 98.8%)
- **Data-quality score:** avg 100.0 (min 100)
- **Status mix:** {'OK': 3}

## Run health
- **Health rows:** 0
- **Status mix:** —
- **Abort/Fail rate:** —

## ✅ No degraded/aborted data-quality days this window

## 🚨 Weekly rebalance (2026-W46): NONE recorded (last: 2026-W45 on 2026-11-04) — the heartbeat's Friday check should have alerted

## Cash drag vs SPY (over-band cash)
- **Cumulative: $+9.66** (cost) · avg excess cash 26.6% above the 10% band · 33 periods

## Stage C readiness
- **ACCUMULATING — not enough evidence to decide yet**
  - ⏳ `quant.composite_score@21d` — ic=-0.132 ci=±0.255 n_eff=59
  - ✅ `persist_mean.composite_7d_mean@21d` — ic=-0.019 ci=±0.055 n_eff=1265
  - ✅ `event_present.flag@21d` — ic=-0.008 ci=±0.055 n_eff=1265

## Scale-rule evidence
- **Gate 1a** (`3.0-beta-neutral`, full universe): 2 non-overlapping period(s), mean IC -0.0661, 95% CI [-0.6048, 0.4726], n_obs 196 → **NOT_YET** · sector-neutral mean IC -0.1223
- **Gate 1b** `pm_selected@21d`: demeaned gap -0.02855, q_bh 0.7845 (family of 15) → NOT_SIGNIFICANT
- **Gate 1b** `da_reject@21d`: demeaned gap 0.0066, q_bh 0.9611 (family of 15) → NOT_SIGNIFICANT

_Generated 2026-11-14T02:30:49_
