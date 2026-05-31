"""
AI Investor - Main Entry Point
Run this every day to fetch market data, get Claude's analysis, and execute trades.
Usage: python main.py
"""

import os
from market_data import get_market_snapshot
from analysis import get_trade_decisions
from execute import execute_trades, get_portfolio_summary, log_trades, get_trade_history

def run_daily_cycle():
    print("\n" + "="*50)
    print("🤖 AI INVESTOR — DAILY CYCLE STARTING")
    print("="*50)

    # Step 1: Get current portfolio
    print("\n📊 Step 1: Fetching current portfolio...")
    portfolio = get_portfolio_summary()
    print(f"   Cash: ${portfolio['cash']:.2f}")
    print(f"   Positions: {len(portfolio['positions'])}")
    print(f"   Total Value: ${portfolio['total_value']:.2f}")

    # Step 2: Get market data
    print("\n📈 Step 2: Fetching market data...")
    market_data = get_market_snapshot()
    print(f"   Loaded data for {len(market_data)} tickers")

    # Step 3: Portfolio manager — asking Claude...
    print("\n🧠 Step 3: Portfolio manager — asking Claude...")
    trade_history = get_trade_history()
    decisions = get_trade_decisions(portfolio, market_data, trade_history)
    if decisions:
        print(f"   {len(decisions)} trade(s):")
        for d in decisions:
            print(f"   → {d['action']} {d['qty']}x {d['ticker']} — {d['rationale']}")
    else:
        print("   No trades today.")

    if not decisions:
        print("\n   Nothing to execute today.")
        print("="*50 + "\n")
        return

    # Step 4: Execute all trades
    print("\n⚡ Step 4: Executing trades on Alpaca (paper)...")
    execute_trades(decisions)
    log_trades(decisions, portfolio, strategy="institutional")

    print("\n✅ Daily cycle complete.")
    print("="*50 + "\n")


if __name__ == "__main__":
    print("🚀 AI Investor — running cycle...")
    run_daily_cycle()
