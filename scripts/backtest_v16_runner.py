from __future__ import annotations

import json
from pathlib import Path

import historical_backtest as hb
import backtest_v15_runner as v15
from backtest_ma5_stop import build_ma5_stop_summary

STOP_OUT = Path("data/backtest-ma5-stop-summary.json")


def _wrapped_execution(ext, data, generated_at, min_group=30, *args, **kwargs):
    execution_summary, execution_sample, trades = v15._wrapped_execution(
        ext, data, generated_at, min_group, *args, **kwargs
    )
    stop_summary = build_ma5_stop_summary(data, generated_at)
    STOP_OUT.parent.mkdir(exist_ok=True)
    STOP_OUT.write_text(json.dumps(stop_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return execution_summary, execution_sample, trades


def main():
    hb.build_execution_summary = _wrapped_execution
    hb.main()
    if hb.OUT.exists() and STOP_OUT.exists():
        summary = json.loads(hb.OUT.read_text(encoding="utf-8"))
        stop = json.loads(STOP_OUT.read_text(encoding="utf-8"))
        summary["ma5RiskStopResearch"] = {
            "version": stop.get("version"),
            "outputFile": str(STOP_OUT),
            "baseRule": stop.get("baseRule"),
            "stopRules": stop.get("stopRules"),
            "validation2025Ranking": stop.get("validation2025Ranking"),
            "promotionPolicy": stop.get("promotionPolicy"),
        }
        hb.OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
