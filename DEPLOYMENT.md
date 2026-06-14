# AI Investor — Pre-Deployment Checklist

This document must be completed in full before any change is deployed to the scheduled trading routine. One missed item can result in duplicate orders, incorrect position sizing, or silent pipeline failures.

**Assumption:** Every change — no matter how small — is treated as a potential capital risk. A typo in `_compute_qty()` is as dangerous as a wrong model name.

---

## Orientation (Read First)

If this is your first time with this codebase, read this before anything else.

### What this system does

Every weekday at 9:45 AM ET, a cloud agent runs a Python pipeline that:
1. Reads the current Robinhood portfolio
2. Downloads market data
3. Asks 7 Claude AI agents to analyze stocks and propose trades
4. Places buy/sell orders on Robinhood
5. Writes a log of everything to git

This runs automatically with real money. There is no human in the loop.

### Key files (the ones you'll touch)

| File | What it does |
|------|-------------|
| `main.py` | Orchestrates the whole pipeline — the "main" of the system |
| `execute.py` | Places orders on Robinhood; computes share quantities |
| `analysis.py` | The 7 Claude agents — all investment reasoning lives here |
| `journal.py` | Writes trade logs; manages the kill switch; prevents double-execution |
| `market_data.py` | Downloads prices and news from Polygon.io |
| `quant_engine.py` | Pure math — computes momentum/quality/valuation scores; no AI |
| `publish.py` | Sends daily portfolio snapshot to Supabase (the public dashboard) |
| `pending_decisions.json` | Written each run; holds today's trade decisions until executed |
| `agent_log.json` | Full output of every agent on every run — the audit trail |
| `trades.csv` | Append-only log of every executed trade |
| `portfolio_peak.json` | Tracks all-time high portfolio value for the kill switch |

### Files you should never commit

| File | Why |
|------|-----|
| `.env` | Contains API keys and Robinhood password |
| `mcp_portfolio.json` | Written by cloud routine with live account data; always overwritten |
| `mcp_market_data.json` | Same — cloud-injected market data |
| `market_snapshot.json` | Large cached data file (3+ MB); not for version control |

`.gitignore` covers most of these but **always run `git status` before committing** and read every line.

### Glossary

**Agentic account** — A dedicated Robinhood account (number: `994046696`) used only by this system. It is separate from any personal Robinhood account. Every order must target this account explicitly via `account_number=AGENTIC_ACCOUNT`. If this argument is missing from an `rh.orders.*` call, the order goes to the default account — a serious bug.

**DRY_RUN** — An environment variable. When `DRY_RUN=true`, the system runs the full pipeline and logs decisions but places no real orders. Always start here. Set to `false` only in production.

**Kill switch** — A safety mechanism in `journal.py`. If the portfolio drops 20%+ from its peak value, the kill switch blocks new BUY orders. SELL orders still execute. This prevents a runaway strategy from losing everything.

**Idempotency** — The property that running the same operation twice produces the same result as running it once. This system achieves it through `pending_decisions.json`: the file records `executed_at` after orders are placed; if the routine runs again, it sees `executed_at` is not null and stops without placing any orders.

**CRO (Chief Risk Officer)** — Agent 7 in the pipeline. It can veto all trades. Its `_safe_call()` default is `approved=False`, meaning if the CRO call fails entirely, all trades are blocked as a safety measure.

**PM (Portfolio Manager)** — Agent 6. Proposes the trade list. The CRO reviews and can reject it.

**Pending decisions envelope** — `pending_decisions.json` is not just a list of trades. It's a dict with `run_id`, `date`, `generated_at`, `executed_at`, and `decisions`. The cloud routine checks `date` (freshness) and `executed_at` (idempotency) before executing anything.

**`seed_today.py`** — A utility script that pre-fetches today's market snapshot and writes `market_snapshot.json`. Run this manually before 9:45 AM if you want the pipeline to use fresh Polygon data instead of triggering live API calls at run time. Not required — the pipeline fetches data itself if the file is missing or stale.

**`fetch_snapshot.py`** — Similar utility. Fetches the current portfolio from Robinhood and writes `mcp_portfolio.json`. Use this if you want to inspect the live portfolio locally without running the full pipeline.

### Before running any command

Always activate the virtual environment first. Every Python command in this document assumes it is active:
```bash
cd /Users/parthchoksi/ai-projects/ai-investor
source venv/bin/activate
# You should see (venv) at the start of your prompt
```

If you see `ModuleNotFoundError` for any import, you forgot this step.

### Verify your API keys are working

Run these before any other step. A bad API key will cause confusing failures deep in the pipeline:
```bash
# Test Anthropic API key
python3 -c "
import anthropic, os
from dotenv import load_dotenv
load_dotenv()
client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
resp = client.messages.create(model='claude-haiku-4-5-20251001', max_tokens=10, messages=[{'role':'user','content':'hi'}])
print('✅ Anthropic API key valid:', resp.content[0].text[:20])
"

# Test Polygon API key
python3 -c "
import requests, os
from dotenv import load_dotenv
load_dotenv()
r = requests.get('https://api.polygon.io/v2/aggs/ticker/AAPL/prev', params={'apiKey': os.getenv('POLYGON_API_KEY')})
print('✅ Polygon API key valid:', r.status_code == 200)
if r.status_code != 200:
    print('   Error:', r.json())
"
```

---

## 0. Change Classification

Before starting, classify the change:

| Class | Example | Requires all sections |
|-------|---------|-----------------------|
| **P0 — Execution path** | `execute.py`, `main.py`, `journal.py`, quantity math | Yes |
| **P1 — Pipeline logic** | `analysis.py`, agent prompts, CRO/PM behavior | Yes |
| **P2 — Data layer** | `market_data.py`, `quant_engine.py`, fallbacks | Yes |
| **P3 — Observability** | `publish.py`, logging, Supabase schema | Sections 1–4 + 8 |
| **P4 — Config/docs** | `CLAUDE.md`, `.env`, requirements, cron schedule | Sections 1–2 + 8 |

If unsure, treat as P0.

---

## 1. Environment & Secrets Audit

### 1.1 Local `.env` completeness
All of the following must be set and valid:
```
ANTHROPIC_API_KEY=...        # not expired; verify with a test call
POLYGON_API_KEY=...          # not expired; verify with a sample ticker
ROBINHOOD_USERNAME=...
ROBINHOOD_PASSWORD=...
ROBINHOOD_MFA_SECRET=...     # TOTP secret (not a code — a base32 secret)
ROBINHOOD_ACCOUNT_NUMBER=994046696   # must be the agentic account
DRY_RUN=true                 # ALWAYS start with dry run
SUPABASE_URL=...
SUPABASE_SERVICE_KEY=...
```

### 1.2 Agentic account lock verification
```bash
# Verify ROBINHOOD_ACCOUNT_NUMBER is the agentic account, never another
grep -r "ROBINHOOD_ACCOUNT_NUMBER" execute.py
# Expected: used in every rh.* call as account_number= argument
# If any rh.* call is missing account_number=, STOP — it will target the default account
```

### 1.3 BLOCKED_TICKERS list
```bash
grep -n "BLOCKED_TICKERS" execute.py
```
Confirm TSLA is present. If the change touches `place_order()` or `execute_trades()`, re-read the entire function and confirm the block check happens before any order is placed.

### 1.4 Cloud routine secret inventory
Confirm that the cloud routine (Routine ID: `trig_01Avvj5aBf3sXbDqUB3g4rTm`) contains:
- `POLYGON_API_KEY` (embedded in prompt)
- Robinhood MCP connector (`13b51fe0-3004-4fa1-ae70-f3535d95ab6f`)
- No plain-text credentials — Robinhood is accessed via MCP only in cloud

---

## 2. Static Code Review

### 2.1 Execution-path review (P0 changes)
For any change touching `execute.py`, `main.py`, or `journal.py`:

- [ ] Every `rh.orders.order_buy_market()` and `order_sell_market()` call passes `account_number=AGENTIC_ACCOUNT`
- [ ] `DRY_RUN` check occurs before any network call in `place_order()`
- [ ] `BLOCKED_TICKERS` check occurs before `DRY_RUN` check (blocked tickers must never reach the broker, even in dry-run logging)
- [ ] `_compute_qty()` returns `0.0` (not raises) when price is missing — confirm `execute_trades()` skips zero-qty orders
- [ ] `mark_pending_executed()` is only called after all orders are submitted, not before
- [ ] `mark_pending_executed()` checks `run_id` match — a mismatched run_id silently no-ops (this is correct behavior; verify it's not swallowed)

### 2.2 Idempotency protocol (P0 changes)
Read `pending_decisions.json` envelope protocol:
- [ ] Cloud routine reads `decisions` from `pending_decisions["decisions"]`, not the root
- [ ] Cloud routine verifies `pending_decisions["date"] == today` before placing orders
- [ ] Cloud routine checks `pending_decisions["executed_at"] is None` before placing orders
- [ ] Cloud routine checks `pending_decisions["execution_started_at"] is None` before placing orders (a non-null claim = a prior attempt crashed mid-execution; recovery is Scenario B, never re-run)
- [ ] Cloud routine stamps + pushes `execution_started_at` BEFORE the first order, and STOPS without placing orders if that push fails (fail toward missed trades, never duplicates)
- [ ] Cloud routine stamps `executed_at` after orders are placed, not before
- [ ] If any order step fails, `executed_at` is NOT stamped — `execution_started_at` (already pushed) is what prevents the next attempt from double-filling

### 2.3 Quantity math audit (P0 changes)
For any change to `_compute_qty()`:
```
BUY:  delta_dollars = (target_weight × total_value) − (current_qty × current_price)
      qty = delta_dollars / current_price   [only if delta > 0]

SELL: delta_dollars = (target_weight × total_value) − (current_qty × current_price)
      qty = |delta_dollars| / current_price [only if delta < 0]
      qty = current_qty                     [if target_weight == 0 → full exit]
```
Manually calculate expected qty for a known portfolio state and compare against `_compute_qty()` output.

### 2.4 CRO default-safe check (P1 changes)
```python
# In analysis.py, the CRO safe_call default must be approved=False
default={"approved": False, ...}
```
If `_safe_call()` fails, ALL trades must be blocked — not approved. Verify this is unchanged.

### 2.5 Kill switch logic
```python
# In journal.py check_kill_switches():
# kill switch must block BUYs only, not SELLs
# In main.py: confirm sell_only path still executes when kill_active=True
```

### 2.6 Validation gate (guardrails.py)
All LLM trade output passes through `guardrails.validate_decisions()` in `main.py` — after qty pre-computation, before `pending_decisions.json` is written. It enforces: action whitelist, `BLOCKED_TICKERS`, ticker ∈ analyzed candidates ∪ holdings, same-ticker BUY+SELL conflict rejection, `target_weight` clamp to [0, 0.10] **with qty recompute**, BUY notional ≤ 12% of portfolio (SELLs exempt — full exits of overweight positions must never be blocked), $5 minimum notional, and the good-faith-violation guard (no SELL within 2 trading days of a broker-accepted BUY, unless the kill switch is active). Interventions are recorded under the `decision_validation` health check (DEGRADED → alert.yml).

Immediately after, `guardrails.enforce_sector_limits()` applies the **25% sector cap** as a code control (it previously lived only in the PM prompt). It uses a static `SECTOR_MAP` (the data layer carries no sector field), rejects the marginal BUY that pushes a sector's projected post-trade weight over 25%, applies SELLs first so a same-sector exit frees budget, and counts existing holdings. Its rejections fold into the same `decision_validation` health check. SELLs are never blocked by the sector cap.

For any change to `guardrails.py` or its call site:
- [ ] Gate still runs AFTER qty pre-computation (notional rules need qty) and BEFORE the `pending_decisions.json` dump
- [ ] Weight clamping still recomputes `qty` — a clamped weight with a stale qty is a no-op at execution
- [ ] SELLs are still exempt from the BUY notional cap
- [ ] SELLs are still exempt from the sector cap (full exits must never be blocked)
- [ ] `enforce_sector_limits` still runs AFTER `validate_decisions` (so weight clamps/conflicts are resolved first)
- [ ] `TestValidateDecisions` and `TestEnforceSectorLimits` in `test_pipeline.py` pass; new rules get new cases
- [ ] PM system prompt still says "0.00–0.10" and "Max sector: 25%" (the gate is defense in depth, not a license to relax the prompt)

---

## 3. Regression Test Suite

Run all of the following before any deployment. Start with the automated suite — it covers the deterministic pipeline logic and the execution-path guards (see 3.9):

```bash
source venv/bin/activate
pytest test_pipeline.py -q     # must be a full pass; a partial pass is not a pass (§12.4)
```

The manual smoke tests below cover paths the automated suite cannot reach (live keys, full pipeline).

### 3.1 Quantity calculation correctness
```bash
source venv/bin/activate
python3 - <<'EOF'
from execute import _compute_qty

# Test: BUY into empty position
# portfolio=$500, price=$100 → 8% target = $40 → 0.4 shares
portfolio = {"total_value": 500.0, "cash": 500.0, "positions": []}
prices = {"NVDA": {"close": 100.0}}
qty = _compute_qty(0.08, "BUY", "NVDA", portfolio, prices)
assert abs(qty - 0.4) < 0.001, f"Expected 0.4 shares, got {qty}"

# Test: BUY to increase an existing position
# portfolio=$500, already hold 0.1 shares ($10), target=$40 → need $30 more → 0.3 shares
portfolio2 = {"total_value": 500.0, "cash": 490.0, "positions": [{"symbol": "NVDA", "qty": 0.1}]}
qty2 = _compute_qty(0.08, "BUY", "NVDA", portfolio2, prices)
assert abs(qty2 - 0.3) < 0.001, f"Expected 0.3 additional shares, got {qty2}"

# Test: SELL full position
# hold 0.5 shares, target=0 → sell all 0.5
portfolio3 = {"total_value": 500.0, "cash": 450.0, "positions": [{"symbol": "NVDA", "qty": 0.5}]}
qty3 = _compute_qty(0.0, "SELL", "NVDA", portfolio3, prices)
assert abs(qty3 - 0.5) < 0.001, f"Expected 0.5 (full exit), got {qty3}"

# Test: SELL partial
# hold 0.5 shares ($50), target=4% ($20) → reduce by $30 → sell 0.3 shares
qty4 = _compute_qty(0.04, "SELL", "NVDA", portfolio3, prices)
assert abs(qty4 - 0.3) < 0.001, f"Expected 0.3 (reduce to 4%), got {qty4}"

# Test: BUY already above target → should be 0
# hold 0.5 shares ($50 = 10%), target=2% ($10) → already above → 0
qty5 = _compute_qty(0.02, "BUY", "NVDA", portfolio3, prices)
assert qty5 == 0.0, f"Expected 0 (already above target), got {qty5}"

# Test: missing price → 0
qty6 = _compute_qty(0.08, "BUY", "MISSING", portfolio, prices)
assert qty6 == 0.0, f"Expected 0 (no price), got {qty6}"

# Test: SELL capped by available_qty (shares_available_for_sells < qty)
portfolio4 = {"total_value": 500.0, "cash": 450.0, "positions": [{"symbol": "NVDA", "qty": 0.5, "available_qty": 0.3}]}
qty7 = _compute_qty(0.0, "SELL", "NVDA", portfolio4, prices)
assert abs(qty7 - 0.3) < 0.001, f"Expected 0.3 (capped by available_qty), got {qty7}"

print("✅ All quantity tests passed")
EOF
```
Expected output: `✅ All quantity tests passed`

### 3.2 Idempotency — double-execution prevention
```bash
python3 - <<'EOF'
import json, os, tempfile, shutil
original_dir = os.getcwd()
tmpdir = tempfile.mkdtemp()
try:
    os.chdir(tmpdir)
    from journal import mark_pending_executed

    run_id = "TEST-RUN-001"
    pending = {"run_id": run_id, "date": "2026-06-08", "generated_at": "2026-06-08T09:45:00Z", "executed_at": None, "decisions": []}
    with open("pending_decisions.json", "w") as f:
        json.dump(pending, f)

    # First stamp — should succeed
    mark_pending_executed(run_id)
    with open("pending_decisions.json") as f:
        state = json.load(f)
    assert state["executed_at"] is not None, "First stamp failed"
    first_stamp = state["executed_at"]

    # Second stamp (simulate retry) — executed_at must NOT change
    mark_pending_executed(run_id)
    with open("pending_decisions.json") as f:
        state2 = json.load(f)
    assert state2["executed_at"] == first_stamp, "Retry overwrote executed_at — double-execution risk!"

    # Wrong run_id — should no-op
    with open("pending_decisions.json", "w") as f:
        json.dump({**pending, "executed_at": None}, f)
    mark_pending_executed("WRONG-ID")
    with open("pending_decisions.json") as f:
        state3 = json.load(f)
    assert state3["executed_at"] is None, "Wrong run_id should not stamp"

    print("✅ Idempotency tests passed")
finally:
    os.chdir(original_dir)
    shutil.rmtree(tmpdir)
EOF
```
Expected output: `✅ Idempotency tests passed`

### 3.3 Kill switch logic
```bash
python3 - <<'EOF'
import os, json, tempfile, shutil
original_dir = os.getcwd()
tmpdir = tempfile.mkdtemp()
try:
    os.chdir(tmpdir)
    from journal import check_kill_switches

    # No peak file — should not trigger
    result, reason = check_kill_switches({"total_value": 500.0})
    assert not result, "Kill switch should not trigger with no peak file"

    # Peak file, small drawdown (5%) — should not trigger
    with open("portfolio_peak.json", "w") as f:
        json.dump({"peak": 526.0}, f)
    result, _ = check_kill_switches({"total_value": 500.0})
    assert not result, "5% drawdown should not trigger kill switch"

    # Peak file, 20%+ drawdown — should trigger
    with open("portfolio_peak.json", "w") as f:
        json.dump({"peak": 630.0}, f)
    result, reason = check_kill_switches({"total_value": 500.0})
    assert result, "20.6% drawdown should trigger kill switch"
    assert "20" in reason or "drawdown" in reason.lower(), f"Reason unclear: {reason}"

    print("✅ Kill switch tests passed")
finally:
    os.chdir(original_dir)
    shutil.rmtree(tmpdir)
EOF
```
Expected output: `✅ Kill switch tests passed`

### 3.4 BLOCKED_TICKERS enforcement
```bash
python3 - <<'EOF'
import os; os.environ["DRY_RUN"] = "true"; os.environ["ROBINHOOD_ACCOUNT_NUMBER"] = "994046696"
from execute import place_order
result = place_order("TSLA", "BUY", 1)
assert result.get("blocked"), f"TSLA should be blocked, got: {result}"
print("✅ BLOCKED_TICKERS test passed")
EOF
```
Expected output: `✅ BLOCKED_TICKERS test passed`

### 3.5 JSON parse resilience
```bash
python3 - <<'EOF'
from analysis import _parse_json

# Valid JSON
assert _parse_json('{"a": 1}', {}) == {"a": 1}

# Fenced JSON
assert _parse_json('```json\n{"a": 1}\n```', {}) == {"a": 1}

# Invalid JSON → returns default
assert _parse_json("not json at all", {"default": True}) == {"default": True}

# Empty string → returns default
assert _parse_json("", []) == []

# Truncation recovery: Haiku hit max_tokens mid-string inside a list (most common real failure)
# The partial incomplete item is stripped; the complete prior items survive.
truncated = '{"ticker": "NVDA", "catalysts": ["AI capex cycle", "china expor'
result = _parse_json(truncated, {})
assert isinstance(result, dict) and result != {}, f"Should recover truncated mid-list JSON, got: {result}"
assert result.get("ticker") == "NVDA", f"Ticker missing after recovery: {result}"
assert result.get("catalysts") == ["AI capex cycle"], f"Should keep complete list items: {result}"
# Note: recovery does NOT handle truncation after a complete number value (e.g. ..., "confidence": 75)
# because the brace-regex misidentifies the closing " of the preceding key. Primary mitigation
# for that case is the max_tokens increase (Jun 11 2026: Agent 2 600→1000, Agent 4 500→800).

print("✅ JSON parse resilience tests passed")
EOF
```
Expected output: `✅ JSON parse resilience tests passed`

### 3.6 Portfolio data source precedence
```bash
python3 - <<'EOF'
import os, json, tempfile, shutil
original_dir = os.getcwd()
tmpdir = tempfile.mkdtemp()
try:
    os.chdir(tmpdir)

    # Write mcp_portfolio.json — should be preferred over robin_stocks.
    # Must carry a fresh as_of (ET) or get_portfolio_summary raises
    # StalePortfolioError (freshness enforcement, Fix 4).
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York")).isoformat()
    mcp = {"as_of": now_et, "cash": 123.45, "total_value": 456.78, "positions": []}
    with open("mcp_portfolio.json", "w") as f:
        json.dump(mcp, f)

    os.environ["DRY_RUN"] = "true"
    os.environ["ROBINHOOD_ACCOUNT_NUMBER"] = "994046696"
    # Note: execute.py must be importable from original_dir — run this from project root
    import sys; sys.path.insert(0, original_dir)
    from execute import get_portfolio_summary
    portfolio = get_portfolio_summary()
    assert portfolio["cash"] == 123.45, f"Should read from mcp_portfolio.json, got: {portfolio['cash']}"

    print("✅ Portfolio data source test passed")
finally:
    os.chdir(original_dir)
    shutil.rmtree(tmpdir)
EOF
```
Expected output: `✅ Portfolio data source test passed`

### 3.7 Quant engine determinism
```bash
python3 - <<'EOF'
from quant_engine import compute_momentum_score, compute_quality_score

# Deterministic: same input → same output, always
history = [{"close": float(100 + i)} for i in range(250)]
r1 = compute_momentum_score(history)
r2 = compute_momentum_score(history)
assert r1 == r2, "Quant engine is not deterministic"

# Empty history → neutral defaults, no exception
r3 = compute_momentum_score([])
assert r3["momentum_score"] == 50

# Empty fundamentals → neutral
r4 = compute_quality_score(None)
assert r4["quality_score"] == 50

print("✅ Quant engine determinism tests passed")
EOF
```
Expected output: `✅ Quant engine determinism tests passed`

### 3.8 Full dry-run pipeline

> **Cost warning:** This test calls the real Anthropic API and Polygon API. Each run costs approximately $0.10–$0.30. Do not run this repeatedly. Run it once, confirm it passes, and move on.

```bash
DRY_RUN=true python main.py 2>&1 | tee /tmp/dry_run_output.txt
```

After it finishes (expect 2–4 minutes), verify these lines appear in the output:
```bash
grep "Daily cycle complete" /tmp/dry_run_output.txt
# Must print: ✅  Daily cycle complete.

grep "DRY RUN" /tmp/dry_run_output.txt
# Must print at least one line containing "DRY RUN" (confirms no real orders placed)

grep "\[7/7\]" /tmp/dry_run_output.txt
# Must print: confirming all 7 agents ran

python3 -c "
import json
with open('pending_decisions.json') as f:
    p = json.load(f)
assert p['executed_at'] is None, 'executed_at should be null after dry run — got: ' + str(p['executed_at'])
print('✅ pending_decisions.json is correct (executed_at is null)')
"
```

What a healthy run looks like (approximate — your output will differ):
```
============================================================
🤖  AI INVESTOR V3 — DAILY CYCLE STARTING
============================================================
📊  Step 1: Fetching portfolio...
   Cash: $500.00
   Positions: 0
   Total Value: $500.00

🛡️   Step 2: Checking kill switches...
   ✅ All clear.

📈  Step 3: Fetching market data...
   85 tickers | 50 news articles | 2 news-discovered

🔢  Step 4: Computing quant scores...
   Top 5 by composite score:
   ...

🧠  Step 5: Running 7-agent pipeline...
   [1/7] Market Regime Strategist...
         Regime: NEUTRAL (confidence: 65)
   [2/7] Research Analyst (20 tickers)...
   [3/7] Earnings & Catalyst Analyst (20 tickers)...
   [4/7] Devil's Advocate (20 tickers)...
   [5/7] Position Review Analyst (0 holdings)...
   [6/7] Portfolio Manager...
         No trades proposed.   ← or a list of BUY/SELL
   [7/7] Chief Risk Officer...
         ✅ APPROVED | risk_budget=20% | ...

   No trades today.   ← or: "3 trade decision(s):"

✅  Daily cycle complete.
```

Red flags — stop and investigate if you see:
- `❌ Order failed` — an order was attempted but rejected (should not happen in DRY_RUN=true)
- `⚠ Agent call failed` — an AI agent errored out; the pipeline continued with defaults
- `🚨 CRO REJECTED` — the risk officer blocked all trades (may be legitimate, or may indicate bad agent output)
- No `[7/7]` line — the pipeline did not complete all 7 agents
- Python traceback — an unhandled exception; the cycle did not finish cleanly

### 3.9 Execution result verification, order routing, and log schema (automated)

These P0 execution-path behaviors are covered by `test_pipeline.py` — run them directly when touching `execute.py` or the Step 6 block of `main.py`:

```bash
pytest test_pipeline.py -q -k "LoadListGuards or TradeLogMigration or OrderExecuted or SellBeforeBuy or ExecutionStampDecision"
```

| Class | Contract it locks in |
|-------|----------------------|
| `TestOrderExecuted` | An order counts as executed only if the broker returned an order id (or DRY_RUN). Rejections (`detail`, `blocked`, empty/None) are never fills. |
| `TestSellBeforeBuyOrdering` | `execute_trades` places all SELLs before any BUY (cash account — BUYs funded by same-day SELLs are otherwise rejected). HOLD and qty-0 decisions are never sent to the broker. |
| `TestExecutionStampDecision` | `pending_decisions.json` is stamped as soon as ANY order was placed (retry must never double-fill) and withheld when NOTHING was placed (next hourly attempt may retry the day). |
| `TestTradeLogMigration` | `trades.csv` is rewritten under the current 12-column header before any append; old rows are preserved. |
| `TestLoadListGuards` | `record_trade` / `record_transaction` / `record_run` survive a `{}`-shaped JSON file instead of crashing after orders are placed. |

Expected output: all selected tests pass.

---

## 4. QA — Agent Pipeline Validation

### 4.1 Agent output shape validation
After the dry-run, inspect `agent_log.json` (last entry):
```bash
python3 - <<'EOF'
import json
with open("agent_log.json") as f:
    log = json.load(f)
last = log[-1]

# Regime
regime = last.get("regime", {})
assert regime.get("regime") in ("RISK_ON", "NEUTRAL", "RISK_OFF"), f"Unexpected regime: {regime}"
assert 0 <= int(regime.get("confidence", -1)) <= 100

# CRO
cro = last.get("cro", {})
assert isinstance(cro.get("approved"), bool), "CRO approved must be bool"
assert "reasoning" in cro

# Final decisions
decisions = last.get("final_decisions", [])
for d in decisions:
    assert d.get("action") in ("BUY", "SELL"), f"Invalid action: {d}"
    assert 0.0 <= float(d.get("target_weight", -1)) <= 0.10, f"Weight out of bounds: {d}"
    assert "ticker" in d and "rationale" in d

print(f"✅ Agent log validation passed ({len(decisions)} decisions)")
EOF
```

### 4.2 Prompt drift check (P1 changes)
If any system prompt was modified in `analysis.py`:
- [ ] PM system prompt still contains "0.00–0.10" weight constraint
- [ ] PM system prompt still contains "Hard-blocked (NEVER propose): TSLA"
- [ ] CRO system prompt still contains "You may reject any trade"
- [ ] CRO `_safe_call` default is still `approved=False`
- [ ] No agent is asked to output tickers in a different format than expected by the parser

### 4.3 Token budget check (P1 changes)
```bash
python3 - <<'EOF'
from analysis import MODEL_FAST, MODEL_SMART, MAX_CANDIDATES
# Current max_tokens per agent (as of Jun 11 2026):
#   Agent 1 (Regime, Sonnet):      700
#   Agent 2 (Research, Haiku):    1000
#   Agent 3 (Earnings, Haiku):     600
#   Agent 4 (Devil's Adv, Haiku):  800
#   Agent 5 (Position, Haiku):     600
#   Agent 6 (PM, Sonnet):         2000
#   Agent 7 (CRO, Sonnet):        1200
# Haiku: ~600–1000 tokens out, ~1200 tokens in (richer news context since Jun 11)
# Sonnet: ~700–2000 tokens out, ~3000–10000 tokens in
# Ensure MAX_CANDIDATES hasn't been raised without understanding cost implications
print(f"MAX_CANDIDATES = {MAX_CANDIDATES}")
assert MAX_CANDIDATES <= 25, "Raising MAX_CANDIDATES above 25 significantly increases cost and latency"
print(f"Estimated Haiku calls per run: {MAX_CANDIDATES * 3}")
print(f"Estimated Sonnet calls per run: 3")
print("✅ Token budget check passed")
EOF
```

---

## 5. Financial Integrity Pre-Flight

### 5.1 Pre-deployment position reconciliation
Before any deployment, snapshot the current Robinhood agentic account state:
```bash
python3 - <<'EOF'
import os; os.environ["DRY_RUN"] = "true"; os.environ["ROBINHOOD_ACCOUNT_NUMBER"] = "994046696"
from execute import get_portfolio_summary
p = get_portfolio_summary()
print(f"Cash: ${p['cash']:,.2f}")
print(f"Total value: ${p['total_value']:,.2f}")
for pos in p["positions"]:
    print(f"  {pos['symbol']}: {pos['qty']} shares @ ${pos['avg_price']:.2f}")
EOF
```

**Where to record this:** Copy the output and paste it into your PR description (or a comment on your commit if working alone). Label it "Pre-deployment portfolio baseline." This is what you compare against if something goes wrong. Do not skip this even for "trivial" changes — you need a before-state to diagnose any after-state problem.

### 5.2 Pending decisions freshness check
```bash
python3 - <<'EOF'
import json
from datetime import date
with open("pending_decisions.json") as f:
    p = json.load(f)
today = date.today().isoformat()
print(f"pending date: {p['date']}  today: {today}")
print(f"executed_at:  {p['executed_at']}")

# Before a non-trading deployment (pushing docs, config, etc.):
# executed_at should be non-null if today's cycle already ran.
# If executed_at is null and date==today, there are unexecuted decisions.
# Deploying now would not affect those (they sit as pending until the routine runs).
# But if the deploy changes execute.py, the routine may behave differently — confirm this is intended.
EOF
```

### 5.3 Stale file audit
```bash
# These files can cause incorrect behavior if stale from a previous run:
ls -la mcp_portfolio.json mcp_market_data.json market_snapshot.json 2>/dev/null

python3 - <<'EOF'
import json, os
from datetime import date
today = date.today().isoformat()

for fname in ["mcp_portfolio.json", "mcp_market_data.json"]:
    if os.path.isfile(fname):
        with open(fname) as f:
            data = json.load(f)
        # mcp_portfolio.json freshness key is as_of (ISO ts, ET); mcp_market_data uses date
        raw = data.get("as_of") or data.get("date", "unknown")
        file_date = str(raw)[:10]  # ISO date portion
        status = "✅ fresh" if file_date == today else f"⚠️  STALE ({raw})"
        print(f"{fname}: {status}")
        if fname == "mcp_portfolio.json" and "as_of" not in data:
            print("   ⚠️  mcp_portfolio.json has no as_of — get_portfolio_summary will raise StalePortfolioError")
    else:
        print(f"{fname}: not present (OK — cloud will write it)")

if os.path.isfile("market_snapshot.json"):
    with open("market_snapshot.json") as f:
        snap = json.load(f)
    snap_date = snap.get("date", "unknown")
    status = "✅ today" if snap_date == today else f"⚠️  {snap_date} — will be ignored by market_data.py"
    print(f"market_snapshot.json: {status}")
EOF
```

### 5.4 Peak portfolio file integrity
```bash
python3 - <<'EOF'
import json, os
if os.path.isfile("portfolio_peak.json"):
    with open("portfolio_peak.json") as f:
        data = json.load(f)
    peak = data.get("peak", 0)
    assert peak > 0, f"Peak value corrupt: {peak}"
    assert peak < 100_000, f"Peak value suspiciously large: {peak}"
    print(f"✅ Portfolio peak: ${peak:,.2f} (updated: {data.get('updated', 'unknown')})")
else:
    print("⚠️  portfolio_peak.json missing — will be created on next run using current portfolio value")
EOF
```

---

## 6. DST / Schedule Verification

Neither cron auto-adjusts for Daylight Saving Time. Both must be updated manually at DST transitions (2nd Sunday March, 1st Sunday November).

### Daily Trading Cycle — `trig_01Avvj5aBf3sXbDqUB3g4rTm`

| Period | UTC cron | ET time |
|--------|----------|---------|
| EDT (Mar–Nov) | `45 13 * * 1-5` | 9:45 AM EDT |
| EST (Nov–Mar) | `45 14 * * 1-5` | 9:45 AM EST |

View/manage: https://claude.ai/code/routines/trig_01Avvj5aBf3sXbDqUB3g4rTm

### EOD Close Snapshot — `trig_01GtedgrYMGHYCJVLLHXZTCq`

| Period | UTC cron | ET time |
|--------|----------|---------|
| EDT (Mar–Nov) | `0 20 * * 1-5` | 4:00 PM EDT |
| EST (Nov–Mar) | `0 21 * * 1-5` | 4:00 PM EST |

View/manage: https://claude.ai/code/routines/trig_01GtedgrYMGHYCJVLLHXZTCq

**Check before deploying near DST transitions:**
```bash
python3 - <<'EOF'
from datetime import datetime, timezone
now_utc = datetime.now(timezone.utc)
print(f"Current UTC hour: {now_utc.hour}:{now_utc.minute:02d}")
print(f"Daily cycle fires at UTC 13:45 (EDT) or 14:45 (EST)")
print(f"EOD snapshot fires at UTC 20:00 (EDT) or 21:00 (EST)")
print("Verify both routine schedules match the current period.")
EOF
```

---

## 7. Deployment Steps

### 7.0 Pre-deploy gates (MANDATORY — both required before §7.2 commit)

These two gates run **before** the git commit for every deploy. Neither is
optional for P0/P1 changes regardless of diff size.

**7.0.1 — Update `RELEASE_NOTES.md`**
- The change must already be described under `[Unreleased]` (add it in the same
  PR as the code).
- On deploy, **move `[Unreleased]` into a new dated release block** at the top of
  the history: `## [YYYY-MM-DD] — <summary>  ·  ~HH:MM PT  ·  <short-hashes>`.
  Leave a fresh empty `[Unreleased]` section above it.
- A deploy with code changes but no `RELEASE_NOTES.md` update **fails this gate**.
  (A regression test asserts an `[Unreleased]` section exists — see §12 /
  `TestReleaseNotes`.)

**7.0.2 — Expert code review**
- Run an **expert code review on the full diff** and resolve every finding before
  committing. In Claude Code: `/code-review high` (use `/code-review ultra` for P0
  execution-path changes — it launches the deeper multi-agent cloud review).
- The reviewer (or the review agent) must complete the §11.2 checklist and leave a
  written verdict in the PR/commit. Unresolved P0/P1 findings **block the deploy**.
- Solo-developer note: the §11 24-hour cooling period still applies to P0/P1; the
  automated expert review supplements it, it does not replace the cool-off.

### 7.1 Local validation (always)

> ⚠️ **Never run the dry-run pipeline during market hours on a trading day.** `DRY_RUN=true python main.py` overwrites `pending_decisions.json` with `executed_at: null` and today's date. If that file is then committed/pushed (or the day's cycle already executed), a later routine attempt (10:45/11:45/12:45 ET) sees "not executed + fresh data" and **places the day's orders again — double-fill**. Run it after 4 PM ET or on a non-trading day, and never stage `pending_decisions.json` from a local run. If you must skip this step on a trading day, document the skip in the commit message.

```bash
# 1. Run all regression tests from Section 3 (pytest suite first — see §3 intro)
# 2. Run dry-run pipeline (subject to the trading-day warning above)
DRY_RUN=true python main.py

# 3. Verify pending_decisions.json is clean
cat pending_decisions.json | python3 -c "import sys,json; d=json.load(sys.stdin); print('executed_at:', d['executed_at'])"
```

### 7.2 Git commit
```bash
git status
git diff
```

Read every line of `git status` before staging anything. Then:
```bash
git add <specific files>   # name files explicitly, one at a time
git commit -m "..."
```

**Why not `git add .` or `git add -A`?**
`.gitignore` protects `.env`, but it can be misconfigured — especially after adding new files. `git add .` will stage anything not in `.gitignore`, including API keys in new files, `market_snapshot.json` (3 MB of market data), or temporary debug files you forgot to delete. Staging files explicitly forces you to see exactly what you're committing.

**Commit message format** — be specific; these messages are the audit trail:
```
fix: clamp target_weight to 0.10 before passing to _compute_qty
feat: add sector exposure validation to CRO agent
ops: manual stamp of pending_decisions after partial failure on 2026-06-08
```

### 7.3 Routine update (if cloud prompt changed)
If the cloud routine prompt was modified:
1. Navigate to: https://claude.ai/code/routines/trig_01Avvj5aBf3sXbDqUB3g4rTm
2. Verify the updated prompt:
   - Contains the current `POLYGON_API_KEY` value
   - Still follows the `pending_decisions.json` idempotency protocol (steps 1–5 in CLAUDE.md)
   - Still stamps `executed_at` after MCP orders, not before
   - Verifies date freshness before executing
   - Verifies `executed_at is None` before executing
3. Do NOT trigger an immediate run — wait for the next scheduled window unless manual test is intended

### 7.4 First live-run monitoring
After deploying, monitor the next scheduled run:
```bash
# After 9:45 AM ET, check:
cat pending_decisions.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d, indent=2))"
# executed_at should be non-null if orders were placed
# decisions list should match what you expected

cat agent_log.json | python3 -c "import sys,json; d=json.load(sys.stdin); last=d[-1]; print('regime:', last.get('regime',{}).get('regime')); print('decisions:', len(last.get('final_decisions', [])))"

# Check Supabase for today's snapshot
# Check trades.csv for any new rows
tail -5 trades.csv
```

---

## 8. Post-Deployment Verification

### 8.1 Within 30 minutes of next cycle
- [ ] `pending_decisions.json` has today's date in `date` field
- [ ] `agent_log.json` has a new entry with today's run_id
- [ ] If decisions were made: `executed_at` is non-null in `pending_decisions.json`
- [ ] If decisions were made: `trades.csv` has new rows with today's date and a non-empty `broker_order_id`
- [ ] Supabase `portfolio_snapshots` table has today's row
- [ ] No Python exceptions in the pipeline output (check `agent_log.json` for truncated/missing agent fields)

### 8.2 Position reconciliation (if trades were executed)
Compare expected positions (from `pending_decisions.json`) against actual Robinhood positions:
```bash
python3 - <<'EOF'
import os, json
os.environ["DRY_RUN"] = "true"
os.environ["ROBINHOOD_ACCOUNT_NUMBER"] = "994046696"

from execute import get_portfolio_summary
portfolio = get_portfolio_summary()

with open("pending_decisions.json") as f:
    pending = json.load(f)

print("=== Actual Robinhood Positions ===")
for pos in portfolio["positions"]:
    print(f"  {pos['symbol']}: {pos['qty']} shares, value=${pos['market_value']:,.2f}")

print(f"\n=== Decisions Executed ===")
for d in pending.get("decisions", []):
    print(f"  {d.get('action')} {d.get('ticker')} target={d.get('target_weight'):.1%}")

# Manually verify each BUY/SELL resulted in the expected position change
# The system does not do this automatically — this is a manual reconciliation step
EOF
```

### 8.3 Transaction log integrity
```bash
python3 - <<'EOF'
import json, os
from datetime import date
today = date.today().isoformat()

if os.path.isfile("transactions.json"):
    with open("transactions.json") as f:
        txs = json.load(f)
    today_txs = [t for t in txs if t.get("date") == today]
    print(f"Transactions today: {len(today_txs)}")
    for tx in today_txs:
        dry = tx.get("dry_run")
        broker_id = tx.get("broker_order_id")
        print(f"  {tx['action']} {tx['ticker']} qty={tx['qty']} broker_id={broker_id} dry_run={dry}")
        if not dry and not broker_id:
            print(f"  ⚠️  LIVE trade missing broker_order_id — order may have failed silently")
else:
    print("transactions.json not found — no transactions recorded")
EOF
```

---

## 9. Rollback Procedure

### 9.1 Emergency stop (immediate)
If something goes wrong during or after execution:

**Step 1 — Disable the routine (do this first)**
1. Open https://claude.ai/code/routines/trig_01Avvj5aBf3sXbDqUB3g4rTm
2. Click the routine to open it
3. Toggle it to **disabled** or **paused** — the exact UI label may vary; look for an on/off toggle or a "Disable" button
4. Confirm it shows as disabled before proceeding — refreshing the page is the safest confirmation

**Step 2 — Preserve state**
Do not delete or overwrite any JSON files in the project. They are the audit trail:
- `pending_decisions.json` — shows what was decided and whether it was executed
- `agent_log.json` — shows every agent's output for the run
- `transactions.json` — shows what was logged as executed

**Step 3 — Check Robinhood for open orders**
Log in to Robinhood on the agentic account. Go to Account → History and look for any orders placed in the last hour. Look specifically for:
- Orders in "pending" state (not yet filled)
- Orders that filled unexpectedly

**Step 4 — Cancel open orders if needed**
Cancel any open orders that should not have been placed. Do this in the Robinhood UI, not via code — code changes may have introduced the bug.

**Step 5 — Document**
Write down: what you changed, when the routine ran, what orders appeared in Robinhood, and what state `pending_decisions.json` was in. You will need this to diagnose the cause.

### 9.2 Code rollback
```bash
# Roll back to the last known-good commit
git log --oneline -10
git checkout <known_good_sha> -- execute.py main.py analysis.py journal.py
# Run regression tests again (Section 3) before re-enabling the routine
```

### 9.3 pending_decisions.json manual stamp (emergency)

> **Danger.** Only do this if you are certain that orders were placed in Robinhood but `executed_at` was never written — for example, the routine crashed between the order placement step and the stamp step. If you stamp a file for a run where orders were NOT placed, the routine will think the run is done and will not re-execute, meaning your portfolio will not be rebalanced today. Verify in Robinhood's order history first.

**Before running this, answer all of the following:**
- [ ] I have opened Robinhood and confirmed orders appear in the account's history for today's run
- [ ] The number and type of orders in Robinhood match what is in `pending_decisions.json`["decisions"]
- [ ] `pending_decisions.json`["executed_at"] is currently `null`
- [ ] `pending_decisions.json`["date"] is today's date

If all four are true:
```bash
python3 -c "
import json
with open('pending_decisions.json') as f:
    p = json.load(f)
print('About to stamp run_id:', p['run_id'])
print('Date:', p['date'])
print('Decisions:', len(p.get('decisions', [])))
input('Press Enter to confirm, Ctrl+C to abort...')
from journal import mark_pending_executed
mark_pending_executed(p['run_id'])
print('Stamped.')
"
git add pending_decisions.json
git commit -m "ops: manually stamp pending_decisions as executed after partial failure"
```

---

## 10. Known Risks and Accepted Limitations

These are documented risks in the current system. Any deployment that touches these areas requires extra scrutiny.

| Risk | Location | Severity | Mitigation |
|------|----------|----------|------------|
| SPY price in morning snapshot is intraday (not official close) | `publish.py` | Low | By design — user wants SPY synced with portfolio on every update. EOD run overwrites with official close via `is_close=True`. Chart labels the index as "indexed to 100" not "closing prices". |
| `market_snapshot.json` missing or stale → SPY falls back to Polygon "prev" (previous day) | `publish.py:_fetch_spy_from_snapshot` | Low | Polygon fallback still gives valid SPY data; worst case is SPY lags by one day in the dashboard, identical to the pre-fix behavior. |
| No `target_weight` bounds validation before execution | `execute.py:_compute_qty` | ~~High~~ **Fixed (guardrails.py)** | `validate_decisions()` clamps weight to [0, 0.10] + recomputes qty, rejects unknown/blocked tickers, caps BUY notional at 12%, GFV guard — see §2.6 |
| 25% sector cap enforced only in the PM prompt (an LLM is not a control) | `analysis.py` PM prompt | ~~Medium~~ **Fixed (guardrails.py)** | `enforce_sector_limits()` rejects BUYs that push a sector's projected weight > 25%, using a static `SECTOR_MAP` — see §2.6. Static map is a known approximation (TODO: source sectors from Polygon paid tier) |
| Quality/valuation factors silently default to 50 (free-tier Polygon returns no fundamentals) → "4-factor" composite is really momentum+volatility | `quant_engine.py` | ~~Medium~~ **Fixed (honest reweight)** | Composite now weights only factors with real data and renormalizes; `factors_used` records which were real; `_fmt_scores` shows N/A. No ranking impact (the 50s were constant) — transparency fix |
| `decision_journal` `actual_return`/`thesis_correct` never populated → no outcome feedback | `journal.py` | ~~Medium~~ **Fixed (close_position)** | A SELL closes the matching open BUY with realized return + correctness. Cloud caveat: closes speculatively under DRY_RUN; a rejected SELL leaves a wrongly-closed BUY that reconciliation won't re-open (feedback-quality, not capital — accepted) |
| Portfolio state is snapshot at pipeline time, not execution time | `main.py`, `execute.py` | Medium | Market orders; prices drift between analysis and execution. Acceptable for small portfolio |
| `agent_log.json` grows unboundedly | `journal.py:record_run` | ~~Low~~ **Fixed `8f0b2e9`** | Capped at 90 entries (~3 months). Previously whole file loaded into memory each run |
| No fill confirmation — broker order "placed" ≠ "filled" | `execute.py:place_order` | Medium | Manual reconciliation in Step 8.2 after each live run. `journal.mark_transactions_live(run_id, fills)` reconciles all three logs (transactions.json / trades.csv / decision_journal.json) against accepted orders; zero-fills-with-decisions records `reconciliation: FAILED` → alert.yml |
| DST clock drift | Routine cron | Medium | Manual update in November/March; noted in CLAUDE.md |
| mcp_portfolio.json staleness in cloud | `execute.py:get_portfolio_summary` | ~~High~~ **Fixed (Fix 4)** | `get_portfolio_summary` raises `StalePortfolioError` if `as_of` is missing or not today (ET); `main.py` aborts before sizing orders (portfolio: FAILED → alert.yml). Routine STEP 1 writes `as_of` |
| Cloud market data limited to MCP quotes only | `market_data.py` | Medium | Quant scores default to 50; agents use training knowledge |
| No automated alerting on pipeline failure | All | ~~High~~ **Partially fixed** | `system_health.json` + `alert.yml` GitHub Issue alerts on FAILED/DEGRADED; 529 API retries prevent silent agent failures |
| JSON files corruptible mid-write | `journal.py`, `health.py` | ~~High~~ **Fixed `8f0b2e9`** | All writes now atomic via `.tmp` + `os.replace()` |
| UTC vs ET date mismatch in logs | `main.py`, `execute.py`, `journal.py` | ~~Medium~~ **Fixed `8f0b2e9`** | All date stamps now use `America/New_York` explicitly |
| SELL qty not bounded by sellable shares | `execute.py:_compute_qty` | ~~Medium~~ **Fixed `8f0b2e9`** | `_compute_qty` now caps SELL to `available_qty` (from `shares_available_for_sells`) |
| Full portfolio churn on bad PM output | `main.py` | ~~Medium~~ **Fixed `8f0b2e9`** | Circuit breaker halts execution if SELL notional > 50% of portfolio |
| Transient Anthropic 529 overloads kill agent pipeline | `analysis.py` | ~~High~~ **Fixed `7652b9d`** | All agents retry 2× with 30s/60s backoff on 529 responses |
| Haiku response truncated mid-JSON when max_tokens too low | `analysis.py:_parse_json` | ~~High~~ **Fixed `61ab95a`** | Raised max_tokens per agent (700–1000 range); added truncation recovery in `_parse_json` (brace-counting + suffix append) |
| `decision_journal.json` initialized as `{}` (empty dict) on first run | `journal.py:record_trade` | ~~Medium~~ **Fixed `b8ec88d`** | Added `isinstance(journal, list)` guard; resets to `[]` when file contains `{}` |
| GitHub Actions scheduled cron silently skipped | `market_data.yml` | ~~Medium~~ **Mitigated `2b21c7f`** | 3 staggered triggers (7:00/8:00/8:30 AM EDT) — one silent skip can't strand the routine. Safety dispatch remains the highest-reliability option |

---

## 11. Code Review Requirements

Every change must pass code review before deployment. The review is not optional for P0/P1 changes regardless of how small the diff appears.

### 11.1 Reviewer selection

| Change class | Required reviewer |
|--------------|-------------------|
| P0 — Execution path | A second human who understands the trade execution flow |
| P1 — Pipeline logic | Anyone familiar with the 7-agent architecture |
| P2 — Data layer | Anyone who understands how quant scores are computed |
| P3/P4 — Observability/config | Self-review acceptable with documented sign-off |

If a second human is not available for a P0 change, delay the deployment until one is. Capital risk does not have a deadline.

**Solo developer exception:** If you are the only developer, you may self-review P0/P1 changes only after a mandatory 24-hour cooling period between writing and reviewing the code. Fresh eyes catch things tired eyes miss. Use the reviewer checklist below as your self-review checklist. Document that you self-reviewed and why a second reviewer was unavailable. Do not skip the waiting period — it exists because you will not catch your own mistakes immediately after writing them.

### 11.2 Code review checklist (reviewer's responsibility)

The reviewer is expected to do more than read the diff. For every P0/P1 PR:

**Execution correctness**
- [ ] Trace the full call path from `main.py` through to `place_order()` for both BUY and SELL scenarios
- [ ] Verify `account_number=AGENTIC_ACCOUNT` is present on every new `rh.*` call introduced
- [ ] Confirm no new code path can reach `rh.orders.*` without passing through `DRY_RUN` check
- [ ] Confirm no new code path can reach `rh.orders.*` without passing through `BLOCKED_TICKERS` check
- [ ] If `_compute_qty()` is touched: manually calculate expected output for at least 3 test scenarios and document them in the PR description

**Idempotency**
- [ ] If `pending_decisions.json` read/write logic is touched: trace through what happens when the routine runs twice with the same file
- [ ] If `mark_pending_executed()` is touched: confirm it still no-ops on wrong `run_id` and on missing file

**State management**
- [ ] Identify every file that the change reads or writes
- [ ] For each file write: confirm it is atomic enough that a crash mid-write does not leave a corrupt file (Python's `json.dump()` to an open filehandle is not atomic — the risk is known and accepted)
- [ ] For each file read: confirm stale or missing file is handled without raising an unhandled exception

**Agent pipeline changes**
- [ ] If any system prompt changed: diff the prompt and confirm constraints (weight bounds, blocked tickers, output format) are preserved
- [ ] If `_safe_call()` default changed for CRO: confirm it is still `approved=False`
- [ ] If new agent added: confirm it has a safe default that does not execute trades when the call fails
- [ ] If `MAX_CANDIDATES` changed: estimate token cost impact and confirm it is acceptable

**Regression test coverage**
- [ ] Every new execution-path behavior introduced has a corresponding test in Section 3 or Section 12
- [ ] All existing tests from Section 3 still pass after the change (author runs them; reviewer verifies output in PR)

**Review output**
The reviewer must leave a written comment in the PR or commit message confirming they completed this checklist. "LGTM" is not sufficient.

---

## 12. QA Requirements for New Functionality

Every new feature or behavioral change must ship with its own QA coverage. This section defines the minimum bar.

### 12.1 What counts as new functionality

- Any new agent added to the pipeline
- Any new data source (new API, new fallback path, new file input)
- Any change to how `target_weight` is computed or capped
- Any change to kill switch conditions or thresholds
- Any new output format (new fields in JSON, new CSV columns)
- Any change to how decisions are executed (new order type, new routing logic)
- Any change to the idempotency protocol (`pending_decisions.json` envelope)
- Any new Supabase table or column

### 12.2 Test requirements per change type

**New agent (analysis.py)**
1. Unit test: mock the Anthropic client and verify the agent returns the correct default when the client raises an exception
2. Unit test: verify the returned dict contains all required keys (match the keys consumed by the next agent in the pipeline)
3. Integration test: run the agent in isolation with real data and inspect the output shape
4. Pipeline test: run the full dry-run pipeline and verify the new agent's output appears in `agent_log.json`
5. Safety test: verify a failed new agent does not block execution of subsequent agents (default is returned, not raised)

**New data source / fallback**
1. Happy path: verify the source returns data in the expected format
2. Failure path: simulate the source being unavailable (e.g., set a bad API key) and verify fallback kicks in
3. Stale data path: simulate the source returning data from a previous day and verify it is detected and handled
4. Empty path: verify an empty response from the source does not result in trades being skipped entirely unless that is the intended behavior

**New kill switch condition**
1. Below threshold: verify normal trading proceeds
2. At threshold: verify the trigger fires at exactly the configured value
3. Above threshold: verify the trigger is active
4. Edge case — `total_value == 0`: verify no divide-by-zero exception
5. SELLs still execute when kill is active: verify this explicitly

**Quantity math change**
Write explicit test cases covering all of these scenarios before committing:
- BUY into a position the portfolio does not currently hold
- BUY to increase an existing position
- BUY where portfolio already meets or exceeds target weight (expected: 0 shares)
- SELL full position (target_weight=0)
- SELL partial position
- SELL where portfolio is already at or below target weight (expected: 0 shares)
- Missing price (expected: 0 shares, no exception)
- Fractional shares (expected: rounded to 6 decimal places)

**New Supabase table or column**
1. Schema change: add the DDL to `schema.sql` (not just the code)
2. Backward compatibility: if adding a column, confirm existing rows without the column are handled (use `IF NOT EXISTS` or nullable column with a default)
3. Publish test: run `publish.py` in isolation and verify the new column is populated correctly
4. Missing column test: verify `publish.py` does not crash if Supabase does not yet have the column (graceful failure, not a blocker for the trading cycle)

**Change to `pending_decisions.json` envelope**
1. New field: verify `mark_pending_executed()` still works when the new field is present
2. New field: verify cloud routine still reads `decisions` correctly
3. Backward compat: verify old files (missing the new field) do not crash the reader
4. Test the full idempotency sequence: generate → read → execute → stamp → re-read (confirm second read does not execute)

### 12.3 Test file location and format

Add all new tests to the regression section (Section 3) of this document or to a dedicated `tests/` directory if the project grows to warrant it. Each test must:
- Be self-contained (creates its own temp state, cleans up after itself)
- Print a clear `✅ ... passed` on success
- Raise an `AssertionError` with a meaningful message on failure
- Not require a live Robinhood login or live Anthropic API call (mock them)
- Run in under 5 seconds

### 12.4 Regression gate

Before every deployment, run the complete regression suite from Section 3. If any test fails:
1. Stop — do not deploy
2. Identify whether the failure is a pre-existing issue or introduced by the change
3. Fix the failure or explicitly document it as a known issue with a remediation plan
4. Re-run the full suite before proceeding

A partial pass is not a pass.

---

## 13. PR Description Template

When opening a pull request (or writing a detailed commit message if working alone), use this template. It forces you to answer the questions a reviewer needs before they can verify your change is safe.

```
## What changed
<!-- One paragraph. What does this PR do? -->

## Why
<!-- What problem does it solve? What was wrong before? -->

## Change class
<!-- P0 / P1 / P2 / P3 / P4 — see Section 0 -->

## Files changed and why each was touched
<!-- List each file and a one-line reason. Be specific. -->
- execute.py — added target_weight clamping to 0.10 in _compute_qty()
- DEPLOYMENT.md — updated regression test 3.1 to cover new clamping case

## Pre-deployment portfolio baseline (from Section 5.1)
<!-- Paste the output of the position snapshot command here -->
Cash: $XXX.XX
Total value: $XXX.XX
  AAPL: X.XXXXX shares @ $XXX.XX
  ...

## Regression tests run
<!-- List each test from Section 3 and whether it passed -->
- [x] 3.1 Quantity calculation — PASSED
- [x] 3.2 Idempotency — PASSED
- [x] 3.3 Kill switch — PASSED
- [x] 3.4 BLOCKED_TICKERS — PASSED
- [x] 3.5 JSON parse resilience — PASSED
- [x] 3.6 Portfolio data source — PASSED
- [x] 3.7 Quant engine determinism — PASSED
- [x] 3.8 Dry-run pipeline — PASSED

## New QA tests added
<!-- Reference the test name and what scenario it covers, or explain why N/A -->

## Dry-run output summary
<!-- Paste the key lines from the dry-run (Step 3.8):
     regime, number of candidates, PM proposed X decisions, CRO approved/rejected -->

## Known risks or concerns
<!-- Anything the reviewer should pay extra attention to, or anything you're not 100% sure about -->

## Reviewer instructions
<!-- Anything specific you want the reviewer to check, or steps to reproduce your testing -->
```

---

## 14. Common Mistakes

These are the mistakes this system's deployment process is specifically designed to prevent. Read through this before your first deployment.

**Running with `DRY_RUN=false` by accident**
The `.env` file has `DRY_RUN=true`. If you change it to test something and forget to change it back, the next dry-run you think you're running will place real orders. Before every pipeline run, confirm:
```bash
grep DRY_RUN .env
```

**Forgetting to activate the virtual environment**
If you see `ModuleNotFoundError: No module named 'anthropic'` or similar, you forgot `source venv/bin/activate`. Every Python command in this document assumes the venv is active.

**Running tests from the wrong directory**
All tests must be run from `/Users/parthchoksi/ai-projects/ai-investor/` (the project root). If you `cd` somewhere else, imports will fail. Check:
```bash
pwd  # should print the project root
```

**Committing `market_snapshot.json`**
This file is 3+ MB and changes every day. It is gitignored but double-check after any `git add`:
```bash
git status | grep market_snapshot
# Should print nothing. If it shows up, remove it: git restore --staged market_snapshot.json
```

**Leaving a stale `mcp_portfolio.json` from local testing**
If you wrote `mcp_portfolio.json` locally while debugging, delete it before committing. The cloud routine writes this file from live data at run time. A stale local copy committed to the repo will cause the cloud routine to use yesterday's (or last week's) portfolio data for position sizing.
```bash
# Check: is this file stale?
python3 -c "import json; d=json.load(open('mcp_portfolio.json')); print('date:', d.get('date'))"
# If the date is not today, either delete it or don't commit it.
```

**Running the dry-run multiple times to "make sure it's fine"**
Each run of `main.py` costs $0.10–$0.30 in API calls. Run it once, read the output carefully, and address any issues before running again. If you run it 10 times in one day, you've spent ~$2–3 for nothing.

**Ignoring `⚠ Agent call failed` warnings**
The pipeline is designed to continue when an agent fails — it uses a safe default. But "continue with defaults" means the system is flying with less information than intended. If you see this warning during a dry-run, it is not automatically fine. Investigate why the call failed before deploying.

**Editing the cloud routine prompt without testing it**
The cloud routine has its own embedded `POLYGON_API_KEY` and instructions. If you update it to use a new protocol (e.g., a new field in `pending_decisions.json`) but haven't deployed the corresponding code change, the next run will fail. Always deploy code and routine prompt changes together.

**Treating `agent_log.json` as disposable**
This file is your 12-month audit trail. Do not delete it, truncate it, or treat it as a temp file. If it grows too large, archive old entries rather than deleting them.

**Not checking `broker_order_id` after a live run**
After any live execution (not dry-run), check that `transactions.json` has a non-empty `broker_order_id` for every trade. A missing `broker_order_id` means the order was submitted but Robinhood returned an error. The position was NOT established. See Section 8.3.

---

## Sign-Off

**Where this goes:** Copy this sign-off into your PR description (using the template in Section 13) or into the commit message if you are working alone. It is not a private checklist — it is a record that the process was followed.

Before pushing any P0/P1 change to a live scheduled run:

```
Change description: ___________________________________
Changed files: ________________________________________
Change class (P0/P1/P2/P3/P4): _______________________
Code review completed by: _____________________________ (or: self-review after 24h cooling period)
Common mistakes checked (Section 14): [ ] YES
All Section 3 regression tests passed: [ ] YES
New QA tests added for new functionality (Section 12): [ ] YES / [ ] N/A
Dry-run completed without errors: [ ] YES
Agent log inspected (Section 4.1): [ ] YES
pending_decisions.json executed_at is null: [ ] YES
Position reconciliation baseline recorded and pasted in PR (Section 5.1): [ ] YES
DST schedule verified (if near transition): [ ] YES / [ ] N/A
Date: _______________
```
