"""Quant-only backtest harness for AI Investor.

Replays the DETERMINISTIC layers (quant_engine scoring + sizing + cost_model
economics) over historical bars. The LLM agents are deliberately absent — a
frozen model knows the future of any historical period, so the LLM layer is
forward-tested via the prediction ledger, never backtested. See FINAL_PLAN.md.
"""
