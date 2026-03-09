"""
AI Investor - Main Entry Point
Run this every day to fetch market data, get Claude's analysis, and execute trades.
Usage: python main.py
"""

import schedule
import time
from market_data import get_market_snapshot
from analysis import get_trade_decisions
from execute import execute_trades, get_portfolio_summary

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

    # Step 3: Ask Claude what to do
    print("\n🧠 Step 3: Asking Claude for trade decisions...")
    decisions = get_trade_decisions(portfolio, market_data)

    if not decisions:
        print("   No trades recommended today.")
        return

    print(f"   Claude recommends {len(decisions)} trades:")
    for d in decisions:
        print(f"   → {d['action']} {d['qty']} shares of {d['ticker']} — {d['rationale']}")

    # Step 4: Execute trades
    print("\n⚡ Step 4: Executing trades on Alpaca (paper)...")
    execute_trades(decisions)

    print("\n✅ Daily cycle complete.")
    print("="*50 + "\n")


if __name__ == "__main__":
    print("🚀 AI Investor started. Running first cycle now...")
    run_daily_cycle()

    # Schedule to run every day at 9:45am (after market open)
    schedule.every().day.at("09:45").do(run_daily_cycle)

    print("⏰ Scheduled to run daily at 9:45 AM.")
    print("   Leave this terminal open to keep it running.")
    print("   Press Ctrl+C to stop.\n")

    while True:
        schedule.run_pending()
        time.sleep(60)
