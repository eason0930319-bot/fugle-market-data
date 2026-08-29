from __future__ import annotations

import json
import math
import os
import re
import statistics
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests


VERSION = "performance-tracker-v1.0"
TAIPEI_TZ = timezone(timedelta(hours=8))

TWSE_DAILY = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_DAILY = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
TWSE_HIST = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"

SCREENER_PATH = Path("data/tw-screener.json")
DISCOVERY_PATH = Path("data/discovery-scan.json")
LEDGER_PATH = Path("data/performance-ledger.json")
SUMMARY_PATH = Path("data/performance-summary.json")

TRACK_LIMIT = int(os.getenv("PERFORMANCE_TRACK_LIMIT", "50"))
MIN_CALIBRATION_SAMPLE = int(os.getenv("PERFORMANCE_MIN_SAMPLE", "100"))
RETENTION_DAYS = int(os.getenv("PERFORMANCE_RETENTION_DAYS", "365"))
BENCHMARK = os.getenv("BENCHMARK_SYMBOL", "0050")

S = requests.Session()
S.headers.update(
    {
        "User-Agent": "Mozilla/5.0 fugle-market-data-performance/1.0",
        "Accept": "application/json,text/plain;q=0.9,*/*;q=0.8",
    }
)


def clean(v) -> str:
    return "" if v is None else str(v).strip()


def num(v):
    if v is None:
        return None

    if isinstance(v, (int, float)):
        if isinstance(v, float) and math.isnan(v):
            return None
        return float(v)

    text = (
        str(v)
        .strip()
        .replace(",", "")
        .replace("%", "")
        .replace("＋", "+")
        .replace("－", "-")
        .replace("−", "-")
    )

    if text in {"", "-", "--", "---", "N/A", "null", "None"}:
        return None

    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None


def rnd(v, digits=3):
    return None if v is None else round(float(v), digits)


def roc_date(v):
    if v is None:
        return None

    digits = re.sub(r"\D", "", str(v))

    try:
        if len(digits) == 8:
            return date(
                int(digits[:4]),
                int(digits[4:6]),
                int(digits[6:8]),
            ).isoformat()

        if len(digits) == 7:
            return date(
                int(digits[:3]) + 1911,
                int(digits[3:5]),
                int(digits[5:7]),
            ).isoformat()
    except ValueError:
        return None

    return None


def get_json(url, params=None, attempts=3):
    last = None

    for i in range(attempts):
        try:
            response = S.get(
                url,
                params=params,
                timeout=35,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            return response.json()

        except Exception as exc:
            last = exc
            time.sleep(0.8 * (i + 1))

    raise RuntimeError(f"GET failed {url}: {last}")


def load_json(path: Path, default):
    if not path.exists():
        return default

    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def normalize_twse(rows):
    output = {}

    for row in rows:
        symbol = clean(row.get("Code"))

        if not (len(symbol) == 4 and symbol.isdigit()):
            continue

        session = roc_date(row.get("Date"))
        close = num(row.get("ClosingPrice"))
        high = num(row.get("HighestPrice"))
        low = num(row.get("LowestPrice"))

        if not session or close is None:
            continue

        output[("TSE", symbol)] = {
            "market": "TSE",
            "symbol": symbol,
            "date": session,
            "close": close,
            "high": high,
            "low": low,
        }

    return output


def normalize_tpex(rows):
    output = {}

    for row in rows:
        symbol = clean(row.get("SecuritiesCompanyCode"))

        if not (len(symbol) == 4 and symbol.isdigit()):
            continue

        session = roc_date(row.get("Date"))
        close = num(row.get("Close"))
        high = num(row.get("High"))
        low = num(row.get("Low"))

        if not session or close is None:
            continue

        output[("OTC", symbol)] = {
            "market": "OTC",
            "symbol": symbol,
            "date": session,
            "close": close,
            "high": high,
            "low": low,
        }

    return output


def dominant_date(quotes):
    counts = defaultdict(int)

    for quote in quotes.values():
        session = quote.get("date")
        if session:
            counts[session] += 1

    if not counts:
        return None

    return max(counts.items(), key=lambda item: item[1])[0]


def month_pairs(session: date):
    pairs = [(session.year, session.month)]

    if session.month == 1:
        pairs.append((session.year - 1, 12))
    else:
        pairs.append((session.year, session.month - 1))

    return list(reversed(pairs))


def benchmark_sessions(current_session: str):
    target = date.fromisoformat(current_session)
    sessions = set()

    for year, month in month_pairs(target):
        payload = get_json(
            TWSE_HIST,
            {
                "response": "json",
                "stockNo": BENCHMARK,
                "date": f"{year:04d}{month:02d}01",
            },
        )

        if not isinstance(payload, dict) or payload.get("stat") != "OK":
            continue

        for row in payload.get("data", []):
            if not isinstance(row, list) or not row:
                continue

            session = roc_date(row[0])

            if session and session <= current_session:
                sessions.add(session)

    return sorted(sessions)


def trading_age(signal_date: str, current_date: str, sessions):
    return sum(
        1
        for session in sessions
        if signal_date < session <= current_date
    )


def pct_change(value, base):
    if value is None or base in (None, 0):
        return None

    return (value / base - 1) * 100


def market_regime(screener):
    all_breadth = (screener.get("breadth") or {}).get("ALL") or {}

    advance = num(all_breadth.get("advanceRatio"))
    median_change = num(all_breadth.get("medianChangePct"))

    if advance is None or median_change is None:
        return "unknown"

    if advance >= 0.55 and median_change > 0:
        return "bull"

    if advance <= 0.40 and median_change < 0:
        return "bear"

    return "neutral"


def score_bucket(score):
    score = num(score)

    if score is None:
        return "unknown"

    if score >= 95:
        return "95-100"

    if score >= 90:
        return "90-94.99"

    if score >= 80:
        return "80-89.99"

    return "<80"


def discovery_maps(discovery, market_date):
    if not isinstance(discovery, dict):
        return {}, {}

    if discovery.get("marketDate") != market_date:
        return {}, {}

    history_map = {}

    for row in discovery.get("topDiscovery") or []:
        symbol = clean(row.get("symbol"))
        market = clean(row.get("market"))

        if symbol and market:
            history_map[(market, symbol)] = row

    dynamic_map = {}

    for row in discovery.get("dynamicPreview") or []:
        symbol = clean(row.get("symbol"))
        market = clean(row.get("market"))

        if symbol and market:
            dynamic_map[(market, symbol)] = row

    return history_map, dynamic_map


def snapshot_milestone(record, horizon, quote):
    entry = num(record.get("entryClose"))

    if entry in (None, 0):
        return

    outcomes = record.setdefault("outcomes", {})

    outcomes[f"close{horizon}d"] = rnd(quote.get("close"), 4)
    outcomes[f"ret{horizon}dPct"] = rnd(
        pct_change(quote.get("close"), entry)
    )

    running_high = num(record.get("runningMaxHigh"))
    running_low = num(record.get("runningMinLow"))

    outcomes[f"mfe{horizon}dPct"] = rnd(
        pct_change(running_high, entry)
    )
    outcomes[f"mae{horizon}dPct"] = rnd(
        pct_change(running_low, entry)
    )


def update_existing_records(records, quotes, current_date, sessions):
    updated = 0
    unavailable = 0

    for record in records:
        signal_date = record.get("signalDate")

        if not signal_date or signal_date >= current_date:
            continue

        age = trading_age(
            signal_date,
            current_date,
            sessions,
        )

        if age <= 0:
            continue

        if age > 5 and record.get("status") == "complete":
            continue

        key = (
            clean(record.get("market")),
            clean(record.get("symbol")),
        )

        quote = quotes.get(key)

        if not quote or quote.get("date") != current_date:
            unavailable += 1
            continue

        entry = num(record.get("entryClose"))

        if entry in (None, 0):
            continue

        high = num(quote.get("high"))
        low = num(quote.get("low"))

        if high is not None:
            previous_high = num(record.get("runningMaxHigh"))

            record["runningMaxHigh"] = rnd(
                high
                if previous_high is None
                else max(previous_high, high),
                4,
            )

        if low is not None:
            previous_low = num(record.get("runningMinLow"))

            record["runningMinLow"] = rnd(
                low
                if previous_low is None
                else min(previous_low, low),
                4,
            )

        observed_dates = record.setdefault("observedDates", [])

        if current_date not in observed_dates:
            observed_dates.append(current_date)
            observed_dates.sort()

        record["lastObservedDate"] = current_date
        record["observedTradingDays"] = age
        record["mfeMaeCoverageDays"] = len(observed_dates)

        outcomes = record.setdefault("outcomes", {})

        for horizon in (1, 3, 5):
            key_name = f"ret{horizon}dPct"

            if age == horizon and outcomes.get(key_name) is None:
                snapshot_milestone(record, horizon, quote)

        if age >= 5:
            record["status"] = "complete"

        updated += 1

    return updated, unavailable


def new_records_from_screener(screener, discovery, existing_ids):
    market_date = screener["marketDate"]
    history_map, dynamic_map = discovery_maps(
        discovery,
        market_date,
    )

    sector_map = {
        item.get("industry"): item
        for item in screener.get("sectorRanking") or []
        if item.get("industry")
    }

    regime = market_regime(screener)
    output = []

    for rank, candidate in enumerate(
        (screener.get("topCandidates") or [])[:TRACK_LIMIT],
        start=1,
    ):
        symbol = clean(candidate.get("symbol"))
        market = clean(candidate.get("market"))

        if not symbol or market not in {"TSE", "OTC"}:
            continue

        record_id = f"{market_date}:{market}:{symbol}"

        if record_id in existing_ids:
            continue

        entry_close = num(
            candidate.get("closePrice")
            if candidate.get("closePrice") is not None
            else candidate.get("close")
        )

        if entry_close in (None, 0):
            continue

        history = history_map.get((market, symbol)) or {}
        dynamic = dynamic_map.get((market, symbol)) or {}

        discovery_v2 = history.get("discoveryV2") or {}
        history_features = history.get("historyFeatures") or {}

        industry = (
            clean(candidate.get("industry"))
            or clean(history.get("industry"))
            or "UNKNOWN"
        )

        sector = sector_map.get(industry) or {}

        output.append(
            {
                "id": record_id,
                "signalDate": market_date,
                "rank": rank,
                "symbol": symbol,
                "name": clean(candidate.get("name")),
                "market": market,
                "industry": industry,
                "entryClose": rnd(entry_close, 4),
                "discoveryScore": rnd(
                    num(candidate.get("discoveryScore")),
                    2,
                ),
                "discoveryScoreBucket": score_bucket(
                    candidate.get("discoveryScore")
                ),
                "signals": list(candidate.get("signals") or []),
                "setup": history.get("setup"),
                "historyDiscoveryScore": rnd(
                    num(discovery_v2.get("score")),
                    2,
                ),
                "dynamicTier": dynamic.get("tier"),
                "marketRegime": regime,
                "sectorStrengthScore": rnd(
                    num(sector.get("sectorStrengthScore")),
                    2,
                ),
                "signalContext": {
                    "changePercent": rnd(
                        num(candidate.get("changePercent"))
                    ),
                    "relativeStrength1dPct": rnd(
                        num(candidate.get("relativeStrength1dPct"))
                    ),
                    "liquidityPercentile": rnd(
                        num(candidate.get("liquidityPercentile")),
                        2,
                    ),
                    "return5": rnd(
                        num(history_features.get("return5"))
                    ),
                    "return20": rnd(
                        num(history_features.get("return20"))
                    ),
                    "rs5": rnd(
                        num(history_features.get("rs5"))
                    ),
                    "rs20": rnd(
                        num(history_features.get("rs20"))
                    ),
                    "rvol20": rnd(
                        num(history_features.get("rvol20"))
                    ),
                    "distanceFrom20DHighPct": rnd(
                        num(
                            history_features.get(
                                "distanceFrom20DHighPct"
                            )
                        )
                    ),
                    "distanceFromMA20Pct": rnd(
                        num(
                            history_features.get(
                                "distanceFromMA20Pct"
                            )
                        )
                    ),
                },
                "source": {
                    "screenerVersion": screener.get("version"),
                    "discoveryVersion": discovery.get("version")
                    if isinstance(discovery, dict)
                    else None,
                },
                "status": "open",
                "runningMaxHigh": None,
                "runningMinLow": None,
                "lastObservedDate": None,
                "observedTradingDays": 0,
                "mfeMaeCoverageDays": 0,
                "observedDates": [],
                "outcomes": {
                    "close1d": None,
                    "ret1dPct": None,
                    "mfe1dPct": None,
                    "mae1dPct": None,
                    "close3d": None,
                    "ret3dPct": None,
                    "mfe3dPct": None,
                    "mae3dPct": None,
                    "close5d": None,
                    "ret5dPct": None,
                    "mfe5dPct": None,
                    "mae5dPct": None,
                },
            }
        )

    return output


def metric_stats(records, horizon):
    return_key = f"ret{horizon}dPct"
    mfe_key = f"mfe{horizon}dPct"
    mae_key = f"mae{horizon}dPct"

    rows = [
        record
        for record in records
        if num((record.get("outcomes") or {}).get(return_key))
        is not None
    ]

    returns = [
        float(record["outcomes"][return_key])
        for record in rows
    ]

    mfes = [
        float(record["outcomes"][mfe_key])
        for record in rows
        if num(record["outcomes"].get(mfe_key)) is not None
    ]

    maes = [
        float(record["outcomes"][mae_key])
        for record in rows
        if num(record["outcomes"].get(mae_key)) is not None
    ]

    if not rows:
        return {
            "count": 0,
            "avgReturnPct": None,
            "medianReturnPct": None,
            "winRate": None,
            "avgMfePct": None,
            "avgMaePct": None,
        }

    return {
        "count": len(rows),
        "avgReturnPct": rnd(statistics.fmean(returns)),
        "medianReturnPct": rnd(statistics.median(returns)),
        "winRate": rnd(
            sum(value > 0 for value in returns) / len(returns),
            4,
        ),
        "avgMfePct": rnd(statistics.fmean(mfes))
        if mfes
        else None,
        "avgMaePct": rnd(statistics.fmean(maes))
        if maes
        else None,
    }


def group_summary(records, key_fn, minimum_display_count=1):
    groups = defaultdict(list)

    for record in records:
        keys = key_fn(record)

        if keys is None:
            continue

        if not isinstance(keys, (list, tuple, set)):
            keys = [keys]

        for key in keys:
            key = clean(key)

            if key:
                groups[key].append(record)

    output = {}

    for key, members in groups.items():
        h1 = metric_stats(members, 1)
        h3 = metric_stats(members, 3)
        h5 = metric_stats(members, 5)

        if max(h1["count"], h3["count"], h5["count"]) < minimum_display_count:
            continue

        output[key] = {
            "recordCount": len(members),
            "1d": h1,
            "3d": h3,
            "5d": h5,
        }

    return dict(
        sorted(
            output.items(),
            key=lambda item: (
                item[1]["5d"]["count"],
                item[1]["3d"]["count"],
                item[1]["1d"]["count"],
            ),
            reverse=True,
        )
    )


def calibration_observations(summary):
    matured = summary["overall"]["5d"]["count"]

    if matured < MIN_CALIBRATION_SAMPLE:
        return [
            (
                f"5日成熟樣本僅 {matured} 筆；"
                f"至少累積 {MIN_CALIBRATION_SAMPLE} 筆前，"
                "不自動調整任何選股門檻。"
            ),
            (
                "目前只評估 Discovery/Screener 層；"
                "不把 Discovery 分數誤當成 V3.2 Opportunity 分數。"
            ),
        ]

    observations = [
        (
            f"5日成熟樣本已達 {matured} 筆，可開始做 Discovery 層校準；"
            "仍只提出觀察，不自動修改正式門檻。"
        )
    ]

    setup_rows = []

    for key, item in summary.get("bySetup", {}).items():
        stats = item["5d"]

        if stats["count"] >= 20 and stats["avgReturnPct"] is not None:
            setup_rows.append(
                (
                    stats["avgReturnPct"],
                    stats["winRate"],
                    stats["count"],
                    key,
                )
            )

    if setup_rows:
        setup_rows.sort(reverse=True)
        best = setup_rows[0]
        worst = setup_rows[-1]

        observations.append(
            (
                f"目前5日表現最佳 setup：{best[3]} "
                f"(n={best[2]}, avg={best[0]:.2f}%, "
                f"win={best[1]:.1%})。"
            )
        )

        if worst[3] != best[3]:
            observations.append(
                (
                    f"目前5日表現最弱 setup：{worst[3]} "
                    f"(n={worst[2]}, avg={worst[0]:.2f}%, "
                    f"win={worst[1]:.1%})。"
                )
            )

    return observations


def build_summary(records, current_date):
    overall = {
        "1d": metric_stats(records, 1),
        "3d": metric_stats(records, 3),
        "5d": metric_stats(records, 5),
    }

    summary = {
        "schemaVersion": 1,
        "version": VERSION,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "marketDate": current_date,
        "scope": (
            "Tracks automatic whole-market Discovery/Screener candidates. "
            "This is not yet the V3.2 Opportunity/A-B-C decision ledger."
        ),
        "recordCount": len(records),
        "openRecordCount": sum(
            record.get("status") != "complete"
            for record in records
        ),
        "completeRecordCount": sum(
            record.get("status") == "complete"
            for record in records
        ),
        "overall": overall,
        "bySignal": group_summary(
            records,
            lambda record: record.get("signals") or [],
        ),
        "bySetup": group_summary(
            records,
            lambda record: record.get("setup"),
        ),
        "byScoreBucket": group_summary(
            records,
            lambda record: record.get("discoveryScoreBucket"),
        ),
        "byMarketRegime": group_summary(
            records,
            lambda record: record.get("marketRegime"),
        ),
        "byIndustry": group_summary(
            records,
            lambda record: record.get("industry"),
            minimum_display_count=3,
        ),
        "calibrationReadiness": {
            "minimum5dSamples": MIN_CALIBRATION_SAMPLE,
            "actual5dSamples": overall["5d"]["count"],
            "ready": (
                overall["5d"]["count"]
                >= MIN_CALIBRATION_SAMPLE
            ),
            "rule": (
                "Do not automatically change production thresholds. "
                "Use statistics as secondary calibration evidence only."
            ),
        },
    }

    summary["observations"] = calibration_observations(summary)

    return summary


def main():
    if not SCREENER_PATH.exists():
        raise RuntimeError(
            "data/tw-screener.json not found. Run tw_screener.py first."
        )

    screener = load_json(SCREENER_PATH, {})
    discovery = load_json(DISCOVERY_PATH, {})

    if not screener.get("ok"):
        raise RuntimeError("tw-screener.json is not healthy")

    market_date = screener.get("marketDate")

    if not market_date:
        raise RuntimeError("tw-screener.json has no marketDate")

    gate = screener.get("marketDateGate") or {}

    if not gate.get("ok"):
        raise RuntimeError(
            f"Screener market-date gate is not ready: {gate}"
        )

    twse_raw, tpex_raw = (
        get_json(TWSE_DAILY),
        get_json(TPEX_DAILY),
    )

    twse_quotes = normalize_twse(twse_raw)
    tpex_quotes = normalize_tpex(tpex_raw)

    twse_date = dominant_date(twse_quotes)
    tpex_date = dominant_date(tpex_quotes)

    if twse_date != market_date or tpex_date != market_date:
        raise RuntimeError(
            (
                "Performance source-date mismatch: "
                f"screener={market_date}, "
                f"TSE={twse_date}, OTC={tpex_date}"
            )
        )

    quotes = {}
    quotes.update(twse_quotes)
    quotes.update(tpex_quotes)

    sessions = benchmark_sessions(market_date)

    if market_date not in sessions:
        raise RuntimeError(
            (
                f"Benchmark session calendar does not include {market_date}. "
                f"Latest={sessions[-5:] if sessions else []}"
            )
        )

    ledger = load_json(
        LEDGER_PATH,
        {
            "schemaVersion": 1,
            "version": VERSION,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "updatedAt": None,
            "lastMarketDate": None,
            "records": [],
        },
    )

    records = ledger.get("records") or []

    updated, unavailable = update_existing_records(
        records,
        quotes,
        market_date,
        sessions,
    )

    existing_ids = {
        record.get("id")
        for record in records
        if record.get("id")
    }

    new_records = new_records_from_screener(
        screener,
        discovery,
        existing_ids,
    )

    records.extend(new_records)

    cutoff = (
        date.fromisoformat(market_date)
        - timedelta(days=RETENTION_DAYS)
    ).isoformat()

    records = [
        record
        for record in records
        if (record.get("signalDate") or market_date) >= cutoff
    ]

    records.sort(
        key=lambda record: (
            record.get("signalDate") or "",
            -(int(record.get("rank") or 999)),
            record.get("symbol") or "",
        ),
        reverse=True,
    )

    now = datetime.now(timezone.utc).isoformat()

    ledger.update(
        {
            "schemaVersion": 1,
            "version": VERSION,
            "updatedAt": now,
            "lastMarketDate": market_date,
            "recordCount": len(records),
            "trackingPolicy": {
                "topCandidatesPerSession": TRACK_LIMIT,
                "horizonsTradingDays": [1, 3, 5],
                "retentionCalendarDays": RETENTION_DAYS,
                "entryReference": "signal-day close",
                "mfeMaeReference": (
                    "daily high/low observed by scheduled runs "
                    "after the signal date"
                ),
            },
            "records": records,
        }
    )

    summary = build_summary(records, market_date)
    summary["lastRun"] = {
        "updatedExistingRecords": updated,
        "quoteUnavailableRecords": unavailable,
        "newRecords": len(new_records),
        "trackedUniverseQuotes": len(quotes),
        "benchmarkSessionCount": len(sessions),
    }

    write_json(LEDGER_PATH, ledger)
    write_json(SUMMARY_PATH, summary)

    print(
        json.dumps(
            {
                "ok": True,
                "version": VERSION,
                "marketDate": market_date,
                "newRecords": len(new_records),
                "updatedExistingRecords": updated,
                "quoteUnavailableRecords": unavailable,
                "recordCount": len(records),
                "1dMatured": summary["overall"]["1d"]["count"],
                "3dMatured": summary["overall"]["3d"]["count"],
                "5dMatured": summary["overall"]["5d"]["count"],
                "calibrationReady": summary[
                    "calibrationReadiness"
                ]["ready"],
                "observations": summary["observations"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
