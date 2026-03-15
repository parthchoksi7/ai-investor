"""
analysis.py — Sends market data to Claude and gets back trade decisions.
"""

import os
import json
import anthropic
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


SYSTEM_PROMPT = """You are an aggressive AI portfolio manager running a $2,000 paper trading account.

MANDATE:
- Goal: Maximum capital appreciation over a 1-3 month horizon
- Style: Aggressive growth — concentrate in high-conviction, high-momentum positions
- Only go LONG (BUY or SELL to exit). Never short. Never use options or leverage.

STRATEGY:
- Focus on stocks with strong price momentum, earnings growth, and sector tailwinds
- Prioritize high-growth sectors: AI, semiconductors, cloud, energy, biotech
- Be willing to concentrate — 2-4 strong positions beat a diluted portfolio of 10
- Rotate out of underperformers quickly — don't hold losers hoping they recover
- Deploy cash aggressively when there is a clear opportunity. Sitting in cash is a missed return.
- Consider recent news and macro conditions when making decisions

RULES:
- No single position can exceed 40% of total portfolio value
- Always keep at least $100 cash reserve
- Do not trade the same ticker on more than 2 consecutive days
- Actions: BUY (open/add to position), SELL (reduce/close position), HOLD (do nothing)

BENCHMARK:
- You are being measured against SPY (S&P 500). Your job is to significantly outperform it.
- If the market is broadly falling, raise cash by selling weak positions. Preservation matters when everything is down.

You must respond with ONLY a valid JSON array of trade decisions. No explanation, no markdown, no preamble.
Each trade should look like:
[
  {
    "ticker": "NVDA",
    "action": "BUY",
    "qty": 2,
    "rationale": "Leading AI infrastructure play, strong earnings momentum, adding on pullback"
  }
]

If no trades are needed today, return an empty array: []
"""


def get_trade_decisions(portfolio, market_data, trade_history=None):
    """
    Sends current portfolio + market data to Claude.
    Returns a list of trade decisions.
    """

    # Format the portfolio for Claude
    positions_text = ""
    if portfolio["positions"]:
        for p in portfolio["positions"]:
            pnl = p.get("unrealized_pnl", 0)
            pnl_pct = (pnl / (p["avg_price"] * p["qty"])) * 100 if p["avg_price"] and p["qty"] else 0
            positions_text += (
                f"  - {p['symbol']}: {p['qty']} shares @ avg ${p['avg_price']:.2f} "
                f"(current: ${p['current_price']:.2f}, P&L: ${pnl:+.2f} / {pnl_pct:+.1f}%)\n"
            )
    else:
        positions_text = "  - No positions (all cash)\n"

    # Format market data for Claude
    prices_text = ""
    for ticker, data in market_data["prices"].items():
        prices_text += f"  - {ticker}: ${data['close']:.2f} (change: {data['change_pct']:+.2f}%)\n"

    news_text = "\n".join([f"  - {h}" for h in market_data["news"][:5]])

    history_text = ""
    if trade_history:
        history_text = "\nRECENT TRADE HISTORY (last 30 trades):\n"
        for t in trade_history:
            history_text += f"  - {t['date']} | {t['action']} {t['qty']}x {t['ticker']} | {t['rationale']}\n"
    else:
        history_text = "\nRECENT TRADE HISTORY:\n  - No prior trades on record\n"

    user_message = f"""
Today's date: {market_data['date']}

CURRENT PORTFOLIO:
  Cash available: ${portfolio['cash']:.2f}
  Total value: ${portfolio['total_value']:.2f}
  Positions:
{positions_text}
{history_text}
MARKET DATA:
{prices_text}

RECENT NEWS:
{news_text}

Based on this, what trades should I make today? Remember to return ONLY a JSON array.
"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}]
        )

        raw = response.content[0].text.strip()

        # Clean up if Claude added markdown fences
        raw = raw.replace("```json", "").replace("```", "").strip()

        decisions = json.loads(raw)
        return decisions

    except json.JSONDecodeError as e:
        print(f"   ⚠ Could not parse Claude's response as JSON: {e}")
        print(f"   Raw response: {raw}")
        return []

    except Exception as e:
        print(f"   ⚠ Error calling Claude API: {e}")
        return []
