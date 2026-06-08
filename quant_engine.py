"""
quant_engine.py — Deterministic scoring. No LLM involved.
"""

import math
import statistics


def _pct_return(closes: list[float], n: int) -> float | None:
    if len(closes) < n + 1:
        return None
    base = closes[-(n + 1)]
    return ((closes[-1] - base) / base) * 100 if base else None


def compute_momentum_score(history: list[dict]) -> dict:
    """Score 0-100. Higher = stronger momentum."""
    if not history:
        return {
            "momentum_score": 50, "return_1m": None,
            "return_3m": None, "return_6m": None,
            "above_50dma": None, "above_200dma": None,
        }

    closes = [d["close"] for d in history]
    current = closes[-1]

    r1m = _pct_return(closes, 21)
    r3m = _pct_return(closes, 63)
    r6m = _pct_return(closes, 126)

    dma50  = statistics.mean(closes[-50:])  if len(closes) >= 50  else None
    dma200 = statistics.mean(closes[-200:]) if len(closes) >= 200 else None
    above_50  = bool(current > dma50)  if dma50  else None
    above_200 = bool(current > dma200) if dma200 else None

    score = 50.0
    if r1m is not None: score += max(-15.0, min(15.0, r1m * 1.5))
    if r3m is not None: score += max(-12.0, min(12.0, r3m * 0.8))
    if r6m is not None: score += max(-8.0,  min(8.0,  r6m * 0.4))
    if above_50  is True:  score += 5
    elif above_50  is False: score -= 5
    if above_200 is True:  score += 10
    elif above_200 is False: score -= 10

    return {
        "momentum_score": round(max(0.0, min(100.0, score)), 1),
        "return_1m":  round(r1m, 2) if r1m is not None else None,
        "return_3m":  round(r3m, 2) if r3m is not None else None,
        "return_6m":  round(r6m, 2) if r6m is not None else None,
        "above_50dma":  above_50,
        "above_200dma": above_200,
    }


def compute_quality_score(fundamentals: dict | None) -> dict:
    """Score 0-100. Higher = better quality."""
    if not fundamentals:
        return {"quality_score": 50}

    scores = []

    gm = fundamentals.get("gross_margin")
    if gm is not None:
        scores.append(90 if gm > 0.60 else 70 if gm > 0.40 else 50 if gm > 0.20 else 25)

    om = fundamentals.get("operating_margin")
    if om is not None:
        scores.append(90 if om > 0.25 else 70 if om > 0.15 else 50 if om > 0.05 else 30 if om > 0 else 10)

    fm = fundamentals.get("fcf_margin")
    if fm is not None:
        scores.append(90 if fm > 0.20 else 70 if fm > 0.10 else 45 if fm > 0 else 15)

    de = fundamentals.get("debt_to_equity")
    if de is not None:
        scores.append(90 if de < 0.5 else 70 if de < 1.0 else 50 if de < 2.0 else 25)

    return {"quality_score": round(statistics.mean(scores), 1) if scores else 50}


def compute_valuation_score(fundamentals: dict | None) -> dict:
    """Score 0-100. Higher = better value (cheaper relative to fundamentals)."""
    if not fundamentals:
        return {"valuation_score": 50}

    scores = []

    pe = fundamentals.get("pe_ratio")
    if pe is not None and pe > 0:
        scores.append(90 if pe < 15 else 70 if pe < 25 else 50 if pe < 35 else 30 if pe < 50 else 10)

    fy = fundamentals.get("fcf_yield")
    if fy is not None:
        scores.append(90 if fy > 0.06 else 70 if fy > 0.03 else 50 if fy > 0.01 else 30 if fy > 0 else 10)

    ev_ebitda = fundamentals.get("ev_ebitda")
    if ev_ebitda is not None and ev_ebitda > 0:
        scores.append(90 if ev_ebitda < 10 else 70 if ev_ebitda < 15 else 50 if ev_ebitda < 25 else 30 if ev_ebitda < 40 else 10)

    return {"valuation_score": round(statistics.mean(scores), 1) if scores else 50}


def compute_risk_metrics(history: list[dict], spy_history: list[dict]) -> dict:
    """Returns annualized volatility, beta vs SPY, and a risk score (higher = lower risk)."""
    if len(history) < 22:
        return {"volatility": None, "beta": None, "volatility_score": 50}

    closes = [d["close"] for d in history]
    daily_ret = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
    recent = daily_ret[-63:]  # 3-month window

    vol = statistics.stdev(recent) * math.sqrt(252) * 100  # annualized %

    beta = None
    if spy_history and len(spy_history) >= 22:
        spy_closes = [d["close"] for d in spy_history]
        spy_ret = [(spy_closes[i] - spy_closes[i - 1]) / spy_closes[i - 1] for i in range(1, len(spy_closes))]
        n = min(len(recent), len(spy_ret))
        sr, mr = recent[-n:], spy_ret[-n:]
        if n > 2:
            mean_s, mean_m = statistics.mean(sr), statistics.mean(mr)
            cov = sum((s - mean_s) * (m - mean_m) for s, m in zip(sr, mr)) / (n - 1)
            spy_var = statistics.variance(mr)
            beta = round(cov / spy_var, 2) if spy_var > 0 else None

    # Normalize 15%–80% annualized vol range to 100→0 score
    vol_score = max(0.0, min(100.0, 100.0 - (vol - 15.0) * (100.0 / 65.0)))

    return {
        "volatility": round(vol, 1),
        "beta": beta,
        "volatility_score": round(vol_score, 1),
    }


def score_all_tickers(market_data: dict) -> dict:
    """Returns {ticker: score_dict} for all tickers that have price history."""
    spy_history = market_data.get("history", {}).get("SPY", [])
    scores = {}

    for ticker, history in market_data.get("history", {}).items():
        fundamentals = market_data.get("fundamentals", {}).get(ticker)
        momentum  = compute_momentum_score(history)
        quality   = compute_quality_score(fundamentals)
        valuation = compute_valuation_score(fundamentals)
        risk      = compute_risk_metrics(history, spy_history)

        composite = (
            momentum["momentum_score"]  * 0.30
            + quality["quality_score"]  * 0.25
            + valuation["valuation_score"] * 0.20
            + risk["volatility_score"]  * 0.25
        )

        scores[ticker] = {
            "ticker": ticker,
            "data_available": len(history) > 0,
            "composite_score": round(composite, 1),
            **momentum,
            **quality,
            **valuation,
            **risk,
        }

    return scores
