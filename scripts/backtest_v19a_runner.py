from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from backtest_engine_state_v19a import build_v19a_summary
from backtest_snapshot import load_snapshot

VERSION = "historical-backtest-engine-state-v1.9a"
OUT = Path("data/backtest-engine-state-v19a-summary.json")
MANIFEST_COPY = Path("data/backtest-engine-state-v19a-freeze-manifest.json")
PREREG = Path("research/v19a-preregister.json")
EXPECTED_FREEZE = "tw-history-2022-20260828-v1"
EXPECTED_SHA = "0dda6a1504e114424de3c37f3b5662bf7a2d2bbac42cfa2802592bdeb1220d42"


def main():
    data_path = Path(os.environ["BACKTEST_FROZEN_DATA"])
    manifest_path = Path(os.environ["BACKTEST_FROZEN_MANIFEST"])
    data, manifest = load_snapshot(data_path, manifest_path)

    if not PREREG.exists():
        raise RuntimeError("V1.9A preregistration file missing")
    prereg = json.loads(PREREG.read_text(encoding="utf-8"))
    if prereg.get("status") != "PREREGISTERED_BEFORE_FIRST_RUN":
        raise RuntimeError("unexpected V1.9A preregistration status")
    if manifest.get("freezeId") != EXPECTED_FREEZE or manifest.get("dataFileSha256") != EXPECTED_SHA:
        raise RuntimeError("V1.9A must use the preregistered immutable freeze and SHA")

    generated_at = datetime.now(timezone.utc).isoformat()
    summary = build_v19a_summary(data, generated_at)
    summary["runnerVersion"] = VERSION
    summary["dataFreeze"] = {
        "mode": "IMMUTABLE_FROZEN_SNAPSHOT",
        "freezeId": manifest.get("freezeId"),
        "dataFileSha256": manifest.get("dataFileSha256"),
        "targetStart": manifest.get("targetStart"),
        "targetEnd": manifest.get("targetEnd"),
        "effectiveEquityStart": manifest.get("effectiveEquityStart"),
        "effectiveEquityEnd": manifest.get("effectiveEquityEnd"),
        "universe": manifest.get("universe"),
        "rows": manifest.get("rows"),
        "priceBasis": manifest.get("priceBasis"),
        "policy": manifest.get("policy"),
    }
    summary["preregistration"] = prereg

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    MANIFEST_COPY.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "ok": True,
        "version": VERSION,
        "freezeId": manifest.get("freezeId"),
        "latestFrozenMarketState": summary.get("latestFrozenMarketState"),
        "tradePerformanceBySignalState": summary.get("tradePerformanceBySignalState"),
        "engineAuditYears": {
            y: {
                "legacyReturn": row["legacyV17C"].get("totalReturnPct"),
                "cleanRecycleReturn": row["cleanPriorCloseRecycle"].get("totalReturnPct"),
                "cleanNoRecycleReturn": row["cleanPriorCloseNoRecycle"].get("totalReturnPct"),
                "impactVsLegacy": row.get("impactVsLegacy"),
            }
            for y, row in summary.get("engineAudit", {}).get("yearly", {}).items()
        },
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
