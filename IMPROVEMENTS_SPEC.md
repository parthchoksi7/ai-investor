# AI Investor — Implementation Brief

> **For Claude Code.** This is a phased work order. Tax-awareness is intentionally
> out of scope. Work the phases in order — Phase 0 is money-safety and ships first.
>
> **Before editing anything:** these reference snippets were written from a partial
> read of the repo. For every task, first open the named file and confirm the real
> function signatures, field names, and imports, then adapt the snippet to match.
> Do **not** paste reference code blindly.
>
> **Definition of done for every phase:** `pytest test_pipeline.py -q` is a full pass
> (a partial pass is not a pass), and any new behavior has a new test. Keep
> `DRY_RUN=true` for all local verification.

---

## Phase 0 — Coded guardrails (money safety, ship first)

The "max 10% per position" and "max 25% per sector" rules currently live **only in
the Portfolio Manager prompt**. An LLM is not a constraint. If the model emits
`target_weight: 0.5`, `execute._compute_qty()` will size a 50% position. Make both
limits hard, server-side rejections.

### 0.1 — Clamp/reject oversized `target_weight`

**File:** `execute.py`

Add a validation gate that runs on the decision list *before* any quantity is computed
or any order is placed. Reject (drop) any BUY whose `target_weight` exceeds the cap;
log it loudly.

```python
MAX_POSITION_WEIGHT = 0.10  # single source of truth; keep in sync with PM prompt

def enforce_position_limits(decisions: list[dict]) -> tuple[list[dict], list[dict]]:
    """Drop BUYs that breach MAX_POSITION_WEIGHT. Returns (kept, rejected)."""
    kept, rejected = [], []
    for d in decisions:
        tw = d.get("target_weight")
        if d.get("action") == "BUY" and tw is not None and tw > MAX_POSITION_WEIGHT + 1e-9:
            d = {**d, "rejected_reason": f"target_weight {tw:.2%} > {MAX_POSITION_WEIGHT:.0%} cap"}
            rejected.append(d)
        else:
            kept.append(d)
    return kept, rejected
```

Call it in `main.py` immediately after `get_trade_decisions(...)` returns, before
`_compute_qty` is mapped over the decisions. Print each rejection and record a
`health.record("position_limits", ...)` line if any fire.

**Acceptance:** a decision with `target_weight=0.5` never reaches `_compute_qty`; it is
logged as rejected and absent from `pending_decisions.json["decisions"]`.

### 0.2 — Enforce the 25% sector cap in code

**File:** `execute.py` (or a new `risk_limits.py`)

You need a ticker→sector map. Check whether `market_data` / fundamentals already carry
a sector field; if not, add a static dict for the current universe and a `TODO` to
source it from Polygon. Compute projected post-trade sector weights and reject the
marginal BUYs that push a sector over 25%.

```python
MAX_SECTOR_WEIGHT = 0.25

def enforce_sector_limits(decisions, portfolio, prices, sector_of: dict[str, str]):
    """Reject BUYs that would push any sector over MAX_SECTOR_WEIGHT."""
    total = portfolio["total_value"]
    # seed projected weights from current holdings
    sector_w: dict[str, float] = {}
    for p in portfolio["positions"]:
        sec = sector_of.get(p["symbol"], "UNKNOWN")
        sector_w[sec] = sector_w.get(sec, 0) + (p["market_value"] / total if total else 0)

    kept, rejected = [], []
    # apply SELLs first (they free up sector budget), then BUYs
    for d in sorted(decisions, key=lambda x: 0 if x.get("action") == "SELL" else 1):
        sec = sector_of.get(d["ticker"], "UNKNOWN")
        if d.get("action") == "BUY":
            projected = sector_w.get(sec, 0) + d.get("target_weight", 0)
            if projected > MAX_SECTOR_WEIGHT + 1e-9:
                rejected.append({**d, "rejected_reason":
                    f"{sec} would hit {projected:.0%} > {MAX_SECTOR_WEIGHT:.0%} cap"})
                continue
            sector_w[sec] = projected
        kept.append(d)
    return kept, rejected
```

**Acceptance:** unit test — three BUYs of 0.10 into the same sector keep two, reject the
third. SELLs in the same sector are applied before BUYs so freed budget is reusable.

---

## Phase 1 — Close the memory loop (outcome feedback)

**Problem:** `decision_journal.json` entries are created with `actual_return=None` and
`thesis_correct=None`, and **nothing ever populates them**. A BUY entry stays
`status="open"` forever even after the position is sold. The system therefore cannot
tell a thesis that worked from one that blew up. This is the single highest-value
memory fix.

**File:** `journal.py`

Add a function that closes the matching open BUY entry when a position is sold and
records the realized outcome. Use the broker's `avg_price` (cost basis) and the exit
price — this is correct for both full and partial exits.

```python
def close_position(
    ticker: str,
    exit_price: float,
    avg_price: float,
    full_exit: bool,
    run_id: str = "",
) -> str | None:
    """Annotate the most recent OPEN buy entry for `ticker` with a realized outcome.

    Full exit  → status='closed'. Partial reduce → stays 'open', notes the trim.
    Realized return is per-share vs cost basis, so it is lot-size independent.
    Returns the trade_id touched, or None if no open entry was found.
    """
    if not avg_price:
        return None
    journal = _load_list(JOURNAL_FILE)
    realized = round((exit_price - avg_price) / avg_price, 4)  # e.g. -0.064 = -6.4%
    # newest open BUY for this ticker
    target = None
    for entry in reversed(journal):
        if entry.get("ticker") == ticker and entry.get("action") == "BUY" \
           and entry.get("status") == "open":
            target = entry
            break
    if target is None:
        return None

    target["actual_return"] = realized
    expected = target.get("expected_return") or 0
    # thesis judged correct if direction matched (and roughly met expectation if one was set)
    target["thesis_correct"] = bool(realized > 0) if not expected else bool(realized >= expected * 0.5)
    target.setdefault("exits", []).append({
        "date": datetime.now(_ET).strftime("%Y-%m-%d"),
        "run_id": run_id,
        "exit_price": round(exit_price, 4),
        "avg_price": round(avg_price, 4),
        "realized_return": realized,
        "full_exit": full_exit,
    })
    if full_exit:
        target["status"] = "closed"
    _save(JOURNAL_FILE, journal)
    return target.get("trade_id")
```

**Wire-up — `main.py`:** in the loop that records executed decisions (where
`record_transaction(...)` / `record_trade(...)` are called), for every executed `SELL`
call `close_position(...)`. Derive `avg_price` from the matching `portfolio["positions"]`
entry, `exit_price` from `market_data["prices"][ticker]["close"]`, and
`full_exit = (target_weight == 0)`.

**Acceptance:**
- After a SELL, the matching open BUY entry has a numeric `actual_return`, a boolean
  `thesis_correct`, and an `exits` record; full exits flip to `status="closed"`.
- A SELL with no matching open BUY is a no-op (no crash).
- New test in `test_pipeline.py`: buy → sell → assert the entry is closed with the
  correct realized return sign.

---

## Phase 2 — Feed memory back into the agents

Right now only the Position Review agent sees a prior thesis, and only for tickers you
**currently hold**. The agents that build the thesis for a *new or re-entered* position
(Research, Portfolio Manager) get essentially nothing. Fix both the recall and the
churn blind spot.

### 2.1 — Per-ticker history helper

**File:** `journal.py`

```python
def get_ticker_history(ticker: str, n: int = 3) -> list[dict]:
    """Most-recent closed/open journal entries for one ticker, with outcomes."""
    rows = [e for e in _load_list(JOURNAL_FILE) if e.get("ticker") == ticker]
    return rows[-n:]

def recently_exited(within_days: int = 10) -> dict[str, dict]:
    """ticker -> the most recent closed entry exited within `within_days`."""
    from datetime import date, timedelta
    cutoff = date.today() - timedelta(days=within_days)
    out: dict[str, dict] = {}
    for e in _load_list(JOURNAL_FILE):
        if e.get("status") != "closed":
            continue
        exits = e.get("exits") or []
        if not exits:
            continue
        try:
            d = date.fromisoformat(exits[-1]["date"])
        except Exception:
            continue
        if d >= cutoff:
            out[e["ticker"]] = e  # last write wins = most recent
    return out
```

### 2.2 — Inject into the Research Analyst and Portfolio Manager

**File:** `analysis.py`

In `run_research_analyst(...)`, when a candidate has prior history, add a block to the
`user_msg`:

```
PRIOR HISTORY FOR {ticker}:
  {date} {action} — thesis: {thesis[:120]}
  outcome: {'+' if actual_return>0 else ''}{actual_return:.1%} | thesis_correct={thesis_correct}
```

In `run_portfolio_manager(...)`, add a **re-entry warning** section built from
`recently_exited()`. For any candidate the system sold in the last ~10 days, surface the
exit and require the model to justify the reversal:

```
RECENTLY EXITED (justify any re-entry):
  {ticker}: exited {exit_date} at {realized_return:+.1%} — reason: {exit rationale}
  Do NOT re-buy unless the original exit reason is now resolved; state why in rationale.
```

Pass `recently_exited()` and a `get_ticker_history` lookup down through
`get_trade_decisions(...)` into these two agents (extend their signatures; update the
caller in `main.py`).

**Acceptance:**
- Given a closed AAPL entry exited 2 days ago, the PM `user_msg` contains an AAPL
  re-entry warning line.
- The Research agent `user_msg` for a previously-traded ticker contains its prior
  outcome.
- This directly addresses the "sold AAPL at \$292, why am I rebuying at \$291?" gap:
  the rebuy decision now sees the exit and its realized result.

---

## Phase 3 — Quant & agent honesty audit

### 3.1 — Quality / valuation placeholders

In the agent logs, nearly every ticker shows `quality_score: 50` and
`valuation_score: 50` — the neutral default. That suggests these two of the four
advertised factors are **not actually populated** (likely because free-tier Polygon
omits the fundamentals), making this effectively a momentum + volatility strategy.

**Task:** open `quant_engine.py` and confirm whether quality/valuation are real or
defaulted. Then either (a) wire up the fundamental inputs, or (b) if the data isn't
available, set `data_available=False` for those sub-scores, weight them to zero in the
composite, and update `README.md`/`CLAUDE.md` to describe the strategy honestly. Do not
leave 50/50 placeholders silently feeding a "4-factor" composite.

**Acceptance:** the composite score's factor weights reflect only factors with real
data; docs match reality.

### 3.2 — Earnings agent empty defaults

Agent 3 frequently returns `earnings_alpha_score: 5`, `MEDIUM`, empty catalyst lists —
the health check already detects the all-default case. It's spending tokens to emit a
constant.

**Task:** gate the earnings agent on a real upcoming-earnings signal. If there's no
earnings event within the 90-day window (from an earnings-calendar source, or skip if
none is available), don't call the model for that ticker — return a cheap `null`/skipped
result and exclude it from the PM context rather than feeding noise.

**Acceptance:** tickers with no near-term catalyst don't trigger an Agent 3 LLM call;
PM context no longer contains all-default earnings blocks.

---

## Phase 4 — Give the CRO real correlation data

The Chief Risk Officer prompt asks it to evaluate "correlation risk," but it's only fed
per-ticker weight, vol, and beta — **no correlation matrix**. Its correlation judgment is
therefore fabricated.

**File:** `analysis.py` (+ a helper, possibly in `quant_engine.py`)

You already fetch 210 days of OHLCV. Compute the pairwise return correlation matrix for
the projected post-trade holdings and feed the top few most-correlated pairs into the CRO
`user_msg`:

```
HIGHEST PAIRWISE CORRELATIONS (post-trade holdings, 120d daily returns):
  GS / MS: 0.86
  JPM / BAC: 0.81
  ...
CONCENTRATION: top sector = Financials 31%
```

**Acceptance:** the CRO `user_msg` contains a real correlation section derived from price
history; remove the pretense if for any reason the matrix can't be computed.

---

## Phase 5 — Robustness

### 5.1 — Structured JSON output instead of brace-counting

`analysis._parse_json` does regex extraction + unmatched-brace counting to recover
truncated JSON. That's a smell. Migrate the agent calls to request structured output
(JSON schema / tool-use) so responses are valid JSON by construction, then delete the
brace-recovery path.

**Acceptance:** `_parse_json`'s truncation-recovery branch is removed; agents still
return well-formed dicts; tests pass.

### 5.2 — Programmatic `invalidates_if` checks

`invalidates_if` conditions are stored but only ever handed to an LLM as prose. Where a
condition is price/level-based and machine-checkable (e.g. "close below \$X",
"drawdown > Y%"), evaluate it in code during position review and auto-flag a forced-exit
candidate. Leave free-text conditions to the LLM.

**Acceptance:** a position whose stored numeric invalidation level is breached is flagged
for exit deterministically, independent of the LLM's judgment.

---

## Phase 6 — Local performance + S&P 500 benchmark report

**Context:** `publish.py` already pushes a portfolio-vs-SPY cumulative-return comparison
to Supabase, but it's price-return SPY (no dividends), has no risk-adjusted metrics, and
requires Supabase. Add a **local, Supabase-independent** report.

**New file:** `performance.py`

Build two equity curves and compare:

- **Portfolio curve:** date → `total_value`, read from `agent_log.json` (each run record
  carries `portfolio_snapshot.total_value` and a date). De-dup to one point per date.
- **SPY curve:** SPY's daily closes from the 210-day history in `market_snapshot.json`
  (SPY is in the universe), aligned to the portfolio's date range.

Normalize both to the portfolio inception date (= first agent-log date) and compute, for
each series: cumulative return, max drawdown, annualized volatility, and a daily-return
Sharpe (rf=0 is fine for a first pass). Emit `performance_report.json` and print a table.

```python
"""performance.py — local portfolio vs SPY performance report (no Supabase needed)."""
import json, os
from datetime import date

AGENT_LOG = "agent_log.json"
SNAPSHOT  = "market_snapshot.json"

def _portfolio_curve() -> list[tuple[str, float]]:
    log = json.load(open(AGENT_LOG)) if os.path.isfile(AGENT_LOG) else []
    by_date = {}
    for run in log:
        d = run.get("date") or (run.get("timestamp") or "")[:10]
        tv = (run.get("portfolio_snapshot") or {}).get("total_value")
        if d and tv:
            by_date[d] = float(tv)   # last run of the day wins
    return sorted(by_date.items())

def _spy_curve(dates: list[str]) -> dict[str, float]:
    snap = json.load(open(SNAPSHOT)) if os.path.isfile(SNAPSHOT) else {}
    hist = snap.get("prices", {}).get("SPY", {})
    # adapt to the actual SPY history shape in market_snapshot.json:
    # may be {"history": [{"date","close"}...]} or a bars array — INSPECT FIRST.
    raise NotImplementedError("Map SPY history to {date: close} per the real snapshot shape")

def _metrics(curve: list[float]) -> dict:
    if len(curve) < 2:
        return {"cumulative_return": 0.0, "max_drawdown": 0.0, "sharpe": None}
    rets = [(curve[i]/curve[i-1] - 1) for i in range(1, len(curve))]
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v/peak - 1)
    import statistics as st
    mean = st.fmean(rets); sd = st.pstdev(rets) or 1e-9
    return {
        "cumulative_return": round(curve[-1]/curve[0] - 1, 4),
        "max_drawdown": round(mdd, 4),
        "sharpe": round((mean/sd) * (252 ** 0.5), 2),
    }

# Build both curves over the shared date range, normalize to day 0, print a table,
# and write performance_report.json with both metric sets + the spread (alpha).
```

**Caveats to print in the report header (be honest):** SPY here is **price return, not
total return** — it excludes dividends and understates the index by roughly 1.3%/yr; the
portfolio figure includes cash drag; and with only a handful of trading days of history
the Sharpe is not yet meaningful.

**Optional wire-up:** call `performance.py` at the end of the daily cycle in `main.py`
and/or expose it as `python performance.py` for ad-hoc runs.

**Acceptance:** `python performance.py` prints a portfolio-vs-SPY table and writes
`performance_report.json` without requiring Supabase; metrics are correct on a small
synthetic fixture (add one test).

---

## Test cases — Phases 0 & 1 (drop into `test_pipeline.py`)

These match the repo's existing conventions: `tmp_path` + `monkeypatch.setattr` to
redirect module-level file constants, feature-grouped classes with an invariant
docstring, and real booleans in `transactions.json`. Adjust import paths / function
names if your implementation differs from the reference snippets above. After adding,
`pytest test_pipeline.py -q` must be a full pass.

```python
# ─────────────────────────────────────────────────────────────────────────────
# Phase 0.1 — execute.enforce_position_limits: the 10% cap is enforced in CODE,
# not just the PM prompt. An LLM emitting target_weight=0.5 must never be sized.
# ─────────────────────────────────────────────────────────────────────────────

class TestEnforcePositionLimits:
    """MAX_POSITION_WEIGHT is a hard, server-side rejection. The PM prompt is not
    a constraint — without this gate a single hallucinated weight sizes a 50%
    position with real money."""

    def test_oversized_buy_rejected(self):
        from execute import enforce_position_limits
        kept, rejected = enforce_position_limits([
            {"ticker": "NVDA", "action": "BUY", "target_weight": 0.50},
        ])
        assert kept == []
        assert len(rejected) == 1
        assert rejected[0]["ticker"] == "NVDA"
        assert "rejected_reason" in rejected[0]

    def test_at_cap_is_kept(self):
        from execute import enforce_position_limits
        kept, rejected = enforce_position_limits([
            {"ticker": "GS", "action": "BUY", "target_weight": 0.10},  # exactly at cap
        ])
        assert [d["ticker"] for d in kept] == ["GS"]
        assert rejected == []

    def test_under_cap_is_kept(self):
        from execute import enforce_position_limits
        kept, _ = enforce_position_limits([
            {"ticker": "JPM", "action": "BUY", "target_weight": 0.08},
        ])
        assert [d["ticker"] for d in kept] == ["JPM"]

    def test_sell_is_never_rejected_by_position_cap(self):
        # A SELL's target_weight is a target to reduce TO, not a position to open;
        # the position cap must not block exits.
        from execute import enforce_position_limits
        kept, rejected = enforce_position_limits([
            {"ticker": "AAPL", "action": "SELL", "target_weight": 0.0},
        ])
        assert [d["ticker"] for d in kept] == ["AAPL"]
        assert rejected == []

    def test_mixed_batch_partitions_correctly(self):
        from execute import enforce_position_limits
        kept, rejected = enforce_position_limits([
            {"ticker": "A", "action": "BUY", "target_weight": 0.05},
            {"ticker": "B", "action": "BUY", "target_weight": 0.25},   # over cap
            {"ticker": "C", "action": "SELL", "target_weight": 0.0},
        ])
        assert {d["ticker"] for d in kept} == {"A", "C"}
        assert [d["ticker"] for d in rejected] == ["B"]


# ─────────────────────────────────────────────────────────────────────────────
# Phase 0.2 — execute.enforce_sector_limits: the 25% sector cap is enforced in
# CODE. SELLs are applied before BUYs so freed sector budget is reusable.
# ─────────────────────────────────────────────────────────────────────────────

class TestEnforceSectorLimits:
    """MAX_SECTOR_WEIGHT (25%) is a hard cap on projected post-trade sector weight.
    The marginal BUY that breaches it is rejected; a same-sector SELL applied first
    frees budget for a subsequent BUY."""

    SECTORS = {"GS": "Financials", "MS": "Financials", "JPM": "Financials",
               "BAC": "Financials", "XOM": "Energy"}

    def _empty_portfolio(self):
        return {"total_value": 1000.0, "cash": 1000.0, "positions": []}

    def test_third_same_sector_buy_rejected(self):
        from execute import enforce_sector_limits
        decisions = [
            {"ticker": "GS",  "action": "BUY", "target_weight": 0.10},
            {"ticker": "MS",  "action": "BUY", "target_weight": 0.10},
            {"ticker": "JPM", "action": "BUY", "target_weight": 0.10},  # → 30% Financials
        ]
        kept, rejected = enforce_sector_limits(
            decisions, self._empty_portfolio(), prices={}, sector_of=self.SECTORS)
        assert {d["ticker"] for d in kept} == {"GS", "MS"}
        assert [d["ticker"] for d in rejected] == ["JPM"]

    def test_other_sector_buy_unaffected(self):
        from execute import enforce_sector_limits
        decisions = [
            {"ticker": "GS",  "action": "BUY", "target_weight": 0.10},
            {"ticker": "MS",  "action": "BUY", "target_weight": 0.10},
            {"ticker": "XOM", "action": "BUY", "target_weight": 0.10},  # Energy, fine
        ]
        kept, rejected = enforce_sector_limits(
            decisions, self._empty_portfolio(), prices={}, sector_of=self.SECTORS)
        assert {d["ticker"] for d in kept} == {"GS", "MS", "XOM"}
        assert rejected == []

    def test_existing_holdings_count_toward_sector_budget(self):
        from execute import enforce_sector_limits
        portfolio = {"total_value": 1000.0, "cash": 800.0,
                     "positions": [{"symbol": "BAC", "market_value": 200.0}]}  # 20% Financials
        decisions = [{"ticker": "GS", "action": "BUY", "target_weight": 0.10}]  # → 30%
        kept, rejected = enforce_sector_limits(
            decisions, portfolio, prices={}, sector_of=self.SECTORS)
        assert kept == []
        assert [d["ticker"] for d in rejected] == ["GS"]

    def test_sell_frees_budget_before_buy(self):
        # Holding 25% BAC (Financials at cap). Selling it should free room for a GS buy
        # even though decisions are passed BUY-first.
        from execute import enforce_sector_limits
        portfolio = {"total_value": 1000.0, "cash": 750.0,
                     "positions": [{"symbol": "BAC", "market_value": 250.0}]}  # 25% Financials
        decisions = [
            {"ticker": "GS",  "action": "BUY",  "target_weight": 0.10},
            {"ticker": "BAC", "action": "SELL", "target_weight": 0.0},
        ]
        kept, rejected = enforce_sector_limits(
            decisions, portfolio, prices={}, sector_of=self.SECTORS)
        assert {d["ticker"] for d in kept} == {"GS", "BAC"}
        assert rejected == []


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — journal.close_position: closes the matching open BUY on a sell and
# records the realized outcome. This is the feedback loop the system lacked —
# actual_return / thesis_correct were never populated before.
# ─────────────────────────────────────────────────────────────────────────────

class TestClosePosition:
    """On a SELL, the most recent open BUY entry for that ticker gains a numeric
    actual_return and a boolean thesis_correct; a full exit flips status to
    'closed'. Realized return is per-share vs cost basis, so it is independent of
    lot size and correct for partial exits."""

    def _open_buy(self, ticker="AAPL", expected_return=0.0, trade_id="t1"):
        return {
            "trade_id": trade_id, "run_id": "r0", "date": "2026-06-01",
            "ticker": ticker, "action": "BUY", "target_weight": 0.08,
            "thesis": "variant perception", "anti_thesis": "", "catalysts": [],
            "confidence": 7, "expected_return": expected_return, "invalidates_if": [],
            "status": "open", "actual_return": None, "thesis_correct": None,
        }

    def _setup(self, tmp_path, monkeypatch, entries):
        import journal
        jf = tmp_path / "decision_journal.json"
        jf.write_text(json.dumps(entries))
        monkeypatch.setattr(journal, "JOURNAL_FILE", str(jf))
        return journal, jf

    def test_full_exit_closes_entry_with_loss(self, tmp_path, monkeypatch):
        # Your example: bought ~312, exit at 292 → realized ≈ -6.4%, thesis wrong.
        journal, jf = self._setup(tmp_path, monkeypatch, [self._open_buy()])
        tid = journal.close_position("AAPL", exit_price=292.0, avg_price=312.0,
                                     full_exit=True, run_id="r1")
        entry = json.loads(jf.read_text())[0]
        assert tid == "t1"
        assert entry["status"] == "closed"
        assert entry["actual_return"] == round((292.0 - 312.0) / 312.0, 4)  # -0.0641
        assert entry["thesis_correct"] is False
        assert entry["exits"][-1]["full_exit"] is True
        assert entry["exits"][-1]["exit_price"] == 292.0

    def test_full_exit_with_gain_marks_thesis_correct(self, tmp_path, monkeypatch):
        journal, jf = self._setup(tmp_path, monkeypatch, [self._open_buy()])
        journal.close_position("AAPL", exit_price=340.0, avg_price=312.0, full_exit=True)
        entry = json.loads(jf.read_text())[0]
        assert entry["actual_return"] > 0
        assert entry["thesis_correct"] is True

    def test_expected_return_threshold_branch(self, tmp_path, monkeypatch):
        # With an expected_return set, thesis is "correct" only if realized met
        # at least half of it. +3% realized against a +10% expectation fails the bar.
        journal, jf = self._setup(
            tmp_path, monkeypatch, [self._open_buy(expected_return=0.10)])
        journal.close_position("AAPL", exit_price=321.36, avg_price=312.0, full_exit=True)
        entry = json.loads(jf.read_text())[0]
        assert entry["actual_return"] == round((321.36 - 312.0) / 312.0, 4)  # ~+0.03
        assert entry["thesis_correct"] is False  # 0.03 < 0.10*0.5

    def test_partial_exit_keeps_entry_open(self, tmp_path, monkeypatch):
        journal, jf = self._setup(tmp_path, monkeypatch, [self._open_buy()])
        journal.close_position("AAPL", exit_price=300.0, avg_price=312.0, full_exit=False)
        entry = json.loads(jf.read_text())[0]
        assert entry["status"] == "open"          # reduce, not exit
        assert entry["actual_return"] is not None  # outcome still recorded
        assert entry["exits"][-1]["full_exit"] is False

    def test_no_matching_open_entry_is_noop(self, tmp_path, monkeypatch):
        # Only a closed entry exists — nothing to close, must not crash.
        closed = {**self._open_buy(), "status": "closed"}
        journal, jf = self._setup(tmp_path, monkeypatch, [closed])
        assert journal.close_position("AAPL", 300.0, 312.0, full_exit=True) is None
        assert json.loads(jf.read_text())[0]["status"] == "closed"  # untouched

    def test_zero_avg_price_guard(self, tmp_path, monkeypatch):
        journal, jf = self._setup(tmp_path, monkeypatch, [self._open_buy()])
        assert journal.close_position("AAPL", 300.0, 0.0, full_exit=True) is None
        assert json.loads(jf.read_text())[0]["status"] == "open"  # no divide-by-zero

    def test_closes_most_recent_open_entry(self, tmp_path, monkeypatch):
        # Two open AAPL buys (re-entry); the newest one is the one being exited.
        older = self._open_buy(trade_id="old")
        newer = {**self._open_buy(trade_id="new"), "date": "2026-06-10"}
        journal, jf = self._setup(tmp_path, monkeypatch, [older, newer])
        tid = journal.close_position("AAPL", 300.0, 312.0, full_exit=True)
        rows = {e["trade_id"]: e for e in json.loads(jf.read_text())}
        assert tid == "new"
        assert rows["new"]["status"] == "closed"
        assert rows["old"]["status"] == "open"  # untouched
```

> **Note on the Phase 1 reconciliation interaction.** The existing
> `_reconcile_journal` flips entries between `open` and `rejected` based on broker
> fills; it explicitly leaves any other status (including `closed`) untouched. So
> `close_position` setting `status="closed"` is safe with that machinery — but add a
> regression test confirming a closed entry survives a later `mark_transactions_live`
> pass if your wire-up could re-run reconciliation over the same run_id.

---

## Suggested order & rough effort

| Phase | What | Risk if skipped | Effort |
|------|------|-----------------|--------|
| 0 | Coded position/sector caps | Uncapped real-money concentration | S |
| 1 | Close the memory loop | System never learns from outcomes | S–M |
| 2 | Feed memory to agents | Churn, incoherent re-entries | M |
| 6 | Local perf vs SPY | Can't tell if strategy beats buy-and-hold | M |
| 3 | Quant/earnings honesty | Decisions driven by undisclosed factors | M |
| 4 | CRO correlation data | Fabricated risk assessment | S–M |
| 5 | Structured output / auto-invalidation | Fragility | M |

Phases 0, 1, 2, and 6 give the most value per unit effort; do those first. Each phase is
independent enough to land as its own PR with its own tests.
