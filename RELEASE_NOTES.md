# Release Notes — AI Investor

User-facing log of what shipped, when. **Update the `[Unreleased]` section in the
same PR as the change, and on deploy move it to a new dated release block** (see
DEPLOYMENT.md §7.0). Newest first.

> Timestamps are **Pacific Time** (reconstructed from git commit offsets `-0700`
> for pre-existing releases; approximate to the commit, not the live cloud run).
> The system trades a dedicated Robinhood agentic account; inception ≈ **2026-06-04**
> with **$500** starting capital.

---

## [Unreleased]

### Security — remove unused API secrets from both routine prompts

Both canonical routine prompts (`ROUTINE_DAILY_CYCLE.md`, `ROUTINE_EOD_CLOSE.md`) had `.env`
blocks writing `POLYGON_API_KEY` / `SUPABASE_URL` / `SUPABASE_SERVICE_KEY`. Verified against the
code that **all three are unused in the cloud plane** (it 403s on both services): `market_data.py`
reads the committed `market_snapshot.json`, and `publish.py` writes `portfolio_snapshot.json`
then skips Supabase cleanly with no keys — the real write runs in GitHub Actions (`publish.yml`)
with the GitHub secret store. STEP 2 is now just `DRY_RUN=true` with a comment explaining why no
secrets belong in the prompt. **Requires a live-routine sync** (MANUAL_TODO #0) to take effect,
and the pasted keys should be rotated.

### Added — Phase 4 (increment 3): `_as_of_filing` stamping (SEC provider)

Makes the dossier's no-look-ahead fundamentals guard **live instead of inert**.
`SECProvider.fundamentals` now stamps `_as_of_filing` — the SEC 10-K `filed` date (when
the figure became public), taken as the LATEST filing among the inputs used (conservative:
the bundle isn't available until its last input was filed).

- **Impact:** the dossier's `fundamentals_age_days` and `fundamentals_stale` are now REAL
  (were always `null` — increment 1 built the reader, but nothing stamped the date). The
  future-filing look-ahead drop (`_as_of_filing > as_of` → drop) now actually fires. Verified
  live: CAT `2026-02-13`, DE `2025-12-18`, JNJ `2026-02-11`.
- **Honest partial coverage:** FMP-covered names (~35%) still lack a filing date (FMP TTM has
  no single filing date) → they report vintage-unknown (`age=null`, `stale=null`), which the
  dossier already handles. `fundamental_coverage` is unaffected (it counts the quality fields,
  not `_as_of_filing`).
- **`/code-review` fix:** stamp `_as_of_filing` ONLY when *every* contributing input has a
  filed date — a partial set could `max()` over the present subset and **understate** vintage,
  which in a historical replay is the look-ahead direction (a not-yet-public filing slipping
  past the `> as_of` drop). Missing → vintage unknown (honest). Cross-file/cache/conventions
  angle returned clean.
- **Producer-side, zero order-path change.** **QA:** **577 tests green** (+4: `TestSECFilingDate`,
  `TestSECFilingDatePartial`).

### Added — Phase 4 (increment 2): the Haiku event digest (`event_digest.py`)

Fills the `events.jsonl` time series the dossier already consumes (increment 1 wired the
reader). Turns the snapshot's raw ~50-article news feed into a small, deduped, per-ticker
set of MATERIAL structured events (`{date, ticker, type, summary, url}`) — compression +
attribution so the Wednesday agents read a handful of real events per name, not the firehose.

- **Runs in GitHub Actions** (the research plane can reach the Anthropic API) as Step 4 of
  `fetch_snapshot.py`, BEFORE `build_dossier` (Step 5) so today's events reach the dossier.
  Uses **Haiku** with a prompt-cached system block; news is **chunked** (20/call) so token
  spend is bounded and one bad chunk degrades gracefully.
- **Enrichment, never gating (§11.4):** a parse failure is recorded (parse-success rate;
  <80% → DEGRADED) but NEVER blocks the pipeline. **Capital-integrity:** research artifact
  only, zero order code.
- **No look-ahead:** an event dated after `as_of` (future-stamped article) is dropped;
  hallucinated/untracked tickers are filtered to the universe; unknown types map to `other`.
- **Idempotent:** dedup by a stable `(ticker,date,type,summary)` key against today's rows,
  so a same-day re-run never duplicates. Folds in per-mover `ticker_news`.
- **Reuses `analysis._safe_call` / `_cached_system` / `MODEL_FAST`** (the existing Haiku
  client + JSON-parse + retry spine); the LLM call is injectable so tests never hit the network.
- **Full `/code-review high` — findings remediated:** (1) **cross-day duplication** — the
  Polygon feed re-surfaces multi-day-old articles, and dedup was bounded to *today* only, so a
  persistent article was re-appended daily; dedup now spans a 60-day window. (2) A Haiku
  response that's a lone object or `{"events":[…]}` is now coerced to a list (was silently
  dropped **and** miscounted as a parse failure, spuriously tripping DEGRADED). (3) A non-ISO/
  epoch date is dropped (was slipping past the no-look-ahead guard as a bogus date). (4)
  `event_key` uses the full summary (an 80-char prefix could collide two distinct events). (5)
  **the DEGRADED signal now reaches production** — a <80% parse rate floors
  `data_quality_report.json` at DEGRADED (→ cloud `data_quality` health check → alert.yml);
  previously that computation lived only in the standalone CLI path. Deferred (tracked
  MANUAL_TODO #8): `events.jsonl` retention/§12.4 storage split; prompt-injection is an accepted
  enrichment-only limitation. Auth/shape/capital-integrity/conventions verified clean.
- **QA:** **573 tests green** (+14: `TestEventDigest`, `TestEventDigestRemediation`).
  `market_data.yml` gains `ANTHROPIC_API_KEY` in the fetch env + `events.jsonl` in the commit.

## [2026-07-02] — Phase 4 (increment 1): research dossier producer  ·  PR #20 · main

### Added — Phase 4 (increment 1): the research dossier producer (`build_dossier.py`)

The single synthesis point of the research pipeline (§11.3/§12.2). `build_dossier.py`
collapses the raw append-only layer (snapshot + `factor_history` + fundamentals + events
+ decision journal) into `research_dossier.json` — one small, denormalized, as-of-dated
record per ticker — the artifact the Wednesday decision agents will eventually read
instead of a firehose (206 OHLCV bars + 50 news articles per name).

- **Capital-integrity invariant honored:** runs in GitHub Actions, writes a research
  artifact ONLY, contains **zero order code**. Blast radius of any bug = "degraded
  dossier," never "unintended trade." Wired as Step 5 of `fetch_snapshot.py`; committed by
  `market_data.yml`.
- **No look-ahead:** a fundamental whose `_as_of_filing` is after `as_of` is dropped;
  persistence is computed only *within* one `formula_version` (never across a re-weight
  boundary — P0-2). Per-ticker `price_as_of` stamped (P0-1) so the future consumer can
  re-quote live rather than trust a 1–4-day-old slice price.
- **Schema validation (P1-5):** `validate_dossier()` gates on required top-level + per-
  ticker keys AND freshness (`as_of == today`, `built_from_days ≥ 2`) — a malformed/stale
  dossier must ABORT the Wednesday gate, never be silently traded on.
- **Reuses the deterministic spine unchanged** (`quant_engine._pct_return` /
  `compute_risk_metrics`; journal + `corporate_actions` helpers) — no divergence from
  scoring. Returns stored as fractions to match the §12.2 contract.
- **PRODUCER ONLY:** the cloud routine does NOT yet consume the dossier — that consumer
  change (+ gate freshness routing) is a later increment coordinated with Phase 5. See
  MANUAL_TODO #8. Deferred: Haiku event digest → `events.jsonl`, `_as_of_filing`
  fundamentals stamping, per-lot FIFO tax dates.
- **Surfaced a real data issue (P0-3):** the dossier exposed split-unadjusted history
  (ORCL `ret_21d ≈ −0.43`) — tracked in MANUAL_TODO #9, not a builder bug.
- **Full multi-agent `/code-review high` — 9 correctness findings remediated:**
  `vol_ann` was reading a non-existent key (`annualized_vol` → `volatility`, was *always
  null*); the build-time freshness gate was a tautology (`validate` now runs against the
  real ET trading date, and asserts the newest factor day == today); `as_of=None` now
  fails loud instead of crashing mid-loop; `_persistence` returns a fixed key set (no
  consumer `KeyError`); a `ticker=None` factor row no longer pollutes the rank maps;
  `rank_chg_7d` is a true 7-day lookback with a numeric guard; the top-level
  `formula_version` comes from the newest row (not an arbitrary ticker); an invalid/stale
  dossier is **no longer written over the committed good one**; `fundamentals_stale` is
  `null` (not `false`) when vintage is unknown, and a future epoch-ms `_as_of_filing` can't
  bypass the no-look-ahead drop. Deferred (non-correctness, tracked in MANUAL_TODO #8):
  storage-wall/git-bloat (§12.4), whole-file reads, `_read_jsonl` reuse, tunables → policy.
- **QA:** **559 tests green** (+21: `TestBuildDossier`, `TestDossierValidation`,
  `TestBuildDossierRemediation`).

## [2026-07-02] — Phase 2 (data layer) + Phase 3 (observability & alerting)  ·  main

### Fixed — `market_data.yml` push race (Phase 3 follow-up)

Live CI validation of the new workflows surfaced it: a bare `git push` in the snapshot
step fails with "failed to push some refs" if another main-pushing workflow (the new
`pipeline_digest`, the EOD publish, a concurrent retry) lands first — dropping the
snapshot + `factor_history` + `data_quality` artifacts for that run. Added a
rebase-and-retry loop (same durability lesson as the routine STEP 5 push). Nothing else
rewrites these files, so the rebase is safe. **Validated end-to-end:** `data_quality_
report.json` (status OK, 96% coverage) + a fresh `data_quality_history.jsonl` row now
reach `main` from the GitHub Actions path; heartbeat + weekly digest both run clean.

### Added — Phase 3: Observability & alerting (the safety net)

"Nothing fails silently" — the layer that protects the year-end verdict from being
confounded by a starved pipeline. All offline / GitHub-Actions; **no live-order-path
code changed** (the `main.py` touch is a health-check record + a provenance dict on
the pending envelope, not order/qty/idempotency).

- **`data_quality.py` — the data-quality gate (§15.2).** `classify_data_quality(snapshot)`
  scores every run against **ABSOLUTE floors** (not delta — a *steady* 28% coverage never
  "drops", which is exactly why June's delta-blind check missed it): universe-fetched %,
  min history depth, fundamental (quality) coverage, and a NaN/Inf scan. Emits an
  OK/DEGRADED/ABORT status, a 0–100 `data_quality_score`, and the floor breaches. Writes
  `data_quality_report.json` + append-mirrors `data_quality_history.jsonl` (the trend the
  digest reads). Valuation coverage is reported but **never gates** (FMP-capped ~35%).
- **Provenance stamp (§15.1).** Every `forecasts.jsonl` row and the `pending_decisions.json`
  envelope now carry `{data_quality_score, status, hash}`, so the harness can **partition
  the December verdict by data quality** and exclude below-floor runs instead of silently
  averaging a starved run in. `main.py` records a first-class `data_quality` health check.
- **Heartbeat dead-man's switch (§15.4) — `heartbeat_check.py` + `heartbeat.yml`.** Runs
  late each weekday and asserts every expected daily artifact exists and is dated today.
  **Tiered** so it never false-alarms: a missing routine artifact is a failure only when
  the *data plane* is fresh (the routine should have run); if the data plane itself is
  stale the routine correctly skipped, so no cascade. Non-trading days self-skip. Opens/
  closes a `heartbeat-alert` GitHub Issue. Catches the class the per-flow checks can't —
  Jun-11 silent cron skip, Jun-18 dead feed.
- **Weekly pipeline-integrity digest (§15.5) — `pipeline_digest.py` + `pipeline_digest.yml`.**
  Friday summary of the week's coverage trend, data-quality score, DEGRADED/ABORT days, and
  abort rate → committed `pipeline_digest.md`. Slow drift (coverage creeping 85%→60% over a
  month) is visible here before it is a crisis.
- **`market_calendar.py` — single-source NYSE calendar.** Extracted from `preflight_gate`
  (which now imports it) so the heartbeat and gate can't drift on which days are trading days.
- **Chaos suite (§16.4).** Each historical silent-failure reproduced and asserted to trip a
  signal: chronic 28% coverage → DEGRADED + strategy-shift block; steady-low (no-drop) still
  caught; NaN close → DEGRADED; partial fetch → universe-floor DEGRADED/ABORT; thin history →
  ABORT; cron skip → heartbeat alert. **537 tests green (+27).**
- **Scope:** the §15.3 matrix rows that depend on Phase 4/5 flows (dossier, risk_watch,
  rebalance-ISO-week, event digest, GH-LLM token budget) are intentionally deferred — the
  classifier/heartbeat are structured to slot them in without a rewrite.

### Fixed — Phase 2: SEC EDGAR User-Agent 403 (the actual cause of the coverage collapse)

The Phase 2 coverage *detector* did its job: the first real CI run (`full_refresh`, 100 names)
surfaced `cik_map_ok: false` with only **41%** quality coverage — EDGAR was contributing nothing
even from GitHub Actions. Root cause was **not** IP blocking (the earlier hypothesis): SEC's Akamai
WAF rejects the bot-style User-Agent `ai-investor-bot/1.0 …` with **HTTP 403**. SEC fair-access
requires the documented `Company Name contact@email` form. Fixed the `SECProvider.HEADERS` UA to
that form (verified: **200 + 10,426 CIK entries**).
- **Impact:** projected quality coverage over the core universe jumps **41% → 96%** (misses are
  ARM/SPOT foreign 20-F filers + the 2 ETFs — correct). This clears the IPS 80% floor, so
  `coverage_ok` flips True and the composite re-weight (quality 35%) is now backed by real data
  rather than renormalizing away — the `⛔ RE-WEIGHT NOT FAIRLY TESTED` caveat can clear once CI
  confirms the number on the full pipeline.
- **No live-order-path change.** Enrichment/scoring only; runs in GitHub Actions (`fetch_snapshot.py`).

### Fixed — Phase 2: `full_refresh` now bypasses the enrichment TTL

Follow-on from the UA fix. The first post-fix CI run showed `cik_map_ok: True` (map loads) but
coverage *still* 41% — the `provider_cache.json` (restored via `actions/cache`) held **empty**
entries stamped today from the 403-era runs, and the manual `full_refresh` bypassed only the 50/50
group, **not** the per-ticker TTL (`age 0 < 7` → "not due"). So a manual "refresh all" couldn't
recover stale empties — they stayed pinned for 7 days. `full_refresh` now forces `due=True`, the
correct semantic. Regression test `test_full_refresh_bypasses_ttl_on_stale_empty`. (Normal daily
crons heal without this too, just slower; the flag is the operator escape hatch.)

### Fixed — Phase 2: code-review remediation (`/code-review high`, 6-angle × verify)

Findings from the pre-PR expert review, remediated before opening the PR:
- **`detect_price_outliers` emitted raw epoch-ms integers** as the finding `date` (live snapshot
  bars carry Polygon epoch-ms, not ISO). Now normalized to `YYYY-MM-DD` (`_norm_date`) — the log
  line and `data_quality.price_outliers` are readable + consistent with every other date field.
- **Coverage was computed by two divergent copies** (`market_data` + `backtest/engine`), each with
  its own quality-fields tuple — the exact "backtest clears the floor, live doesn't" drift risk.
  Consolidated into ONE `data_providers.fundamental_coverage`. It now also reports **valuation
  coverage separately** (the gate is on QUALITY, EDGAR-achievable; valuation is FMP-capped ~35% and
  transparency-only) — closes the "coverage_ok True while valuation can't express" depth gap.
- **`log_factor_history` used a non-atomic temp-file dance + an O(whole-file) dedup read.** Replaced
  with a plain append (repo ledger convention) and a dedup set bounded to *today's* rows.
- **Fetch cursor keyed on universe *size* only** → a same-length membership swap silently resumed
  mid-sweep (coverage gap). Now keyed on a content **fingerprint** — any membership/order change
  resets the sweep.
- **`SECProvider` empty-200 body** now records a diagnostic error string (was a silent blank map);
  **`price_outlier_pct`** is also honored if placed under `guardrails:` (was a silent no-op);
  **`cik_map_ok` banner softened** (SEC is a fallback — FMP data may be fine); **`FORMULA_VERSION`
  comment** de-overstated (it's a provenance label the future IC analyzer must group by, not an
  enforced invariant).
- **Accepted/deferred (documented):** `universe` cursor + `find_unpriced_holdings` live wiring →
  Phase 4/5; a `factor_history` freshness alert → Phase 3; the live re-weight lands before a *fair*
  backtest can run (blocked on GH-Actions coverage) — intentional per plan §9, surfaced for the owner.
- **QA:** **509 green** (+5: valuation coverage, shared-helper agreement, epoch-ms→ISO, cursor
  content-swap reset, empty-200 diagnostic).

### Changed — Phase 2: quant-only shadow arm re-backtested on the new weighting + coverage gate

- **Re-ran `backtest/` on the quality-tilted composite** (it reuses `score_all_tickers` unchanged).
  The report is now explicitly framed as the **quant-only SHADOW ARM** (IPS §3.3 baseline the LLM
  book is measured against) and stamps `formula_version` + `fundamental_coverage_pct`.
- **Honest verdict (with the caveat that makes it honest):** on the committed 2026-06-26 snapshot
  the re-weighted quant-only arm returns **−3.96% vs SPY +10.42%** (after-tax alpha −14.38%). **This
  is NOT a fair test of the re-weight** — snapshot coverage is **39.8%**, far below the 80% floor, so
  the quality/valuation tilt cannot express (61% of names score momentum+vol only and the higher
  quality weight just renormalizes away). The report now **inserts a loud `⛔ RE-WEIGHT NOT FAIRLY
  TESTED` caveat** below the floor and directs a re-run once GH Actions coverage clears (plan §9-3).
  No verdict on the re-weight is drawn until then — per the quant-researcher discipline, an
  unproven signal is reported as unproven, not dressed up.
- **QA:** +4 (formula-version stamp, below-floor caveat present / above-floor absent, backtest
  determinism / reproducibility).

### Added — Phase 2: gated universe expansion (→ ~400) + resumable fetch cursor

- **`universe.py`** — new single source of truth for the trading/scoring universe. `CORE_UNIVERSE`
  (the historical 100-name WATCHLIST, verbatim; `market_data.WATCHLIST` now aliases it — DRY) plus
  `EXPANDED_UNIVERSE` (**393 S&P-500-class names**, ~400 target).
- **Coverage-gated:** `get_active_universe(coverage_ok, enabled)` returns the expanded pool ONLY
  when the operator has enabled it (`UNIVERSE_EXPANDED` env, default OFF) **AND** fundamental
  coverage has cleared the 80% floor. Both required — a wider pool on thin coverage adds
  momentum-only names with no quality/value signal. **Zero behavior change now:** active universe
  stays the core 100 until the operator flips the flag after verifying coverage in GH Actions logs.
- **Resumable cursor** (`next_batch`/`save_batch`, `fetch_progress.json`) — hands out bounded fetch
  batches with a wrap-around cursor persisted only on success, so a crash retries the same batch
  (no gap) and a universe-size change resets to a fresh sweep. Needed because 400×210-day histories
  can't be fetched in one run under Polygon's 5-calls/min. **Cursor is built + tested but not yet
  wired into the fetch loop** — that needs the history carry-forward (Phase 4 storage split);
  documented in MANUAL_TODO #6 as a hard prerequisite before enabling expansion.
- **QA:** +9 (`TestUniverse`: core/expanded, gate requires both conditions, env flag, sequential
  batches, wrap-around, size-change reset, crash-retry-same-batch, empty-universe).

### Added — Phase 2: corporate-action / split-adjustment guard + delisting detection (P0-3)

- **Explicit `adjusted=true`** on the Polygon aggregates call (`market_data.get_extended_history`) —
  no longer relying on the API default. An unadjusted split reads as a ~-50% one-day crash and
  poisons momentum/vol for that name.
- **`corporate_actions.py`** (new, detection-only): `detect_price_outliers` flags any 1-day move
  beyond the IPS `price_outlier_pct` (35%) with no corporate action as a **suspect print**
  (unhandled split / bad data) — a REVIEW signal, not an auto-drop (a genuine earnings crash must
  not be discarded). `find_unpriced_holdings` surfaces held names with no fresh price (likely
  delisting / M&A) so a rebalance isn't sized against a stale basis. Outliers are recorded on the
  snapshot's `data_quality.price_outliers`. On the live 2026-06-26 snapshot it correctly flags 3
  genuine earnings gaps (MDB +38%, SNOW +36%, ORCL +36%).
- **`policy.price_outlier_pct` (35)** migrated from IPS Appendix A into `policy.yaml`/`policy.py`
  with a fraction-typo validator (rejects `0.35`); `policy_version` → **`1.1-phase2-dataquality`**
  (detection-only — all guardrail values remain the Phase 0 parity baseline). Dividends are
  intentionally NOT adjusted: book and SPY/QQQ benchmarks are both price-return, so it's consistent.
- **Live-path note:** the delisting SELL wiring lands in Phase 5 (`risk_watch`, `ultra` gate); Phase 2
  ships the detector + tests only.
- **QA:** +10 (`TestCorporateActions` ×8, `TestPriceOutlierPolicyParam` ×2).

### Changed — Phase 2: composite re-weight (quality tilt) + `formula_version` + factor_history

- **Re-weighted `FACTOR_WEIGHTS`** from `momentum .30 / quality .25 / valuation .20 / low-vol .25`
  → **`momentum .15 / quality .35 / valuation .25 / low-vol .25`** — momentum demoted to a minor
  confirm; quality/value/low-vol carry the signal. Rationale (IPS 9–12mo horizon, CA top-bracket):
  momentum is short-horizon and turnover-heavy (tax-suicidal); quality/value/low-vol are the
  persistent, lower-turnover factors for a multi-quarter hold. **Deterministic change — its edge is
  proven or falsified in `backtest/`, not on faith**, and gated on the coverage fix (quality/value
  are only real once EDGAR clears the 80% floor).
- **`FORMULA_VERSION` ("2.0-quality-tilt")** stamped on every composite score and every
  `factor_history` row. IC / factor persistence must **never** be computed across a formula
  boundary (mixing pre-/post-reweight composites corrupts the signal) — **P0-2**.
- **`factor_history.jsonl`** — new append-only, full-universe, point-in-time factor time series
  (`quant_engine.log_factor_history`), idempotent per `(date, ticker, formula_version)`, the
  substrate for factor-persistence / IC. Written & committed by the **GH Actions** path
  (`fetch_snapshot.py` + `market_data.yml`) — the only plane that scores the whole universe daily.
- **Observed:** on the committed pre-fix 2026-06-26 snapshot, quality coverage is **39/100 (39%)** —
  below the 80% floor, exactly what the new measurement is meant to surface; universe expansion
  stays gated until GH Actions (with EDGAR reachable) clears the floor.
- **QA:** +7 (`TestFactorHistory` ×4, formula_version stamp + `FACTOR_WEIGHTS`-derived composite tests).

### Fixed — Phase 2: SEC fundamental-coverage swallow + first-class coverage measurement

- **Root cause (`data_providers.py`):** `SECProvider._ensure_cik_map` swallowed *any* CIK-map
  fetch failure into `self._cik = {}` with **no signal** — after which every ticker lookup returned
  `None`, collapsing fundamental coverage to 0% invisibly (the June 28%-coverage incident class).
  Worse, an empty dict is falsy, so the "load once" guard never latched and every per-ticker call
  re-hit SEC (a silent retry storm).
- **Fix:** the CIK-map load is now attempted **exactly once** (latched on `_cik_load_attempted`),
  and its outcome is recorded on `_cik_load_ok`. New `SECProvider.cik_map_ok()` (and a
  `CascadeProvider` delegate) let the enrichment layer distinguish a real load **failure**
  (→ surfaced loudly, recorded) from a legitimate ticker-not-in-map (→ `None`). `raise_for_status()`
  now treats a 403/500 as a failure instead of parsing an error body into an empty map.
- **Measurement:** every snapshot now carries a **`data_quality`** block —
  `fundamental_coverage_pct`, `fundamentals_covered/active_universe`, `cik_map_ok`, and an
  **absolute** `coverage_ok` gate against the IPS **80% floor** (`market_data._compute_fundamental_coverage`).
  The floor is absolute, not a WoW delta, because a *steady* 28% (nothing dropping) was the exact
  June bug a delta check would have missed. Coverage is printed each fetch.
- **QA:** **477 green** (+8: CIK-map ok/failure/http-error/once-on-failure, Cascade delegate,
  coverage counts quality fields / above-floor / empty-universe).

### Fixed — Phase 1: forecast feed un-broken + backfilled (the measurement evidence clock)

- **Root cause (silent since 2026-06-18):** `forecasts.jsonl` / `forecasts_scored.jsonl` /
  `agent_scorecards.json` were **gitignored and never committed**, and absent from the routine's
  `git add` list — so every cloud run wrote ~60–90 forecasts into an ephemeral container that were
  then lost. **Not a `calibration.py` bug** (values are numeric, structure is correct) — the same
  silent-`git add`-no-op class as the Jun-17 `fills.json` fix. Diagnosed per the plan's "fix the feed
  FIRST" gate (wiring scoring against a dead feed would green-light an empty scorecard).
- **Fix:** un-ignored the three ledger files (`.gitignore` now documents *why* they must be tracked);
  added them to the routine's claim + daily-cycle `git add` lines (`ROUTINE_DAILY_CYCLE.md`) and the
  `CLAUDE.md` commit list.
- **Backfill:** reconstructed the entire ledger from `agent_log.json` (committed; carries full
  `pipeline_state` for all 21 runs) → **1,494 forecasts across 12 trading days** (06-08 → 06-26),
  recovered vs the 144 stranded locally. `signal_close` recovered from `market_snapshot.json` history
  (reference-only field). 2026-06-19 correctly yields 0 (Juneteenth — market closed, no signal).
- **⚠️ Live-routine sync required:** the live daily routine prompt must be re-synced from
  `ROUTINE_DAILY_CYCLE.md` (routines UI) or the cloud `git add` still omits the ledger.
- **QA:** **456 green** (+3 `TestForecastFeedPersistence`: not-gitignored regression guard, in-commit-list
  guard, ledger integrity / no-dup-keys).

### Added — Phase 1: multi-horizon forecast ladder {21,63,126,189,252}d (§7.3.2)

- **`log_forecasts` now logs each forecast at every horizon** in `calibration.HORIZONS`
  `(21,63,126,189,252)` — a medium/long-term signal should look weak at 21d and strengthen
  at 63–252d (189/252 ≈ 9/12mo = the owner's primary holding horizon). 21d stays the
  pre-registered PRIMARY metric; the rest are BH-adjusted secondary.
- **`score_matured` idempotency key now includes `horizon_days` (P1-9)** — one forecast
  matures at several horizons; the old `(run_id,agent,field,ticker)` key would have scored
  only the first and skipped the rest. **`agent_scorecard` groups by `(agent,field,horizon)`**
  → an IC curve per agent across horizons (card keys are now `agent.field@<h>d`).
- **Re-backfilled** the ledger at all horizons → **7,470 forecasts** (1,494 × 5), no dup keys.
- **QA:** **457 green** (+1 `test_score_matured_multi_horizon_independent` guarding P1-9; 3
  existing calibration tests updated for the new counts/keys). End-to-end smoke test on real
  data: 0 matured (correct — earliest forecast 06-08 + 21d > the 06-26 snapshot), scorecard
  primary key `quant.composite_score@21d`.

### Added — Phase 1: scoring wired into the run (the evidence clock now self-advances)

- **`main.py` now calls `score_matured` + `agent_scorecard` every run** (observational,
  try/except-wrapped, never raises into the pipeline). The harness was fully built but
  *switched off* — these were called only from tests, so the clock never advanced. Now each
  run joins matured forecasts to realized next-open forward returns (no look-ahead) and
  rewrites `agent_scorecards.json`.
- **File-existence guarantee:** `score_matured` only appends when something matured, so the
  wiring touches `forecasts_scored.jsonl` to ensure it exists — the routine's `git add` of it
  can never fail on a missing file (the silent-break class that froze the feed). Both outputs
  exist before the routine's commit step.
- **QA:** **458 green** (+1 `test_scoring_wired_into_run` regression guard against reverting to
  test-only callers). Smoke test: 0 matured (correct), both output files created.

### Added — Phase 1 §7.5: counterfactual rejected-name tracking (the highest-leverage measurement)

- **The system rejects far more than it buys, and never tracked any of it.** New `log_decisions`
  records, per candidate per horizon, three binary decision flags — **`da_reject`** (Devil's
  Advocate `recommend_reject`, M4), **`cro_veto`** (CRO `rejected_tickers`, M7), **`pm_selected`**
  (final BUYs, M6) — scored forward by the *same* `score_matured` machinery (no new scorer).
- **`counterfactual_report`** → `counterfactual.json`: per (signal, horizon), the mean forward
  return of FLAGGED vs KEPT names, the gap, and a `verdict` (`ADDS_VALUE` / `NO_VALUE` /
  `NOT_SIGNIFICANT`). Answers *"do the names the CRO/DA killed actually underperform the ones we
  held?"* — direct evidence of whether M4/M7 reduce risk. Block-sampled effective N; honest
  `NOT_SIGNIFICANT` until both sides clear the threshold (months at this cadence, by design).
- Wired into `main.py` (logging + scoring, observational, never raises); backfilled from
  `agent_log.json` → **5,970 decision flags** (DA rejected 84/398, CRO vetoed 6/398, PM bought
  25/398 at 21d). New ledgers added to `.gitignore`-tracked + the routine commit lists.
- **QA:** **461 green** (+3 `TestCounterfactual`). Smoke test end-to-end: 0 matured (clock ticking).

### Added — Phase 1 §7.6: measurement rigor (TWR · risk-adjusted · breadth ceiling · reconciliation)

- **Time-weighted return** (`_twr`) — chains sub-period returns and removes external cash-flow
  (deposit/withdrawal) distortion; equals the simple cumulative return until a flow is logged.
  This is the methodologically-correct fix for the documented "a deposit inflates total_value →
  false new peak / wrong return" bug.
- **Risk-adjusted, not just return** — added **Sortino** (downside deviation) to `_metrics` and a
  portfolio-level **information ratio** vs SPY total-return. Beating the benchmark's *return* at
  materially higher vol is not a win.
- **Breadth ceiling** (`breadth_ceiling`) — Grinold's Fundamental Law `IR ≈ IC × √breadth` from the
  pre-registered primary metric's block-IC + effective N. Surfaces the structural cap: the honest
  verdict may be "positive IC but breadth too low to beat SPY after tax."
- **Honesty metadata in `build_report`** — a `tax_reconciliation` status (`UNRECONCILED`: the
  after-tax figure is an estimate; the broker 1099 is authoritative) and a `verdict_scope` note (the
  three clocks — 12-mo window vs 9–12mo horizon vs 1–2yr LLM-IC power — so a near-term verdict isn't
  over-claimed).
- **QA:** **467 green** (+6 `TestMeasurementRigor`: TWR no-flow identity + deposit-neutralization,
  Sortino, information ratio, breadth-ceiling availability + Fundamental-Law math). `build_report`
  smoke: all new fields present.

### Added — Phase 0: single-source the deterministic limits into `policy.yaml` (redesign pod)

- **`policy.yaml` + `policy.py` (new):** every operative deterministic limit is now defined in
  exactly one place — `policy.yaml`, the machine mirror of `IPS.md` Appendix A — and read by
  `guardrails.py` and `execute.py` via a tolerant loader. This removes the drift-bug class where
  a limit lived in code, a prompt, and a doc independently (e.g. "the sector limit lived only in
  the PM prompt"). First step of the strategy-redesign pod (`IMPLEMENTATION_PLAN.md` Phase 0).
- **Zero behavior change (parity):** `policy.yaml` carries the historical constants verbatim
  (`MAX_TARGET_WEIGHT 0.10`, `MAX_SECTOR_WEIGHT 0.25`, `MIN_HOLDING_TRADING_DAYS 5`,
  `WASH_SALE_REENTRY_DAYS 30`, `GFV_WINDOW 2`, `MAX_BUY_NOTIONAL_PCT 0.12`,
  `MIN_ORDER_NOTIONAL 5.00`, `MIN_NET_EDGE 0.0`, `BLOCKED_TICKERS {TSLA}`). The IPS *target*
  values (min-hold 30, −25% stop, universe 400) are tracked migrations for later phases, noted in
  `policy.yaml`'s header — NOT applied here.
- **Capital-integrity posture:** the loader falls back to built-in defaults (== the historical
  constants) if `policy.yaml` or PyYAML is unreadable, so it can never change behavior silently or
  break the live trade path. `PyYAML` added to `requirements.txt`.
- **Per-value validation (code-review hardening):** every guardrail value is range/type-checked on
  load — an out-of-range or wrong-type value (e.g. a percent/fraction units typo `max_target_weight:
  10` instead of `0.10`) is **rejected with a loud warning and the safe default kept**, so a typo in
  the capital-limit source of truth can never silently disable a cap.
- **Change-control provenance:** `pending_decisions.json` now carries `policy_version`
  (`1.0-phase0-parity`) so every decision records which policy regime produced it (§18.4).
- **QA:** full suite **453 green** (+12 `TestPolicyParity`: defaults==historical, yaml==historical,
  guardrails/execute sourced-from-policy, missing-file & malformed-yaml fallback, partial overlay,
  version helper, units-typo/wrong-type/bad-list rejection, valid-override acceptance). `main.py`
  compiles; all workflows parse. Expert `/code-review high` run pre-commit (2 findings, both fixed:
  loader validation + dead-code removal). No live-path behavior changed.

### Fixed — Preflight gate now skips closed-market days (weekends + NYSE holidays) (P0)

- **`preflight_gate.py` (P0, live incident):** the gate had no market-calendar awareness. On
  Juneteenth (2026-06-19) the `market_data.yml` job still committed a snapshot stamped with
  *today's* date, so the gate saw "fresh data, not yet executed" and PROCEEDed. The pipeline
  ran and placed 4 GFD orders that the broker accepted but could never fill on a closed
  market — they sat `queued` and would have expired at the (nonexistent) close. New **check 0**
  (`_market_closed_today()`) runs before idempotency/freshness: a weekend or any date in the
  hardcoded `NYSE_HOLIDAYS` calendar (2026–2027) returns **SKIP/RETRY (exit 10)**. Early-close
  half-days still trade and are intentionally not blocked.
- **Manual remediation (2026-06-19):** the 4 queued orders (SELL LIN/GS/PANW, BUY VRTX) were
  cancelled via MCP (all `state=cancelled`, zero fills) and the phantom fills reverted across
  every log — `transactions.json` (dry_run=True, broker_order_id removed), `trades.csv`
  (dry_run=True, order_id cleared), `decision_journal.json` (4 speculative entries → rejected;
  the prematurely-closed PANW BUY thesis reopened), and `fills.json` cleared.
- **`PREFLIGHT_DATE_OVERRIDE` env var:** overrides the gate's effective date for both the
  freshness comparison and the weekday/holiday calendar — makes the date-dependent gate tests
  deterministic and doubles as a manual override.
- Tests: **441 passing** (+5: `TestPreflightGateMarketClosed` — holiday, Sat, Sun, holiday-beats-
  idempotency, open-day control; existing gate tests pinned to a fixed open trading day).

### Added — Sector weights injected into PM context; rebalancing instruction (P1)

- **`analysis.py` (Change 1):** `run_portfolio_manager()` now computes current sector weights
  from holdings before calling the LLM, using `sector_of()` from `guardrails.py`. The PM
  receives a `SECTOR ALLOCATION` block showing every occupied sector with its current %, a
  ⛔ OVER CAP flag for sectors ≥ 25%, and a ⚠ NEAR CAP flag for sectors ≥ 20%. With
  Financials at 26.6% (MS + GS + BAC), the PM now sees the cap is broken before it proposes
  anything — it was previously proposing JPM 14 consecutive runs in a row, always vetoed by
  the CRO, leaving 33.7% cash idle in RISK_ON.
- **`analysis.py` (Change 2):** Rebalancing instruction added to CONSTRAINTS: when a sector
  is AT/NEAR CAP and the PM has higher conviction in a new name, it may propose REDUCE of the
  weakest holding (lowest `hold_score`) in that sector and BUY the new name in the same
  decision list. Turns the sector cap from a dead end into a rebalancing signal.
- Tests: 430 passing; sector logic is a pure data transformation over existing fields.

### Fixed — CRO partial veto wiped all trades; fundamentals backoff 30→7 days; full-refresh mode (P0/P1)

- **`analysis.py` (P0):** CRO partial veto bug — when CRO returned `approved=false` +
  `rejected_tickers=["JPM"]`, the code cleared `decisions=[]` first and then filtered the now-empty
  list. Result: all trades erased and the log falsely reported "CRO removed 0 ticker(s)". Fix:
  only clear all decisions when `rejected_tickers` is empty (true full veto); otherwise remove only
  the named tickers and keep the rest. On 2026-06-18 this silently dropped SELL PANW.
- **`market_data.py` (P1):** empty-entry cache TTL reduced from 30 days to 7. The 62 tickers
  with no FMP coverage had been in a 30-day backoff since before `FMP_API_KEY` was added; with the
  new TTL they retry SEC EDGAR within a week instead of a month.
- **`market_data.py` + `market_data.yml` (P1):** `FULL_REFRESH=true` env var bypasses the
  alternate-day 50/50 group split and processes all 100 tickers in a single workflow run.
  `workflow_dispatch` now exposes a `full_refresh` boolean input so GH Actions can be triggered
  once to seed all 100 tickers simultaneously instead of waiting 2 days.
- **`market_data.yml`:** cache key bumped `fundamentals-` → `fundamentals-v2-` to bust the old
  GH Actions cache carrying the 30-day-backoff empty entries.

### Fixed — Portfolio Manager truncation: 2× agent token budgets + tightened PM schema (P0)

- **`analysis.py`:** doubled `max_tokens` for all 7 agents (Regime 770→1540, Research
  1100→2200, Earnings 660→1320, Devil's Advocate 4125→8250, Position Review 440→880,
  **Portfolio Manager 1320→2640**, CRO 440→880). The PM was hitting `stop_reason=max_tokens`
  mid-JSON and returning `[]` — a parse failure masquerading as a deliberate no-trade —
  silently dropping live REDUCE/BUY signals (e.g. the PANW REDUCE on 2026-06-18).
- **PM output schema tightened:** the response must start with `[`, contain no prose or
  markdown, and cap each `rationale` at 10 words — front-loading compactness so a verbose
  response can no longer truncate the entire trade list.
- Tests: full suite green (430), no new behavior to cover (token/prompt tuning only).

### Added — CascadeProvider: FMP + SEC EDGAR for near-100% quality signal coverage (P2)

- **`data_providers.py`:** new `CascadeProvider` class wraps `FMPProvider` + `SECProvider`.
  When `FMP_API_KEY` is set, `get_provider()` now returns `CascadeProvider` instead of
  `FMPProvider`. FMP is tried first for all 6 factors; on a free-tier miss (402), SEC EDGAR
  fills in `gross_margin / operating_margin / debt_to_equity` for free. FMP covered ~37/100
  tickers; cascade targets ~100% for quality factors and ~37% for full 6-factor coverage.
  The alternate-day 50/50 cache in `_enrich_with_provider` is unchanged — the cascade is
  transparent to the caching layer (one merged dict per ticker per TTL).
- **`test_pipeline.py`:** `TestCascadeProvider` (7 tests) — FMP hit/miss/partial, FMP wins
  on overlap, both-None → None, earnings/estimates delegate to primary, factory returns
  `CascadeProvider` when FMP key is set. Updated existing `test_get_provider_factory` to
  expect `CascadeProvider` instead of `FMPProvider`.

### Added — Consecutive-run cash discipline tracking (P2)

- **`journal.py`:** `consecutive_cash_above(threshold)` reads `agent_log.json` (capped at
  90 entries; already committed per run) and returns the count of consecutive recent pipeline
  runs where `cash_pct > threshold`. The current run's portfolio_snapshot is written to
  agent_log before this is called, so the count includes today.
- **`main.py`:** `cash_discipline` DEGRADED health message now includes
  `consecutive_runs_above_threshold` — e.g. "Cash 33.5% exceeds 15% ceiling — 3 consecutive
  run(s) above threshold". A single-day overage is noise; a multi-day streak is a structural
  signal worth reviewing.
- **`test_pipeline.py`:** `TestConsecutiveCashAbove` (6 tests) — streak counting, broken
  streak, no-streak when last run is below, empty log, missing total_value.

### Fixed — PM retry waste on genuine no-trade (P3)

- **`analysis.py` `_safe_call`:** when `return_meta=True` (used by `run_portfolio_manager`
  only) and `parsed_ok=True`, a result equal to the default (e.g. PM proposes `[]`) is now
  returned immediately without retrying. Previously a genuine "no trades today" response
  triggered 2 unnecessary retries, burning ~2× the token cost for nothing. Parse failures
  (`parsed_ok=False`) still retry as before.
- **`test_pipeline.py`:** `TestSafeCallNoRetryOnGenuineDefault` (3 tests) — no retry on
  genuine `[]`, retry still fires on parse failure without meta, retry still fires on parse
  failure with meta.

### Chore — branch hygiene (P3)

- Deleted 10 stale `claude/*` local branches (GH Actions worktree leftovers).

---

## [2026-06-17] — post-run gap audit: regime publish · alerting · PM auditability  ·  ~16:08 PT  ·  main

### Fixed — live dashboard published a STALE regime (P0 data)

- **`publish.py` + `main.py`:** the published regime was read from the *previous*
  day's `portfolio_snapshot.json` first; because any non-empty string is truthy,
  the `agent_log.json` fallback never ran, so a **RISK_ON run was shown as NEUTRAL**
  on the public dashboard. `publish_to_supabase()` now takes an explicit `regime=`
  arg (passed by `main.py` from the live pipeline) and resolves
  **arg → today's agent_log → snapshot file (last resort)**, with a date guard so a
  prior-day agent_log is treated as stale too.

### Fixed — Supabase egress 403 no longer marks every clean cloud run FAILED (P1)

- **`main.py`:** the expected cloud egress block (`Host not in allowlist`) is now
  recorded **OK** ("publish deferred to GitHub Actions"), not FAILED. Previously
  every clean cloud run reported `overall_status=FAILED`, making the health signal
  pure noise and **blocking `alert.yml` from ever auto-closing** a recovered issue
  (status never returned to OK). A genuine publish error (bad key, schema) is still
  FAILED. Downstream `health_check.yml` still verifies the row actually landed.

### Fixed — a mangled PM response could masquerade as a deliberate no-trade (P1)

- **`analysis.py` + `main.py`:** `_safe_call(return_meta=True)` now reports whether
  the model output actually parsed, so the Portfolio Manager returning `[]` because
  it **failed to parse** is distinguished from a genuine no-trade. The agent_6 health
  check records DEGRADED on a parse-failure `[]`, and the raw PM response is logged
  to `agent_log.json` (`portfolio_manager_raw`) for auditability.

### Added — cash-discipline observability signal (P1, alert-only)

- **`main.py`:** a `cash_discipline` health check records DEGRADED when cash exceeds
  `CASH_DISCIPLINE_PCT` (15%) **and** the run places no BUYs — surfacing idle capital
  for review (observed: 33.5% cash in a RISK_ON regime, 0 trades). It **does not**
  force trades; auto-deploying would churn a CA top-bracket taxable account against
  the turnover/wash-sale guards.

### Changed — `fills.json` is now tracked; `alert.yml` is manually dispatchable

- **`.gitignore`:** `fills.json` un-ignored so the broker-fill audit trail reaches
  the remote (a crash after STEP 4 can now be reconciled from it).
- **`alert.yml`:** added `workflow_dispatch` so the checkout/permissions fix
  (`contents: read`) can be verified without waiting for a `main` push.

### Fixed — routine now operates on `main` (closes a confirmed double-execution vector)

- **`ROUTINE_DAILY_CYCLE.md` + `ROUTINE_EOD_CLOSE.md` STEP 0:** both now begin with
  `git fetch origin main && git checkout -B main origin/main`. The routine had been
  running on Claude worktree branches (`claude/*`), so a bare `git push` landed on the
  branch — the gate read a stale `pending_decisions.json` and the 6/17 12:45 retry
  **re-ran the whole pipeline** (`run 20260617-164755`; 0-trade so harmless, but a
  double-fill with real trades). Operating on `main` makes the gate read the canonical
  envelope and every existing push land on `main`. Minimal change — all STEP 4/5
  claim/push semantics preserved. **⚠️ Requires manual live-routine sync** (both
  routines) before it takes effect.

---

## [2026-06-16] — NaN→Supabase publish fix · canary auth · routine observability hardening  ·  main

Triggered by the Jun 16 run: the Supabase publish broke with *"Out of range float
values are not JSON compliant"* — a NaN volatility (TXN/TJX/CAT showed `vol=nan%`)
reached the serializer, so the website stopped updating. Two layers of fix plus
unrelated reliability hardening surfaced during review.

### Fixed — NaN volatility no longer breaks the publish (website was stale)

- **Source** (`quant_engine.compute_risk_metrics`): a non-finite or ≤0 close in the
  snapshot propagated through the return series into a NaN annualized volatility.
  Now a degenerate price series returns `volatility_available: False` (dropped from
  the honest composite), and a NaN vol can never escape.
- **Boundary** (`publish.py`): new `_sanitize()` scrubs NaN/Inf → `None` recursively
  at all four serialization points (snapshot-file write + the snapshot/positions/
  trades/quant upserts); the file write uses `allow_nan=False` to fail loud if
  anything ever slips past. Defense-in-depth even after the source fix.

### Fixed — preflight canary couldn't authenticate in the cloud (529 protection was a no-op)

- `preflight_gate._check_api_health()` built a bare `anthropic.Anthropic()` when no
  `ANTHROPIC_API_KEY` was set, but the cloud authenticates via the OAuth token file
  (`auth_token=`), as `analysis.py:_get_client()` does. The canary therefore failed
  auth in the cloud, fell through to "proceed", and never actually caught a 529
  overload. Now it authenticates identically to the real agents.

### Changed — daily routine: failures are now observable, crashes can't cascade

`ROUTINE_DAILY_CYCLE.md` (live routine must be re-synced):
- **Always push `system_health.json` after `main.py`**, before validating — an
  ABORTED/FAILED day now fires `alert.yml` instead of stopping silently before the
  STEP 5 commit.
- **Capture `main.py`'s exit code + crash guard**: a non-zero exit stops the routine
  (no STEP 4) so orders are never sized/placed against a partial plan.
- **STEP 3 validation guards a missing/unreadable `pending_decisions.json`.**
- **STEP 5 push gets a rebase retry + escalation** (was `git push || echo WARNING`):
  on persistent failure it records an `artifact_push` FAILED health check and pushes
  it so you get paged — orders can be live, the push must not be lost silently.

### Tests

- `+19` regression tests: `TestSanitizeNaN` (5), `TestCanaryAuth` (3), NaN-guard
  cases in `TestRiskMetrics` (3), plus existing coverage. **Full suite green (401).**
- Dry-run (`DRY_RUN=true python main.py`) **skipped** per DEPLOYMENT §7.1 — deployed
  on a trading day; running it would overwrite `pending_decisions.json` and risk a
  double-fill. Validated via the test suite + targeted integration checks.

---

## [2026-06-15 afternoon] — Devil's Advocate: Sonnet model + prompt recalibration + 2.5× token budget  ·  main

Diagnosed zero `recommend_reject` events across 132 evaluations. Root cause was three stacked failures: (1) the Jun 15 morning fix addressed the 800-token truncation that caused 132/140 defaults; (2) the JSON template showed `"recommend_reject": false` as an example value, anchoring the model; (3) `recommend_reject` was the last schema field, so any remaining truncation silently dropped it.

### Fixed — `recommend_reject` anchoring bias

- **Old:** template showed `"recommend_reject": false` — Haiku/Sonnet copy example values.
- **New:** field uses a decision rule: `<true if overall_risk_score >= 7 AND a fatal flaw exists, else false>`. No literal boolean in the template.

### Fixed — `recommend_reject` dropped on truncation

- `overall_risk_score` and `recommend_reject` are now the **first two fields** in the JSON schema so they are captured even if the response is long. Previously they were last.

### Added — Rejection calibration instruction

- Explicit ~20–30% expected rejection rate in the prompt with three concrete criteria: (a) central assumption empirically false, (b) permanent capital loss risk >40%, (c) valuation already prices in the bull case.

### Changed — Devil's Advocate model: Haiku → Sonnet

- Agent 4 now uses `MODEL_SMART` (Sonnet) for genuine adversarial depth.
- Live test: NVDA → `risk=8, reject=True`; JNJ → `risk=6, reject=False`. Full 8-field schema returned, no truncation.

### Changed — Devil's Advocate max_tokens: 1650 → 4125 (2.5×)

- Sonnet produces more tokens than Haiku at the same prompt depth; budget raised proportionally to avoid truncation risk on the new model.

---

## [2026-06-15 evening] — PM SELL-only fix + 3-signal backstop + +10% token budget  ·  main

Portfolio Manager (Agent 6) returned 0 trades when the only correct action was a SELL on a deteriorating position (LLY). Fixed with two complementary changes.

### Fixed — PM skipped SELL-only decisions

- **Root cause:** `_PM_SYSTEM` prompt was framed purely around BUY capital allocation. When no BUY candidates existed, the PM saw no action to take and returned `[]`, ignoring REDUCE/EXIT signals from position review.
- **PM prompt fix:** Added explicit instruction: *"SELL decisions are independent of BUY decisions. When a holding shows recommended_action=REDUCE or EXIT … you MUST propose a SELL … even if you have no new BUYs to make."*

### Added — Deterministic 3-signal backstop in `main.py`

- New `apply_pm_backstop()` helper auto-appends a SELL for any holding where **all three** signals agree: position_review REDUCE/EXIT, hold_score < 5, AND DA recommend_reject=True.
- Backstop fires AFTER the PM runs and BEFORE qty pre-computation, so the SELL goes through the full guardrail + CRO pipeline.
- Existing PM SELL decisions are never duplicated (idempotent check).
- 8 regression tests added (`TestPMBackstop`), covering: all-3-trigger, EXIT action, missing one signal, hold_score=5 boundary, already-selling dedup, HOLD-doesn't-suppress, multi-ticker independence, null hold_score treated as 10.

### Changed — All agent max_tokens raised +10%

Defensive increase to reduce truncation risk across all agents:
- Agent 1 (Regime): 700 → 770
- Agent 2 (Research): 1000 → 1100
- Agent 3 (Earnings): 600 → 660
- Agent 4 (DA): 1500 → 1650 (note: 800→1500 was the Jun 15 morning fix)
- Agent 5 (Position Review): 400 → 440
- Agent 6 (PM): 1200 → 1320
- Agent 7 (CRO): 400 → 440

---

## [2026-06-15] — Devil's Advocate empties fixed (truncation, not throughput)  ·  ~PT  ·  main

Daily-cycle reports were flagging **DEGRADED — most Devil's Advocate (Agent 4) responses came back empty even after retries** (e.g. 15–17 of 20 candidates). Root-caused and fixed.

### Fixed — Agent 4 truncated mid-JSON, collapsing to the empty default
- **Not an API-throughput problem.** Diagnosed deterministically: the same tickers (JPM, BAC, GS, MRK, NVDA, META, …) failed on *every* run while others (AMGN) never did — and Research (same model, same machinery, same run) had **zero** empties. Throughput would scatter failures randomly and hit Research too.
- **Real cause:** the hostile Devil's Advocate prompt elicits a long, essay-style `bear_case` (~3.3k chars / ~1.1k tokens end-to-end), but the call was capped at **`max_tokens=800`**. Every response hit `stop_reason=max_tokens` and was truncated *inside* the first big string field. `_parse_json`'s recovery then stripped that whole value and returned the literal `default` (`bear_case: ""`), so `_safe_call` saw `result == default`, retried the *identical* prompt twice (same truncation), and returned the default — hence "empty even after retries."
- **`run_devils_advocate`** — `max_tokens` 800 → **1500**, plus a prompt instruction to keep `bear_case` to 2–3 sentences and list items to short phrases. Output now lands at ~1.1k tokens with `stop_reason=end_turn`; all previously-failing tickers return populated, sensible bear cases.
- **`_safe_call`** — now threads `stop_reason` from `_call` and **does not retry on `max_tokens` truncation** (deterministic — an identical prompt at the same cap reproduces the same over-long output; retrying just burned calls).
- **`_parse_json`** — truncation recovery now **closes an unterminated string value** (preserving a partial first field, e.g. a cut-off `bear_case`) before falling back to the old strip-and-close path. This also recovers the "cut right after a number" case that was previously documented as unhandled.

### Test
- `TestParseJson::test_truncated_first_string_value_is_preserved` and `::test_truncated_after_first_field_recovers_remaining` lock in the recovery behavior.
- `_call` stubs updated to the new `(text, stop_reason)` signature. Full suite green (**382 passing**).

---

## [2026-06-14] — QA batch: crash-safety fix + 60 new tests (302 → 362)  ·  ~PT  ·  fix/fmp-stable-api

End-to-end QA/UAT review of the full pipeline. One latent crash-safety defect found and fixed; 13 new test classes locking in untested code paths across every module.

### Fixed — `journal._load` corrupt-JSON crash
- **`journal._load()`** now wraps `json.load()` in `try/except (JSONDecodeError, ValueError)`. Previously a corrupt/truncated `transactions.json` or `decision_journal.json` (disk error, partial write) would raise an unhandled exception — the most dangerous timing is *after* orders are placed. The atomic `os.replace()` write pattern reduces the window, but does not eliminate it on power-loss. Now degrades gracefully to the empty default instead of crashing.

### Test — 60 new tests across 13 classes (302 → 362 total)

| Class | Count | What was missing |
|---|---|---|
| `TestLoadListCorruptJSON` | 3 | Locks in the `_load` fix: corrupt/truncated/dict JSON → `[]` |
| `TestAppendCheck` | 7 | `health.append_check` was entirely untested — creates from scratch, escalates status, overwrites check, rebuilds alerts, ABORTED > FAILED, stores kwargs |
| `TestComputeQty` | 13 | `execute._compute_qty` never tested directly — all BUY/SELL/HOLD paths, `available_qty` cap, missing price, ticker-not-in-positions |
| `TestTaxLotsAdditional` | 8 | Oversell (no negative lots), multi-ticker independence, ticker filter, `holding_days` null/bad-date edge cases |
| `TestPortfolioCurveEdgeCases` | 4 | Non-list log, missing `portfolio_snapshot`, null `total_value`, `timestamp` key fallback |
| `TestAlignEdgeCases` | 2 | Portfolio predating SPY → empty result; bars with null close skipped by `_spy_curve` |
| `TestValidateDecisionsAdditional` | 4 | Empty ticker, `None` target_weight (TypeError path), holdings-only SELL passes universe check, HOLD doesn't increment `passed` |
| `TestEnforceWashSaleEdgeCases` | 2 | Malformed sell-date → guard skipped → BUY passes; multiple SELLs uses most-recent (max) |
| `TestPreflightGateMissingPending` | 2 | No pending file + fresh snapshot → PROCEED; malformed snapshot.json → SKIP/RETRY |
| `TestCostModelEdgeCases` | 4 | Both gains zero, zero notional, zero-return net edge, LT rate yields higher net than ST |
| `TestRecordRunRotation` | 2 | Agent log capped at 90; oldest entry dropped first |
| `TestRecentlyExitedEdgeCases` | 3 | Bad exit date silently skipped; empty exits excluded; `open` status excluded |

### Added — SECProvider: free EDGAR fundamentals for the full universe
- **`data_providers.SECProvider`** — uses the SEC EDGAR company-facts XBRL API
  (`data.sec.gov/api/xbrl/companyfacts`) to source `gross_margin`,
  `operating_margin`, and `debt_to_equity` for ~100% of US-listed equities.
  Completely free, no API key, no rate-limit concerns. Powers the full **quality
  score** for every ticker in the watchlist (was all-N/A without `FMP_API_KEY`).
- **`get_provider()` factory** now returns `SECProvider` (not the inert `StubProvider`)
  when no `FMP_API_KEY` is present. Existing behaviour when FMP key is set is
  unchanged — `FMPProvider` still wins and supplies all 6 factors + earnings calendar.
- **`_enrich_with_provider()`** no longer requires `FMP_API_KEY` to proceed; the
  `StubProvider` check is kept as a test injection point.
- **Provider chain (priority order):**
  1. `FMP_API_KEY` set → `FMPProvider`: all 6 quant factors + earnings calendar + estimates
  2. No key → `SECProvider`: 3 quality factors (gross_margin / operating_margin / D/E); no earnings calendar
  3. Test fixtures → `StubProvider`: deterministic no-op

> **What EDGAR does NOT provide:** P/E, FCF yield, EV/EBITDA (price-dependent ratios), and
> the forward earnings calendar. Those still require `FMP_API_KEY`.

(+8 tests: `TestSECProvider` — ratio computation, most-recent annual, zero-equity guard,
HTTP error → None, CIK map loaded once, Protocol conformance.)

### Fixed — #1 FMP provider migrated to the stable API
- FMP deprecated the legacy `/api/v3` endpoints for keys issued after 2025-08-31
  (they 403 "Legacy Endpoint"), so `FMPProvider` was returning `None` even with a
  valid key (graceful no-op — no regression, just no data). Migrated to the
  `/stable` API with **live-validated** endpoints + field names:
  `ratios-ttm` + `key-metrics-ttm` (fundamentals), `earnings` (calendar),
  `analyst-estimates`. Confirmed against AAPL/NVDA/JPM — real margins, P/E,
  FCF yield, EV/EBITDA, and verified next-earnings dates now flow into the
  snapshot. **#1 is now active with `FMP_API_KEY` set.**
- **Alternate-day 50/50 enrichment cache** (`market_data._enrich_with_provider`,
  `provider_cache.json`) — the universe is hash-split into two groups; one refreshes
  each day, so ~50 tickers × 3 stable-API calls ≈ **150 FMP calls/day** (under the
  250/day free-tier limit), each ticker refreshes every ~2 days. **Coverage-aware
  backoff:** FMP free tier covers only **~35%** of the universe (the rest 402
  "premium only"); empty/premium tickers are re-checked every 30 days, not every 2,
  so the budget isn't wasted on them. Cache persisted via `actions/cache`.
- **`market_data.yml`** now passes `FMP_API_KEY` to the fetch step (was missing) and
  persists `provider_cache.json`.

> **Coverage reality (FMP free tier):** ~35/100 tickers return all 6 factors (mega-caps:
> AAPL/MSFT/NVDA/GOOGL/META/AMZN/JPM/BAC/GS/COST/NFLX…); the other ~65 get 3 quality
> factors from EDGAR + momentum+vol from Polygon. Full 6-factor coverage for all tickers
> needs a paid FMP tier (~$22/mo).

---

## [2026-06-14] — Edge batch: #1 real data + #6 net-edge gate + #2 forecast ledger  ·  ~23:20 PT  ·  PR #11/#12/#13

Deployed together after two persona test rounds (289 unit tests + 14 cross-feature
interaction probes, all green). 5.1 (structured output) deferred — needs a live
API dry-run. `FMP_API_KEY` still needed to activate #1's real data (no-op until then).

### Added — #1 real data (provider layer) + Phase 3.2 earnings gate
- **`data_providers.py`** — `MarketDataProvider` Protocol + `StubProvider`
  (testable without a key) + `FMPProvider` (Financial Modeling Prep) + `get_provider()`
  factory. Degrades gracefully: no `FMP_API_KEY` → stub → `None` → free-tier
  fallback, **zero regression**.
- **`market_data.get_market_snapshot()`** now overlays provider `fundamentals`
  (so quant quality/valuation go live — no `quant_engine` change) and a verified
  `earnings_calendar`. No-op without a key.
- **`analysis.run_earnings_catalyst_analyst`** — injects the **verified** earnings
  date with a **fabrication guard** (the calendar date overrides the model's
  guess; flags `earnings_date_corrected`), and **IMPROVEMENTS_SPEC Phase 3.2**:
  when a real calendar exists, **skips the LLM call** for names with no event in
  90 days (no token spend, no all-default noise to the PM). Falls back to current
  behavior when no calendar is available.

> ⚠️ **VENDOR KEY BLOCKER:** set `FMP_API_KEY` in `.env` (local) and as a GitHub
> Actions secret (for `market_data.yml`) to activate real data. Until then the
> stub path runs — identical to today's free-tier behavior. FMP field mappings
> are best-effort vs the v3 schema; validate against a live response.

(+11 tests: `TestDataProviders`, `TestEarningsGateAndFabrication`.)

### Added — #6 net-edge gate (tax-aware trade filter)
- **`tax_lots.py`** — read-only FIFO open-lot accounting (qty / cost basis /
  acquired date) derived from `transactions.json` on demand; persists nothing, so
  it stays out of the money/state path. Plus `holding_days()`.
- **`guardrails.enforce_net_edge`** (using `cost_model.net_edge`) — rejects a BUY
  whose expected return, after round-trip cost **and ~54% CA short-term tax**, is
  below `MIN_NET_EDGE`. **Conditional** on an explicit `expected_return`: a
  decision without one is passed through (no regression). **SELLs exempt** (exits
  never blocked). Wired into `main.py` after the turnover/sector guards; folded
  into the `decision_validation` health check.
- **PM now emits `expected_return`** (gross fraction over the 1–3 mo horizon) — so
  the gate has input, and the journal's feedback loop (`thesis_correct` threshold)
  gets a real expectation instead of 0.

> Makes "is this trade worth it after CA tax?" a **code-level control** rather than
> a hope. Mechanism (fewer marginal trades → less short-term tax) is consistent
> with the backtest finding that monthly rebalance (+$4,185) >> daily (−$242).
> `MIN_NET_EDGE` defaults to $0 (must be net-positive after tax+cost); tunable.

(+8 tests: `TestTaxLots`, `TestNetEdgeGate`.)

### Added — #2 forecast ledger (the learning clock)
- **`calibration.py`** — `log_forecasts()` appends each run's structured agent
  forecasts (quant composite, research confidence, earnings alpha, devil's-advocate
  risk, position hold score) to `forecasts.jsonl`, one row per (agent, ticker,
  field) with the entry price + horizon. **OBSERVATIONAL — logging only, wired
  after `record_run`, never affects a decision and never raises into the pipeline.**
- `score_matured()` joins matured forecasts to realized forward returns from the
  snapshot history (idempotent) → `forecasts_scored.jsonl`.
- `agent_scorecard()` — per-agent rank-IC + sign-hit-rate with **shrinkage toward a
  no-skill prior** (`ic_shrunk = ic·n/(n+k)`), so a lucky handful can't read as
  signal. Nothing sizes a trade on it yet — it's a scoreboard, gated behind sample
  size (future work).

> Scores the **full candidate universe** every run (not just executed trades), so
> it accrues hundreds of labeled forecasts/month — the only way to beat the
> small-sample problem on a $500 book. The ledger files are gitignored.

(+5 tests: `TestCalibrationLedger`.)

> **Integrated suite: 285 passing** (#1 + #6 + #2 together).

---

## [2026-06-14] — P1: quant backtest harness + cost_model spine + QA hardening  ·  ~22:15 PT  ·  PR #10

### Added
- **`cost_model.py`** — shared cost & tax spine (P1 foundation for the backtest
  #3 and the future net-edge gate #6). Holds the CA top-bracket tax rates +
  IRS-style ST/LT netting (`tax_on_realized`), a round-trip cost/slippage
  estimate (`round_trip_cost`), and `net_edge()` (gross − cost − CA tax).
  `performance.py` now imports the rates + netting from it (single source of
  truth), so simulated and live economics can't drift. (+9 tests; suite **245**.)
- **`backtest/` — quant-only backtest harness (#3 / P1).** Event loop over the
  `market_snapshot.json` history that reuses `quant_engine.score_all_tickers`
  unchanged (scores exactly what live scores), fills at next-day open (no
  look-ahead), and imports `cost_model` for after-cost/after-tax economics.
  Includes a momentum/inverse-vol strategy, an after-tax-vs-SPY report
  (CAGR/vol/Sharpe/max-DD/turnover, gross & net-of-tax), and `python -m backtest`.
  **No LLM in the backtest** (a frozen model knows the future — the LLM layer is
  forward-tested, not backtested). (+8 tests; suite **253**.)

  > **First result (honest):** the quant-only momentum/vol strategy returned
  > **−0.03%** over ~10 months vs SPY **+8.77%** — gross alpha **−8.8%**, 23.6×
  > annual turnover. The deterministic layer has **no demonstrated edge** at this
  > config; this is exactly the validation P1 exists to provide.

### Fixed (QA hardening — two independent review passes, all personas)
- **Timezone-flaky test** — `TestPublishSpyDataSource` computed "today" with local
  `date.today()` (Pacific) while the production function uses ET, so it failed ~3
  hours **every day** (the midnight-ET-to-midnight-PT window). Now uses ET to match.
- **Survivorship-bias caveat** — the backtest report now discloses that the universe
  is only tickers in today's snapshot (no delisted names) and fixed over the window,
  so absolute returns are biased upward.
- **+8 edge-case regression tests** — degenerate backtests (no SPY / empty history /
  warmup overflow / no-leverage / no-look-ahead), guard boundaries (exactly-5-day
  hold and exactly-30-day wash-sale both correctly allowed), survivorship disclosure.
  Suite **261**.

> **QA insight (Portfolio Manager lens):** monthly rebalancing yields **+$4,185
> realized vs −$242 for daily** (86 vs 1,360 trades) — churn is value-destructive,
> empirically backing the turnover/tax guards shipped in PR #9.

---

## [2026-06-14] — Paper-shadow 100× + after-tax scorecard + turnover discipline  ·  ~17:30 PT

Edge-upgrade batch P0/P0.5. Three shipped changes + two planning docs.

### Added
- **Paper-shadow 100× columns on `trades.csv`** (`qty_100x`, `total_value_100x`,
  `portfolio_value_100x`) — models the same trades on a hypothetical $50,000 book
  (same price; qty and dollar value ×100). Existing rows backfilled; new rows and
  broker reconciliation keep the twin in sync. `SHADOW_MULTIPLIER`/`_scaled` in
  `execute.py`. *Caveat: a linear projection (zero market impact), not proof of scale.*
- **After-tax scorecard** (`performance.py`) — net return after **California
  top-bracket** tax (short-term ≈54%, long-term ≈37.1%) vs holding SPY in the same
  account. Tracks **realized gain and after-tax realized gain separately**, via
  FIFO lot-matching with ST/LT classification. SELLs with no in-log cost basis are
  reported as "uncovered," never assigned a guessed basis. Flags
  `not_significant` below 60 trading days.
- **Turnover / tax guardrails** (`guardrails.py`, wired in `main.py`):
  `enforce_min_holding_period` (block SELLs of names bought < 5 trading days ago;
  risk exits exempt) and `enforce_wash_sale_reentry` (block BUYs of names sold
  within 30 calendar days — hardens the soft 10-day re-entry warning into a control).
  Folded into the `decision_validation` health check.

### Docs
- `SOLUTION_PLANS.md` — expert-panel designs for improvements #1/#2/#3/#5/#6/#9.
- `FINAL_PLAN.md` — phased roadmap (P0–P6) + California tax recalibration + a
  Shreyas-Doshi-style expert pre-mortem.

### Fixed (pre-deploy expert code review)
- **Tax netting** — `realized_summary` now nets ST/LT gains and losses per IRS
  ordering (a term loss offsets the other term's gain before tax) instead of
  taxing gains in full while crediting losses fully against after-tax.
- **Guard ordering** — turnover guards now run **before** the sector cap in
  `main.py`, so the cap projects against the SELL set that will actually execute
  (a SELL dropped after the cap freed its budget could otherwise let a
  same-sector BUY breach 25%).
- **Single transactions read** — `validate_decisions` + both turnover guards now
  share one `transactions.json` read (one consistent view, not three).
- DRY: `_last_live_buy_date`/`_last_live_sell_date` share `_last_live_trade_date`;
  min-holding skips the file read when the kill switch short-circuits.

### Tests
- +31: `TestPaperShadowColumns`, `TestRealizedLots`, `TestRealizedSummary` (incl.
  ST/LT netting), `TestAfterTaxScorecard` (incl. the "beats SPY pre-tax, loses
  after CA tax" case), `TestMinHoldingPeriod`, `TestWashSaleReentry`,
  `TestReleaseNotes`. Suite: **236 passing**.

---

## [2026-06-13] — IMPROVEMENTS_SPEC batch  ·  ~12:18 PT  ·  `722539a`, merge `26ded5f`

Critical evaluation + selective implementation of `IMPROVEMENTS_SPEC.md` (6 of 9
phases implemented, 3 rejected on inspection).

- **Sector cap (25%) in code** — `guardrails.enforce_sector_limits` + static
  `SECTOR_MAP`; the limit previously lived only in the PM prompt (not a control).
- **Outcome feedback loop** — `journal.close_position` populates
  `actual_return`/`thesis_correct`/`exits` on the matching open BUY when sold.
- **Agent memory** — `get_ticker_history` + `recently_exited` fed to the Research
  Analyst (prior outcomes) and Portfolio Manager (re-entry warning).
- **Honest quant composite** — sub-scores carry `*_available`; weights renormalize
  over real factors; `N/A` instead of a fake 50.
- **CRO real correlation** — pairwise 120d return correlation + sector
  concentration injected into the CRO prompt.
- **`performance.py`** — local portfolio-vs-SPY report (price-return) with drawdown/
  vol/Sharpe.

## [2026-06-12] — Senior code-review remediation (6 phases)  ·  ~19:12 PT  ·  `5f2144d`…`8acff18`

All P0 unless noted; each phase an independent commit.

- **Deterministic guardrails gate** (`5f2144d`) — action whitelist, BLOCKED_TICKERS,
  candidate-membership, BUY+SELL conflict rejection, weight clamp + qty recompute,
  notional cap, $5 min, GFV guard.
- **Stamp-first idempotency** (`7cc2e01`) — `execution_started_at` set + pushed
  before the first order; closes the cross-attempt double-fill window.
- **Authoritative fill reconciliation** (`b3739d9`) — `mark_transactions_live`
  reconciles all three logs against broker fills; `fills=None` now raises.
- **Portfolio freshness** (`02bc9a0`) — `get_portfolio_summary` raises
  `StalePortfolioError` unless `mcp_portfolio.json` `as_of` is today (ET).
- **Per-order failure isolation** (`8483759`) — one order exception can't strand
  the rest (SELL-before-BUY otherwise stranded capital in cash).
- **Hygiene** (`d580c7c`) — ET timestamps, single-lookup `_compute_qty`,
  health_check 1:15 PM ET.

## [2026-06-12] — Cloud trade reconciliation + EOD publish fixes  ·  ~07:26–07:55 PT  ·  `0e17c7d`…`0e502cf`

- **Mark cloud trades live** (`0e17c7d`) — cloud `main.py` runs `DRY_RUN=true`, so
  MCP trades were stamped `dry_run=True` and never reached the website; reconcile
  flips broker-accepted trades live.
- **Fill-aware reconcile + `close_value` immutability** (`67b0bf8`, `0e502cf`).
- **EOD publish** (`9f1ad2e`) — stage `portfolio_snapshot.json` (not just
  `mcp_portfolio.json`) and drop `[skip ci]`, so the 4 PM `close_value` auto-publishes.
- **Node 24** for all GitHub Actions (`e6185fe`).

## [2026-06-11] — Publish / SPY source fix  ·  ~12:24 PT  ·  `a35323c`, `dde9b84`

- Read SPY price from `market_snapshot.json` (today's live price) instead of
  Polygon "prev" (yesterday's close) → no duplicate chart rows; guard `is_close`
  inheritance.
- Remove `[skip ci]` from routine commits + add `workflow_dispatch` to `publish.yml`.

## [2026-06-11] — Pipeline resilience + richer news + CI  ·  ~06:51–11:44 PT  ·  `2b21c7f`…`57cd8d7`

- **Broker order verification + SELL-before-BUY + trades.csv schema migration**
  (`fd9d56a`) — rejected orders were being logged as fills.
- **max_tokens increase + JSON truncation recovery** (`61ab95a`) — verbose Haiku
  responses were truncating mid-JSON → all agents returned defaults → 0 trades.
- **Richer news feed** (`ac69e20`) + **news-before-history** ordering (`e2a18b3`)
  to survive the 5-calls/min free tier.
- **Redundant market-data crons** (`2b21c7f`) to survive GitHub silent skips.
- `{}`-shaped journal guard (`b8ec88d`); fetch cache purge (`a3ffe86`, `57cd8d7`).

## [2026-06-10] — 529 resilience + integrity + parallelization  ·  ~07:31–18:08 PT  ·  `7652b9d`…`736038f`

- **Retry all agents on Anthropic 529 overload** with exponential backoff
  (`7652b9d`); retry when a response parses to default (`a39f338`); canary 529
  gate (`53ba8e9`).
- **Integrity** (`8f0b2e9`) — atomic JSON writes, ET timezone everywhere, SELL cap
  vs `available_qty`, 50% daily-turnover circuit breaker.
- **Parallelize agents 2–5** (`736038f`); atomic fundamentals cache write.
- Publish source-of-truth fixes (`6531571`, `da7a180`, `ab844cc`).

## [2026-06-09 and earlier] — Foundation (see CLAUDE.md changelog for full detail)

- `alert.yml` block-scalar fix (health alerting had been silently dead).
- Route Supabase publish through GitHub Actions (Anthropic cloud blocks Supabase).
- Health tracking, pre-flight abort, failure alerting.
- Idempotent `mark_pending_executed`; stale market-data guard.
- Initial 7-agent pipeline, quant engine, Robinhood MCP execution, website.
