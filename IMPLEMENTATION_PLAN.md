# Implementation Plan — AI Investor Redesign Pod

> The build & rollout plan for the Rev 3 strategy redesign + governance pod. Sequences
> every work item from the design docs by **dependency and risk**, offline/zero-live-risk
> work first, live-trade-path changes gated hardest.
>
> **Linked artifacts:** [IPS.md](IPS.md) (mandate / source of truth) ·
> [STRATEGY_REDESIGN_PLAN.md](STRATEGY_REDESIGN_PLAN.md) (design) ·
> [MODEL_REGISTER.md](MODEL_REGISTER.md) (MRM inventory).
>
> | | |
> |---|---|
> | **Version** | 1.0 (2026-06-27) |
> | **Status** | **Phases 0–5 BUILT AND DEPLOYED (2026-06-27 → 2026-07-04); Phase 6 not yet started.** (Updated 2026-07-05 — see §1 status column below. For day-to-day status, prefer `CLAUDE.md`'s Changelog + `MANUAL_TODO.md`'s roadmap table over this doc.) |
> | **Owner lenses** | backend · quant · ml_ai · devops · PM (the five review personas) |

---

## 0. Governing principles for the rollout

1. **"One pod" is the design unit, NOT the release unit.** You do not big-bang a system
   that trades real money. Each phase is **independently deployable, independently
   reversible**, and lands behind the DEPLOYMENT §7.0 gates.
2. **Offline before live.** Phases 0–4 are single-sourcing, measurement, data, and
   observability — **zero live-trade-path risk**. The live-path changes (Phases 5–6) ship
   only after the measurement and safety nets exist to catch them.
3. **Measurement before strategy.** The harness and data-quality gate must work *before*
   the strategy shift, or you can't tell a bad strategy from a starved one (the whole
   point of [IPS.md](IPS.md) §3.3).
4. **Every limit derives from one place.** Phase 0 single-sources `policy.yaml` from IPS
   Appendix A first, so all later phases read limits from one governed source.
5. **Gate by blast radius.** Offline/observability → `/code-review high`. New live SELL
   path / cadence change → **`/code-review ultra` + weekend dry-run + live-routine sync**.

---

## 1. Phase overview

| Phase | Theme | Touches live trade path? | Risk | Gate | Status (2026-07-05) |
|-------|-------|--------------------------|------|------|----------------------|
| **0** | Single-source the limits (`policy.yaml`) | No (parity refactor) | Low | high + parity tests | ✅ Built |
| **1** | Measurement foundation (harness + attribution) | No (offline) | Low | high | ✅ Built |
| **2** | Data layer: coverage fix, composite re-weight, universe | No (offline/backtest) | Low–Med | high | ✅ Built |
| **3** | Observability & alerting (the safety net) | No | Low | high + chaos suite | ✅ Built |
| **4** | Research pipeline: dossier, memory, analyst split | No (GH Actions writes artifacts only) | Med | high | ✅ Built (3 increments) |
| **5** | **Live path: weekly cadence + `risk_watch.py`** | **YES** | **High** | **ultra + dry-run + routine sync** | ✅ **Built + LIVE** (Stages A–D, 2026-07-04); +hardening batch 2026-07-05 |
| **6** | Exit logic & the momentum→fundamental shift | **YES** | High | ultra + forward-test | ⬜ Not started |
| **7** | Governance operationalization (ongoing) | No | Low | high | 🔁 Ongoing (by design) |

---

## 2. Critical path & parallelism

```
        ┌──────────────────────────────────────────────────────────────┐
   P0 ──▶ single source (policy.yaml)  ── unblocks every later phase ────┤
        └──────────────────────────────────────────────────────────────┘
                 │
     ┌───────────┼───────────────┬───────────────────────┐
     ▼           ▼               ▼                        ▼
   P1 harness   P2 data layer   P3 observability   ( P1·P2·P3 run in PARALLEL —
   (measure)    (coverage,      (safety net,         all offline, no live risk )
     │           re-weight)      chaos suite)
     │           │               │
     └─────┬─────┴───────┬───────┘
           ▼             ▼
        P4 research pipeline (dossier + bench)   ◀── needs P1 feed + P2 data + P3 alerts
           │
           ▼
        P5 LIVE: weekly cadence + risk_watch     ◀── needs P4 dossier + P3 safety net
           │
           ▼
        P6 exit logic + momentum→fundamental shift  ◀── needs P2 coverage proven live
           │
           ▼
        P7 governance ops (quarterly review, incident/BCP)  ── runs continuously
```

**Critical path:** P0 → (P1 ∥ P2 ∥ P3) → P4 → P5 → P6. P7 is continuous from P0.
**Fastest value:** P0 + P1 + P3 deliver single-sourcing, the evidence clock, and the
safety net with **zero live risk** — ship these first.

---

## 3. Phase detail

### Phase 0 — Single-source the limits (foundation)
- **Objective:** every limit derives from one governed source; zero behavior change.
- **Work items:**
  - Create `policy.yaml` from [IPS.md](IPS.md) Appendix A.
  - Refactor `guardrails.py` to read limits from `policy.yaml` (not hard-coded constants).
  - Point agent prompts (`analysis.py`) at the same source for any quoted limit.
  - Add `policy_version` loader; stamp it where decisions are logged.
- **Definition of done:** all existing limits identical, sourced from `policy.yaml`.
- **Tests:** parity suite — assert every guardrail behaves bit-identically pre/post.
- **Gate:** `/code-review high`. **Owner:** backend.

### Phase 1 — Measurement foundation (offline, the evidence clock)
- **Objective:** the harness ticks, grades, and attributes — the year-end verdict machinery.
- **Work items:**
  - **Fix `log_forecasts`** (dead since Jun 18) — diagnose the silent stop, repair the feed.
  - Wire `score_matured` + `agent_scorecard` into the run (currently test-only callers).
  - Multi-horizon `{21,63,126,189,252}d`; matured records keyed `(forecast_id, horizon)` (P1-9).
  - §7.5: **counterfactual rejected-name tracking** (log + score vetoed/rejected names);
    **Brinson attribution**; model-register KPI wiring → `signal_scorecard.json`.
  - §7.6: **time-weighted returns**, **broker-1099 reconciliation**, risk-adjusted metrics
    (Sharpe/Sortino/IR), breadth-ceiling reporting, `NOT_SIGNIFICANT` flag.
- **Definition of done:** a run logs forecasts, matures them, and emits a scorecard +
  attribution; the register's KPI columns populate.
- **Tests:** idempotent multi-horizon scoring; TWR vs known curves; attribution sums to
  active return; counterfactual scoring.
- **Gate:** `/code-review high`. **Owner:** quant + ml_ai.

### Phase 2 — Data layer (offline / backtest)
- **Objective:** real fundamentals, honest composite, clean data, the quant-only shadow.
- **Work items:**
  - **Fix SEC coverage** — the silent-empty-CIK-map swallow (`data_providers.py:183-184`) in
    the GH Actions enrichment path; target ≥ 80% (IPS floor).
  - **Corporate-action / split-dividend adjustment** + delisting handler (P0-3).
  - **Composite re-weight** toward quality/value/low-vol; **`formula_version` stamp** on
    every `factor_history` row; never compute persistence/IC across a boundary (P0-2).
  - **Universe expansion → ~400** (gated on coverage ≥ 80%); resumable cursor
    (`fetch_progress.json`).
  - Re-run `backtest/` on the new weighting; stand up the **quant-only shadow** arm.
- **Definition of done:** ≥ 80% fundamental coverage; re-weighted composite backtested;
  shadow arm produces a parallel book.
- **Tests:** coverage-floor assertion; split adjustment; formula-version continuity guard;
  backtest reproducibility (fixed seed).
- **Gate:** `/code-review high`. **Owner:** quant.

### Phase 3 — Observability & alerting (the safety net)
- **Objective:** nothing fails silently — protects the year-end verdict.
- **Work items:**
  - `data_quality_report.json` every run with **absolute floors** (§15.2).
  - Extend `system_health.json` with the §15.3 matrix (success/failure/**MISSING** per flow).
  - **Heartbeat dead-man's switch** workflow (§15.4) — alerts on any missing daily artifact.
  - Extend `alert.yml`; weekly pipeline-integrity digest (§15.5); token-budget cap + alert.
  - Provenance: stamp `data_quality_score` on every forecast/decision (harness covariate).
- **Definition of done:** every flow has 3 signals; degraded runs are flagged & excludable.
- **Tests:** **the §16.4 chaos suite** — each historical failure (28% coverage, dead feed,
  skipped cron, NaN, partial fetch, Supabase 403, malformed dossier, stale price, token
  runaway) reproduced and asserted to trip a loud signal.
- **Gate:** `/code-review high`. **Owner:** devops.

### Phase 4 — Research pipeline (GH Actions; dossier + memory + bench)
- **Objective:** Wednesday reads a small, warm, as-of-dated dossier + bench instead of a firehose.
- **Work items:**
  - Evolve `market_data.yml` → research pipeline (slice fetch · fundamentals · scoring ·
    Haiku event digest · `build_dossier`); earlier cron; cursor.
  - `build_dossier.py` + `research_dossier.json` schema + **schema validation** (P1-5);
    per-ticker `price_as_of` (P0-1).
  - Storage split (§12): commit the small dossier; push raw history to **Supabase + cache
    fallback** (P1-8); `factor_history.jsonl`, `events.jsonl`.
  - Temporal context (§13): `last_decision` persistence + `since_entry` diff;
    **per-lot FIFO dates** in `tax_lots.py` (P0-4).
  - Analyst split (§14): event-driven deep-dive analyst → `bench.json` +
    `research_notes.jsonl`; move agents 2–5 to the weekday step.
- **Definition of done:** a schema-valid dossier + bench build daily; agents have memory.
- **Tests:** dossier schema validation; freshness gate (`as_of`, `built_from_days`);
  holiday-aware edges; mid-week new-name eligibility.
- **Gate:** `/code-review high`. **Owner:** backend + ml_ai.

### Phase 5 — Live path: weekly cadence + `risk_watch.py` (HIGHEST RISK)
- **Objective:** weekly Wednesday rebalance + daily deterministic SELL-only risk-watch.
- **Work items:**
  - `preflight_gate.py`: mode routing (exit 0/30/10/20), once-per-ISO-week stamp on `main`,
    Thu/Fri catch-up, **holiday-aware**.
  - **`risk_watch.py`** — SELL-only decision generator on the **existing** execute envelope
    (no new order path); §6.7 trigger set; price-vs-data trigger split (P1-7); cross-mode
    SELL interlock.
  - `main.py` reads the dossier; **size/execute against live MCP quotes, not dossier price**
    (P0-1); new min-hold (30d), tax-aware hold, −25% stop, wash-sale — all from `policy.yaml`.
  - Crisis safe-mode + forward scenario stress (§18.5).
  - **Sync both live routine prompts** (`ROUTINE_DAILY_CYCLE.md`, `ROUTINE_EOD_CLOSE.md`).
- **Definition of done:** a full simulated week routes correctly; no double-execution;
  risk-watch never emits a BUY.
- **Tests:** the full §16 suite incl. idempotency, cross-mode interlock, holiday weeks,
  catch-up; **weekend dry-run** on frozen data.
- **Gate:** **`/code-review ultra` + weekend dry-run + live-routine sync.** **Owner:** backend + devops.

### Phase 6 — Exit logic & the momentum→fundamental shift
- **Objective:** hold to fundamental invalidation; the actual strategy change, forward-tested.
- **Work items:**
  - Position Review prompt rewrite (carry-forward framing; `recommended_action` gated on
    invalidation, §13.6).
  - Exit items 5/6/7 (§4), now coherent on a fundamental entry.
  - Validate the shift in backtest **and** the forward harness at 63/126/252d.
- **Definition of done:** exits fire only on invalidation/risk; the shift is forward-tested,
  not assumed.
- **Gate:** **`/code-review ultra` + forward-test.** **Owner:** PM + quant.

### Phase 7 — Governance operationalization (continuous from P0)
- **Objective:** run it like an institution.
- **Work items:**
  - `quarterly_review.py` — the §18.6 post-mortem (after-tax vs benchmarks, attribution,
    thesis-correct rate, counterfactual, model-register status, exclusion rate).
  - Incident lifecycle + BCP/vendor-risk register runbooks in DEPLOYMENT (§18.9).
  - Change-control live: `policy_version` + model/prompt-version stamping; rollback runbook.
  - Capital-graduation ladder tracking (IPS §3.5).
- **Gate:** `/code-review high`. **Owner:** devops + PM.

---

## 4. Rollout & reversibility

- **Ship order:** P0 → P1/P3 (fast, zero-risk value) → P2 → P4 → **P5 (the live cutover)** →
  P6 → P7 continuous.
- **The live cutover (P5) is the one irreversible-feeling step** — it changes cadence and
  adds a SELL path. De-risk it: dry-run a full simulated week, `/code-review ultra`, sync
  the routine prompts, and keep the **rollback** (revert `policy_version` / fall back to the
  daily cycle) one documented command away (§18.4, P2-11).
- **Each phase is a separate PR** with its own RELEASE_NOTES entry and review gate.
- **Validation status governs capital, not deployment:** per IPS §3.4 the live book keeps
  trading the trivial account throughout; the paper arms (100× + quant-only) carry the
  verdict.

---

## 5. Immediate next action

**Phase 0, step 1:** extract the `guardrails.py` constants into `policy.yaml` mirroring
[IPS.md](IPS.md) Appendix A, refactor the guards to read from it, and prove parity with the
existing test suite. Zero behavior change — it single-sources every limit and unblocks
every later phase. Start there.
