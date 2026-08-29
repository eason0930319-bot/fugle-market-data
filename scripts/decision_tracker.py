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

VERSION = "decision-tracker-v1.0"
MARKER = "<!-- DECISION_LEDGER_V1 -->"

OWNER = os.getenv("GITHUB_REPOSITORY_OWNER", "eason0930319-bot")
REPO = os.getenv("GITHUB_REPOSITORY", "eason0930319-bot/fugle-market-data").split("/")[-1]
ISSUE_NUMBER = int(os.getenv("DECISION_LEDGER_ISSUE", "1"))
TOKEN = os.getenv("GITHUB_TOKEN", "")

TWSE = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"

SCREENER = Path("data/tw-screener.json")
LEDGER = Path("data/decision-ledger.json")
SUMMARY = Path("data/decision-summary.json")

RETENTION_DAYS = int(os.getenv("DECISION_RETENTION_DAYS", "540"))
MIN_SAMPLE = int(os.getenv("DECISION_MIN_SAMPLE", "50"))
MISSED_MOVE_PCT = float(os.getenv("DECISION_MISSED_MOVE_PCT", "5"))
STOP_RECOVERY_PCT = float(os.getenv("DECISION_STOP_RECOVERY_PCT", "2"))

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 fugle-market-data-decision-tracker/1.0",
    "Accept": "application/json,text/plain;q=0.9,*/*;q=0.8",
})


def clean(v):
    return "" if v is None else str(v).strip()


def num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        if isinstance(v, float) and math.isnan(v):
            return None
        return float(v)
    text = str(v).strip().replace(",", "").replace("%", "").replace("－", "-").replace("−", "-")
    if text in {"", "-", "--", "---", "N/A", "null", "None"}:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(m.group()) if m else None


def rnd(v, digits=3):
    return None if v is None else round(float(v), digits)


def roc_date(v):
    digits = re.sub(r"\D", "", clean(v))
    try:
        if len(digits) == 8:
            return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8])).isoformat()
        if len(digits) == 7:
            return date(int(digits[:3]) + 1911, int(digits[3:5]), int(digits[5:7])).isoformat()
    except ValueError:
        pass
    return None


def get_json(url, params=None, headers=None, attempts=3):
    last = None
    for i in range(attempts):
        try:
            r = S.get(url, params=params, headers=headers, timeout=35)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last = exc
            time.sleep(0.8 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def load(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def enum(v, allowed, default):
    value = clean(v).upper()
    return value if value in allowed else default


def fetch_comments():
    if not TOKEN:
        raise RuntimeError("GITHUB_TOKEN is required")
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/issues/{ISSUE_NUMBER}/comments"
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    out = []
    for page in range(1, 21):
        rows = get_json(url, {"per_page": 100, "page": page}, headers=headers)
        if not isinstance(rows, list):
            raise RuntimeError("Issue comments response was not a list")
        out.extend(rows)
        if len(rows) < 100:
            return out
    raise RuntimeError("Decision Ledger exceeded 2000 issue comments")


def parse_comment(body):
    if MARKER not in (body or ""):
        return None
    text = body.split(MARKER, 1)[1].strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```\s*$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or int(payload.get("schemaVersion") or 0) != 1:
        return None
    return payload


def quotes():
    out = {}
    for x in get_json(TWSE):
        symbol = clean(x.get("Code"))
        close = num(x.get("ClosingPrice"))
        session = roc_date(x.get("Date"))
        if len(symbol) == 4 and symbol.isdigit() and session and close is not None:
            out[("TSE", symbol)] = {
                "date": session,
                "close": close,
                "high": num(x.get("HighestPrice")),
                "low": num(x.get("LowestPrice")),
            }

    for x in get_json(TPEX):
        symbol = clean(x.get("SecuritiesCompanyCode"))
        close = num(x.get("Close"))
        session = roc_date(x.get("Date"))
        if len(symbol) == 4 and symbol.isdigit() and session and close is not None:
            out[("OTC", symbol)] = {
                "date": session,
                "close": close,
                "high": num(x.get("High")),
                "low": num(x.get("Low")),
            }
    return out


def ingest(records, comments):
    ids = {r.get("id") for r in records if r.get("id")}
    added = 0
    bad = 0

    for comment in comments:
        body = comment.get("body") or ""
        payload = parse_comment(body)
        if payload is None:
            bad += int(MARKER in body)
            continue
        if payload.get("test") is True:
            continue

        decision_date = clean(payload.get("decisionDate"))
        try:
            date.fromisoformat(decision_date)
        except Exception:
            bad += 1
            continue

        regime = enum(payload.get("marketRegime"), {"BULL", "NEUTRAL", "BEAR", "UNKNOWN"}, "UNKNOWN")

        for i, item in enumerate(payload.get("decisions") or []):
            symbol = clean(item.get("symbol"))
            market = enum(item.get("market"), {"TSE", "OTC"}, "")
            if not (len(symbol) == 4 and symbol.isdigit() and market):
                continue

            rid = f"{comment.get('id')}:{i}:{market}:{symbol}"
            if rid in ids:
                continue

            records.append({
                "id": rid,
                "sourceCommentId": comment.get("id"),
                "sourceCommentUrl": comment.get("html_url"),
                "decisionDate": decision_date,
                "generatedAt": payload.get("generatedAt"),
                "strategyVersion": clean(payload.get("strategyVersion")) or "V3.3",
                "marketRegime": regime,
                "portfolioDirection": clean(payload.get("portfolioDirection")) or None,
                "symbol": symbol,
                "name": clean(item.get("name")),
                "market": market,
                "industry": clean(item.get("industry")) or None,
                "scope": enum(item.get("scope"), {"NEW", "HOLDING"}, "NEW"),
                "grade": enum(item.get("grade"), {"A", "B", "C", "DEFER", "VETO"}, "C"),
                "setup": enum(
                    item.get("setup"),
                    {"BREAKOUT", "PULLBACK", "REVERSAL", "BREAKDOWN", "OTHER"},
                    "OTHER",
                ),
                "action": enum(
                    item.get("action"),
                    {"BUY_PLAN", "ADD", "WAIT", "OBSERVE", "HOLD", "REDUCE", "TAKE_PROFIT", "STOP", "EXIT", "VETO"},
                    "OBSERVE",
                ),
                "opportunityScore": rnd(num(item.get("opportunityScore")), 2),
                "rr": rnd(num(item.get("rr")), 2),
                "readiness": rnd(num(item.get("readiness")), 2),
                "execution": rnd(num(item.get("execution")), 2),
                "fundamentalGate": enum(item.get("fundamentalGate"), {"PASS", "FAIL", "UNKNOWN"}, "UNKNOWN"),
                "referenceClose": rnd(num(item.get("referenceClose")), 4),
                "triggerPrice": rnd(num(item.get("triggerPrice")), 4),
                "triggerRule": enum(
                    item.get("triggerRule"),
                    {"AT_OR_ABOVE", "AT_OR_BELOW", "NONE", "TEXT_ONLY"},
                    "NONE",
                ),
                "triggerText": clean(item.get("triggerText")) or None,
                "invalidationPrice": rnd(num(item.get("invalidationPrice")), 4),
                "targetPrice": rnd(num(item.get("targetPrice")), 4),
                "positionPct": rnd(num(item.get("positionPct")), 2),
                "reasonCodes": [clean(x) for x in (item.get("reasonCodes") or []) if clean(x)],
                "status": "open",
                "promotedFromBRecordId": None,
                "promotedToAOn": None,
                "promotionRecordId": None,
                "triggered": False,
                "triggeredOn": None,
                "targetHit": False,
                "targetHitOn": None,
                "stopTouched": False,
                "stopTouchedOn": None,
                "possibleTooTightStop": False,
                "dailyObservations": [],
                "outcomes": {
                    "close1d": None, "ret1dPct": None, "mfe1dPct": None, "mae1dPct": None,
                    "close3d": None, "ret3dPct": None, "mfe3dPct": None, "mae3dPct": None,
                    "close5d": None, "ret5dPct": None, "mfe5dPct": None, "mae5dPct": None,
                },
            })
            ids.add(rid)
            added += 1
    return added, bad


def promotions(records):
    ordered = sorted(records, key=lambda r: (r.get("decisionDate") or "", r.get("id") or ""))
    for i, current in enumerate(ordered):
        if current.get("scope") != "NEW" or current.get("grade") != "A" or current.get("promotedFromBRecordId"):
            continue
        candidates = []
        for prior in ordered[:i]:
            if (
                prior.get("scope") == "NEW"
                and prior.get("grade") == "B"
                and prior.get("symbol") == current.get("symbol")
                and not prior.get("promotionRecordId")
            ):
                delta = (date.fromisoformat(current["decisionDate"]) - date.fromisoformat(prior["decisionDate"])).days
                if 0 < delta <= 10:
                    candidates.append(prior)
        if candidates:
            prior = candidates[-1]
            prior["promotedToAOn"] = current["decisionDate"]
            prior["promotionRecordId"] = current["id"]
            current["promotedFromBRecordId"] = prior["id"]


def hit_trigger(record, q):
    price = num(record.get("triggerPrice"))
    rule = record.get("triggerRule")
    if price is None:
        return False
    if rule == "AT_OR_ABOVE":
        return num(q.get("high")) is not None and q["high"] >= price
    if rule == "AT_OR_BELOW":
        return num(q.get("low")) is not None and q["low"] <= price
    return False


def pct(value, base):
    if value is None or base in (None, 0):
        return None
    return (value / base - 1) * 100


def update(records, market_quotes, market_date):
    updated = 0
    missing = 0

    for r in records:
        if not r.get("decisionDate") or r["decisionDate"] >= market_date:
            continue
        if r.get("status") == "complete":
            continue

        q = market_quotes.get((r.get("market"), r.get("symbol")))
        if not q or q.get("date") != market_date:
            missing += 1
            continue

        obs = r.setdefault("dailyObservations", [])
        if not any(x.get("date") == market_date for x in obs):
            obs.append({
                "date": market_date,
                "close": rnd(q.get("close"), 4),
                "high": rnd(q.get("high"), 4),
                "low": rnd(q.get("low"), 4),
            })
            obs.sort(key=lambda x: x["date"])

        if not r.get("triggered") and hit_trigger(r, q):
            r["triggered"], r["triggeredOn"] = True, market_date

        target = num(r.get("targetPrice"))
        if not r.get("targetHit") and target is not None and num(q.get("high")) is not None and q["high"] >= target:
            r["targetHit"], r["targetHitOn"] = True, market_date

        stop = num(r.get("invalidationPrice"))
        if not r.get("stopTouched") and stop is not None and num(q.get("low")) is not None and q["low"] <= stop:
            r["stopTouched"], r["stopTouchedOn"] = True, market_date

        reference = num(r.get("referenceClose"))
        n = len(obs)
        if reference not in (None, 0):
            highs = [num(x.get("high")) for x in obs if num(x.get("high")) is not None]
            lows = [num(x.get("low")) for x in obs if num(x.get("low")) is not None]
            if n in {1, 3, 5}:
                o = r["outcomes"]
                o[f"close{n}d"] = rnd(q.get("close"), 4)
                o[f"ret{n}dPct"] = rnd(pct(q.get("close"), reference))
                o[f"mfe{n}dPct"] = rnd(pct(max(highs), reference)) if highs else None
                o[f"mae{n}dPct"] = rnd(pct(min(lows), reference)) if lows else None

        stop_date = r.get("stopTouchedOn")
        if stop_date and reference not in (None, 0):
            later_highs = [
                num(x.get("high"))
                for x in obs
                if x.get("date") > stop_date and num(x.get("high")) is not None
            ]
            if later_highs and max(later_highs) >= reference * (1 + STOP_RECOVERY_PCT / 100):
                r["possibleTooTightStop"] = True

        if n >= 5:
            r["status"] = "complete"
        updated += 1

    return updated, missing


def stats(records, horizon):
    key = f"ret{horizon}dPct"
    rows = [r for r in records if num((r.get("outcomes") or {}).get(key)) is not None]
    if not rows:
        return {
            "count": 0, "avgReturnPct": None, "medianReturnPct": None, "winRate": None,
            "avgMfePct": None, "avgMaePct": None, "targetHitRate": None, "stopTouchRate": None,
        }

    returns = [float(r["outcomes"][key]) for r in rows]
    mfes = [float(r["outcomes"][f"mfe{horizon}dPct"]) for r in rows if num(r["outcomes"].get(f"mfe{horizon}dPct")) is not None]
    maes = [float(r["outcomes"][f"mae{horizon}dPct"]) for r in rows if num(r["outcomes"].get(f"mae{horizon}dPct")) is not None]

    return {
        "count": len(rows),
        "avgReturnPct": rnd(statistics.fmean(returns)),
        "medianReturnPct": rnd(statistics.median(returns)),
        "winRate": rnd(sum(x > 0 for x in returns) / len(returns), 4),
        "avgMfePct": rnd(statistics.fmean(mfes)) if mfes else None,
        "avgMaePct": rnd(statistics.fmean(maes)) if maes else None,
        "targetHitRate": rnd(sum(bool(r.get("targetHit")) for r in rows) / len(rows), 4),
        "stopTouchRate": rnd(sum(bool(r.get("stopTouched")) for r in rows) / len(rows), 4),
    }


def grouped(records, field):
    groups = defaultdict(list)
    for r in records:
        value = clean(r.get(field))
        if value:
            groups[value].append(r)
    return {
        key: {
            "recordCount": len(rows),
            "1d": stats(rows, 1),
            "3d": stats(rows, 3),
            "5d": stats(rows, 5),
        }
        for key, rows in groups.items()
    }


def summary(records, market_date, bad, last_run):
    new = [r for r in records if r.get("scope") == "NEW"]
    b = [r for r in new if r.get("grade") == "B"]
    promoted = [r for r in b if r.get("promotionRecordId")]
    matured = [r for r in new if num((r.get("outcomes") or {}).get("ret5dPct")) is not None]

    missed_pool = [r for r in matured if r.get("grade") in {"C", "DEFER", "VETO"}]
    missed = [
        r for r in missed_pool
        if num(r["outcomes"].get("mfe5dPct")) is not None
        and r["outcomes"]["mfe5dPct"] >= MISSED_MOVE_PCT
    ]

    stop_pool = [r for r in matured if r.get("stopTouched")]
    too_tight = [r for r in stop_pool if r.get("possibleTooTightStop")]

    triggerable = [
        r for r in new
        if r.get("triggerRule") in {"AT_OR_ABOVE", "AT_OR_BELOW"}
        and num(r.get("triggerPrice")) is not None
    ]
    triggered = [r for r in triggerable if r.get("triggered")]

    ready = len(matured) >= MIN_SAMPLE

    return {
        "schemaVersion": 1,
        "version": VERSION,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "marketDate": market_date,
        "scope": (
            "Final ChatGPT V3.3 decisions from GitHub Issue #1. "
            "Forward returns use 19:00 referenceClose; trigger/stop metrics are tracked separately."
        ),
        "recordCount": len(records),
        "newCandidateCount": len(new),
        "badPayloadCount": bad,
        "overallNew": {"1d": stats(new, 1), "3d": stats(new, 3), "5d": stats(new, 5)},
        "byGrade": grouped(new, "grade"),
        "bySetup": grouped(new, "setup"),
        "byMarketRegime": grouped(new, "marketRegime"),
        "triggerStats": {
            "triggerableCount": len(triggerable),
            "triggeredCount": len(triggered),
            "triggerRate": rnd(len(triggered) / len(triggerable), 4) if triggerable else None,
        },
        "bToA": {
            "bRecordCount": len(b),
            "promotedCount": len(promoted),
            "promotionRate": rnd(len(promoted) / len(b), 4) if b else None,
        },
        "missedMoveProxy": {
            "definition": f"C/DEFER/VETO with 5D MFE >= {MISSED_MOVE_PCT:.1f}%",
            "eligibleCount": len(missed_pool),
            "missedCount": len(missed),
            "rate": rnd(len(missed) / len(missed_pool), 4) if missed_pool else None,
        },
        "stopTightnessProxy": {
            "definition": (
                f"Invalidation touched, then a later day's high recovered to "
                f"referenceClose + {STOP_RECOVERY_PCT:.1f}%."
            ),
            "stopTouchedCount": len(stop_pool),
            "possibleTooTightCount": len(too_tight),
            "rate": rnd(len(too_tight) / len(stop_pool), 4) if stop_pool else None,
            "note": "Daily OHLC cannot determine same-day stop/target sequence; proxy only.",
        },
        "calibrationReadiness": {
            "minimum5dNewDecisionSamples": MIN_SAMPLE,
            "actual5dNewDecisionSamples": len(matured),
            "ready": ready,
            "rule": "Never auto-change V3.3 production thresholds; use statistics as evidence only.",
        },
        "observations": (
            ["Sample threshold reached; compare A/B/C, setup and market regime, but do not auto-tune."]
            if ready
            else [
                f"5日成熟的新候選決策只有 {len(matured)} 筆；至少 {MIN_SAMPLE} 筆前不調整 V3.3 正式門檻。",
                "Decision Ledger 與 Discovery Performance 分開統計，避免把初篩品質與最終操作決策混為一談。",
            ]
        ),
        "lastRun": last_run,
    }


def main():
    screener = load(SCREENER, {})
    if not screener.get("ok") or not screener.get("marketDate"):
        raise RuntimeError("tw-screener.json is not healthy")

    market_date = screener["marketDate"]
    market_quotes = quotes()

    tse_dates = [q["date"] for (m, _), q in market_quotes.items() if m == "TSE"]
    otc_dates = [q["date"] for (m, _), q in market_quotes.items() if m == "OTC"]
    tse_date = statistics.mode(tse_dates) if tse_dates else None
    otc_date = statistics.mode(otc_dates) if otc_dates else None
    if tse_date != market_date or otc_date != market_date:
        raise RuntimeError(
            f"Decision tracker date mismatch: screener={market_date}, TSE={tse_date}, OTC={otc_date}"
        )

    comments = fetch_comments()

    ledger = load(LEDGER, {
        "schemaVersion": 1,
        "version": VERSION,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "updatedAt": None,
        "lastMarketDate": None,
        "records": [],
    })
    records = ledger.get("records") or []

    added, bad = ingest(records, comments)
    promotions(records)
    updated, missing = update(records, market_quotes, market_date)

    cutoff = (date.fromisoformat(market_date) - timedelta(days=RETENTION_DAYS)).isoformat()
    records = [r for r in records if (r.get("decisionDate") or market_date) >= cutoff]
    records.sort(key=lambda r: (r.get("decisionDate") or "", r.get("id") or ""), reverse=True)

    ledger.update({
        "schemaVersion": 1,
        "version": VERSION,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "lastMarketDate": market_date,
        "recordCount": len(records),
        "source": {
            "repository": f"{OWNER}/{REPO}",
            "issueNumber": ISSUE_NUMBER,
            "marker": MARKER,
        },
        "trackingPolicy": {
            "horizonsObservedTradingDays": [1, 3, 5],
            "entryReference": "19:00 decision referenceClose",
            "promotionWindowCalendarDays": 10,
            "retentionCalendarDays": RETENTION_DAYS,
        },
        "records": records,
    })

    last_run = {
        "issueCommentCount": len(comments),
        "newRecords": added,
        "updatedExistingRecords": updated,
        "quoteUnavailableRecords": missing,
        "badPayloadCount": bad,
        "trackedUniverseQuotes": len(market_quotes),
    }

    report = summary(records, market_date, bad, last_run)
    save(LEDGER, ledger)
    save(SUMMARY, report)

    print(json.dumps({
        "ok": True,
        "version": VERSION,
        "marketDate": market_date,
        "recordCount": len(records),
        "newRecords": added,
        "updatedExistingRecords": updated,
        "badPayloadCount": bad,
        "5dNewDecisionSamples": report["calibrationReadiness"]["actual5dNewDecisionSamples"],
        "calibrationReady": report["calibrationReadiness"]["ready"],
        "triggerStats": report["triggerStats"],
        "bToA": report["bToA"],
        "missedMoveProxy": report["missedMoveProxy"],
        "stopTightnessProxy": report["stopTightnessProxy"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
