
from __future__ import annotations

import html
import json
import math
import os
import re
import statistics
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

VERSION = "tw-screener-github-v1.0"
TAIPEI_TZ = timezone(timedelta(hours=8))

TWSE_DAILY = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_DAILY = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
ISIN = "https://isin.twse.com.tw/isin/C_public.jsp"

MIN_TRADE_VALUE = int(os.getenv("SCREENER_MIN_TRADE_VALUE", "50000000"))
BUCKET_LIMIT = int(os.getenv("SCREENER_BUCKET_LIMIT", "30"))
TOP_LIMIT = int(os.getenv("SCREENER_TOP_LIMIT", "50"))
SECTOR_LIMIT = int(os.getenv("SCREENER_SECTOR_LIMIT", "20"))
STRICT_TODAY = os.getenv("SCREENER_STRICT_TODAY", "1").lower() not in {"0", "false", "no"}

S = requests.Session()
S.headers.update(
    {
        "User-Agent": "Mozilla/5.0 fugle-market-data-screener/1.0",
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
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

    m = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(m.group()) if m else None


def rnd(v, n=3):
    return None if v is None else round(float(v), n)


def roc_date(v):
    if v is None:
        return None
    digits = re.sub(r"\D", "", str(v))
    try:
        if len(digits) == 8:
            return date(
                int(digits[:4]), int(digits[4:6]), int(digits[6:8])
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
            r = S.get(
                url,
                params=params,
                timeout=35,
                headers={"Accept": "application/json"},
            )
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last = exc
            time.sleep(0.8 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def get_text(url, params=None, attempts=3):
    last = None
    for i in range(attempts):
        try:
            r = S.get(
                url,
                params=params,
                timeout=35,
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
            r.raise_for_status()
            return r.content.decode("big5", errors="replace")
        except Exception as exc:
            last = exc
            time.sleep(0.8 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def security_master(str_mode: int, market: str):
    text = get_text(ISIN, {"strMode": str_mode})
    items = {}

    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", text, flags=re.I | re.S):
        cells = []
        for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.I | re.S):
            cell = html.unescape(re.sub(r"<[^>]+>", "", td))
            cells.append(re.sub(r"\s+", " ", cell).strip())

        if len(cells) < 6:
            continue

        match = re.match(r"^(\d{4})\s+(.+)$", cells[0])
        if not match:
            continue

        symbol = match.group(1)
        name = match.group(2).strip()
        industry = cells[4].strip() if len(cells) > 4 else ""
        cfi = cells[5].strip().upper() if len(cells) > 5 else ""

        # Ordinary equity-style CFI records only; removes ETFs, warrants, bonds etc.
        if not re.fullmatch(r"ES[A-Z0-9]{4}", cfi):
            continue

        items[symbol] = {
            "symbol": symbol,
            "name": name,
            "market": market,
            "industry": industry or "UNKNOWN",
            "cfi": cfi,
        }

    if not items:
        raise RuntimeError(f"{market} security master returned no common stocks")

    return items


def change_pct(close, change):
    if close is None or change is None:
        return None
    prev = close - change
    if not prev:
        return None
    return change / prev * 100


def norm_twse(x):
    symbol = clean(x.get("Code"))
    if not (len(symbol) == 4 and symbol.isdigit()):
        return None

    close = num(x.get("ClosingPrice"))
    change = num(x.get("Change"))
    volume = num(x.get("TradeVolume"))
    value = num(x.get("TradeValue"))

    return make_row(
        symbol=symbol,
        name=clean(x.get("Name")),
        market="TSE",
        session=roc_date(x.get("Date")),
        open_price=num(x.get("OpeningPrice")),
        high_price=num(x.get("HighestPrice")),
        low_price=num(x.get("LowestPrice")),
        close_price=close,
        change=change,
        trade_volume=volume,
        trade_value=value,
    )


def norm_tpex(x):
    symbol = clean(x.get("SecuritiesCompanyCode"))
    if not (len(symbol) == 4 and symbol.isdigit()):
        return None

    close = num(x.get("Close"))
    change = num(x.get("Change"))
    volume = num(x.get("TradingShares"))

    value = None
    for key in ("TransactionAmount", "TradingValue", "TradeValue", "TradeAmount"):
        candidate = num(x.get(key))
        if candidate is not None:
            value = candidate
            break

    if value is None and volume is not None and close is not None:
        value = volume * close

    return make_row(
        symbol=symbol,
        name=clean(x.get("CompanyName")),
        market="OTC",
        session=roc_date(x.get("Date")),
        open_price=num(x.get("Open")),
        high_price=num(x.get("High")),
        low_price=num(x.get("Low")),
        close_price=close,
        change=change,
        trade_volume=volume,
        trade_value=value,
    )


def make_row(
    *,
    symbol,
    name,
    market,
    session,
    open_price,
    high_price,
    low_price,
    close_price,
    change,
    trade_volume,
    trade_value,
):
    previous_close = (
        close_price - change
        if close_price is not None and change is not None
        else None
    )

    day_range = (
        high_price - low_price
        if high_price is not None and low_price is not None
        else None
    )

    close_position = (
        (close_price - low_price) / day_range
        if day_range and close_price is not None and low_price is not None
        else 0.5
    )

    low_drawdown = (
        (low_price / previous_close - 1) * 100
        if low_price is not None and previous_close
        else None
    )

    rebound = (
        (close_price / low_price - 1) * 100
        if close_price is not None and low_price
        else None
    )

    range_pct = (
        day_range / previous_close * 100
        if day_range is not None and previous_close
        else None
    )

    return {
        "symbol": symbol,
        "name": name,
        "market": market,
        "date": session,
        "openPrice": open_price,
        "highPrice": high_price,
        "lowPrice": low_price,
        "closePrice": close_price,
        "previousClose": rnd(previous_close, 4),
        "change": change,
        "changePercent": rnd(change_pct(close_price, change)),
        "tradeVolume": trade_volume,
        "tradeValue": trade_value,
        "closePosition": rnd(close_position, 4),
        "lowDrawdownPct": rnd(low_drawdown),
        "reboundFromLowPct": rnd(rebound),
        "rangePct": rnd(range_pct),
    }


def dominant_date(rows):
    counts = defaultdict(int)
    for row in rows:
        if row and row.get("date"):
            counts[row["date"]] += 1
    if not counts:
        return None, 0, {}
    session, count = max(counts.items(), key=lambda kv: kv[1])
    return session, count, dict(
        sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
    )


def median(values):
    data = [float(v) for v in values if v is not None]
    return statistics.median(data) if data else None


def average(values):
    data = [float(v) for v in values if v is not None]
    return statistics.fmean(data) if data else None


def percentile_map(rows, key):
    valid = sorted(
        (float(row[key]), i)
        for i, row in enumerate(rows)
        if row.get(key) is not None
    )
    output = {}
    if not valid:
        return output
    if len(valid) == 1:
        output[valid[0][1]] = 100.0
        return output

    for rank, (_, index) in enumerate(valid):
        output[index] = rank / (len(valid) - 1) * 100
    return output


def apply_percentiles(rows):
    mapping = [
        ("tradeValue", "liquidityPercentile"),
        ("changePercent", "changePercentile"),
        ("relativeStrength1dPct", "relativeStrengthPercentile"),
        ("reboundFromLowPct", "reboundPercentile"),
        ("rangePct", "rangePercentile"),
    ]
    for input_key, output_key in mapping:
        pm = percentile_map(rows, input_key)
        for i, row in enumerate(rows):
            row[output_key] = rnd(pm.get(i), 2)


def breadth(rows):
    changes = [r.get("changePercent") for r in rows if r.get("changePercent") is not None]
    advancers = sum(x > 0.001 for x in changes)
    decliners = sum(x < -0.001 for x in changes)
    unchanged = len(changes) - advancers - decliners
    return {
        "stockCount": len(rows),
        "validChangeCount": len(changes),
        "advancers": advancers,
        "decliners": decliners,
        "unchanged": unchanged,
        "advanceRatio": rnd(advancers / len(changes), 4) if changes else None,
        "medianChangePct": rnd(median(changes)),
        "averageChangePct": rnd(average(changes)),
        "totalTradeValue": int(sum((r.get("tradeValue") or 0) for r in rows)),
    }


def sector_ranking(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.get("industry") or "UNKNOWN"].append(row)

    sectors = []
    total_value = sum((r.get("tradeValue") or 0) for r in rows)

    for industry, members in grouped.items():
        if industry == "UNKNOWN" or len(members) < 3:
            continue

        changes = [
            r.get("changePercent")
            for r in members
            if r.get("changePercent") is not None
        ]
        if not changes:
            continue

        advancers = sum(x > 0.001 for x in changes)
        value = sum((r.get("tradeValue") or 0) for r in members)

        leaders = sorted(
            members,
            key=lambda r: (
                r.get("relativeStrength1dPct") or -999,
                r.get("tradeValue") or 0,
            ),
            reverse=True,
        )[:3]

        sectors.append(
            {
                "industry": industry,
                "stockCount": len(members),
                "medianChangePct": rnd(median(changes)),
                "averageChangePct": rnd(average(changes)),
                "advanceRatio": rnd(advancers / len(changes), 4),
                "tradeValue": int(value),
                "tradeValueShare": rnd(value / total_value, 5) if total_value else None,
                "tradeValuePerStock": value / len(members),
                "leaders": [
                    {
                        "symbol": r["symbol"],
                        "name": r["name"],
                        "market": r["market"],
                        "changePercent": r.get("changePercent"),
                        "relativeStrength1dPct": r.get("relativeStrength1dPct"),
                        "tradeValue": r.get("tradeValue"),
                    }
                    for r in leaders
                ],
            }
        )

    med_pct = percentile_map(sectors, "medianChangePct")
    turnover_pct = percentile_map(sectors, "tradeValuePerStock")

    for i, sector in enumerate(sectors):
        sector["medianReturnPercentile"] = rnd(med_pct.get(i), 2)
        sector["turnoverPerStockPercentile"] = rnd(turnover_pct.get(i), 2)
        sector["sectorStrengthScore"] = rnd(
            max(
                0,
                min(
                    100,
                    (med_pct.get(i, 0) * 0.55)
                    + ((sector["advanceRatio"] or 0) * 100 * 0.25)
                    + (turnover_pct.get(i, 0) * 0.20),
                ),
            ),
            1,
        )
        sector.pop("tradeValuePerStock", None)

    return sorted(
        sectors,
        key=lambda x: x.get("sectorStrengthScore") or 0,
        reverse=True,
    )


def compact(row):
    return {
        "symbol": row["symbol"],
        "name": row["name"],
        "market": row["market"],
        "industry": row.get("industry"),
        "date": row.get("date"),
        "openPrice": row.get("openPrice"),
        "highPrice": row.get("highPrice"),
        "lowPrice": row.get("lowPrice"),
        "closePrice": row.get("closePrice"),
        "previousClose": row.get("previousClose"),
        "changePercent": row.get("changePercent"),
        "tradeVolume": row.get("tradeVolume"),
        "tradeValue": row.get("tradeValue"),
        "closePosition": row.get("closePosition"),
        "lowDrawdownPct": row.get("lowDrawdownPct"),
        "reboundFromLowPct": row.get("reboundFromLowPct"),
        "relativeStrength1dPct": row.get("relativeStrength1dPct"),
        "liquidityPercentile": row.get("liquidityPercentile"),
    }


def candidate(row, signal, score):
    item = compact(row)
    item["signal"] = signal
    item["discoveryScore"] = rnd(max(0, min(100, score)), 1)
    return item


def momentum_score(row):
    return (
        (row.get("changePercentile") or 0) * 0.35
        + (row.get("relativeStrengthPercentile") or 0) * 0.25
        + (row.get("closePosition") or 0.5) * 100 * 0.20
        + (row.get("liquidityPercentile") or 0) * 0.20
    )


def recovery_score(row):
    return (
        (row.get("reboundPercentile") or 0) * 0.35
        + (row.get("closePosition") or 0.5) * 100 * 0.25
        + (row.get("liquidityPercentile") or 0) * 0.20
        + (row.get("relativeStrengthPercentile") or 0) * 0.20
    )


def sector_leader_score(row, sector):
    return (
        (sector.get("sectorStrengthScore") or 0) * 0.40
        + (row.get("relativeStrengthPercentile") or 0) * 0.25
        + (row.get("liquidityPercentile") or 0) * 0.20
        + (row.get("closePosition") or 0.5) * 100 * 0.15
    )


def merge_candidates(buckets):
    merged = {}
    for bucket in buckets:
        for item in bucket:
            symbol = item["symbol"]
            if symbol not in merged:
                merged[symbol] = {
                    **item,
                    "signals": [item["signal"]],
                }
            else:
                existing = merged[symbol]
                existing["discoveryScore"] = max(
                    existing["discoveryScore"], item["discoveryScore"]
                )
                if item["signal"] not in existing["signals"]:
                    existing["signals"].append(item["signal"])

    for item in merged.values():
        item.pop("signal", None)

    return sorted(
        merged.values(),
        key=lambda x: x.get("discoveryScore") or 0,
        reverse=True,
    )


def load_existing_discovery(market_date):
    path = Path("data/discovery-scan.json")
    if not path.exists():
        return {
            "available": False,
            "reason": "discovery-scan.json missing",
            "historyCandidates": [],
            "dynamicPreview": [],
        }

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "available": False,
            "reason": f"invalid discovery-scan.json: {exc}",
            "historyCandidates": [],
            "dynamicPreview": [],
        }

    same_date = payload.get("marketDate") == market_date
    base_ok = bool((payload.get("dataIntegrityGate") or {}).get("baseIntegrityOk"))

    if not same_date:
        return {
            "available": False,
            "reason": "discovery scan market date mismatch",
            "historyCandidates": [],
            "dynamicPreview": [],
            "discoveryMarketDate": payload.get("marketDate"),
        }

    return {
        "available": True,
        "baseIntegrityOk": base_ok,
        "version": payload.get("version"),
        "historyCandidates": payload.get("topDiscovery") or [],
        "dynamicPreview": payload.get("dynamicPreview") or [],
        "historyEnrichedCount": payload.get("historyEnrichedCount"),
        "historyErrorCount": payload.get("historyErrorCount"),
    }


def main():
    generated_at = datetime.now(timezone.utc)
    taipei_now = generated_at.astimezone(TAIPEI_TZ)
    expected_date = taipei_now.date().isoformat()

    twse_raw, tpex_raw = get_json(TWSE_DAILY), get_json(TPEX_DAILY)

    if not isinstance(twse_raw, list) or len(twse_raw) < 500:
        raise RuntimeError(f"TWSE daily row count abnormal: {len(twse_raw) if isinstance(twse_raw, list) else 'not-list'}")
    if not isinstance(tpex_raw, list) or len(tpex_raw) < 500:
        raise RuntimeError(f"TPEx daily row count abnormal: {len(tpex_raw) if isinstance(tpex_raw, list) else 'not-list'}")

    twse_rows = [r for x in twse_raw if (r := norm_twse(x))]
    tpex_rows = [r for x in tpex_raw if (r := norm_tpex(x))]

    twse_date, twse_date_count, twse_dates = dominant_date(twse_rows)
    tpex_date, tpex_date_count, tpex_dates = dominant_date(tpex_rows)

    same_date = bool(twse_date and tpex_date and twse_date == tpex_date)
    current_date = bool(same_date and twse_date == expected_date)

    gate = {
        "ok": same_date and (current_date or not STRICT_TODAY),
        "strictToday": STRICT_TODAY,
        "expectedTaipeiDate": expected_date,
        "TSE": twse_date,
        "OTC": tpex_date,
        "sameDate": same_date,
        "currentTaipeiDate": current_date,
        "TSEDateCoverage": {
            "dominantCount": twse_date_count,
            "rawCount": len(twse_rows),
            "distinctDates": twse_dates,
        },
        "OTCDateCoverage": {
            "dominantCount": tpex_date_count,
            "rawCount": len(tpex_rows),
            "distinctDates": tpex_dates,
        },
        "reasons": [],
    }

    if not same_date:
        gate["reasons"].append("TSE_OTC_DATE_MISMATCH")
    if twse_date != expected_date:
        gate["reasons"].append("TSE_NOT_CURRENT_TAIPEI_DATE")
    if tpex_date != expected_date:
        gate["reasons"].append("OTC_NOT_CURRENT_TAIPEI_DATE")

    if not gate["ok"]:
        print(json.dumps({"ok": False, "version": VERSION, "marketDateGate": gate}, ensure_ascii=False, indent=2))
        raise SystemExit(2)

    tse_master = security_master(2, "TSE")
    otc_master = security_master(4, "OTC")
    master = {("TSE", k): v for k, v in tse_master.items()}
    master.update({("OTC", k): v for k, v in otc_master.items()})

    raw_rows = twse_rows + tpex_rows
    common = []
    for row in raw_rows:
        meta = master.get((row["market"], row["symbol"]))
        if not meta:
            continue
        item = dict(row)
        item["name"] = meta.get("name") or item["name"]
        item["industry"] = meta.get("industry") or "UNKNOWN"
        item["cfi"] = meta.get("cfi")
        common.append(item)

    if len(common) < 1200:
        raise RuntimeError(f"Common-stock universe unexpectedly small: {len(common)}")

    by_market = {
        "TSE": [r for r in common if r["market"] == "TSE"],
        "OTC": [r for r in common if r["market"] == "OTC"],
    }

    market_median = {
        "TSE": median([r.get("changePercent") for r in by_market["TSE"]]),
        "OTC": median([r.get("changePercent") for r in by_market["OTC"]]),
        "ALL": median([r.get("changePercent") for r in common]),
    }

    for row in common:
        benchmark = market_median.get(row["market"])
        row["relativeStrength1dPct"] = (
            rnd(row["changePercent"] - benchmark)
            if row.get("changePercent") is not None and benchmark is not None
            else None
        )

    apply_percentiles(common)

    eligible = [
        r
        for r in common
        if (r.get("tradeValue") or 0) >= MIN_TRADE_VALUE
        and r.get("closePrice")
    ]

    sectors = sector_ranking(common)
    top_sector_names = {x["industry"] for x in sectors[:10]}
    sector_map = {x["industry"]: x for x in sectors}

    momentum = sorted(
        (
            candidate(r, "當日強勢", momentum_score(r))
            for r in eligible
            if (r.get("changePercent") or 0) >= 0.8
            and (r.get("closePosition") or 0) >= 0.65
        ),
        key=lambda x: x["discoveryScore"],
        reverse=True,
    )[:BUCKET_LIMIT]

    recovery = sorted(
        (
            candidate(r, "盤中低點回升", recovery_score(r))
            for r in eligible
            if r.get("lowDrawdownPct") is not None
            and r["lowDrawdownPct"] <= -1.5
            and (r.get("closePosition") or 0) >= 0.65
            and (r.get("reboundFromLowPct") or 0) >= 1.2
        ),
        key=lambda x: x["discoveryScore"],
        reverse=True,
    )[:BUCKET_LIMIT]

    sector_leaders = sorted(
        (
            candidate(
                r,
                "強勢產業領先股",
                sector_leader_score(r, sector_map[r["industry"]]),
            )
            for r in eligible
            if r.get("industry") in top_sector_names
            and (r.get("relativeStrength1dPct") or 0) > 0
        ),
        key=lambda x: x["discoveryScore"],
        reverse=True,
    )[:BUCKET_LIMIT]

    top_candidates = merge_candidates(
        [momentum, recovery, sector_leaders]
    )[:TOP_LIMIT]

    prior_discovery = load_existing_discovery(twse_date)

    payload = {
        "ok": True,
        "schemaVersion": 1,
        "version": VERSION,
        "generatedAt": generated_at.isoformat(),
        "generatedAtTaipei": taipei_now.isoformat(),
        "marketDate": twse_date,
        "marketDateGate": gate,
        "sources": {
            "TSE": TWSE_DAILY,
            "OTC": TPEX_DAILY,
            "securityMaster": ISIN,
            "referenceImplementation": "twjackysu/TWSEMCPServer (MIT) used as an implementation reference for official TWSE/TPEx access patterns",
        },
        "coverage": {
            "rawTSE": len(twse_rows),
            "rawOTC": len(tpex_rows),
            "commonStockUniverseCount": len(common),
            "TSECommonStockCount": len(by_market["TSE"]),
            "OTCCommonStockCount": len(by_market["OTC"]),
            "eligibleCount": len(eligible),
            "minTradeValue": MIN_TRADE_VALUE,
            "knownIndustryCount": sum(
                1
                for r in common
                if r.get("industry") not in (None, "", "UNKNOWN")
            ),
        },
        "marketMedianChangePct": {
            k: rnd(v) for k, v in market_median.items()
        },
        "breadth": {
            "ALL": breadth(common),
            "TSE": breadth(by_market["TSE"]),
            "OTC": breadth(by_market["OTC"]),
        },
        "sectorRanking": sectors[:SECTOR_LIMIT],
        "candidateBuckets": {
            "momentum": momentum,
            "recovery": recovery,
            "sectorLeaders": sector_leaders,
            "liquidityLeaders": [
                compact(r)
                for r in sorted(
                    eligible,
                    key=lambda x: x.get("tradeValue") or 0,
                    reverse=True,
                )[:BUCKET_LIMIT]
            ],
            "topGainers": [
                compact(r)
                for r in sorted(
                    eligible,
                    key=lambda x: x.get("changePercent") or -999,
                    reverse=True,
                )[:BUCKET_LIMIT]
            ],
        },
        "topCandidates": top_candidates,
        "historyLayer": prior_discovery,
        "methodology": {
            "purpose": "Full TWSE+TPEx discovery layer before V3.2 deep analysis.",
            "important": "discoveryScore is a search-priority score, NOT the V3.2 Opportunity Score.",
            "fullMarket": True,
            "signals": [
                "當日強勢：單日相對表現、收盤位階、流動性",
                "盤中低點回升：低點跌幅、低檔回升、收盤位階",
                "強勢產業領先股：產業中位數報酬、上漲家數、成交值與個股相對強弱",
            ],
            "historyLayer": "Reuses same-day data/discovery-scan.json for top-candidate 5d/20d RS, MA20, RVOL20 and setup classification when available.",
            "limitations": [
                "The full universe does not yet have persistent 20/60-session history for every stock.",
                "True full-market MA20/MA60/ATR/20-day breakout scanning will be added by accumulating daily market snapshots.",
                "V3.2 must still validate fundamentals, catalysts, multi-day structure and risk/reward before a trade recommendation.",
            ],
        },
    }

    Path("data").mkdir(exist_ok=True)
    Path("data/tw-screener.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "ok": True,
                "version": VERSION,
                "marketDate": twse_date,
                "coverage": payload["coverage"],
                "breadth": payload["breadth"],
                "topSectors": [
                    (x["industry"], x["sectorStrengthScore"])
                    for x in sectors[:5]
                ],
                "topCandidates": [
                    (x["symbol"], x["name"], x["discoveryScore"], x["signals"])
                    for x in top_candidates[:10]
                ],
                "historyLayerAvailable": prior_discovery.get("available"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
