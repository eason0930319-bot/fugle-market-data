from __future__ import annotations

import json, os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from backtest_data import add_features, adjusted_prices, download_history, normalize_frame, security_master, yf_download
from backtest_model import grouped_stats, perf_stats, rnd, build_day

VERSION = "historical-backtest-v1.1"
TZ = timezone(timedelta(hours=8))
START = os.getenv("BACKTEST_START", "2024-01-01")
END = os.getenv("BACKTEST_END", "").strip()
MIN_VALUE = float(os.getenv("BACKTEST_MIN_TRADE_VALUE", "50000000"))
BATCH = int(os.getenv("BACKTEST_DOWNLOAD_BATCH", "80"))
TOP = int(os.getenv("BACKTEST_TOP_LIMIT", "50"))
HIST_TOP = int(os.getenv("BACKTEST_HISTORY_LIMIT", "40"))
MIN_GROUP = int(os.getenv("BACKTEST_MIN_GROUP_SAMPLE", "30"))
OUT = Path("data/backtest-summary.json")
SAMPLE = Path("data/backtest-signal-sample.json")


def benchmark(start, end):
    raw = yf_download(["0050.TW"], start, end)
    f = normalize_frame(raw, "0050.TW")
    if f.empty:
        raise RuntimeError("0050 benchmark unavailable")
    f = f.reset_index().rename(columns={f.index.name or "index": "date", "Date": "date"})
    if "date" not in f:
        f = f.rename(columns={f.columns[0]: "date"})
    for k, v in {"symbol": "0050", "name": "0050", "market": "BENCH", "industry": "BENCH"}.items():
        f[k] = v
    return add_features(adjusted_prices(f))


def stats_by_split(df, group_key, explode=False):
    out = {}
    for split, g in df.groupby("split"):
        out[str(split)] = grouped_stats(g, group_key, MIN_GROUP, explode)
    return out


def rule_tags(row):
    flags = set(row.extensionFlags if isinstance(row.extensionFlags, list) else [])
    screen = set(row.screenerSignals if isinstance(row.screenerSignals, list) else [])
    tags = []
    if "STRONG_CONTINUATION" in flags and "EXHAUSTION_RISK" not in flags:
        tags.append("HQ_CONTINUATION")
    if row.sectorBucket == "STRONG_70_PLUS" and row.closePosition is not None and row.closePosition >= .70 and ("MOMENTUM" in screen or "SECTOR_LEADER" in screen):
        tags.append("HOT_SECTOR_MOMENTUM")
    if row.distanceFromMA20Pct is not None and row.distanceFromMA20Pct >= 25:
        tags.append("EXTREME_STRETCH_GE25MA")
    if row.closePosition is not None and row.closePosition < .45:
        tags.append("WEAK_CLOSE_EXTENDED")
    if row.gapPct is not None and row.gapPct >= 3 and row.rvol20 is not None and row.rvol20 >= 1.5:
        tags.append("GAP_PLUS_HIGH_VOLUME")
    if row.rvol20 is not None and 1.15 <= row.rvol20 < 2.5 and row.closePosition is not None and row.closePosition >= .70:
        tags.append("CONTROLLED_VOLUME_STRONG_CLOSE")
    return tags or ["OTHER"]


def consistency_table(ext):
    rows = []
    dimensions = ["extensionSubtype", "sectorBucket", "rvolBucket", "distMABucket", "marketRegime"]
    for dim in dimensions:
        values = sorted(set(ext[dim].dropna().astype(str)))
        for value in values:
            item = {"dimension": dim, "value": value}
            ok = True
            for split in ["TRAIN_2024", "VALIDATION_2025", "TEST_2026_PLUS"]:
                g = ext[(ext[dim].astype(str) == value) & (ext.split == split)]
                s = perf_stats(g, 5)
                item[split] = s
            tr = item["TRAIN_2024"]
            va = item["VALIDATION_2025"]
            item["trainValidationSameSign"] = bool(
                tr["count"] >= MIN_GROUP
                and va["count"] >= MIN_GROUP
                and tr["avgReturnPct"] is not None
                and va["avgReturnPct"] is not None
                and ((tr["avgReturnPct"] > 0 and va["avgReturnPct"] > 0) or (tr["avgReturnPct"] < 0 and va["avgReturnPct"] < 0))
            )
            rows.append(item)
    return rows


def main():
    start = pd.Timestamp(START).normalize()
    end = pd.Timestamp(END).normalize() if END else pd.Timestamp(datetime.now(TZ).date())
    if end < start:
        raise RuntimeError("BACKTEST_END before BACKTEST_START")
    fetch_start = (start - pd.Timedelta(days=120)).date().isoformat()
    fetch_end = (end + pd.Timedelta(days=7)).date().isoformat()

    tse = security_master(2, "TSE")
    otc = security_master(4, "OTC")
    items = list(tse.values()) + list(otc.values())
    print(f"current universe {len(items)}; TSE={len(tse)} OTC={len(otc)}")

    raw, failed = download_history(items, fetch_start, fetch_end, BATCH)
    data = add_features(adjusted_prices(raw))
    actual = data[["market", "symbol"]].drop_duplicates()
    coverage = len(actual) / len(items)
    if coverage < .70:
        raise RuntimeError(f"historical ticker coverage too low: {coverage:.1%}")

    b = benchmark(fetch_start, fetch_end)
    bm = {r.date.date().isoformat(): r for _, r in b.iterrows()}
    target = data[(data.date >= start) & (data.date <= end) & data.prev.notna()].copy()

    signals = []
    days = []
    sessions = sorted(target.date.unique())
    for i, dt in enumerate(sessions, 1):
        if i == 1 or i % 25 == 0 or i == len(sessions):
            print(f"backtest {i}/{len(sessions)} {pd.Timestamp(dt).date()}")
        key = pd.Timestamp(dt).date().isoformat()
        br = bm.get(key)
        b5 = np.nan if br is None else br.ret5
        b20 = np.nan if br is None else br.ret20
        ss, st = build_day(target[target.date == dt], b5, b20, MIN_VALUE, TOP, HIST_TOP)
        if st:
            signals.extend(ss)
            days.append(st)

    if not signals:
        raise RuntimeError("backtest produced no signals")

    f = pd.DataFrame(signals)
    d = pd.DataFrame(days)
    dv = f[f.source == "DISCOVERY_V2"].copy()
    selected = f[f.source.isin(["DISCOVERY_V2", "SCREENER"])].copy()
    ext = f[f.source == "EXTENSION_RESEARCH"].copy()
    if ext.empty:
        raise RuntimeError("V1.1 extension research produced no records")

    thresholds = {}
    for sp, g in dv.groupby("split"):
        thresholds[sp] = {}
        for t in [50, 55, 60, 65, 70, 75, 80, 85]:
            x = g[g.score >= t]
            thresholds[sp][str(t)] = {"recordCount": len(x), "1d": perf_stats(x, 1), "3d": perf_stats(x, 3), "5d": perf_stats(x, 5)}

    ext["subtypeRegime"] = ext.extensionSubtype.astype(str) + "|" + ext.marketRegime.astype(str)
    ext["subtypeSector"] = ext.extensionSubtype.astype(str) + "|" + ext.sectorBucket.astype(str)
    ext["subtypeRvol"] = ext.extensionSubtype.astype(str) + "|" + ext.rvolBucket.astype(str)
    ext["subtypeDistMA"] = ext.extensionSubtype.astype(str) + "|" + ext.distMABucket.astype(str)
    ext["ruleTags"] = ext.apply(rule_tags, axis=1)

    extension_research = {
        "recordCount": int(len(ext)),
        "overall": {"1d": perf_stats(ext, 1), "3d": perf_stats(ext, 3), "5d": perf_stats(ext, 5)},
        "bySubtype": grouped_stats(ext, "extensionSubtype", MIN_GROUP),
        "byFlag": grouped_stats(ext, "extensionFlags", MIN_GROUP, True),
        "bySplit": grouped_stats(ext, "split", MIN_GROUP),
        "bySubtypeAndSplit": stats_by_split(ext, "extensionSubtype"),
        "byMarketRegime": grouped_stats(ext, "marketRegime", MIN_GROUP),
        "bySectorBucket": grouped_stats(ext, "sectorBucket", MIN_GROUP),
        "byRvolBucket": grouped_stats(ext, "rvolBucket", MIN_GROUP),
        "byDistanceFromMA20Bucket": grouped_stats(ext, "distMABucket", MIN_GROUP),
        "byChangeBucket": grouped_stats(ext, "changeBucket", MIN_GROUP),
        "byScreenerSignal": grouped_stats(ext[ext.screenerSignals.map(bool)], "screenerSignals", MIN_GROUP, True),
        "diagnosticRules": grouped_stats(ext, "ruleTags", MIN_GROUP, True),
        "diagnosticRulesBySplit": stats_by_split(ext, "ruleTags", True),
        "intersections": {
            "subtypeRegime": grouped_stats(ext, "subtypeRegime", MIN_GROUP),
            "subtypeSector": grouped_stats(ext, "subtypeSector", MIN_GROUP),
            "subtypeRvol": grouped_stats(ext, "subtypeRvol", MIN_GROUP),
            "subtypeDistanceMA": grouped_stats(ext, "subtypeDistMA", MIN_GROUP),
        },
        "trainValidationConsistency": consistency_table(ext),
        "taxonomy": {
            "STRONG_CONTINUATION": "Close position >=70%, RVOL20 >=1.15, positive RS20, sector strength >=65, and positive MA20 slope or return20 >5%.",
            "EXHAUSTION_RISK": "Weak close (<=40%) or large upper wick/range with weak close.",
            "GAP_EXTENSION": "Opening gap >=3%.",
            "HIGH_VOLUME_EXTENSION": "RVOL20 >=1.5.",
            "OTHER_EXTENSION": "Extended by daily gain or MA20 distance without the above primary pattern.",
            "note": "Flags overlap. extensionSubtype is a priority label (exhaustion > strong continuation > gap > high-volume > other); use byFlag for orthogonal evidence.",
        },
    }

    summary = {
        "ok": True,
        "schemaVersion": 1,
        "version": VERSION,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "period": {
            "requestedStart": START,
            "effectiveStart": days[0]["date"],
            "effectiveEnd": days[-1]["date"],
            "splits": {"TRAIN_2024": "2024", "VALIDATION_2025": "2025", "TEST_2026_PLUS": "2026+; reporting only"},
        },
        "coverage": {
            "currentUniverse": len(items),
            "TSE": len(tse),
            "OTC": len(otc),
            "tickersWithData": len(actual),
            "coverageRatio": rnd(coverage, 4),
            "failedYahooCount": len(failed),
            "failedYahooSample": failed[:50],
            "rawRows": len(raw),
            "signalCount": len(signals),
            "extensionResearchCount": len(ext),
            "minTradeValue": int(MIN_VALUE),
        },
        "dailyCoverage": {
            "sessionCount": len(d),
            "medianStocks": rnd(d.stockCount.median(), 0),
            "medianEligible": rnd(d.eligibleCount.median(), 0),
            "marketRegimeCounts": d.marketRegime.value_counts().to_dict(),
        },
        "methodology": {
            "purpose": "Historical calibration of mechanical Screener/Discovery setup/regime/sector skeleton, with V1.1 dedicated EXTENDED taxonomy; NOT historical full V3.3.",
            "walkForward": "2024 train, 2025 validation, 2026+ untouched test reporting",
            "priceBasis": "Yahoo OHLC scaled by Adj Close/Close; unadjusted volume",
            "rrProxy": "Exploratory only: nearest MA20/prior10Low - 0.25 ATR vs prior60High; never production V3.3 R/R",
            "extensionResearch": "All liquid stocks meeting existing EXTENDED definition, not capped by Discovery's daily extended quota.",
        },
        "knownBiases": [
            "SURVIVORSHIP_BIAS: current security master; delisted historical names absent",
            "CURRENT_INDUSTRY_LABELS reused historically",
            "Yahoo/yfinance is not official archival bulk data",
            "No historical fundamentals/catalysts/valuation/flows",
            "Not a backfill of final V3.3 Opportunity or ChatGPT A/B/C decisions",
            "Extension taxonomy thresholds are hypothesis-driven diagnostics, not optimized production parameters",
        ],
        "overall": {src: {"recordCount": len(g), "1d": perf_stats(g, 1), "3d": perf_stats(g, 3), "5d": perf_stats(g, 5)} for src, g in f.groupby("source")},
        "bySplit": grouped_stats(selected, "split", MIN_GROUP),
        "bySetup": grouped_stats(selected, "setup", MIN_GROUP),
        "byMarketRegime": grouped_stats(selected, "marketRegime", MIN_GROUP),
        "byDiscoveryTier": grouped_stats(dv, "tier", MIN_GROUP),
        "byScreenerSignal": grouped_stats(f[f.source == "SCREENER"], "signals", MIN_GROUP, True),
        "discoveryScoreThresholdsBySplit": thresholds,
        "extensionResearch": extension_research,
        "calibrationPolicy": {
            "automaticProductionChanges": False,
            "rule": "Only consider changes consistent in 2024 train and 2025 validation; never tune on 2026+ test; confirm with forward Decision Ledger.",
        },
    }

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    sample = {
        "schemaVersion": 1,
        "version": VERSION,
        "generatedAt": summary["generatedAt"],
        "first": f.head(30).to_dict("records"),
        "latest": f.tail(50).to_dict("records"),
        "extensionExamples": ext.groupby("extensionSubtype", group_keys=False).head(12).to_dict("records"),
    }
    SAMPLE.write_text(json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "version": VERSION,
        "period": summary["period"],
        "coverage": summary["coverage"],
        "extensionResearch": {
            "overall": extension_research["overall"],
            "bySubtype": extension_research["bySubtype"],
            "diagnosticRules": extension_research["diagnosticRules"],
        },
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
