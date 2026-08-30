from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from backtest_oos_v18 import build_oos_v18_summary
from backtest_snapshot import load_snapshot

VERSION = "historical-backtest-oos-v1.8"
OUT = Path("data/backtest-oos-v18-summary.json")
MANIFEST_COPY = Path("data/backtest-oos-v18-freeze-manifest.json")


def main():
    data_path = Path(os.environ["BACKTEST_FROZEN_DATA"])
    manifest_path = Path(os.environ["BACKTEST_FROZEN_MANIFEST"])
    data, manifest = load_snapshot(data_path, manifest_path)

    if str(manifest.get("targetStart")) > "2022-01-01":
        raise RuntimeError("V1.8 OOS freeze starts too late; 2022 must be included")
    if str(manifest.get("targetEnd")) < "2024-01-01":
        raise RuntimeError("V1.8 OOS freeze needs post-2023 rows so late-2023 positions can exit naturally")

    generated_at = datetime.now(timezone.utc).isoformat()
    summary = build_oos_v18_summary(data, generated_at=generated_at)
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
    summary["knownBiases"] = [
        "CURRENT_UNIVERSE_SURVIVORSHIP: the snapshot uses the security master available when the freeze is created, so delisted/historical constituents may be missing.",
        "YAHOO_ARCHIVE: OHLC is a frozen Yahoo/yfinance archive scaled by Adj Close/Close at snapshot creation; corporate-action archive revisions are eliminated only after this freeze is created.",
        "OOS_POLICY: 2022-2023 are being used only to test the already-selected V1.7C winner. Do not tune the strategy on these results if they are to remain out-of-sample evidence.",
    ]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    MANIFEST_COPY.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "ok": True,
        "version": VERSION,
        "freezeId": manifest.get("freezeId"),
        "freezeSha256": manifest.get("dataFileSha256"),
        "allOosYearsBasicPass": summary.get("allOosYearsBasicPass"),
        "worstOosYearReturnPct": summary.get("worstOosYearReturnPct"),
        "yearly": {
            y: {
                "totalReturnPct": r.get("totalReturnPct"),
                "maxDrawdownPct": r.get("maxDrawdownPct"),
                "profitFactor": r.get("profitFactor"),
                "expectancyPct": r.get("expectancyPct"),
                "tradeCount": r.get("tradeCount"),
                "benchmark0050PriceReturnPct": r.get("benchmark0050PriceReturnPct"),
            }
            for y, r in (summary.get("yearly") or {}).items()
        },
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
