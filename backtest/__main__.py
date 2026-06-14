"""CLI: python -m backtest [--strategy quant_vol|equal] [--rebalance N] [--capital N]"""

import argparse

from backtest.engine import run_backtest
from backtest.report import build_report, print_report
from backtest.strategies import quant_momentum_vol, equal_weight_topn

_STRATS = {"quant_vol": quant_momentum_vol, "equal": equal_weight_topn}


def main() -> None:
    ap = argparse.ArgumentParser(prog="backtest")
    ap.add_argument("--strategy", choices=_STRATS, default="quant_vol")
    ap.add_argument("--rebalance", type=int, default=5, help="rebalance every N trading days")
    ap.add_argument("--capital", type=float, default=50_000.0)
    ap.add_argument("--snapshot", default="market_snapshot.json")
    args = ap.parse_args()

    from backtest.engine import load_snapshot
    result = run_backtest(
        _STRATS[args.strategy],
        snapshot=load_snapshot(args.snapshot),
        initial_capital=args.capital,
        rebalance_days=args.rebalance,
    )
    print_report(build_report(result))


if __name__ == "__main__":
    main()
