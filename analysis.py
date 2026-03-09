"""
analysis.py — Sends market data to Claude and gets back trade decisions.
"""

import os
import json
import anthropic
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


SYSTEM_PROMPT = """You are an AI portfolio manager running a $2,000 paper trading account.
Your goal is to maximize returns by making smart, calculated trades.

Rules:
- Never put more than 30% of total portfolio value into a single stock
- Always keep at least $100 as cash reserve
- Only trade stocks from the provided watchlist
- You can BUY (open new position), SELL (close a position), or HOLD (do nothing)
- Be decisive — if the market looks good, deploy capital

You must respond with ONLY a valid JSON array of trade decisions. No explanation, no markdown.
Each trade should look like:
[
  {
    "ticker": "NVDA",
    "action": "BUY",
    "qty": 2,
    "rationale": "Strong AI momentum, underweighted in portfolio"
  }
]

If no trades are needed, return an empty array: []
"""


def get_trade_decisions(portfolio, market_data):
    """
    Sends current portfolio + market data to Claude.
    Returns a list of trade decisions.
    """

    # Format the portfolio for Claude
    positions_text = ""
    if portfolio["positions"]:
        for p in portfolio["positions"]:
            positions_text += f"  - {p['symbol']}: {p['qty']} shares @ avg ${p['avg_price']:.2f} (current: ${p['current_price']:.2f})\n"
    else:
        positions_text = "  - No positions (all cash)\n"

    # Format market data for Claude
    prices_text = ""
    for ticker, data in market_data["prices"].items():
        prices_text += f"  - {ticker}: ${data['close']:.2f} (change: {data['change_pct']:+.2f}%)\n"

    news_text = "\n".join([f"  - {h}" for h in market_data["news"][:5]])

    user_message = f"""
Today's date: {market_data['date']}

CURRENT PORTFOLIO:
  Cash available: ${portfolio['cash']:.2f}
  Total value: ${portfolio['total_value']:.2f}
  Positions:
{positions_text}

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
