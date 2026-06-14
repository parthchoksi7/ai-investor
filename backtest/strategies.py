"""
backtest/strategies.py — pluggable strategies: scores -> {ticker: target_weight}.

Pure functions of the quant scores so the backtest stays deterministic. Long-only,
weights sum to ≤ 1 (the remainder is cash, matching the live 0–10% cash target).
"""


def quant_momentum_vol(scores: dict,
                       top_n: int = 8,
                       max_weight: float = 0.10,
                       min_composite: float = 50.0) -> dict:
    """Top-N by composite score, inverse-volatility weighted, capped at max_weight.

    Mirrors the live universe rules (≤10% per name, long-only, exclude benchmarks)
    and the honest composite (momentum + inverse-vol when fundamentals are absent).
    """
    ranked = sorted(
        ((t, s) for t, s in scores.items()
         if t not in ("SPY", "QQQ")
         and s.get("data_available")
         and s.get("momentum_available")
         and s.get("composite_score", 0) >= min_composite),
        key=lambda x: x[1].get("composite_score", 0),
        reverse=True,
    )[:top_n]
    if not ranked:
        return {}

    inv = {t: (1.0 / s["volatility"] if s.get("volatility") else 0.0) for t, s in ranked}
    total = sum(inv.values())
    if total <= 0:                          # no vol data → equal weight, capped
        w = min(max_weight, 1.0 / len(ranked))
        return {t: w for t, _ in ranked}
    return {t: min(max_weight, inv[t] / total) for t, _ in ranked}


def equal_weight_topn(scores: dict, top_n: int = 8, max_weight: float = 0.10) -> dict:
    """Baseline: equal-weight the top-N by composite (for A/B vs the vol-sized one)."""
    ranked = sorted(
        ((t, s) for t, s in scores.items()
         if t not in ("SPY", "QQQ") and s.get("data_available") and s.get("momentum_available")),
        key=lambda x: x[1].get("composite_score", 0),
        reverse=True,
    )[:top_n]
    if not ranked:
        return {}
    w = min(max_weight, 1.0 / len(ranked))
    return {t: w for t, _ in ranked}
