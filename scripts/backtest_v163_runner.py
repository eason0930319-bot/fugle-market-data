from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

import historical_backtest as hb
import backtest_v162_runner as v162
from backtest_data import add_features
from backtest_snapshot import load_snapshot

VERSION = "historical-backtest-data-freeze-v1.6.3"


def _install_frozen_data(data_path: Path, manifest_path: Path):
    frozen, manifest = load_snapshot(data_path, manifest_path)
    equity = frozen[frozen.market != "BENCH"].copy()
    bench = frozen[frozen.market == "BENCH"].copy()
    if equity.empty or bench.empty:
        raise RuntimeError("frozen dataset missing equity or BENCH rows")

    requested_end = pd.Timestamp(hb.END).normalize() if hb.END else pd.Timestamp(manifest["targetEnd"]).normalize()
    frozen_end = pd.Timestamp(manifest["targetEnd"]).normalize()
    if requested_end > frozen_end:
        raise RuntimeError(f"BACKTEST_END {requested_end.date()} exceeds frozen targetEnd {frozen_end.date()}; create a new freeze instead of silently mixing data")

    # Make the existing historical engine consume the immutable snapshot without
    # changing the downstream research code. This freezes universe, labels and
    # adjusted OHLCV together.
    masters = {}
    for market in ("TSE", "OTC"):
        u = equity[equity.market == market][["symbol", "name", "market", "industry"]].drop_duplicates("symbol")
        masters[market] = {
            str(r.symbol): {
                "symbol": str(r.symbol),
                "name": str(r.name),
                "market": str(r.market),
                "industry": str(r.industry),
                "ticker": f"{r.symbol}.TW" if market == "TSE" else f"{r.symbol}.TWO",
            }
            for _, r in u.iterrows()
        }

    def frozen_security_master(mode: int, market: str):
        if market not in masters:
            raise RuntimeError(f"unsupported frozen market {market}")
        return masters[market]

    def frozen_download_history(items, start, end, batch_size=80):
        return equity.copy(), list(manifest.get("universe", {}).get("failedTickers") or [])

    def already_adjusted(d):
        required = {"date", "symbol", "name", "market", "industry", "open", "high", "low", "close", "volume", "tradeValue"}
        if not required.issubset(d.columns):
            raise RuntimeError("frozen adjusted-price frame has unexpected schema")
        return d.copy()

    def frozen_benchmark(start, end):
        return add_features(bench.copy())

    hb.security_master = frozen_security_master
    hb.download_history = frozen_download_history
    hb.adjusted_prices = already_adjusted
    hb.benchmark = frozen_benchmark
    return manifest


def main():
    data_path = Path(os.environ["BACKTEST_FROZEN_DATA"])
    manifest_path = Path(os.environ["BACKTEST_FROZEN_MANIFEST"])
    manifest = _install_frozen_data(data_path, manifest_path)

    v162.main()

    if hb.OUT.exists():
        summary = json.loads(hb.OUT.read_text(encoding="utf-8"))
        summary["dataReproducibility"] = {
            "version": VERSION,
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
        hb.OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
