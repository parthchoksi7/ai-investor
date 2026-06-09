"""
analysis.py — 7-agent investment pipeline.

Pipeline order:
  1. Market Regime Strategist  (portfolio-level, Sonnet)
  2. Research Analyst           (per-ticker,       Haiku)
  3. Earnings & Catalyst Analyst(per-ticker,       Haiku)
  4. Devil's Advocate           (per-ticker,       Haiku)
  5. Position Review Analyst    (per-holding,      Haiku)
  6. Portfolio Manager          (portfolio-level,  Sonnet)
  7. Chief Risk Officer         (portfolio-level,  Sonnet)
"""

import os
import json
import anthropic
from dotenv import load_dotenv

load_dotenv()

def _make_client() -> anthropic.Anthropic:
    """Create Anthropic client, supporting both API key and Claude Code OAuth token."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if api_key:
        return anthropic.Anthropic(api_key=api_key)
    # Cloud execution: read session ingress token
    token_file = os.getenv("CLAUDE_SESSION_INGRESS_TOKEN_FILE", "")
    if token_file and os.path.isfile(token_file):
        with open(token_file) as _tf:
            token = _tf.read().strip()
        return anthropic.Anthropic(auth_token=token)
    return anthropic.Anthropic()

client = _make_client()

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

def _call(model: str, system: str | list, user_msg: str, max_tokens: int = 600) -> str:
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    return response.content[0].text.strip()


def _cached_system(prompt: str) -> list:
    """Wrap a system prompt with prompt caching — efficient when called many times."""
    return [{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}]


def _parse_json(text: str, default):
    import re
    text = text.strip()
    # Strip markdown code fences wherever they appear
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Model wrapped JSON in prose — extract the first {...} or [...] block
        match = re.search(r'(\{[\s\S]*\}|\[[\s\S]*\])', text)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        return default


def _safe_call(model, system, user_msg, default, max_tokens=600):
    try:
        raw = _call(model, system, user_msg, max_tokens=max_tokens)
        return _parse_json(raw, default)
    except Exception as e:
        print(f"   ⚠ Agent call failed: {e}")
        return default


def _fmt_scores(scores: dict) -> str:
    # data_available=False is set by score_all_tickers when history is empty (cloud mode).
    # Default True so old cached scores without the flag are treated as having data.
    if not scores.get("data_available", True):
        return "QUANT DATA UNAVAILABLE (no historical prices — scores are not real)"
    return (
        f"composite={scores.get('composite_score','?')} "
        f"mom={scores.get('momentum_score','?')} "
        f"quality={scores.get('quality_score','?')} "
        f"val={scores.get('valuation_score','?')} "
        f"vol={scores.get('volatility','?')}% "
        f"beta={scores.get('beta','?')} "
        f"1M={scores.get('return_1m','?')}% "
        f"3M={scores.get('return_3m','?')}% "
        f"above50={scores.get('above_50dma','?')} "
        f"above200={scores.get('above_200dma','?')}"
    )


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
    headlines = "\n".join(f"  - {a['title']}" for a in market_data.get("news", [])[:8])

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
        max_tokens=400,
    )


# ── Agent 2: Research Analyst ─────────────────────────────────────────────────

def run_research_analyst(ticker: str, market_data: dict, quant_scores: dict) -> dict:
    data   = market_data["prices"].get(ticker) or market_data.get("news_discovered", {}).get(ticker, {})
    scores = quant_scores.get(ticker, {})
    news   = "\n".join(
        f"  - {a['title']}"
        for a in market_data.get("news", [])
        if ticker in a.get("tickers", [])
    ) or "  - No recent headlines"

    user_msg = f"""\
TICKER: {ticker}
Price: ${data.get('close', 'N/A')} ({data.get('change_pct', 0):+.2f}%)

QUANT: {_fmt_scores(scores)}

NEWS:
{news}

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
        max_tokens=600,
    )


# ── Agent 3: Earnings & Catalyst Analyst ─────────────────────────────────────

def run_earnings_catalyst_analyst(ticker: str, market_data: dict) -> dict:
    data = market_data["prices"].get(ticker, {})
    news = "\n".join(
        f"  - {a['title']}"
        for a in market_data.get("news", [])
        if ticker in a.get("tickers", [])
    ) or "  - No recent headlines"

    user_msg = f"""\
TICKER: {ticker}
Price: ${data.get('close', 'N/A')}  Date: {market_data['date']}

NEWS:
{news}

Use your training knowledge of {ticker}'s typical earnings calendar and business cycle. \
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

    return _safe_call(
        MODEL_FAST, _cached_system(_EARNINGS_SYSTEM), user_msg,
        default={"earnings_alpha_score": 5, "beat_probability": "MEDIUM",
                 "guidance_cut_probability": "LOW", "key_catalysts_90d": []},
        max_tokens=400,
    )


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

Output JSON (fill in every field):
{{
  "bear_case": "the strongest argument against owning this stock",
  "weakest_assumptions": ["assumption 1", "assumption 2"],
  "hidden_risks": ["risk 1"],
  "crowding_risk": "MEDIUM",
  "valuation_risk": "MEDIUM",
  "catalyst_failure_probability": "MEDIUM",
  "overall_risk_score": 5,
  "recommend_reject": false
}}"""

    return _safe_call(
        MODEL_FAST, _cached_system(_DEVILS_SYSTEM), user_msg,
        default={"bear_case": "", "overall_risk_score": 5,
                 "recommend_reject": False, "hidden_risks": []},
        max_tokens=500,
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
{chr(10).join(f"  - {a['title']}" for a in market_data.get('news', []) if ticker in a.get('tickers', [])) or '  - None'}

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
        max_tokens=400,
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
) -> list[dict]:
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

    user_msg = f"""\
Date: {date}

MARKET REGIME: {json.dumps(regime)}

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

OUTPUT: Return ONLY a JSON array. Each element:
{{
  "ticker": "...",
  "action": "BUY | SELL",
  "target_weight": 0.00–0.10,
  "source_of_capital": "ticker being reduced, or 'cash'",
  "rationale": "one sentence"
}}
Omit HOLD decisions. Return [] if no trades improve portfolio expected value."""

    return _safe_call(
        MODEL_SMART, _PM_SYSTEM, user_msg,
        default=[],
        max_tokens=1200,
    )


# ── Agent 7: Chief Risk Officer ───────────────────────────────────────────────

def run_chief_risk_officer(
    decisions: list[dict],
    portfolio: dict,
    quant_scores: dict,
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

    user_msg = f"""\
PROPOSED TRADES:
{json.dumps(decisions, indent=2)}

PROJECTED PORTFOLIO (post-trade weights):
{chr(10).join(risk_lines)}

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
                 "largest_risk": "CRO call failed",
                 "rejected_tickers": [],
                 "reasoning": "CRO unavailable — all trades blocked as precaution."},
        max_tokens=400,
    )


# ── Main Orchestration ────────────────────────────────────────────────────────

def get_trade_decisions(
    portfolio: dict,
    market_data: dict,
    quant_scores: dict,
    trade_history: list | None = None,
    prior_journal: dict | None = None,
) -> tuple[list[dict], dict]:
    """
    Run the full 7-agent pipeline.
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

    # ── 2. Research Analyst ───────────────────────────────────────────────────
    print(f"   [2/7] Research Analyst ({len(candidates)} tickers)...")
    research_map: dict = {}
    for ticker in candidates:
        research_map[ticker] = run_research_analyst(ticker, market_data, quant_scores)

    # ── 3. Earnings & Catalyst Analyst ───────────────────────────────────────
    print(f"   [3/7] Earnings & Catalyst Analyst ({len(candidates)} tickers)...")
    earnings_map: dict = {}
    for ticker in candidates:
        earnings_map[ticker] = run_earnings_catalyst_analyst(ticker, market_data)

    # ── 4. Devil's Advocate (all candidates — confidence filter was backwards) ──
    # High-confidence Haiku output is exactly the case most likely to need adversarial review.
    devil_candidates = candidates
    print(f"   [4/7] Devil's Advocate ({len(devil_candidates)} tickers)...")
    devil_map: dict = {}
    for ticker in devil_candidates:
        devil_map[ticker] = run_devils_advocate(
            ticker, research_map[ticker], earnings_map[ticker], market_data, quant_scores
        )

    # ── 5. Position Review (current holdings only) ────────────────────────────
    holdings = portfolio.get("positions", [])
    print(f"   [5/7] Position Review Analyst ({len(holdings)} holdings)...")
    position_reviews: dict = {}
    for holding in holdings:
        ticker = holding["symbol"]
        position_reviews[ticker] = run_position_review_analyst(
            holding, market_data, quant_scores, research_map.get(ticker),
            (prior_journal or {}).get(ticker),
        )
        review = position_reviews[ticker]
        print(
            f"         {ticker}: hold={review.get('hold_score')}/10 "
            f"alpha={review.get('remaining_alpha')} → {review.get('recommended_action')}"
        )

    # ── 6. Portfolio Manager ──────────────────────────────────────────────────
    print("   [6/7] Portfolio Manager...")
    decisions = run_portfolio_manager(
        regime, research_map, earnings_map, devil_map,
        position_reviews, quant_scores, portfolio, trade_history,
        date=market_data_date,
    )

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
    risk = run_chief_risk_officer(decisions, portfolio, quant_scores)
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
        "cro": risk,
        "final_decisions": decisions,
    }
    return decisions, pipeline_state
