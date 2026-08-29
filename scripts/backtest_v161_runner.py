from __future__ import annotations

import json
from pathlib import Path

import historical_backtest as hb
import backtest_v16_runner as v16
from backtest_ma5_robustness import build_ma5_robustness_summary

ROBUST_OUT = Path("data/backtest-ma5-robustness-summary.json")


def _wrapped_execution(ext, data, generated_at, min_group=30, *args, **kwargs):
    execution_summary, execution_sample, trades = v16._wrapped_execution(
        ext, data, generated_at, min_group, *args, **kwargs
    )
    robust_summary = build_ma5_robustness_summary(data, generated_at)
    ROBUST_OUT.parent.mkdir(exist_ok=True)
    ROBUST_OUT.write_text(
        json.dumps(robust_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return execution_summary, execution_sample, trades


def main():
    hb.build_execution_summary = _wrapped_execution
    hb.main()
    if hb.OUT.exists() and ROBUST_OUT.exists():
        summary = json.loads(hb.OUT.read_text(encoding="utf-8"))
        robust = json.loads(ROBUST_OUT.read_text(encoding="utf-8"))
        summary["ma5StopRobustnessResearch"] = {
            "version": robust.get("version"),
            "outputFile": str(ROBUST_OUT),
            "researchPolicy": robust.get("researchPolicy"),
            "grid": robust.get("grid"),
            "robustGate": robust.get("robustGate"),
            "robustPassCount": robust.get("robustPassCount"),
            "robustPassRate": robust.get("robustPassRate"),
            "topByWorstTrainValidationReturn": (robust.get("topByWorstTrainValidationReturn") or [])[:5],
            "automaticProductionChanges": False,
        }
        hb.OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
