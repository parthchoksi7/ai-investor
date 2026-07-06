# Manual To-Do — owner-only actions

Actions that **cannot be done from the repo by Claude** — they require the live Anthropic
routines UI, real secrets (redacted from this repo), or an owner merge/deploy decision.
Newest concern first. Check items off as you do them.

_Last refreshed: 2026-07-05 (Phase 1 hardening batch MERGED via PR #27; both owner decisions (#18
cash mandate, #19 stop-loss text) made and shipped; PM `expected_return` scoring (#16) and the
expansion fetch-cursor wiring (#6) both shipped — `UNIVERSE_EXPANDED` is now safe to flip whenever
you choose (#0b); the ORCL "P0-3" item (#9) was investigated and found to be a misdiagnosis, not a
bug, and closed. Daily routine sync CONFIRMED synced — see item 0. Remaining open work is all
either genuinely time-gated (item 13's Monday–Friday observation window) or deliberately deferred
past it (items 14/15/17)._

**Status legend:** `[x]` = done and verified · `[ ]` = not yet done · **DONE**, **PARTIAL**,
**PENDING**, **AWAITING DECISION** tags after each item title give the one-line state without
reading the body. "Verified" means checked against a real artifact/API in this repo, not assumed.

## 📋 At a glance

| # | Item | Status |
|---|------|--------|
| 0 | Daily routine prompt sync | ✅ **DONE** — verified byte-for-byte |
| 0b | Flip `UNIVERSE_EXPANDED` | ⬜ pending (your call — both gating conditions now met as of 2026-07-05) |
| 3 | PyYAML in cloud routine | 🟡 **strong indirect evidence, not yet directly confirmed** — resolves automatically with item 13's Monday check |
| 5 | Parked IPS open questions | ⬜ pending (optional — defaults already applied) |
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
  future-filing look-ahead drop is now LIVE, not inert. Note: FMP-covered names (~35%) still
  lack a filing date (FMP TTM has no single filing); those report vintage-unknown, which is
  honest. Old `provider_cache.json` entries backfill `_as_of_filing` on their normal TTL refresh.
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

### [ ] 5. Settle the two parked IPS open questions — **PENDING, optional** (defaults already applied; only revisit if you disagree)
- Both already have sensible defaults applied; only revisit if you disagree with them.

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
