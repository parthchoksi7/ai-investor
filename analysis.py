"""
analysis.py — 7-agent investment pipeline.

Pipeline order:
  1. Market Regime Strategist  (portfolio-level, Sonnet)
  2. Research Analyst           (per-ticker,       Haiku)
  3. Earnings & Catalyst Analyst(per-ticker,       Haiku)
  4. Devil's Advocate           (per-ticker,       Sonnet)
  5. Position Review Analyst    (per-holding,      Haiku)
  6. Portfolio Manager          (portfolio-level,  Sonnet)
  7. Chief Risk Officer         (portfolio-level,  Sonnet)
"""

import os
import re
import time
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

_client = None
_client_lock = threading.Lock()


def _get_client():
    """Return (and lazily create) the shared Anthropic client."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:  # double-checked locking — safe under Python's GIL
                import anthropic  # deferred so tests can import this module without the package
                api_key = os.getenv("ANTHROPIC_API_KEY")
                if api_key:
                    _client = anthropic.Anthropic(api_key=api_key)
                else:
                    token_file = os.getenv("CLAUDE_SESSION_INGRESS_TOKEN_FILE", "")
                    if token_file and os.path.isfile(token_file):
                        with open(token_file) as _tf:
                            token = _tf.read().strip()
                        _client = anthropic.Anthropic(auth_token=token)
                    else:
                        _client = anthropic.Anthropic()
    return _client

MODEL_FAST  = "claude-haiku-4-5-20251001"  # per-ticker agents
MODEL_SMART = "claude-sonnet-4-6"          # portfolio-level agents

MAX_CANDIDATES = 20  # max tickers to run Research/Earnings/Devil for


# ── System Prompts ────────────────────────────────────────────────────────────

_REGIME_SYSTEM = """\
You are the Chief Macro Strategist.

Your sole responsibility is identifying the current market regime.

You do not recommend stocks.
You do not allocate capital.

Determine:
- Risk-On vs Risk-Off
- Growth vs Value leadership
- Liquidity conditions
- Volatility environment
- Economic regime

Output only structured JSON. Avoid making stock-specific recommendations.
Your job is to provide portfolio context."""

_RESEARCH_SYSTEM = """\
You are a Senior Buy-Side Equity Research Analyst.

Your job is to find opportunities.

Focus on:
- Variant perception
- Expectation gaps
- Catalysts
- Earnings revisions
- Industry leadership
- Competitive advantages

For every stock answer:
1. What does the market believe?
2. Why might the market be wrong?
3. What catalyst could force repricing?
4. What is the strongest evidence supporting the thesis?

Provide: Thesis, Consensus View, Variant View, Catalysts, Confidence, Key Risks.

Do not discuss position sizing.
Do not discuss portfolio construction."""

_EARNINGS_SYSTEM = """\
You are an Earnings and Catalyst Specialist.

Focus only on events likely to occur in the next 90 days.

Evaluate:
- Earnings reports
- Guidance changes
- Product launches
- Regulatory events
- M&A activity
- Capital allocation decisions

Determine:
- Expected impact
- Probability of occurrence
- Market awareness

Rank catalysts by: Magnitude, Timing, Visibility, Consensus awareness.

Ignore long-term narratives.
Focus only on events likely to move the stock within the investment horizon."""

_DEVILS_SYSTEM = """\
You are a hostile investment committee member.

Your goal is to kill investment ideas.

Assume the market is correct until proven otherwise.

Challenge:
- Revenue assumptions
- Margin assumptions
- Valuation assumptions
- Competitive advantages
- Management quality
- Catalyst likelihood

Answer:
- Why is this investment likely to fail?
- What is the strongest bear case?
- What assumptions appear weakest?
- What evidence contradicts the thesis?
- What would cause a permanent loss of capital?

Do not generate a bull case.
Only identify weaknesses."""

_POSITION_REVIEW_SYSTEM = """\
You are responsible for reviewing existing positions.

You do not evaluate new ideas.

For each holding determine:
- Is the thesis still valid?
- Is alpha still available?
- Has risk increased?
- Have catalysts already played out?
- Is there a superior opportunity?

Provide:
- Hold Score (1-10)
- Remaining Alpha: High / Medium / Low
- Recommended Action: Hold / Reduce / Exit

Never anchor to purchase price."""

_PM_SYSTEM = """\
You are the Portfolio Manager.

You receive:
- Market Regime Analysis
- Research Analyst Output
- Earnings & Catalyst Analysis
- Devil's Advocate Review
- Position Reviews
- Quant Scores
- Current Portfolio

Your responsibility is capital allocation.
You are not a stock picker.
Every dollar must compete for capital.

Before every buy identify:
- Source of capital
- Position being displaced
- Expected portfolio improvement

Prioritize:
1. Expected Alpha
2. Risk-adjusted return
3. Portfolio diversification
4. Opportunity cost

SELL decisions are independent of BUY decisions.
When a holding shows recommended_action=REDUCE or EXIT in the position review
AND Devil's Advocate has recommend_reject=True for that same holding, you MUST
propose a SELL (target_weight=0.0) even if you have no new BUYs to make.
Freeing capital from a deteriorating position is a valid primary action.
Do not let lack of attractive BUY candidates prevent you from exiting a bad position.

Default action is HOLD.
Trade only when portfolio expected value increases."""

_CRO_SYSTEM = """\
You are the Chief Risk Officer.

You are independent from the Portfolio Manager.
Your objective is portfolio survival.

Evaluate:
- Concentration risk
- Factor exposure
- Correlation risk
- Drawdown risk
- Sector exposure
- Theme exposure

You may reject any trade.

Assume unexpected events occur regularly.

Focus on: Avoiding catastrophic losses."""


# ── Helpers ───────────────────────────────────────────────────────────────────

# ── A12 reproducibility: per-run model/sampling/usage manifest ────────────────
# The released code lets a reader inspect the pipeline, but exact replay of a
# historical decision needs the RESOLVED model snapshot (not just the family
# alias), the sampling params, and the prompts. We capture all three per run.
# Sampling note: temperature / top_p are NOT set on the call, so they use the
# Anthropic API default (temperature 1.0). We RECORD that rather than pin it —
# pinning would change behavior and must be a deliberate, separate change.
SAMPLING_PARAMS = {"temperature": "api_default(1.0)", "top_p": "api_default"}

_RUN_MANIFEST: dict = {"calls": {}}


def _record_call(model: str, max_tokens: int, response) -> None:
    """Accumulate resolved model + token usage per requested model. Best-effort —
    instrumentation must never affect the call result or raise into the pipeline."""
    try:
        c = _RUN_MANIFEST["calls"].setdefault(
            model, {"n_calls": 0, "resolved_models": [], "max_tokens_seen": [],
                    "input_tokens": 0, "output_tokens": 0,
                    "cache_read_tokens": 0, "cache_creation_tokens": 0})
        c["n_calls"] += 1
        resolved = getattr(response, "model", None)
        if resolved and resolved not in c["resolved_models"]:
            c["resolved_models"].append(resolved)
        if max_tokens not in c["max_tokens_seen"]:
            c["max_tokens_seen"].append(max_tokens)
        u = getattr(response, "usage", None)
        if u is not None:
            c["input_tokens"]          += getattr(u, "input_tokens", 0) or 0
            c["output_tokens"]         += getattr(u, "output_tokens", 0) or 0
            c["cache_read_tokens"]     += getattr(u, "cache_read_input_tokens", 0) or 0
            c["cache_creation_tokens"] += getattr(u, "cache_creation_input_tokens", 0) or 0
    except Exception:
        pass


def _call(model: str, system: str | list, user_msg: str,
          max_tokens: int = 600) -> tuple[str, str | None]:
    """Return (text, stop_reason). stop_reason == "max_tokens" means the output
    was truncated at the cap — the caller uses this to skip pointless retries."""
    response = _get_client().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    _record_call(model, max_tokens, response)
    return response.content[0].text.strip(), response.stop_reason


def _cached_system(prompt: str) -> list:
    """Wrap a system prompt with prompt caching — efficient when called many times."""
    return [{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}]


def _parse_json(text: str, default):
    text = text.strip()
    # Strip markdown code fences wherever they appear
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    text = text.strip()

    def _try(s):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return None

    result = _try(text)

    if result is None:
        # Model wrapped JSON in prose — extract the first {...} or [...] block
        match = re.search(r'(\{[\s\S]*\}|\[[\s\S]*\])', text)
        if match:
            result = _try(match.group(1))

    # Truncation recovery: response hit max_tokens mid-JSON so closing braces are missing.
    # Count unmatched braces and append enough "}" to close the object, then retry.
    if result is None and '{' in text:
        # First, try to preserve a value that was cut open mid-string: close the
        # dangling string and any open arrays/objects. This keeps a partial (but
        # real) first field — e.g. a long "bear_case" — instead of discarding it
        # and collapsing to the default.
        candidate = re.sub(r',\s*$', '', text.rstrip())
        if len(re.findall(r'(?<!\\)"', candidate)) % 2 == 1:
            candidate += '"'
        open_braces = candidate.count('{') - candidate.count('}')
        open_arrays = candidate.count('[') - candidate.count(']')
        suffix = ']' * max(open_arrays, 0) + '}' * max(open_braces, 0)
        result = _try(candidate + suffix)

        # Last resort: drop the trailing partial token entirely, then close.
        if result is None:
            truncated = re.sub(r',?\s*"[^"]*$', '', text.rstrip())
            truncated = re.sub(r',\s*$', '', truncated)
            open_braces = truncated.count('{') - truncated.count('}')
            open_arrays = truncated.count('[') - truncated.count(']')
            suffix = ']' * max(open_arrays, 0) + '}' * max(open_braces, 0)
            if suffix:
                result = _try(truncated + suffix)

    if result is None:
        return default

    # Unwrap list → dict when a dict is expected (model occasionally wraps in array)
    if isinstance(result, list) and isinstance(default, dict):
        for item in result:
            if isinstance(item, dict):
                return item
        return default
    return result


_PARSE_FAILED = object()  # sentinel: _parse_json returned its default (no real parse)


def _safe_call(model, system, user_msg, default, max_tokens=600, retries=2, return_meta=False):
    """Call an agent and parse its JSON. When return_meta=True, also return a meta
    dict {raw, stop_reason, parsed_ok} so the caller can distinguish a model that
    GENUINELY produced the default value (e.g. the PM legitimately returning [] —
    no trades) from one whose output FAILED to parse and silently collapsed to the
    default. Both look identical in the return value alone, which made a mangled PM
    response indistinguishable from a deliberate no-trade day (a silent alpha hole)."""
    last_raw, last_stop, last_parsed_ok = "", None, False
    for attempt in range(retries + 1):
        try:
            raw, stop_reason = _call(model, system, user_msg, max_tokens=max_tokens)
            # Parse against a unique sentinel so we can tell a real parse of the
            # default value apart from a parse failure that fell back to it.
            parsed = _parse_json(raw, _PARSE_FAILED)
            parsed_ok = parsed is not _PARSE_FAILED
            result = parsed if parsed_ok else default
            last_raw, last_stop, last_parsed_ok = raw, stop_reason, parsed_ok
            # If result is identical to the default, the response was likely empty/truncated
            # under API load — treat as retryable rather than silently accepting blank fields.
            # Exception: a max_tokens truncation is deterministic — retrying the same prompt
            # at the same cap reproduces the same over-long output, so accept the best-effort
            # parse instead of burning identical calls.
            # Skip retry when return_meta=True and the parse succeeded: the model
            # genuinely returned the default value (e.g. PM proposed 0 trades) —
            # retrying would burn 2 identical calls for no reason.
            if (result == default and stop_reason != "max_tokens" and attempt < retries
                    and not (return_meta and parsed_ok)):
                raise ValueError(f"Response parsed to default (raw_len={len(raw)}) — retrying")
            if return_meta:
                return result, {"raw": raw[:4000], "stop_reason": stop_reason, "parsed_ok": parsed_ok}
            return result
        except Exception as e:
            if attempt < retries:
                err = str(e)
                # 529 = Anthropic server overloaded; use longer backoff than generic errors
                delay = 30 * (attempt + 1) if ("529" in err or "overloaded" in err.lower()) else 2 ** attempt
                print(f"   ⚠ Agent call failed (attempt {attempt + 1}/{retries + 1}), retrying in {delay}s: {e}")
                time.sleep(delay)
            else:
                print(f"   ⚠ Agent call failed: {e}")
                if return_meta:
                    return default, {"raw": last_raw[:4000], "stop_reason": last_stop,
                                     "parsed_ok": last_parsed_ok}
                return default


def _fmt_scores(scores: dict) -> str:
    # data_available=False is set by score_all_tickers when history is empty (cloud mode).
    # Default True so old cached scores without the flag are treated as having data.
    if not scores.get("data_available", True):
        return "QUANT DATA UNAVAILABLE (no historical prices — scores are not real)"
    # Show factors with no real data as N/A rather than a misleading neutral 50.
    # quality/valuation default to 50 when fundamentals are absent (the live
    # case on free-tier Polygon) — labelling that "50" would imply a real read.
    quality = scores.get('quality_score', '?') if scores.get('quality_available', True) else "N/A"
    val     = scores.get('valuation_score', '?') if scores.get('valuation_available', True) else "N/A"
    return (
        f"composite={scores.get('composite_score','?')} "
        f"mom={scores.get('momentum_score','?')} "
        f"quality={quality} "
        f"val={val} "
        f"vol={scores.get('volatility','?')}% "
        f"beta={scores.get('beta','?')} "
        f"1M={scores.get('return_1m','?')}% "
        f"3M={scores.get('return_3m','?')}% "
        f"above50={scores.get('above_50dma','?')} "
        f"above200={scores.get('above_200dma','?')}"
    )


def _fmt_news(articles: list[dict], limit: int = 15) -> str:
    """Format a list of news articles for an agent prompt, including descriptions."""
    if not articles:
        return "  - No recent headlines"
    lines = []
    for a in articles[:limit]:
        date = (a.get("published_utc") or "")[:10]
        desc = (a.get("description") or "").strip()
        line = f"  - [{date}] {a['title']}"
        if desc:
            line += f"\n    {desc[:200]}"
        lines.append(line)
    return "\n".join(lines)


def _fmt_ticker_history(history: list[dict] | None) -> str:
    """Render prior journal theses + realized outcomes for one ticker (Phase 2).

    Gives the thesis-builder a memory of how past calls on this name played out,
    so a re-entry sees its prior result instead of starting blind.
    """
    if not history:
        return ""
    lines = ["PRIOR HISTORY FOR THIS TICKER (your past theses and how they resolved):"]
    for e in history:
        ar = e.get("actual_return")
        if ar is None:
            outcome = f"{e.get('status', 'open')} — no realized outcome yet"
        else:
            outcome = f"{ar:+.1%} realized | thesis_correct={e.get('thesis_correct')}"
        lines.append(f"  {e.get('date', '?')} {e.get('action', '?')} — "
                     f"thesis: {(e.get('thesis') or '')[:120]}")
        lines.append(f"    outcome: {outcome}")
    return "\n".join(lines)


def _fmt_recently_exited(recently_exited: dict | None) -> str:
    """Render a re-entry warning block for the PM (Phase 2).

    Directly addresses the churn blind spot: a name sold days ago should not be
    silently rebought. The model must justify any reversal in its rationale.
    """
    if not recently_exited:
        return ""
    lines = ["RECENTLY EXITED — justify any re-entry. Do NOT re-buy a name below "
             "unless its original exit reason is now resolved; if you propose a "
             "re-buy, say why in the rationale:"]
    for t, e in recently_exited.items():
        last = (e.get("exits") or [{}])[-1]
        rr = last.get("realized_return")
        rr_s = f"{rr:+.1%}" if isinstance(rr, (int, float)) else "?"
        lines.append(f"  {t}: exited {last.get('date', '?')} at {rr_s} — "
                     f"original thesis: {(e.get('thesis') or '')[:100]}")
    return "\n".join(lines)


def _ticker_news(ticker: str, market_data: dict, limit: int = 5) -> str:
    """Return formatted news for a ticker: per-ticker feed first, global feed as fallback."""
    specific = market_data.get("ticker_news", {}).get(ticker, [])
    if specific:
        return _fmt_news(specific, limit=limit)
    filtered = [a for a in market_data.get("news", []) if ticker in a.get("tickers", [])]
    return _fmt_news(filtered, limit=limit)


# ── Candidate Selection ───────────────────────────────────────────────────────

def _select_candidates(portfolio: dict, market_data: dict, quant_scores: dict) -> list[str]:
    """Choose which tickers to run the Research / Earnings / Devil agents for."""
    candidates: set[str] = set()

    # Current holdings always included
    for p in portfolio["positions"]:
        candidates.add(p["symbol"])

    # Top by composite quant score (exclude pure benchmarks)
    sorted_q = sorted(
        quant_scores.items(),
        key=lambda x: x[1].get("composite_score", 0),
        reverse=True,
    )
    for ticker, _ in sorted_q:
        if ticker not in ("SPY", "QQQ") and len(candidates) < MAX_CANDIDATES:
            candidates.add(ticker)

    # News-mentioned tickers that have price data
    for article in market_data.get("news", []):
        for t in article.get("tickers", []):
            if t in market_data["prices"] and len(candidates) < MAX_CANDIDATES:
                candidates.add(t)

    # News-discovered tickers
    for t in market_data.get("news_discovered", {}):
        if len(candidates) < MAX_CANDIDATES:
            candidates.add(t)

    return list(candidates)


# ── Agent 1: Market Regime Strategist ────────────────────────────────────────

def run_market_regime_strategist(market_data: dict, quant_scores: dict) -> dict:
    spy  = market_data["prices"].get("SPY", {})
    qqq  = market_data["prices"].get("QQQ", {})
    spy_s = quant_scores.get("SPY", {})
    qqq_s = quant_scores.get("QQQ", {})
    headlines = _fmt_news(market_data.get("news", []), limit=15)

    user_msg = f"""\
Date: {market_data['date']}

SPY: ${spy.get('close', 'N/A')} ({spy.get('change_pct', 0):+.2f}%)
  1M={spy_s.get('return_1m','?')}%  3M={spy_s.get('return_3m','?')}%  6M={spy_s.get('return_6m','?')}%
  Above 50DMA: {spy_s.get('above_50dma')}  Above 200DMA: {spy_s.get('above_200dma')}

QQQ: ${qqq.get('close', 'N/A')} ({qqq.get('change_pct', 0):+.2f}%)
  1M={qqq_s.get('return_1m','?')}%  3M={qqq_s.get('return_3m','?')}%

RECENT HEADLINES:
{headlines}

Output JSON:
{{
  "regime": "RISK_ON | NEUTRAL | RISK_OFF",
  "confidence": 0-100,
  "growth_value": "GROWTH | VALUE | NEUTRAL",
  "favored_factors": [],
  "avoid_factors": [],
  "key_observations": []
}}"""

    return _safe_call(
        MODEL_SMART, _REGIME_SYSTEM, user_msg,
        default={"regime": "NEUTRAL", "confidence": 50, "growth_value": "NEUTRAL",
                 "favored_factors": [], "avoid_factors": [], "key_observations": []},
        max_tokens=1540,
        retries=2,
    )


# ── Agent 2: Research Analyst ─────────────────────────────────────────────────

def run_research_analyst(ticker: str, market_data: dict, quant_scores: dict,
                         ticker_history: list[dict] | None = None) -> dict:
    data   = market_data["prices"].get(ticker) or market_data.get("news_discovered", {}).get(ticker, {})
    scores = quant_scores.get(ticker, {})
    news = _ticker_news(ticker, market_data)
    history_block = _fmt_ticker_history(ticker_history)

    user_msg = f"""\
TICKER: {ticker}
Price: ${data.get('close', 'N/A')} ({data.get('change_pct', 0):+.2f}%)

QUANT: {_fmt_scores(scores)}

NEWS:
{news}
{(chr(10) + history_block) if history_block else ''}

Draw on your training knowledge of {ticker}'s business model, competitive position, \
and sector dynamics. All fields below must be non-empty — do not return blank strings.

Output JSON (fill in every field):
{{
  "thesis": "bull thesis in one sentence",
  "consensus_view": "what the market currently believes",
  "variant_view": "where you disagree with consensus",
  "catalysts": ["catalyst 1", "catalyst 2"],
  "key_risks": ["risk 1", "risk 2"],
  "invalidates_if": ["condition that would break the thesis"],
  "confidence": 6,
  "evidence_quality": 5
}}"""

    return _safe_call(
        MODEL_FAST, _cached_system(_RESEARCH_SYSTEM), user_msg,
        default={"thesis": "", "catalysts": [], "confidence": 5,
                 "key_risks": [], "invalidates_if": [], "variant_view": ""},
        max_tokens=2200,
    )


# ── Agent 3: Earnings & Catalyst Analyst ─────────────────────────────────────

def _within_90d(date_str: str | None, today: str) -> bool:
    """True if date_str (YYYY-MM-DD) is in [today, today+90]. None/unparseable → False."""
    if not date_str:
        return False
    try:
        from datetime import date as _date
        d = _date.fromisoformat(str(date_str)[:10])
        t = _date.fromisoformat(str(today)[:10])
    except (ValueError, TypeError):
        return False
    return 0 <= (d - t).days <= 90


def run_earnings_catalyst_analyst(ticker: str, market_data: dict) -> dict:
    data = market_data["prices"].get(ticker, {})
    news = _ticker_news(ticker, market_data)

    # Verified earnings calendar (#1). When a real calendar is available, Phase 3.2
    # gates the LLM call: skip names with no event in the next 90 days (don't spend
    # tokens to emit a constant, and don't feed all-default noise to the PM). With
    # NO calendar (free-tier / no key) we can't know the date, so we run as before
    # — no regression.
    calendar      = market_data.get("earnings_calendar") or {}
    has_calendar  = bool(calendar)
    verified_date = calendar.get(ticker)

    if has_calendar and not _within_90d(verified_date, market_data.get("date", "")):
        return {"earnings_alpha_score": None, "skipped_no_catalyst": True,
                "next_earnings_est": verified_date or "none",
                "beat_probability": "N/A", "guidance_cut_probability": "N/A",
                "key_catalysts_90d": []}

    verified_line = (f"VERIFIED next earnings date (from calendar): {verified_date} — "
                     f"use this EXACT date; do not invent another.\n" if verified_date else "")

    user_msg = f"""\
TICKER: {ticker}
Price: ${data.get('close', 'N/A')}  Date: {market_data['date']}
{verified_line}
NEWS:
{news}

Use your training knowledge of {ticker}'s business cycle. \
All fields must be filled in — do not return empty arrays or placeholder values.

Output JSON (fill in every field):
{{
  "next_earnings_est": "YYYY-MM-DD or unknown",
  "earnings_alpha_score": 6,
  "beat_probability": "MEDIUM",
  "guidance_raise_probability": "LOW",
  "guidance_cut_probability": "LOW",
  "key_catalysts_90d": ["catalyst 1", "catalyst 2"],
  "expected_move_pct": 4
}}"""

    result = _safe_call(
        MODEL_FAST, _cached_system(_EARNINGS_SYSTEM), user_msg,
        default={"earnings_alpha_score": 5, "beat_probability": "MEDIUM",
                 "guidance_cut_probability": "LOW", "key_catalysts_90d": []},
        max_tokens=1320,
    )

    # Fabrication guard: the verified calendar date wins over the model's guess.
    if verified_date and result.get("next_earnings_est") != verified_date:
        result["next_earnings_est_model"] = result.get("next_earnings_est")
        result["next_earnings_est"]       = verified_date
        result["earnings_date_corrected"] = True
    return result


# ── Agent 4: Devil's Advocate ─────────────────────────────────────────────────

def run_devils_advocate(
    ticker: str,
    research: dict,
    earnings: dict,
    market_data: dict,
    quant_scores: dict,
) -> dict:
    data   = market_data["prices"].get(ticker, {})
    scores = quant_scores.get(ticker, {})

    bull_thesis = research.get('thesis', '') or f"{ticker} — no explicit thesis provided"

    user_msg = f"""\
TICKER: {ticker}
Price: ${data.get('close', 'N/A')} | Vol: {scores.get('volatility', '?')}% | Beta: {scores.get('beta', '?')}

BULL THESIS TO DESTROY:
  {bull_thesis}

VARIANT VIEW:
  {research.get('variant_view', '') or 'Not provided'}

CATALYSTS CLAIMED:
  {json.dumps(research.get('catalysts', []))}

KEY RISKS IDENTIFIED BY ANALYST:
  {json.dumps(research.get('key_risks', []))}

EARNINGS ASSESSMENT:
  Beat prob: {earnings.get('beat_probability', '?')} | Guidance cut: {earnings.get('guidance_cut_probability', '?')}
  Upcoming catalysts: {json.dumps(earnings.get('key_catalysts_90d', []))}

Use your training knowledge of {ticker} to construct a rigorous bear case. \
All fields must be non-empty — do not return blank strings or empty arrays.

REJECTION CALIBRATION: recommend_reject=true is expected on roughly 20-30% of \
evaluations — not every idea is fatally flawed, but not every idea is sound. \
Set recommend_reject=true when overall_risk_score >= 7 AND at least one of: \
(a) the central bull assumption is empirically false or unverifiable, \
(b) downside scenarios include permanent capital loss (>40% drawdown), \
(c) the valuation already prices in the bull case leaving no margin of safety. \
Do NOT default to false — force an honest verdict.

Output JSON. overall_risk_score and recommend_reject come FIRST so they are \
captured even if the response is long. Keep "bear_case" to 2-3 tight sentences \
and each list item to one short phrase:
{{
  "overall_risk_score": <integer 1-10; 7+ means high risk>,
  "recommend_reject": <true if overall_risk_score >= 7 AND a fatal flaw exists, else false>,
  "bear_case": "the single strongest argument against owning this stock in 2-3 sentences",
  "weakest_assumptions": ["assumption 1", "assumption 2"],
  "hidden_risks": ["risk 1"],
  "crowding_risk": "LOW | MEDIUM | HIGH",
  "valuation_risk": "LOW | MEDIUM | HIGH",
  "catalyst_failure_probability": "LOW | MEDIUM | HIGH"
}}"""

    return _safe_call(
        MODEL_SMART, _cached_system(_DEVILS_SYSTEM), user_msg,
        default={"bear_case": "", "overall_risk_score": 5,
                 "recommend_reject": False, "hidden_risks": []},
        # Sonnet for genuine adversarial depth; overall_risk_score + recommend_reject
        # are first so any truncation still captures the verdict.
        max_tokens=8250,
    )


# ── Agent 5: Position Review Analyst ─────────────────────────────────────────

def run_position_review_analyst(
    holding: dict,
    market_data: dict,
    quant_scores: dict,
    research: dict | None,
    prior_journal_entry: dict | None = None,
) -> dict:
    ticker = holding["symbol"]
    scores = quant_scores.get(ticker, {})
    data   = market_data["prices"].get(ticker, {})
    pnl    = holding.get("unrealized_pnl", 0)
    pnl_pct = (pnl / (holding["avg_price"] * holding["qty"])) * 100 if holding["avg_price"] and holding["qty"] else 0

    thesis_block = ""
    if research:
        thesis_block = f"\nCURRENT THESIS:\n  {research.get('thesis', 'None available')}"
        thesis_block += f"\n  Confidence: {research.get('confidence', '?')}/10"

    prior_block = ""
    if prior_journal_entry:
        invalidates = prior_journal_entry.get("invalidates_if") or []
        prior_block = (
            f"\nORIGINAL ENTRY THESIS (entered {prior_journal_entry.get('date', '?')}):\n"
            f"  {prior_journal_entry.get('thesis', 'N/A')}\n"
            f"  Anti-thesis: {prior_journal_entry.get('anti_thesis', 'N/A')}\n"
            f"  Invalidates if:\n"
            + "\n".join(f"    - {c}" for c in invalidates)
        )

    user_msg = f"""\
POSITION: {ticker}
  Shares: {holding['qty']} @ avg ${holding['avg_price']:.2f} | Current: ${data.get('close', '?')} | P&L: {pnl_pct:+.1f}%

QUANT: {_fmt_scores(scores)}
{thesis_block}
{prior_block}

NEWS:
{_ticker_news(ticker, market_data, limit=3)}

Output JSON:
{{
  "hold_score": 1-10,
  "remaining_alpha": "HIGH | MEDIUM | LOW",
  "thesis_intact": true | false,
  "catalysts_played_out": true | false,
  "recommended_action": "HOLD | REDUCE | EXIT",
  "reasoning": ""
}}"""

    return _safe_call(
        MODEL_FAST, _cached_system(_POSITION_REVIEW_SYSTEM), user_msg,
        default={"hold_score": 6, "remaining_alpha": "MEDIUM",
                 "thesis_intact": True, "recommended_action": "HOLD", "reasoning": ""},
        max_tokens=880,
    )


# ── Agent 6: Portfolio Manager ────────────────────────────────────────────────

def run_portfolio_manager(
    regime: dict,
    research_map: dict,
    earnings_map: dict,
    devil_map: dict,
    position_reviews: dict,
    quant_scores: dict,
    portfolio: dict,
    trade_history: list | None,
    date: str = "",
    recently_exited: dict | None = None,
) -> tuple[list[dict], dict]:
    total = portfolio["total_value"]
    cash  = portfolio["cash"]
    cash_pct = (cash / total * 100) if total else 0

    # Format current holdings
    holdings_lines = []
    for p in portfolio["positions"]:
        t = p["symbol"]
        weight = (p["market_value"] / total * 100) if total else 0
        review = position_reviews.get(t, {})
        holdings_lines.append(
            f"  {t}: {p['qty']} sh @ ${p['avg_price']:.2f} = ${p['market_value']:,.0f} ({weight:.1f}%) "
            f"| hold={review.get('hold_score','?')}/10 alpha={review.get('remaining_alpha','?')} "
            f"action={review.get('recommended_action','?')}"
        )

    # Format quant scores table (top 25 by composite, exclude ETFs)
    sorted_q = sorted(
        [(t, s) for t, s in quant_scores.items() if t not in ("SPY", "QQQ")],
        key=lambda x: x[1].get("composite_score", 0),
        reverse=True,
    )[:25]
    quant_lines = [
        f"  {t}: cmp={s.get('composite_score','?')} mom={s.get('momentum_score','?')} "
        f"q={s.get('quality_score','?')} val={s.get('valuation_score','?')} "
        f"vol={s.get('volatility','?')}% beta={s.get('beta','?')}"
        for t, s in sorted_q
    ]

    # Format research summaries
    research_lines = []
    for t, r in research_map.items():
        d = devil_map.get(t, {})
        e = earnings_map.get(t, {})
        research_lines.append(
            f"\n  {t} (conf={r.get('confidence','?')}/10 | devil_risk={d.get('overall_risk_score','?')}/10 "
            f"| reject={d.get('recommend_reject','?')} | earnings_alpha={e.get('earnings_alpha_score','?')}/10):"
            f"\n    Thesis: {r.get('thesis','')}"
            f"\n    Variant: {r.get('variant_view','')}"
            f"\n    Catalysts: {r.get('catalysts', [])}"
            f"\n    Key risks: {r.get('key_risks', [])}"
            f"\n    Bear case: {d.get('bear_case', '')}"
            f"\n    90d catalysts: {e.get('key_catalysts_90d', [])}"
        )

    history_lines = []
    for t in (trade_history or [])[-10:]:
        history_lines.append(f"  {t.get('date')} | {t.get('action')} {t.get('ticker')} | {t.get('rationale','')[:80]}")

    reentry_block = _fmt_recently_exited(recently_exited)

    user_msg = f"""\
Date: {date}

MARKET REGIME: {json.dumps(regime)}
{(reentry_block + chr(10)) if reentry_block else ''}

CURRENT PORTFOLIO:
  Cash: ${cash:,.2f} ({cash_pct:.1f}%)  Total: ${total:,.2f}
  Holdings:
{chr(10).join(holdings_lines) or '  (none — all cash)'}

QUANT SCORES (top candidates):
{chr(10).join(quant_lines)}

RESEARCH & DEVIL'S ADVOCATE:
{''.join(research_lines)}

RECENT TRADES:
{chr(10).join(history_lines) or '  (none)'}

CONSTRAINTS:
  Holdings target: 8–15 | Max position: 10% | Max sector: 25% | Cash: 0–10%
  Hard-blocked (NEVER propose): TSLA

OUTPUT FORMAT — CRITICAL FOR PARSING:
Return ONLY a JSON array — no prose, no reasoning, no markdown, nothing before or
after it. Your response MUST start with [ and end with ]. Omit HOLD decisions.
Return [] if no trade improves portfolio expected value.

Keep every field compact — a long response gets truncated and discarded, so a
verbose rationale loses the whole trade list. Each element:
{{
  "ticker": "TICKER",
  "action": "BUY" or "SELL",
  "target_weight": 0.00,
  "source_of_capital": "cash" or "TICKER_being_reduced",
  "expected_return": 0.05,
  "rationale": "max 10 words"
}}
expected_return = honest, conservative GROSS return estimate over the 1–3 month
horizon as a decimal (0.05 = +5%); a downstream gate rejects BUYs not worth it
after ~54% CA short-term tax + trading cost."""

    return _safe_call(
        MODEL_SMART, _PM_SYSTEM, user_msg,
        default=[],
        max_tokens=2640,
        retries=2,
        return_meta=True,
    )


# ── Agent 7: Chief Risk Officer ───────────────────────────────────────────────

def _correlation_block(projected: dict, history: dict | None) -> str:
    """Build a real correlation + concentration section for the CRO from price
    history. Returns '' when no matrix can be computed (no pretense)."""
    if not history:
        return ""
    held = [t for t, w in projected.items() if w > 0.001]
    from quant_engine import compute_return_correlations
    pairs = compute_return_correlations(history, held)

    lines = []
    if pairs:
        lines.append("HIGHEST PAIRWISE CORRELATIONS (post-trade holdings, ~120d daily returns):")
        lines += [f"  {a} / {b}: {c:+.2f}" for a, b, c in pairs]

    # Sector concentration from the static guardrails map (lazy import to avoid
    # an import-time dependency on the execution stack).
    try:
        from guardrails import sector_of
        sector_w: dict[str, float] = {}
        for t, w in projected.items():
            if w > 0.001:
                sector_w[sector_of(t)] = sector_w.get(sector_of(t), 0.0) + w
        if sector_w:
            top_sec, top_w = max(sector_w.items(), key=lambda x: x[1])
            lines.append(f"CONCENTRATION: top sector = {top_sec} {top_w:.0%}")
    except Exception:
        pass

    return "\n".join(lines)


def run_chief_risk_officer(
    decisions: list[dict],
    portfolio: dict,
    quant_scores: dict,
    history: dict | None = None,
) -> dict:
    if not decisions:
        return {"approved": True, "risk_budget_used": 0, "largest_risk": "none",
                "rejected_tickers": [], "reasoning": "No trades to evaluate."}

    total = portfolio["total_value"]

    # Project resulting portfolio after proposed trades
    projected: dict[str, float] = {}
    for p in portfolio["positions"]:
        weight = (p["market_value"] / total) if total else 0
        projected[p["symbol"]] = weight
    for d in decisions:
        if d.get("action") == "BUY":
            projected[d["ticker"]] = d.get("target_weight", 0)
        elif d.get("action") == "SELL":
            projected[d["ticker"]] = d.get("target_weight", 0)

    # Risk lines per ticker
    risk_lines = []
    for ticker, weight in sorted(projected.items(), key=lambda x: -x[1]):
        if weight > 0.001:
            s = quant_scores.get(ticker, {})
            risk_lines.append(
                f"  {ticker}: {weight:.1%} | vol={s.get('volatility','?')}% "
                f"beta={s.get('beta','?')}"
            )

    corr_block = _correlation_block(projected, history)

    user_msg = f"""\
PROPOSED TRADES:
{json.dumps(decisions, indent=2)}

PROJECTED PORTFOLIO (post-trade weights):
{chr(10).join(risk_lines)}
{(chr(10) + corr_block) if corr_block else ''}

CURRENT CASH: ${portfolio['cash']:,.2f} ({portfolio['cash']/total*100:.1f}%)

Output JSON:
{{
  "approved": true | false,
  "risk_budget_used": 0-100,
  "largest_risk": "one sentence describing the biggest risk",
  "rejected_tickers": [],
  "reasoning": "brief explanation"
}}
Set approved=false only for severe concentration / correlation risks that could cause catastrophic loss."""

    return _safe_call(
        MODEL_SMART, _CRO_SYSTEM, user_msg,
        default={"approved": False, "risk_budget_used": 0,
                 "largest_risk": "CRO unavailable (API error)",
                 "rejected_tickers": [],
                 "reasoning": "CRO call failed — trades blocked until CRO can run.",
                 "api_failed": True},
        max_tokens=880,
        retries=2,
    )


# ── Main Orchestration ────────────────────────────────────────────────────────

def get_trade_decisions(
    portfolio: dict,
    market_data: dict,
    quant_scores: dict,
    trade_history: list | None = None,
    prior_journal: dict | None = None,
    ticker_history: dict | None = None,
    recently_exited: dict | None = None,
) -> tuple[list[dict], dict]:
    """
    Run the full 7-agent pipeline.

    ticker_history: {ticker: [journal entries]} fed to the Research Analyst so a
        new/re-entered thesis sees how prior calls on that name resolved.
    recently_exited: {ticker: closed entry} fed to the Portfolio Manager as a
        re-entry warning (churn guard).
    Returns (decisions, pipeline_state) where pipeline_state is the full paper trail
    of every agent's output for this run.
    """
    market_data_date = market_data.get("date", "")

    # ── 1. Market Regime ──────────────────────────────────────────────────────
    print("   [1/7] Market Regime Strategist...")
    regime = run_market_regime_strategist(market_data, quant_scores)
    print(f"         Regime: {regime.get('regime')} (confidence: {regime.get('confidence')})")

    # ── Select candidates ─────────────────────────────────────────────────────
    candidates = _select_candidates(portfolio, market_data, quant_scores)
    print(f"   Candidates for analysis ({len(candidates)}): {', '.join(candidates)}")

    _WORKERS = min(5, max(1, len(candidates)))

    # ── 2 & 3: Research and Earnings are fully independent — run both in parallel ──
    print(f"   [2/7] Research Analyst + [3/7] Earnings Analyst ({len(candidates)} tickers, parallel)...")
    research_map: dict = {}
    earnings_map: dict = {}
    with ThreadPoolExecutor(max_workers=_WORKERS * 2) as _pool:
        r_futs = {_pool.submit(run_research_analyst, t, market_data, quant_scores,
                               (ticker_history or {}).get(t)): t for t in candidates}
        e_futs = {_pool.submit(run_earnings_catalyst_analyst, t, market_data): t for t in candidates}
        for fut in as_completed(list(r_futs) + list(e_futs)):
            if fut in r_futs:
                research_map[r_futs[fut]] = fut.result()
            else:
                earnings_map[e_futs[fut]] = fut.result()

    # ── 4. Devil's Advocate — depends on 2 & 3, but parallel across tickers ──
    print(f"   [4/7] Devil's Advocate ({len(candidates)} tickers, parallel)...")
    devil_map: dict = {}
    with ThreadPoolExecutor(max_workers=_WORKERS) as _pool:
        d_futs = {
            _pool.submit(run_devils_advocate, t, research_map[t], earnings_map[t], market_data, quant_scores): t
            for t in candidates
        }
        for fut in as_completed(d_futs):
            devil_map[d_futs[fut]] = fut.result()

    # ── 5. Position Review — parallel across holdings ─────────────────────────
    holdings = portfolio.get("positions", [])
    print(f"   [5/7] Position Review Analyst ({len(holdings)} holdings, parallel)...")
    position_reviews: dict = {}
    if holdings:
        _h_workers = min(5, len(holdings))
        with ThreadPoolExecutor(max_workers=_h_workers) as _pool:
            h_futs = {
                _pool.submit(
                    run_position_review_analyst,
                    h, market_data, quant_scores,
                    research_map.get(h["symbol"]),
                    (prior_journal or {}).get(h["symbol"]),
                ): h["symbol"]
                for h in holdings
            }
            for fut in as_completed(h_futs):
                ticker = h_futs[fut]
                review = fut.result()
                position_reviews[ticker] = review
                print(
                    f"         {ticker}: hold={review.get('hold_score')}/10 "
                    f"alpha={review.get('remaining_alpha')} → {review.get('recommended_action')}"
                )

    # ── 6. Portfolio Manager ──────────────────────────────────────────────────
    print("   [6/7] Portfolio Manager...")
    decisions, pm_meta = run_portfolio_manager(
        regime, research_map, earnings_map, devil_map,
        position_reviews, quant_scores, portfolio, trade_history,
        date=market_data_date,
        recently_exited=recently_exited,
    )
    # Surface whether an empty decision list is a GENUINE no-trade or a parse
    # failure (see _safe_call return_meta). Logged into agent_log + used by the
    # agent_6 health check so a mangled PM response can't masquerade as "hold".
    if not decisions and not pm_meta.get("parsed_ok"):
        print(f"   ⚠ PM returned no decisions due to a PARSE FAILURE "
              f"(stop_reason={pm_meta.get('stop_reason')}) — not a deliberate no-trade.")

    if decisions:
        for d in decisions:
            print(
                f"         → {d.get('action')} {d.get('ticker')} "
                f"target={d.get('target_weight', 0):.1%} | {d.get('rationale', '')}"
            )
    else:
        print("         No trades proposed.")

    # ── 7. Chief Risk Officer ─────────────────────────────────────────────────
    print("   [7/7] Chief Risk Officer...")
    risk = run_chief_risk_officer(decisions, portfolio, quant_scores,
                                  history=market_data.get("history"))
    print(
        f"         {'✅ APPROVED' if risk.get('approved') else '🚨 REJECTED'} | "
        f"risk_budget={risk.get('risk_budget_used')}% | {risk.get('largest_risk')}"
    )

    decisions_proposed = list(decisions)  # PM's output before CRO filtering

    if not risk.get("approved", False):
        print(f"   🚨 CRO REJECTED all trades: {risk.get('reasoning')}")
        decisions = []

    rejected = set(risk.get("rejected_tickers", []))
    if rejected:
        before = len(decisions)
        decisions = [d for d in decisions if d.get("ticker") not in rejected]
        print(f"   ⚠ CRO removed {before - len(decisions)} ticker(s): {rejected}")

    pipeline_state = {
        "date": market_data_date,
        "regime": regime,
        "candidates": candidates,
        "quant_scores": {
            t: {k: v for k, v in s.items() if k != "history"}
            for t, s in quant_scores.items()
            if t in candidates
        },
        "research": research_map,
        "earnings": earnings_map,
        "devils_advocate": devil_map,
        "position_reviews": position_reviews,
        "portfolio_manager_proposed": decisions_proposed,
        "portfolio_manager_raw": pm_meta.get("raw", ""),
        "portfolio_manager_parsed_ok": pm_meta.get("parsed_ok", True),
        "cro": risk,
        "final_decisions": decisions,
    }
    return decisions, pipeline_state


# ── A12 reproducibility export ────────────────────────────────────────────────

def export_reproducibility(path: str = "reproducibility.json",
                           prompts_dir: str = "prompts",
                           run_id: str | None = None, date: str | None = None) -> dict:
    """Write a per-run reproducibility manifest: resolved model snapshots + token
    usage (from this run's calls), sampling params, the verbatim agent prompts,
    and their SHA-256 hashes. Lets another researcher reproduce a decision exactly
    (PAPER_DRAFT §6.11). Best-effort — wrap the caller; never break the pipeline.
    """
    import hashlib
    from datetime import datetime, timezone

    prompts = {
        "regime":          _REGIME_SYSTEM,
        "research":        _RESEARCH_SYSTEM,
        "earnings":        _EARNINGS_SYSTEM,
        "devils_advocate": _DEVILS_SYSTEM,
        "position_review": _POSITION_REVIEW_SYSTEM,
        "portfolio_manager": _PM_SYSTEM,
        "cro":             _CRO_SYSTEM,
    }
    prompt_meta = {}
    try:
        os.makedirs(prompts_dir, exist_ok=True)
    except Exception:
        prompts_dir = None
    for name, text in prompts.items():
        meta = {"sha256_16": hashlib.sha256(text.encode()).hexdigest()[:16],
                "chars": len(text)}
        if prompts_dir:
            try:
                with open(os.path.join(prompts_dir, f"{name}.txt"), "w") as f:
                    f.write(text)
                meta["file"] = f"{prompts_dir}/{name}.txt"
            except Exception:
                pass
        prompt_meta[name] = meta

    try:
        import anthropic
        lib_version = getattr(anthropic, "__version__", "unknown")
    except Exception:
        lib_version = "unknown"

    manifest = {
        "run_id":       run_id,
        "date":         date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "models":       {"fast": MODEL_FAST, "smart": MODEL_SMART},
        "sampling":     SAMPLING_PARAMS,
        "anthropic_sdk": lib_version,
        "calls":        _RUN_MANIFEST.get("calls", {}),  # resolved snapshots + usage
        "prompts":      prompt_meta,
        "note": ("Resolved model snapshots and token usage are this run's actual "
                 "API values. Sampling uses Anthropic API defaults (not pinned). "
                 "Prompt text is exported verbatim with SHA-256 hashes."),
    }
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2)
    return manifest
