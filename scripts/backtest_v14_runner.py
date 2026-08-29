from __future__ import annotations

import json
from pathlib import Path

import historical_backtest as hb
from backtest_execution import build_execution_summary as _build_execution_summary
from backtest_portfolio import build_portfolio_summary
from backtest_ranker import build_ranking_summary

PORT_OUT = Path("data/backtest-portfolio-summary.json")
RANK_OUT = Path("data/backtest-ranking-summary.json")


def _wrapped_execution(ext, data, generated_at, min_group=30, *args, **kwargs):
    execution_summary, execution_sample, trades = _build_execution_summary(
        ext, data, generated_at, min_group, *args, **kwargs
    )

    portfolio_summary = build_portfolio_summary(trades, data, generated_at)
    PORT_OUT.parent.mkdir(exist_ok=True)
    PORT_OUT.write_text(
        json.dumps(portfolio_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    ranking_summary = build_ranking_summary(trades, ext, data, generated_at)
    RANK_OUT.write_text(
        json.dumps(ranking_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return execution_summary, execution_sample, trades


def main():
    hb.build_execution_summary = _wrapped_execution
    hb.main()

    if hb.OUT.exists():
        summary = json.loads(hb.OUT.read_text(encoding="utf-8"))
        if PORT_OUT.exists():
            portfolio = json.loads(PORT_OUT.read_text(encoding="utf-8"))
            summary["portfolioResearch"] = {
                "version": portfolio.get("version"),
                "outputFile": str(PORT_OUT),
                "portfolioAssumptions": portfolio.get("portfolioAssumptions"),
                "validation2025Top5": (portfolio.get("validation2025RankingByTotalReturn") or [])[:5],
                "automaticProductionChanges": False,
            }
        if RANK_OUT.exists():
            ranker = json.loads(RANK_OUT.read_text(encoding="utf-8"))
            summary["rankingResearch"] = {
                "version": ranker.get("version"),
                "outputFile": str(RANK_OUT),
                "trainingPolicy": ranker.get("trainingPolicy"),
                "validation2025Top5": (ranker.get("validation2025Ranking") or [])[:5],
                "automaticProductionChanges": False,
            }
        hb.OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
