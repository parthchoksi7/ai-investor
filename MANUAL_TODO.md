# Manual To-Do — owner-only actions

Actions that **cannot be done from the repo by Claude** — they require the live Anthropic
routines UI, real secrets (redacted from this repo), or an owner merge/deploy decision.
Newest concern first. Check items off as you do them.

_Last refreshed: 2026-07-05 (Phase 1 hardening batch committed on `fix/phase1-hardening-evidence-clock`,
not yet merged; daily routine sync CONFIRMED synced — see item 0 below; go-live observation window
open through 2026-07-10)._

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

### [ ] 12. Merge `fix/phase1-hardening-evidence-clock` to `main`
- Batch 1 of the remediation (committed 2026-07-05, not pushed/merged): plane-aware Supabase
  health classification, the heartbeat holiday-Friday missed-week fix, and the calibration
  evidence-clock integrity fixes (formula-version partition + read-only `factor_history.jsonl`
  join + zero-variance-day exclusion + real counterfactual significance test). 694 tests green,
  `/code-review high` clean. **No live-order-path code changed** — safe to merge any day, not
  just a non-trading day, but do it BEFORE Monday if possible so the plane-aware Supabase fix is
  live for the first-ever risk-watch run.
- **Expected visible side effect post-merge:** `agent_scorecards.json`'s primary
  `quant.composite_score@21d` key will read "not scored yet" (ACCUMULATING) instead of a mixed-
  vintage IC, until enough post-2026-07-02-formula forecasts mature (~early August). This is the
  intended, honest consequence — not a regression. `stage_c_readiness.py` / `pipeline_digest.md`
  will visibly show less evidence for a few weeks.

### [ ] 13. Watch these specific checkpoints during the observation window (no action unless something looks wrong)
| Day | What to verify |
|-----|-----------------|
| **Mon Jul 6** (first-ever risk-watch day) | Gate exits 30; `risk_watch` health row written; `pending_decisions.json` has `mode: "risk_watch"`; zero BUYs; the envelope's `policy_version` stamps `2.0-phase5-weekly` |
| **Wed Jul 8** (first-ever Phase-5 weekly rebalance) | Gate exits 0; `research_dossier` health check OK; `last_rebalance.json` written for this ISO week; a stale-priced decision triggers the P0-1 re-quote; guardrail rejections look sane |
| **Thu Jul 9** | Gate exits 30, citing "rebalance already attempted this ISO week" from the `last_rebalance.json` mirror — the FIRST live test of the once-per-week lock |
| **Fri Jul 10** | `pipeline_digest.md` reports the week's rebalance status; heartbeat's `weekly_rebalance` check is OK; no `health-alert` issue stuck open |

Anything that deviates from this table is a finding, not automatically a bug — bring it back for
a look before assuming something is broken.

### [ ] 14. (Ready to build once the observation window closes, not urgent) Narrow the risk_watch cross-mode interlock
- Currently `_mirror_rebalance_stamp` (journal.py) records ALL rebalance-traded tickers — BUYs
  AND SELLs — and `risk_watch._interlocked_tickers` refuses to stop-loss-sell ANY of them for the
  rest of the ISO week. The interlock only needs to protect against double-selling a name the
  rebalance already SOLD; a name the rebalance just BOUGHT should still be protected by the daily
  −25% stop if it craters days later. **Fix:** key the mirror/interlock off rebalance SELLs only.
  This is a change to `risk_watch.py`'s decision set → real order-path code → `/code-review ultra`
  + a weekend dry-run before merging, per DEPLOYMENT.md §7.0. Not urgent (DEGRADED health still
  pages you if a fired-but-interlocked stop is ever hit); do this in the first quiet week.

### [ ] 15. (Ready to build once the observation window closes) Crash-evidence preservation in risk_watch
- If a Wednesday rebalance crashes after claiming but before stamping `executed_at`, Thursday's
  `risk_watch.py` overwrites `pending_decisions.json` — destroying the exact envelope
  `reconcile.py`/Scenario B need to diff intended-vs-actual orders. Fix: `risk_watch.py` archives
  a claimed-but-unstamped prior envelope (e.g. to `pending_decisions.crashed.json`) before writing
  its own, and `reconcile.py` prefers the archived file when present. Small, testable, no change
  to risk_watch's own decision logic — but touches the same file the order path depends on, so
  still `/code-review high` minimum.

### [ ] 16. (Ready to build, no urgency) Score the Portfolio Manager's `expected_return` in calibration.py
- `guardrails.enforce_net_edge` gates every BUY on the PM's own self-reported `expected_return` —
  nothing currently measures whether that number is calibrated (over- or under-confident) against
  realized returns. Add `pm.expected_return` as a first-class forecast in `calibration.log_forecasts`
  / `agent_scorecard` (same machinery already used for `quant`/`research`/etc.), so the net-edge
  gate's only input eventually earns (or loses) trust from real evidence instead of running on
  faith. Prerequisite to ever tightening or loosening `MIN_NET_EDGE` with confidence.

### [ ] 17. (Ready to build, needs a routine-prompt sync after) Prompt-drift automation
- The recurring "requires a live-routine sync" failure class (this repo's most common operational
  incident — see the Jun 16/17 branch-execution and STEP-3/5 drift entries) has no automated
  detection: the only way to know the live prompt matches `ROUTINE_DAILY_CYCLE.md` is to manually
  diff it (as done for item 0 above). Fix: have the routine echo a short prompt-version string
  (e.g. a hash of the canonical .md, or a manually-bumped version line) into `system_health.json`;
  the heartbeat compares it against the current `ROUTINE_DAILY_CYCLE.md`'s stamped version and
  alerts on mismatch. Code is buildable now; taking effect requires pasting the updated prompt
  into the live routine same as any other prompt change.

### [ ] 18. Owner decision — the deployment mandate (no code fix; a policy choice)
- Every guardrail in the system is a BRAKE (min-hold, wash-sale, tax-hold, sector cap, safe-mode,
  net-edge, kill-switch, stop-loss); nothing converts idle cash into positions except the weekly
  PM's own disposition. At weekly cadence (~52 decisions/year) and with SELLs locking capital
  behind a 30-day wash-sale re-entry window, the system structurally drifts toward under-
  deployment. Options: **(a)** ratify defensive cash explicitly in the IPS with a review trigger
  (recommended for now, until the partitioned evidence clock has something to say); **(b)** a
  bounded mechanical re-deployment rule (e.g. cash > threshold for N consecutive weeks relaxes the
  net-edge floor for index-diversified adds, still inside every hard cap) — a real order-path
  change, `/code-review ultra`; **(c)** accept the risk and do nothing. This is a decision only
  you can make; Claude can implement whichever you pick.

### [ ] 19. Owner decision — reconcile the stop-loss IPS text with its actual implementation
- IPS/policy describe the −25% single-name stop as evaluated "at daily close, no trailing"; the
  live `risk_watch.py` implementation evaluates it on a MORNING intraday MCP quote (9:45–12:45 ET),
  not the actual 4 PM close (the EOD routine deliberately places no orders, so a true daily-close
  evaluation isn't currently wired). Pick one: **(a)** amend the IPS/policy text to describe the
  mechanism as implemented ("morning evaluation," recommended — cheapest, no behavior change), or
  **(b)** build a true close-based evaluation (bigger change: would need the EOD routine or a
  same-day-later check to place orders, which it currently structurally cannot do).

---

## ✅ Live routine prompt sync — DONE (2026-07-03) *(superseded by item 0 above)*

- **[x] Secrets stripped, keys rotated; daily + EOD synced to the Jul-3 prompts.**

---

## 🟠 Data-layer gates (Phase 2 — deployed) + universe expansion

### [ ] 3. Verify PyYAML is installed in the cloud routine environment
- **Why:** `policy.py` **silently falls back to built-in defaults** if PyYAML is missing.
  Phase 2 shipped `policy_version → 1.1-phase2-dataquality` and `price_outlier_pct` — a cloud
  env without PyYAML keeps using the old defaults ("works locally, silent no-op in prod").
- **How:** confirm the routine's `pip install -r requirements.txt` succeeds (`PyYAML` is in
  `requirements.txt`), or check a cloud run log for a `⚠ policy.yaml not loaded` warning.

### [ ] 6. `UNIVERSE_EXPANDED` — coverage gate now MET; cursor wiring still pending
- **Status update (2026-07-02):** condition (a) is **satisfied** — GH Actions logs now show
  **96% fundamental coverage, `data_quality.coverage_ok=true`** (the SEC User-Agent 403 fix).
  Condition (b) is **NOT yet met**: the resumable fetch cursor (`universe.next_batch/
  save_batch`) is built + tested but **still not wired into the fetch loop** (Phase 4 raw→
  curated storage split, §12). Fetching ~400×210-day histories under Polygon's 5-calls/min
  needs the batch-fetch + history carry-forward first.
- **Do NOT set `UNIVERSE_EXPANDED=true` yet** — a run would fetch only a partial universe.
  Flip it (in the `market_data.yml` env) only after the Phase 4 cursor wiring lands AND you've
  seen coverage hold ≥80% over the *expanded* set.

---

## 🟡 Phase 3 (observability — deployed): monitor, no action required

### [ ] 7. Watch the two NEW workflows' first scheduled runs
- `heartbeat.yml` (weekdays 6 PM ET) + `pipeline_digest.yml` (Fri 6:30 PM ET) auto-activate on
  merge — no setup needed. Just confirm their first *scheduled* (not dispatch) runs go green in
  the Actions tab, and that a `heartbeat-alert` issue opens/closes correctly the first time an
  artifact is genuinely missing. **DST:** both new crons are intentionally left OUT of
  `update_dst.yml` — a 1-hour seasonal drift is harmless (both fire after the 4 PM close).
- Permissions are declared in-workflow (`issues: write` / `contents: write`); no repo-setting
  change required.

---

## 🔵 Phase 4 (research pipeline — landing incrementally)

### [ ] 8. The dossier is PRODUCER-ONLY so far — the consumer change is a FUTURE routine sync
- **What shipped (increment 1):** `build_dossier.py` builds + schema-validates + commits
  `research_dossier.json` from GH Actions (zero order code — a research artifact only). It does
  NOT yet drive any decision.
- **Not yet done (later increment, WILL need a routine-prompt sync):** having the cloud routine
  read `research_dossier.json` instead of the raw snapshot, with a gate freshness check
  (`dossier.as_of == today` AND `built_from_days ≥ 2` → else SKIP/RETRY). That is an
  execution-adjacent change and must be coordinated with the Phase 5 weekly cadence — do not
  wire the consumer piecemeal.
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
- **Deferred sub-workstreams (documented, not built):** per-lot FIFO tax dates (P0-4).
- **Deferred `/code-review high` findings (non-correctness — tracked, not blocking):**
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

### [ ] 11. `since_entry` dossier anchor is structurally always `None` (found 2026-07-05, Phase 1 dry-verify)
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

### [ ] 9. Known live DATA issue surfaced by the dossier — split-unadjusted history (P0-3)
- The dossier's `history_summary` shows e.g. ORCL `ret_21d ≈ −0.43` — a real artifact of
  **split-unadjusted OHLCV** in the snapshot (`corporate_actions.detect_price_outliers` already
  flags ORCL's ~36% one-day jump). Momentum/vol on such a series is corrupted. The Phase 4/§11.4
  fix is to assert Polygon history is split/dividend-adjusted (the fetch already sends
  `adjusted=true`, so confirm why ORCL slipped through) + the delisting/M&A handler for held
  names. **Not a dossier bug — a data-source correctness item to run down before the dossier
  drives trades.**
- **Follow-up refinement (from the Jul 4-5 review):** `corporate_actions.detect_price_outliers`
  currently only FLAGS an outlier into `data_quality` — `quant_engine.score_all_tickers` still
  consumes the corrupted series for momentum/vol either way. Once the source fix above lands,
  consider whether a flagged ticker should also be QUARANTINED from scoring (its composite
  reported N/A rather than a number computed on a known-bad series) as defense-in-depth, not
  just visible-but-unactioned in the data-quality report.

---

## 🟢 Merges / decisions (Claude CAN do these — just say so)

### [ ] 5. (Optional) Settle the two parked IPS open questions
- Both already have sensible defaults applied; only revisit if you disagree with them.

### [ ] 10. (Parked, your call) The Devil's-Advocate-on-holdings nudge (`feat/pm-devil-tension`)
- A June-17 review branch surfaces the DA verdict on PM holdings lines. You deliberately held it
  back ("may increase turnover"). It can't be merged as-is (stale base). If you want it, say so
  and Claude will re-implement it on current `main` **with** the turnover/after-tax trade-off
  called out — turnover is ~54% short-term tax in this account.

---

_Maintained by Claude as new owner-only steps arise. Items move to "done" by deletion or a checked box._
