from __future__ import annotations

import argparse
import hashlib
import json
import platform
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

from backtest_data import adjusted_prices, download_history, normalize_frame, security_master, yf_download

SCHEMA_VERSION = 1


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _benchmark(start: str, end: str) -> pd.DataFrame:
    raw = yf_download(["0050.TW"], start, end)
    f = normalize_frame(raw, "0050.TW")
    if f.empty:
        raise RuntimeError("0050 benchmark unavailable while creating frozen snapshot")
    f = f.reset_index().rename(columns={f.index.name or "index": "date", "Date": "date"})
    if "date" not in f:
        f = f.rename(columns={f.columns[0]: "date"})
    for k, v in {"symbol": "0050", "name": "0050", "market": "BENCH", "industry": "BENCH"}.items():
        f[k] = v
    return adjusted_prices(f)


def create_snapshot(start: str, end: str, data_path: Path, manifest_path: Path, freeze_id: str, batch_size: int = 80):
    target_start = pd.Timestamp(start).normalize()
    target_end = pd.Timestamp(end).normalize()
    if target_end < target_start:
        raise RuntimeError("snapshot end before start")

    fetch_start = (target_start - pd.Timedelta(days=120)).date().isoformat()
    fetch_end = (target_end + pd.Timedelta(days=14)).date().isoformat()

    tse = security_master(2, "TSE")
    otc = security_master(4, "OTC")
    items = list(tse.values()) + list(otc.values())
    print(f"freeze universe {len(items)}; TSE={len(tse)} OTC={len(otc)}")

    raw, failed = download_history(items, fetch_start, fetch_end, batch_size)
    equity = adjusted_prices(raw)
    actual = equity[["market", "symbol"]].drop_duplicates()
    coverage = len(actual) / len(items)
    if coverage < 0.98:
        raise RuntimeError(f"snapshot ticker coverage too low: {coverage:.2%}")

    bench = _benchmark(fetch_start, fetch_end)
    frozen = pd.concat([equity, bench], ignore_index=True)
    frozen["date"] = pd.to_datetime(frozen["date"]).dt.tz_localize(None)
    frozen = frozen.drop_duplicates(["market", "symbol", "date"], keep="last")
    frozen = frozen.sort_values(["market", "symbol", "date"]).reset_index(drop=True)

    # The research horizon is intentionally immutable. Keep lookback rows before
    # target_start and any downloaded rows through target_end, but never silently
    # extend a freeze on a later run.
    frozen = frozen[frozen.date <= target_end].copy()

    data_path.parent.mkdir(parents=True, exist_ok=True)
    frozen.to_csv(
        data_path,
        index=False,
        date_format="%Y-%m-%d",
        float_format="%.10g",
        compression={"method": "gzip", "compresslevel": 6, "mtime": 0},
    )
    file_sha = _sha256(data_path)

    equity_frozen = frozen[frozen.market != "BENCH"]
    bench_frozen = frozen[frozen.market == "BENCH"]
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "freezeId": freeze_id,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "targetStart": target_start.date().isoformat(),
        "targetEnd": target_end.date().isoformat(),
        "fetchStart": fetch_start,
        "fetchEndRequested": fetch_end,
        "effectiveEquityStart": pd.Timestamp(equity_frozen.date.min()).date().isoformat(),
        "effectiveEquityEnd": pd.Timestamp(equity_frozen.date.max()).date().isoformat(),
        "effectiveBenchmarkEnd": pd.Timestamp(bench_frozen.date.max()).date().isoformat(),
        "universe": {
            "total": len(items),
            "TSE": len(tse),
            "OTC": len(otc),
            "tickersWithData": int(len(actual)),
            "coverageRatio": round(float(coverage), 6),
            "failedTickerCount": len(failed),
            "failedTickers": failed,
        },
        "rows": {
            "equity": int(len(equity_frozen)),
            "benchmark": int(len(bench_frozen)),
            "total": int(len(frozen)),
        },
        "columns": list(frozen.columns),
        "dataFile": data_path.name,
        "dataFileSha256": file_sha,
        "format": "csv.gz",
        "priceBasis": "Yahoo OHLC scaled by Adj Close/Close at snapshot creation; unadjusted volume",
        "software": {
            "python": platform.python_version(),
            "pandas": pd.__version__,
            "yfinance": yf.__version__,
        },
        "policy": {
            "immutable": True,
            "refreshRule": "Create a new freezeId/release asset to extend or replace history; never overwrite this freeze.",
            "purpose": "Make strategy and execution comparisons use identical historical rows and prices across runs.",
        },
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "freezeId": freeze_id, "sha256": file_sha, "rows": manifest["rows"], "universe": manifest["universe"]}, ensure_ascii=False))
    return manifest


def load_snapshot(data_path: str | Path, manifest_path: str | Path):
    data_path = Path(data_path)
    manifest_path = Path(manifest_path)
    if not data_path.exists() or not manifest_path.exists():
        raise RuntimeError("frozen snapshot data/manifest missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual_sha = _sha256(data_path)
    expected_sha = str(manifest.get("dataFileSha256") or "")
    if not expected_sha or actual_sha != expected_sha:
        raise RuntimeError(f"frozen snapshot SHA256 mismatch: expected={expected_sha} actual={actual_sha}")

    d = pd.read_csv(data_path, dtype={"symbol": str, "name": str, "market": str, "industry": str})
    d["date"] = pd.to_datetime(d["date"], errors="raise").dt.tz_localize(None)
    for c in ("open", "high", "low", "close", "volume", "tradeValue"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.sort_values(["market", "symbol", "date"]).reset_index(drop=True)
    if int(manifest.get("rows", {}).get("total", -1)) != len(d):
        raise RuntimeError("frozen snapshot row count mismatch")
    return d, manifest


def verify_snapshot(data_path: Path, manifest_path: Path):
    d, manifest = load_snapshot(data_path, manifest_path)
    eq = d[d.market != "BENCH"]
    bench = d[d.market == "BENCH"]
    if eq.empty or bench.empty:
        raise RuntimeError("snapshot missing equity or benchmark rows")
    if str(manifest.get("targetEnd")) < pd.Timestamp(eq.date.max()).date().isoformat():
        raise RuntimeError("snapshot contains rows beyond immutable targetEnd")
    print(json.dumps({
        "ok": True,
        "freezeId": manifest.get("freezeId"),
        "sha256": manifest.get("dataFileSha256"),
        "rows": len(d),
        "equityEnd": pd.Timestamp(eq.date.max()).date().isoformat(),
        "benchmarkEnd": pd.Timestamp(bench.date.max()).date().isoformat(),
    }, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create")
    c.add_argument("--start", required=True)
    c.add_argument("--end", required=True)
    c.add_argument("--data", required=True)
    c.add_argument("--manifest", required=True)
    c.add_argument("--freeze-id", required=True)
    c.add_argument("--batch-size", type=int, default=80)

    v = sub.add_parser("verify")
    v.add_argument("--data", required=True)
    v.add_argument("--manifest", required=True)

    args = p.parse_args()
    if args.cmd == "create":
        create_snapshot(args.start, args.end, Path(args.data), Path(args.manifest), args.freeze_id, args.batch_size)
    else:
        verify_snapshot(Path(args.data), Path(args.manifest))


if __name__ == "__main__":
    main()
