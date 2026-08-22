# Manual To-Do — owner-only actions

Actions that **cannot be done from the repo by Claude** — they require the live Anthropic
routines UI, real secrets (redacted from this repo), or an owner merge/deploy decision.
Newest concern first. Check items off as you do them.

_Last refreshed: 2026-07-10 (Jul 8 rebalance post-mortem remediation merged to main `d8418e9`;
both routine prompts re-synced and CONFIRMED byte-for-byte — see item 20. Remaining open item
from that batch is #21 (age-out watch, self-resolving, no action needed unless it persists past
~Aug 4). Earlier: Phase 1 hardening batch MERGED via PR #27; both owner decisions (#18 cash
mandate, #19 stop-loss text) made and shipped; PM `expected_return` scoring (#16) and the
expansion fetch-cursor wiring (#6) both shipped — `UNIVERSE_EXPANDED` is now safe to flip whenever
you choose (#0b); the ORCL "P0-3" item (#9) was investigated and found to be a misdiagnosis, not a
bug, and closed. Remaining open work is all either genuinely time-gated (item 13's Monday–Friday
observation window) or deliberately deferred past it (items 14/15/17)._

**Status legend:** `[x]` = done and verified · `[ ]` = not yet done · **DONE**, **PARTIAL**,
**PENDING**, **AWAITING DECISION** tags after each item title give the one-line state without
reading the body. "Verified" means checked against a real artifact/API in this repo, not assumed.

## 📋 At a glance

| # | Item | Status |
|---|------|--------|
| 23 | **Nasdaq 100 benchmark migration + backfill** | ✅ **DONE (2026-08-22)** — migration run by owner, 54 rows backfilled and verified |
| 25 | **Reset `portfolio_peak.json` on the $500→$1,000 deposit** | 🟡 **AWAITING DECISION** — do before funding, not after |
| 24 | **Coverage "regression" DIAGNOSED — not a data failure** (universe-gate oscillation + rotating-batch metric) | 🟡 **OPEN — 2 defects to fix; does NOT block Phase 2** |
| 22 | **Re-quote qty mismatch: absolute vs delta** (routine STEP 4 vs `_compute_qty`) | 🔴 **OPEN — P1** — can over-buy an add-to-holding BUY on a stale-price day |
| 21b | **35%-financials breach — CLOSED**, aged out as designed (Financials 25.06% on 2026-08-19) | ✅ **CLOSED** — superseded by IPS §6.1 (entry-time caps) |
| 20 | **Re-sync BOTH routine prompts** (Jul 9 hardening: STEP 2 dep-verify, STEP 3 no-source-edit rule) | ✅ **DONE** — verified byte-for-byte |
| 21 | **35%-financials breach — age-out watch** | ⬜ **MONITOR** — documented deviation; no action unless it persists past ~Aug 4 |
| 0 | Daily routine prompt sync | ✅ **DONE** — verified byte-for-byte |
| 0b | Flip `UNIVERSE_EXPANDED` | ⬜ pending (your call — both gating conditions now met as of 2026-07-05) |
| 3 | PyYAML in cloud routine | 🟡 **strong indirect evidence, not yet directly confirmed** — resolves automatically with item 13's Monday check |
| 6 | `UNIVERSE_EXPANDED` cursor wiring | ✅ **DONE** — both gating conditions met, safe to flip via item 0b |
| 7 | Heartbeat/digest first scheduled runs | 🟢 **substantially confirmed** — real artifact content exists; can't fully rule out a dispatch vs. cron firing |
| 8 | Dossier consumer wiring | ✅ **DONE** — shipped as Stage C (2026-07-04); sub-items below still open |
| 9 | ORCL "split-unadjusted history" (P0-3) | ✅ **RESOLVED** — was a misdiagnosis; real price action, verified live, no bug |
| 10 | DA-on-holdings nudge | ⬜ pending (parked, your call) |
| 11 | `since_entry` always `None` | ⬜ **PENDING** — found, not fixed |
| 12 | Merge Phase 1 hardening branch | ✅ **DONE** — merged via PR #27 |
| 13 | Go-live observation checklist | ⬜ **PENDING** — window opens Monday 2026-07-06 |
| 14 | Narrow risk_watch interlock | ⬜ **PENDING** — not built |
| 15 | Crash-evidence preservation | ⬜ **PENDING** — not built |
| 16 | Score PM `expected_return` | ✅ **DONE** |
| 17 | Prompt-drift automation | ⬜ **PENDING** — not built |
| 18 | Deployment mandate | ✅ **DECIDED** — ratified defensive cash in IPS.md §6 with a review trigger |
| 19 | Stop-loss IPS text reconciliation | ✅ **DECIDED** — IPS/policy/CLAUDE.md text corrected to match implementation |

---

## ✅ DONE (2026-07-10) — Jul 9 routine re-sync

### [x] 20. Re-sync BOTH routine prompts (Jul 9 hardening) — **routines UI, owner-only**
**Verified via `RemoteTrigger(action="list")`**: both live prompts are byte-for-byte identical to
`ROUTINE_DAILY_CYCLE.md` / `ROUTINE_EOD_CLOSE.md` (the only diffs are the expected
`YOUR_ACCOUNT_NUMBER` → `994046696` substitutions). Both now carry the Jul 9 hardening: STEP 2
verifies `anthropic`/`robin_stocks`/etc. actually import (retry `--ignore-installed`, else STOP)
— Jul 8 died with `No module named 'anthropic'` because a bare `pip install` aborted on
Debian-managed PyJWT; STEP 3 (daily) / the dep-install block (EOD) both carry the hard
**"never edit/commit a .py source file"** rule (Jul 8 the routine hot-fixed `main.py` mid-run and
committed it to `main`, bypassing the §7.0 review gate). `updated_at` 2026-07-10T02:5x; daily
`next_run_at` 2026-07-10T13:45:00Z, EOD `next_run_at` 2026-07-10T20:04Z.

## 🆕 OPEN (2026-08-22) — beta/alpha split prerequisites

See `PLAN_BETA_ALPHA_SPLIT.md` for the full plan this feeds. Phase 1
(`beta_stable`) shipped in PR #37; Phases 2–7 are gated on the two items below.

### [ ] 24. Coverage "regression" — DIAGNOSED 2026-08-22, two defects to fix — **not owner-only**

**Original symptom:** `data_quality_report.json` read `fundamental_coverage_pct` **72.9%**,
`status: DEGRADED`, `strategy_shift_ok: false` — apparently a coverage collapse blocking
Phase 2 of `PLAN_BETA_ALPHA_SPLIT.md`.

**It is not a coverage collapse.** Verified against `data_quality_history.jsonl` (119 rows),
four same-day committed snapshots, and the live GH Actions log for run `32486847810`:

| Time (UTC) | `universe_expanded` | names | coverage | status |
|---|---|---|---|---|
| ~10:1x | False | 102 | **96.1%** | OK |
| ~11:4x | True | 174 | 79.7% | DEGRADED |
| ~13:1x | False | 102 | **96.1%** | OK |
| ~13:5x | True | 174 | 72.9% | DEGRADED |

Core-universe coverage is **96.0%** (96/100 names carry real margin/leverage fields) — healthy
and consistent with the post-SEC-User-Agent-fix level. The 72.9% headline is a *blended*
number over a universe that only exists on alternate runs, and it is simply **the last run
of the day**, which is the one that lands in `data_quality_report.json`.

**Defect 1 — the universe gate is an oscillator with no hysteresis.**
`market_data._prior_coverage_ok()` reads the previously-**committed snapshot's**
`data_quality.coverage_ok`. Its docstring says "yesterday's fundamental-coverage verdict",
but `market_data.yml` fires **4 crons a day** (09:00/11:00/12:00/12:30 UTC) and each run
commits a new `market_snapshot.json` — so it actually reads the *previous run's* verdict.
`universe.get_active_universe(coverage_ok=...)` then flips: expanded → coverage below floor
→ next run contracts to core → coverage above floor → next run expands. A stable 2-cycle.

> **Trading-path consequence (the part that matters).** The candidate set the Portfolio
> Manager sees is **non-deterministic** — 102 or 174 names depending on which cron last
> committed before the 9:45 AM ET (13:45 UTC) routine. Aug 19 got the core snapshot
> (13:12 commit; the expanded 13:53 commit landed *after* the routine) — correct **by luck
> of cron timing, with roughly a 10-minute margin**. If GH Actions runs faster on any given
> day the routine consumes the expanded snapshot instead, putting ~41 momentum+vol-only
> names into the candidate pool — precisely what `get_active_universe`'s own docstring says
> the gate exists to prevent, and doubly bad given those two factors are the beta-loaded
> ones (`PLAN_BETA_ALPHA_SPLIT.md` §2).

**Defect 2 — coverage is measured over a moving target, so the gate can never re-open.**
`_compute_fundamental_coverage(all_tickers, ...)` measures over *this run's* `all_tickers` =
core(102) + the **rotating 75-name expansion batch** (log: `batch 75/288 ... cursor=150`,
advancing to 225). A given expansion name is only enriched when it is simultaneously (a) in
the current batch **and** (b) in today's alternate-day 50/50 group — roughly 1 run in 8 —
while its cache entry expires on a **2-day TTL**. Enrichment cannot keep pace with the
rotation, so the batch's own coverage sits near **44.6%** (33/74 verified on commit
`3f6cd25`) and the blend is pinned. Five days of evidence, zero convergence:

    8/17: 79.7 / 72.9   8/18: 78.2 / 74.0   8/19: 79.7 / 72.9
    8/20: 78.2 / 74.0   8/21: 79.7 / 72.9

A sweep genuinely filling in would climb. This is a limit cycle.

**Ruled out (verified, not assumed):**
- *Not* a cache-persistence failure — GH Actions log shows `Cache restored successfully`,
  2 MB, key `fundamentals-v2-Linux-233`, then `Cache saved with key: ...-234`.
- *Not* absent EDGAR data — the uncovered names are large US filers (LVS, MAA, MCO, MDLZ,
  MET, MGM, MKC, MMM) that certainly file, and they are **clustered in cursor order**,
  which is a sweep position, not a data gap.
- *Not* a `min_depth` or universe-fetch problem — 205 bars, 98.3–100% fetched throughout.

**Still unverified:** what happened on **2026-08-14**, when an expanded run dropped from the
94.4–95.8% it had sustained all of 8/13 to 44.6%, and core runs then read 45.1% (the ~50%
signature of a cold enrichment cache). The cache restores correctly *now*, so whatever
invalidated it has passed; CI logs for that date may have aged out. Recorded as unexplained
rather than guessed — the ORCL misdiagnosis (item 9) is the cautionary precedent.

**Impact on Phase 2:** **does not block it.** Phase 2's re-weight is measured on the core
universe, which is at 96.0%. The 80% floor should be evaluated against core coverage, not
against a rotating-batch blend.

**Fix options (needs a decision, not yet implemented):**
1. **Latch the universe decision once per calendar day** (read the first snapshot of the day,
   or stamp the decision) — smallest change, kills the oscillation, makes the routine's
   candidate set deterministic.
2. **Add hysteresis** — expand at ≥80%, contract only below ~70% — standard control fix.
3. **Gate on CORE-universe coverage** (arguably most correct): the gate's real question is
   "is the fundamentals pipeline healthy?", which is a property of the core universe, not of
   how far an in-progress expansion sweep has got. Blending sweep progress into the gate is
   what created the loop.
4. Separately, align the coverage **denominator** with what is actually enrichable, or widen
   the TTL / batch interaction so a name can be refreshed while it is in-batch.

⚠ **Do not ship any of these before the Wed 2026-08-26 rebalance** — it is the sector clamp's
first live fire (`PLAN_BETA_ALPHA_SPLIT.md` §6) and must not be confounded. Option 1 or 3
would change which universe that run sees.

### [ ] 25. Reset `portfolio_peak.json` when funding $500 → $1,000 — **owner action, timing-sensitive**
`portfolio_peak.json` currently reads `{"peak": 528.0813949525, "updated": "2026-08-19"}`.
It tracks raw `total_value`, so a **deposit inflates the peak** — a $500 deposit would
read as a new ~$1,028 peak, and the kill-switch drawdown math `(peak − current) / peak`
then measures from a number that includes deposited cash, not performance (documented
limitation, CLAUDE.md "Known Limitations"). **Deposit first, then edit
`portfolio_peak.json`** and set `"peak"` to the post-deposit `total_value` — Manual
Execution Runbook Scenario C. Doing this after a run has already recomputed drawdown
against the inflated peak risks a false kill-switch trip blocking all BUYs.

## 🆕 PENDING (2026-08-22) — Nasdaq 100 benchmark

### [x] 23. Run `migrations/2026-08-22_add_qqq_benchmark.sql` — **DONE 2026-08-22**
_Migration applied by the owner in the Supabase SQL Editor; `backfill_qqq.py` then wrote 54 rows
(2026-08-11 left NULL, matching its NULL `spy_close`). Verified: inception baseline = 0.00%,
no core field disturbed, re-run is idempotent._
Adds `qqq_close` and `qqq_cumulative_return_pct` to `portfolio_snapshots` so the dashboard can
benchmark against the Nasdaq 100 alongside the S&P 500. The service key can read and write rows
over PostgREST but **cannot run DDL**, so this one statement has to be pasted into the Supabase
SQL Editor by hand. It is `add column if not exists` — safe to re-run.

```sql
alter table public.portfolio_snapshots
  add column if not exists qqq_close                 numeric,
  add column if not exists qqq_cumulative_return_pct numeric;
```

**Then, in this repo:**

```bash
./venv/bin/python backfill_qqq.py --dry-run   # review the plan
./venv/bin/python backfill_qqq.py             # write 54 historical rows
```

**Nothing breaks while this sits undone.** `publish.py` catches the missing-column error, drops
the two QQQ keys, and republishes the row without them (printing which migration to run).
`PerformanceChart` only draws a series the data actually contains, so the site renders the S&P 500
line alone and the "vs Nasdaq 100" stat card shows "—" until the columns exist and are backfilled.

## 🆕 PENDING (2026-08-19) — Aug 19 rebalance post-mortem follow-ups

### [ ] 22. Re-quote qty mismatch — ABSOLUTE vs DELTA (P1, execution-path) — **owner decision + routine sync**
Surfaced by `/code-review high` during the Aug 19 sector-clamp remediation. **Pre-existing —
not introduced by that batch — and it affects every BUY that ADDS to an existing holding, not
just clamped ones.**

- `execute._compute_qty()` returns a **DELTA** for a held name:
  `(target_weight × total_value − current_market_value) ÷ price`.
- The routine's stale-price re-quote (`ROUTINE_DAILY_CYCLE.md` STEP 4, the P0-1 path) recomputes
  an **ABSOLUTE** quantity: `target_weight × total_value ÷ live_price`.

When `decision["price_as_of"] != today` the routine re-quotes, and an add-to-holding BUY is
placed at the **full target size on top of the position already held** — roughly doubling the
intended add and, for a clamped BUY, breaching the sector cap at the broker. It fires only on a
stale-price day, which is why it has not been observed live.

**Options:** (a) make the routine re-quote delta-aware (needs the held qty, which
`mcp_portfolio.json` already carries) — correct but requires a live-routine sync; (b) have
`main.py` stamp an explicit `sizing_basis: "delta"` on each decision and have the routine honor
it; (c) skip the re-quote for add-to-holding BUYs and let them execute at the stamped qty.
Recommend **(a)**. Until it is fixed, a stale-price day is the one path where the sector cap can
be exceeded at the broker — noted in the `enforce_sector_limits` docstring.

### [x] 21b. 35%-financials age-out watch — **CLOSED 2026-08-19**
Resolved as designed: Financials measured **25.06%** on 2026-08-19 (down from ~35%), entirely via
min-hold expiries with no forced trim. Superseded by the new **IPS §6.1** (caps bind at entry;
drift is compliant and never auto-trimmed), which makes the residual 0.06pp a non-issue rather
than a deviation. No further action.

## 🆕 PENDING (2026-07-09) — Jul 8 rebalance post-mortem follow-ups

### [ ] 21. 35%-financials breach — age-out watch (owner-decided: let it age out)
The Jul 8 rebalance left the live book at **~35% financials** (MS + AXP + CFG + CB) vs the 25%
IPS cap, because the SECTOR_MAP hole let the orphaned CB/CFG BUYs through (now fixed +
fail-closed). **Decision: age it out, do not force a taxable trim** (consistent with the §6 cash
exception and the anti-churn min-hold policy). The fixed sector cap blocks any NEW financial BUY
immediately. The book self-corrects as the min-holds expire and the PM re-proposes the
AXP/MS rotation it wanted on Jul 8:
- **MS** sellable ~**2026-07-21** (bought 06-08), **AXP** ~**2026-08-04** (bought 06-22),
  **CB/CFG** ~**2026-08-19** (bought 07-08).
- **No action needed** unless financials is still > 25% after ~2026-08-04 with the PM NOT
  proposing a trim — then investigate why the rotation isn't re-proposed (PM prompt / candidate
  set). Documented as an IPS deviation (IPS.md §6, mirroring the ratified cash posture).

## ✅ DONE (2026-07-05) — daily routine prompt sync

### [x] 0. Paste the new `ROUTINE_DAILY_CYCLE.md` into the live daily routine (`YOUR_ROUTINE_ID_DAILY`)
- **Verified via `RemoteTrigger(action="list")`**: the live prompt is byte-for-byte identical to
  `ROUTINE_DAILY_CYCLE.md` (account number substituted; only diff was a trailing newline). Contains
  the exit-30 branch, GUARD 4 (mode integrity), and the P0-1 stale-price re-quote. `updated_at`
  2026-07-05T13:30:44Z; `next_run_at` 2026-07-06T13:45:00Z (Monday 9:45 AM EDT) — the first-ever
  risk-watch day. The EOD routine is unchanged (no sync needed there).

### [ ] 0b. (Later, your call) Flip `UNIVERSE_EXPANDED` for the ~400-name universe
- Set the GitHub Actions **variable** (Settings → Secrets and variables → Actions →
  Variables) `UNIVERSE_EXPANDED=true`. Code-gated on prior-day coverage ≥ 80% too, so
  the flip alone is safe. Watch the first expanded run's duration + coverage, and that
  the committed `market_snapshot.json` stays ~6 MB (slimmed). Until flipped: zero change.

---

## 📍 Redesign status & roadmap (where we are)

| Phase | State |
|-------|-------|
| 0 Single-source limits · 1 Measurement · 2 Data layer · 3 Observability | ✅ deployed |
| 4 Research pipeline — dossier producer · event digest · `_as_of_filing` | ✅ deployed |
| **5 Stage A** — pre-consumer hardening | ✅ deployed |
| **5 Stage B** — `risk_watch.py` (SELL-only daily safety net) | ✅ **deployed 2026-07-04** (owner-directed) — live after the routine sync above |
| **5 Stage C** — dossier consumer + weekly Wednesday rebalance | ✅ **deployed 2026-07-04** (owner-directed, **overriding the evidence gate** — `stage_c_readiness` still ACCUMULATING; it keeps measuring, the §10.3 success/kill bar is unchanged) |
| **Stage D** — expansion-ready fetch + interim §12.4 storage split | ✅ code deployed, **operator-gated** — flips with the `UNIVERSE_EXPANDED` variable (item 0b) |
| Phase 6 — exit-logic prompt rewrite (invalidation-gated exits) | ⏭ next build phase (forward-tested, `/code-review ultra`) |
| Full §12.4 storage split (dossier-only commit, Supabase raw) | ⏭ deferred until the expanded snapshot hits the §12.6 triggers |
| **Post-go-live hardening batch 1** (Supabase plane detection, heartbeat holiday-Friday fix, evidence-clock formula-version partition + significance test) | ✅ **committed** on `fix/phase1-hardening-evidence-clock`, **not yet merged** — see item 12 below |

---

## 🟣 Go-live observation window (2026-07-06 → 2026-07-10) — from the post-Phase-5 critical review

This is the review-and-remediation plan from the Jul 4–5 multi-persona critical review, tracked
here (NOT numbered as a redesign "Phase" — that term is reserved for the Phase 0–6 rows in the
table above; this is a short-lived verification + hardening sequence layered on top of the
already-deployed Phase 5). Nothing below blocks Monday — it is what to WATCH and what to BUILD
next, in order.

### [x] 12. Merge `fix/phase1-hardening-evidence-clock` to `main` — ✅ **DONE (2026-07-05, PR #27)**
- Batch 1 of the remediation: plane-aware Supabase health classification, the heartbeat
  holiday-Friday missed-week fix, and the calibration evidence-clock integrity fixes
  (formula-version partition + read-only `factor_history.jsonl` join + zero-variance-day
  exclusion + real counterfactual significance test). Pushed, PR'd, and merged (squash-free
  merge commit, matching this repo's convention) before Monday — the plane-aware Supabase fix
  is live for Monday's first-ever risk-watch run.
- **Expected visible side effect post-merge:** `agent_scorecards.json`'s primary
  `quant.composite_score@21d` key will read "not scored yet" (ACCUMULATING) instead of a mixed-
  vintage IC, until enough post-2026-07-02-formula forecasts mature (~early August). This is the
  intended, honest consequence — not a regression. `stage_c_readiness.py` / `pipeline_digest.md`
  will visibly show less evidence for a few weeks.

### [ ] 13. Watch these specific checkpoints during the observation window — **PENDING** (window opens Mon 2026-07-06; no action unless something looks wrong)
| Day | What to verify |
|-----|-----------------|
| **Mon Jul 6** (first-ever risk-watch day) | Gate exits 30; `risk_watch` health row written; `pending_decisions.json` has `mode: "risk_watch"`; zero BUYs; the envelope's `policy_version` stamps `2.0-phase5-weekly` |
| **Wed Jul 8** (first-ever Phase-5 weekly rebalance) | Gate exits 0; `research_dossier` health check OK; `last_rebalance.json` written for this ISO week; a stale-priced decision triggers the P0-1 re-quote; guardrail rejections look sane |
| **Thu Jul 9** | Gate exits 30, citing "rebalance already attempted this ISO week" from the `last_rebalance.json` mirror — the FIRST live test of the once-per-week lock |
| **Fri Jul 10** | `pipeline_digest.md` reports the week's rebalance status; heartbeat's `weekly_rebalance` check is OK; no `health-alert` issue stuck open |

Anything that deviates from this table is a finding, not automatically a bug — bring it back for
a look before assuming something is broken.

### [ ] 14. Narrow the risk_watch cross-mode interlock — **PENDING, not built** (ready to build once the observation window closes; not urgent)
- Currently `_mirror_rebalance_stamp` (journal.py) records ALL rebalance-traded tickers — BUYs
  AND SELLs — and `risk_watch._interlocked_tickers` refuses to stop-loss-sell ANY of them for the
  rest of the ISO week. The interlock only needs to protect against double-selling a name the
  rebalance already SOLD; a name the rebalance just BOUGHT should still be protected by the daily
  −25% stop if it craters days later. **Fix:** key the mirror/interlock off rebalance SELLs only.
  This is a change to `risk_watch.py`'s decision set → real order-path code → `/code-review ultra`
  + a weekend dry-run before merging, per DEPLOYMENT.md §7.0. Not urgent (DEGRADED health still
  pages you if a fired-but-interlocked stop is ever hit); do this in the first quiet week.

### [ ] 15. Crash-evidence preservation in risk_watch — **PENDING, not built** (ready to build once the observation window closes)
- If a Wednesday rebalance crashes after claiming but before stamping `executed_at`, Thursday's
  `risk_watch.py` overwrites `pending_decisions.json` — destroying the exact envelope
  `reconcile.py`/Scenario B need to diff intended-vs-actual orders. Fix: `risk_watch.py` archives
  a claimed-but-unstamped prior envelope (e.g. to `pending_decisions.crashed.json`) before writing
  its own, and `reconcile.py` prefers the archived file when present. Small, testable, no change
  to risk_watch's own decision logic — but touches the same file the order path depends on, so
  still `/code-review high` minimum.

### [x] 16. Score the Portfolio Manager's `expected_return` in calibration.py — ✅ **DONE (2026-07-05)**
- `guardrails.enforce_net_edge` gates every BUY on the PM's own self-reported `expected_return` —
  nothing measured whether that number was calibrated (over- or under-confident) against realized
  returns. **Shipped:** `calibration.log_pm_forecasts` — a new emitter (not folded into
  `log_forecasts`, since `portfolio_manager_proposed` is a LIST of decisions, not a per-ticker
  dict like every other agent) scores `pm.expected_return` from the PM's RAW proposal (before the
  CRO veto or any guardrail — scoring only guard-survived decisions would bias the sample toward
  predictions that already cleared the floor). Only BUYs carry the field, matching
  `enforce_net_edge`'s own convention (including its exact `float()` coercion, so the same
  decisions that get gated are the ones scored). Wired into `main.py`'s existing forecast-logging
  block. 7 new tests (`TestPMForecastScoring`); orientation defaults to +1 (no `_FORECASTS` entry
  needed — see the code comment). Prerequisite to ever tuning `MIN_NET_EDGE` with evidence instead
  of faith.

### [ ] 17. Prompt-drift automation — **PENDING, not built** (ready to build; needs a routine-prompt sync after)
- The recurring "requires a live-routine sync" failure class (this repo's most common operational
  incident — see the Jun 16/17 branch-execution and STEP-3/5 drift entries) has no automated
  detection: the only way to know the live prompt matches `ROUTINE_DAILY_CYCLE.md` is to manually
  diff it (as done for item 0 above). Fix: have the routine echo a short prompt-version string
  (e.g. a hash of the canonical .md, or a manually-bumped version line) into `system_health.json`;
  the heartbeat compares it against the current `ROUTINE_DAILY_CYCLE.md`'s stamped version and
  alerts on mismatch. Code is buildable now; taking effect requires pasting the updated prompt
  into the live routine same as any other prompt change.

### [x] 18. Owner decision — the deployment mandate — ✅ **DECIDED (2026-07-05): option (a)**
- **Decision:** ratify defensive cash explicitly in the IPS with a review trigger, rather than
  (b) a bounded mechanical re-deployment rule or (c) doing nothing silently. **Shipped:** IPS.md
  §6 now carries a formal "Ratified interim exception" note (the ~63% cash / 4-holding state,
  why every guardrail stays a brake rather than being relaxed on faith, and a two-part review
  trigger — the first scored current-formula quant reading (~early Aug 2026) and
  `stage_c_readiness.py` DECIDABLE, with a Q1 2027 hard backstop regardless). Logged in §11
  (Monitoring & Review) and Appendix B (Amendment log, v1.1). Options (b)/(c) remain available
  to revisit at the review trigger.

### [x] 19. Owner decision — reconcile the stop-loss IPS text with its actual implementation — ✅ **DECIDED (2026-07-05): option (a)**
- **Decision:** amend the IPS/policy text to describe the mechanism as implemented, rather than
  (b) building a true close-based evaluation. **Shipped:** corrected the "daily-close" claim
  everywhere it appeared — `IPS.md` §4 table + Appendix A comment, `policy.yaml` (both the v2.0
  migration-note comment and the `risk.single_name_stop_pct` comment), `CLAUDE.md`'s Investment
  Rules summary, AND the actual runtime rationale string in `risk_watch.py` (was
  self-contradictory: "daily close, live MCP quote" in the same breath) — now consistently
  "evaluated each trading-day morning via `risk_watch.py` on a live MCP quote, not the 4 PM
  close." Appendix B v1.1 entry added. Zero test dependency on the old string; full suite green.

---

## ✅ Live routine prompt sync — DONE (2026-07-03) *(superseded by item 0 above)*

- **[x] Secrets stripped, keys rotated; daily + EOD synced to the Jul-3 prompts.**

---

## 🟠 Data-layer gates (Phase 2 — deployed) + universe expansion

### [ ] 3. Verify PyYAML is installed in the cloud routine environment — 🟡 **strong indirect evidence, not directly confirmed yet**
- **Why:** `policy.py` **silently falls back to built-in defaults** if PyYAML is missing.
  Phase 2 shipped `policy_version → 1.1-phase2-dataquality` and `price_outlier_pct` — a cloud
  env without PyYAML keeps using the old defaults ("works locally, silent no-op in prod").
- **How:** confirm the routine's `pip install -r requirements.txt` succeeds (`PyYAML` is in
  `requirements.txt`), or check a cloud run log for a `⚠ policy.yaml not loaded` warning.
- **Evidence so far (2026-07-05):** the last live cloud envelope (`pending_decisions.json`, run
  `20260702-134722`) stamps `policy_version: "1.0-phase0-parity"` — a YAML-sourced value (the
  hardcoded-fallback stamp is `"0.0-builtin-defaults"`), so PyYAML **was** loading in the cloud
  as of Jul 2. The `1.0` (not the current `1.1`/`2.0`) is explained by a benign race: that run
  fired minutes after the version bump merged. **Not yet directly re-confirmed post-Phase-5** —
  resolves automatically the moment item 13's Monday check reads the new envelope's
  `policy_version`; no separate action needed, just watch that field Monday.

### [x] 6. `UNIVERSE_EXPANDED` — ✅ **BOTH conditions now met (2026-07-05)** — safe to flip when you choose
- **Condition (a)** satisfied since 2026-07-02 — GH Actions logs show **96% fundamental coverage**,
  `data_quality.coverage_ok=true` (the SEC User-Agent 403 fix).
- **Condition (b) — SHIPPED 2026-07-05:** `market_data.select_fetch_batch` (new pure helper) wires
  `universe.next_batch`/`save_batch` into the fetch loop. Core + held + SP500 + benchmarks are
  fetched in full every run regardless of expansion state (zero behavior change today); only the
  ~300 EXPANSION-ONLY names are swept a batch at a time (`EXPANSION_BATCH_SIZE = 75`/run, ~15 min
  at Polygon's 5-calls/min, full sweep in ~4 runs / about one day at the 4 daily triggers). Cursor
  persists via the EXISTING `actions/cache` step in `market_data.yml` (already declared
  `fetch_progress.json` in its cache path before this fix — the workflow was ahead of the code).
  5 new tests (`TestSelectFetchBatch`), including an explicit "matches pre-feature behavior
  exactly when not expanded" regression test.
- **You can now flip `UNIVERSE_EXPANDED=true`** (GitHub Actions variable, item 0b) whenever you
  want the expansion to begin — both gating conditions are met. Expect the committed
  `market_snapshot.json`'s expansion-name histories to fill in gradually over the first few days
  (63-bar tails per Stage D's storage split) rather than all at once — that's the batching working
  as designed, not a bug. Watch coverage stays ≥80% over the *expanded* set as it fills in.

---

## 🟡 Phase 3 (observability — deployed): monitor, no action required

### [ ] 7. Watch the two NEW workflows' first scheduled runs — 🟢 **substantially confirmed**
- `heartbeat.yml` (weekdays 6 PM ET) + `pipeline_digest.yml` (Fri 6:30 PM ET) auto-activate on
  merge — no setup needed. Just confirm their first *scheduled* (not dispatch) runs go green in
  the Actions tab, and that a `heartbeat-alert` issue opens/closes correctly the first time an
  artifact is genuinely missing. **DST:** both new crons are intentionally left OUT of
  `update_dst.yml` — a 1-hour seasonal drift is harmless (both fire after the 4 PM close).
- Permissions are declared in-workflow (`issues: write` / `contents: write`); no repo-setting
  change required.
- **Verified 2026-07-05:** both artifacts exist with real, non-placeholder content —
  `heartbeat_report.json` has `as_of: "2026-07-02"`, `ok: true`; `pipeline_digest.md` reports
  "week ending 2026-07-03" with a genuine `Generated 2026-07-03T02:13:28` stamp embedded in the
  file content (not just a filesystem mtime, which `git checkout` can reset misleadingly). This
  confirms both scripts ran successfully at least once with real data. **Residual gap:** the
  artifact content alone can't fully distinguish a true scheduled cron firing from a manual
  `workflow_dispatch` during testing — if you want that last mile of certainty, check the Actions
  tab's run-trigger column once.

---

## 🔵 Phase 4 (research pipeline — landing incrementally)

### [ ] 8. Dossier consumer wiring — ✅ **DONE**, shipped as Stage C (2026-07-04); sub-items below still open
- **The original ask is DONE.** This item was written when the dossier was producer-only; the
  cloud routine now DOES read `research_dossier.json` (verified: `main.py` calls `load_dossier()`
  + `validate_dossier()` before the agents run, aborting the rebalance on a stale/invalid dossier)
  — this shipped as **Phase 5 Stage C** (2026-07-04) and the routine-prompt sync is confirmed
  (item 0). The sub-items below are separate, still-open follow-ups, not blockers on the main ask.
- **What shipped (increment 1):** `build_dossier.py` builds + schema-validates + commits
  `research_dossier.json` from GH Actions (zero order code — a research artifact only).
- **Increment 2 shipped — the Haiku event digest** (`event_digest.py`, `events.jsonl`): now
  runs as Step 4 of the GH-Actions fetch and feeds the dossier. **Manual check:** confirm the
  `ANTHROPIC_API_KEY` Actions secret is present (it is per the Jun-9 incident log) — if unset,
  the digest self-skips (events stay empty; the dossier still builds). **Token cost:** the
  digest adds Anthropic spend to `market_data.yml` (Haiku, chunked 20/call, cached) — the §15.2
  token-budget cap + alert (P2-13) is still deferred; watch the first few runs' cost. A parse
  failure ≥20% now floors `data_quality_report.json` at DEGRADED (→ cloud health check → alert).
  **Accepted limitations (documented, not bugs):** (i) `events.jsonl` is committed to git and
  appended forever — it joins `factor_history` in the §12.4 storage-split/retention work (the
  dedup read scans the whole file, bounded logically to a 60-day window). (ii) The digest is
  LLM news summarization — a crafted headline tagged to a real ticker can yield a fabricated
  "material" event with a structured veneer; severity is low (enrichment-only, and the same raw
  feed already reaches the agents), but treat dossier events as leads, not facts.
- **Increment 3 shipped — `_as_of_filing` stamping** (`data_providers.SECProvider`): SEC
  fundamentals now carry the 10-K `filed` date (the no-look-ahead availability date), so the
  dossier reports REAL `fundamentals_age_days` / `fundamentals_stale` (was `null`) and the
  future-filing look-ahead drop is now LIVE, not inert. ~~Note: FMP-covered names (~35%) still
  lack a filing date (FMP TTM has no single filing); those report vintage-unknown, which is
  honest.~~ **Superseded by PLAN_SEC_VALUATION Phase 3 (2026-07-24):** `CascadeProvider` now
  always consults SEC EDGAR (not just on an FMP quality-miss), so EVERY ticker — including the
  ~35% FMP covers — gets a real `_as_of_filing` stamp. Old `provider_cache.json` entries
  backfill `_as_of_filing` on their normal TTL refresh.
- **⬜ PENDING, not built:** per-lot FIFO tax dates (P0-4).
- **⬜ PENDING (non-correctness — tracked, not blocking):**
  (a) **storage wall (§12.4):** `research_dossier.json` is committed whole to git daily and grows
  with the universe — the planned raw→curated storage split (dossier to object storage / compact
  digest only) should land before the 400-name expansion. (b) **efficiency:** `build_dossier`
  loads the entire (unbounded) `factor_history.jsonl` + double-reads the snapshot from disk — fine
  now, revisit with the storage split (tail-read the recent window; pass the in-memory snapshot).
  (c) **reuse:** `_read_jsonl` / `_load_json` / atomic-write / `_max_drawdown` are duplicated across
  `data_quality` / `pipeline_digest` / `build_dossier` / `health` / `journal` / `performance` — a
  shared `io_utils` helper is warranted in a dedicated cleanup PR. (d) **tunables:**
  `_PERSISTENCE_WINDOW` / `_FUNDAMENTALS_STALE_DAYS` (and `market_data.FUNDAMENTAL_COVERAGE_FLOOR_PCT`)
  should migrate into `policy.yaml` for the single-source-of-truth invariant.

### [ ] 11. `since_entry` dossier anchor is structurally always `None` — **PENDING, not fixed** (found 2026-07-05, Phase 1 dry-verify)
- The dossier's **entry anchor** (`_fmt_since_entry` → the "judge the position against entry,
  not last week" block the Stage C Position-Review agent reads, STRATEGY_REDESIGN_PLAN §13.3)
  renders `last_decision` fine but **never** the `since_entry` cumulative-return line — verified
  against both held names with open BUYs (AXP, EBAY): both show `since_entry=None`.
- **Root cause:** `build_dossier._last_decision` computes `since_entry` from
  `last.get("entry_price") or last.get("price")`, but `journal.record_trade()` has **no
  `entry_price`/`price` parameter at all** — every journal entry is written with those fields
  absent, so the guard `isinstance(entry_px, (int,float))` is always False. The feature has
  been inert since it shipped.
- **Fix (execution-adjacent — next batch, not Phase 1):** thread the executed/decision price
  into `record_trade()` at both call sites (`main.py`, `risk_watch.py`) — or, better, populate
  it from the broker fill during `mark_transactions_live` reconciliation so it reflects the
  REAL entry, not the decision-time quote. Touches the trade-journal write path → `/code-review
  high` + tests. Quietly defeats a headline Phase 5 Stage C mechanism until fixed.

### [x] 9. ORCL "split-unadjusted history" (P0-3) — ✅ **RESOLVED (2026-07-05): investigated, was a MISDIAGNOSIS, not a bug**
- **Original claim (retracted):** ORCL's `ret_21d ≈ −0.43` in the dossier was assumed to be a
  data-corruption artifact of split-unadjusted OHLCV, on the theory that
  `corporate_actions.detect_price_outliers`'s flag on ORCL's ~36% one-day jump explained the
  negative 21-day return.
- **What investigation actually found:** the ~36% one-day jump the outlier detector flags is
  **2025-09-10** — ten months before the dossier's `as_of` date, nowhere near the 21-day lookback
  window — and is unrelated to `ret_21d`. `market_snapshot.json`'s actual daily closes for ORCL
  show a **smooth, gradual, real decline from ~$248 (late May 2026) to ~$140 (early Jul 2026)** —
  no single day exceeds the outlier threshold, and no bar-to-bar discontinuity resembles an
  unhandled split (which would show one overnight halving/doubling, not a multi-week bleed).
  **Independently verified against a live Massive/Polygon query** (not just the committed
  snapshot) — the closes match exactly. This is genuine, if severe, price action: ORCL really
  did decline ~43% over ~5–6 weeks. The dossier's `ret_21d ≈ −0.43` is CORRECT, not corrupted.
- **Corrected conclusion:** `adjusted=true` (already set in `market_data.py`) was never broken;
  `corporate_actions.detect_price_outliers`'s design (flag as a review signal, never auto-drop —
  "a genuine crash should not be thrown away") was already right. **No code change needed.**
  The **"QUARANTINE flagged tickers from scoring" follow-up idea from the Jul 4–5 review is
  RETRACTED** — it was built on the false premise that a large move must mean bad data;
  quarantining ORCL here would have discarded real, valuable momentum signal, not fixed a bug.
  ORCL is not currently held, so this had no live portfolio impact either way.

---

## 🟢 Merges / decisions (Claude CAN do these — just say so)

### [ ] 10. The Devil's-Advocate-on-holdings nudge (`feat/pm-devil-tension`) — **PENDING, parked** (your call)
- A June-17 review branch surfaces the DA verdict on PM holdings lines. You deliberately held it
  back ("may increase turnover"). It can't be merged as-is (stale base). If you want it, say so
  and Claude will re-implement it on current `main` **with** the turnover/after-tax trade-off
  called out — turnover is ~54% short-term tax in this account.

---

_Maintained by Claude as new owner-only steps arise. The `[x]`/`[ ]` checkbox tracks the item AS A
WHOLE (checked only once every sub-part is resolved and can be deleted); the **DONE**/**PARTIAL**/
**PENDING**/**AWAITING DECISION** title tag gives the finer-grained read for compound items whose
main ask shipped but which still carry open sub-workstreams (e.g. #8) — read both, not just the
checkbox. "Verified"/"confirmed" always means checked against a real artifact or API in this repo
in this session, not assumed from memory._
