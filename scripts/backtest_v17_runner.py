from __future__ import annotations

import json
import os
from pathlib import Path

import historical_backtest as hb
import backtest_v162_runner as v162
import backtest_v163_runner as v163
from backtest_execution_v17 import build_execution_v17_summary

VERSION = "historical-backtest-execution-v1.7a"
EXEC17_OUT = Path("data/backtest-execution-v17-summary.json")


def _wrapped_execution(ext, data, generated_at, min_group=30, *args, **kwargs):
    execution_summary, execution_sample, trades = v162._wrapped_execution(
        ext, data, generated_at, min_group, *args, **kwargs
    )
    research = build_execution_v17_summary(data, generated_at)
    EXEC17_OUT.parent.mkdir(exist_ok=True)
    EXEC17_OUT.write_text(json.dumps(research, ensure_ascii=False, indent=2), encoding="utf-8")
    return execution_summary, execution_sample, trades


def main():
    data_path = Path(os.environ["BACKTEST_FROZEN_DATA"])
    manifest_path = Path(os.environ["BACKTEST_FROZEN_MANIFEST"])
    manifest = v163._install_frozen_data(data_path, manifest_path)

    hb.build_execution_summary = _wrapped_execution
    hb.main()

    if hb.OUT.exists() and EXEC17_OUT.exists():
        summary = json.loads(hb.OUT.read_text(encoding="utf-8"))
        research = json.loads(EXEC17_OUT.read_text(encoding="utf-8"))
        summary["dailyExecutionResearch"] = {
            "version": research.get("version"),
            "outputFile": str(EXEC17_OUT),
            "researchPolicy": research.get("researchPolicy"),
            "entryModeCount": research.get("entryModeCount"),
            "bothYearsBasicPassCount": research.get("bothYearsBasicPassCount"),
            "topByWorstTrainValidationReturn": (research.get("topByWorstTrainValidationReturn") or [])[:4],
            "automaticProductionChanges": False,
        }
        summary["dataReproducibility"] = {
            "version": v163.VERSION,
            "mode": "IMMUTABLE_FROZEN_SNAPSHOT",
            "freezeId": manifest.get("freezeId"),
            "dataFileSha256": manifest.get("dataFileSha256"),
            "targetStart": manifest.get("targetStart"),
            "targetEnd": manifest.get("targetEnd"),
            "effectiveEquityEnd": manifest.get("effectiveEquityEnd"),
            "universe": manifest.get("universe"),
            "rows": manifest.get("rows"),
            "policy": manifest.get("policy"),
            "automaticProductionChanges": False,
        }
        summary.setdefault("knownBiases", []).append(
            "DATA_FREEZE: market rows/prices/universe are immutable for reproducible strategy comparisons; create a new freezeId to extend the sample."
        )
        summary.setdefault("knownBiases", []).append(
            "EXECUTION_V1.7A: entry timing is tested on a fixed MA5 mechanical signal/risk architecture and is not yet the final production V3.3 candidate stream."
        )
        hb.OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
