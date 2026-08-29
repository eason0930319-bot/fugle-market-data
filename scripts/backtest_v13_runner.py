from __future__ import annotations

import json
from pathlib import Path

import historical_backtest as hb
from backtest_execution import build_execution_summary as _build_execution_summary
import backtest_portfolio as bp

PORT_OUT = Path("data/backtest-portfolio-summary.json")
_PRICE_CACHE = {}
_ORIGINAL_PRICE_SERIES = bp._price_series


def _cached_price_series(data, column):
    # V1.3 runs many portfolio/cohort combinations over the same 1.4M-row history.
    # Build each MultiIndex price series once instead of once per simulation.
    if column not in _PRICE_CACHE:
        _PRICE_CACHE[column] = _ORIGINAL_PRICE_SERIES(data, column)
    return _PRICE_CACHE[column]


def _wrapped_execution(ext, data, generated_at, min_group=30, *args, **kwargs):
    execution_summary, execution_sample, trades = _build_execution_summary(
        ext, data, generated_at, min_group, *args, **kwargs
    )
    bp._price_series = _cached_price_series
    portfolio_summary = bp.build_portfolio_summary(trades, data, generated_at)
    PORT_OUT.parent.mkdir(exist_ok=True)
    PORT_OUT.write_text(
        json.dumps(portfolio_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return execution_summary, execution_sample, trades


def main():
    # Keep the established V1.2 historical/execution pipeline intact and attach V1.3
    # portfolio evaluation at the exact point where the full executable trade table and
    # historical price frame are both available in memory.
    hb.build_execution_summary = _wrapped_execution
    hb.main()

    if PORT_OUT.exists() and hb.OUT.exists():
        portfolio = json.loads(PORT_OUT.read_text(encoding="utf-8"))
        summary = json.loads(hb.OUT.read_text(encoding="utf-8"))
        summary["portfolioResearch"] = {
            "version": portfolio.get("version"),
            "outputFile": str(PORT_OUT),
            "portfolioAssumptions": portfolio.get("portfolioAssumptions"),
            "validation2025Top5": (portfolio.get("validation2025RankingByTotalReturn") or [])[:5],
            "automaticProductionChanges": False,
        }
        hb.OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
