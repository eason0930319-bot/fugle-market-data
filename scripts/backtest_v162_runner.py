from __future__ import annotations

import json
from pathlib import Path

import historical_backtest as hb
import backtest_v161_runner as v161
from backtest_ma5_regime_reentry import build_ma5_regime_reentry_summary

REGIME_OUT = Path("data/backtest-ma5-regime-reentry-summary.json")


def _wrapped_execution(ext, data, generated_at, min_group=30, *args, **kwargs):
    execution_summary, execution_sample, trades = v161._wrapped_execution(
        ext, data, generated_at, min_group, *args, **kwargs
    )
    research = build_ma5_regime_reentry_summary(data, generated_at)
    REGIME_OUT.parent.mkdir(exist_ok=True)
    REGIME_OUT.write_text(json.dumps(research, ensure_ascii=False, indent=2), encoding="utf-8")
    return execution_summary, execution_sample, trades


def main():
    hb.build_execution_summary = _wrapped_execution
    hb.main()
    if hb.OUT.exists() and REGIME_OUT.exists():
        summary = json.loads(hb.OUT.read_text(encoding="utf-8"))
        research = json.loads(REGIME_OUT.read_text(encoding="utf-8"))
        summary["ma5RegimeReentryResearch"] = {
            "version": research.get("version"),
            "outputFile": str(REGIME_OUT),
            "researchPolicy": research.get("researchPolicy"),
            "variantCount": research.get("variantCount"),
            "bothYearsBasicPassCount": research.get("bothYearsBasicPassCount"),
            "topByWorstTrainValidationReturn": (research.get("topByWorstTrainValidationReturn") or [])[:5],
            "automaticProductionChanges": False,
        }
        hb.OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
