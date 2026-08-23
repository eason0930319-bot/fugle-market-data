from __future__ import annotations

import html
import json
import math
import os
import re
import statistics
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests

TWSE_DAILY = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_DAILY = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
TWSE_HIST = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
TPEX_HIST = "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock/st43_result.php"
ISIN = "https://isin.twse.com.tw/isin/C_public.jsp"

MIN_TRADE_VALUE = int(os.getenv("MIN_TRADE_VALUE", "50000000"))
RESULT_LIMIT = int(os.getenv("RESULT_LIMIT", "30"))
HISTORY_LIMIT = int(os.getenv("HISTORY_CANDIDATE_LIMIT", "40"))
DYNAMIC_LIMIT = int(os.getenv("DYNAMIC_PREVIEW_LIMIT", "11"))
BENCHMARK = os.getenv("BENCHMARK_SYMBOL", "0050")

S = requests.Session()
S.headers.update({"User-Agent": "fugle-market-data-discovery/2.0"})


def num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return None if isinstance(v, float) and math.isnan(v) else float(v)

    t = (
        str(v)
        .strip()
        .replace(",", "")
        .replace("%", "")
        .replace("+", "")
    )

    if t in {"", "-", "--", "---", "N/A", "null", "None"}:
        return None

    m = re.search(r"-?\d+(?:\.\d+)?", t)
    return float(m.group()) if m else None


def rnd(v, n=3):
    return None if v is None else round(float(v), n)


def get_json(url, params=None):
    last = None

    for i in range(3):
        try:
            r = S.get(
                url,
                params=params,
                timeout=30,
                headers={"Accept": "application/json"},
            )
            r.raise_for_status()
            time.sleep(0.12)
            return r.json()

        except Exception as e:
            last = e
            time.sleep(0.7 * (i + 1))

    raise RuntimeError(f"GET failed {url}: {last}")


def roc_date(v):
    if v is None:
        return None

    d = re.sub(r"\D", "", str(v))

    try:
        if len(d) == 8:
            return date(
                int(d[:4]),
                int(d[4:6]),
                int(d[6:8]),
            ).isoformat()

        if len(d) == 7:
            return date(
                int(d[:3]) + 1911,
                int(d[3:5]),
                int(d[5:7]),
            ).isoformat()

    except ValueError:
        return None

    return None


def change_pct(close, change):
    if close is None or change is None:
        return None

    prev = close - change

    if prev == 0:
        return None

    return change / prev * 100


def norm_twse(x):
    s = str(x.get("Code", "")).strip()

    if not (len(s) == 4 and s.isdigit()):
        return None

    c = num(x.get("ClosingPrice"))
    ch = num(x.get("Change"))

    return {
        "symbol": s,
        "name": str(x.get("Name", "")).strip(),
        "market": "TSE",
        "date": roc_date(x.get("Date")),
        "openPrice": num(x.get("OpeningPrice")),
        "highPrice": num(x.get("HighestPrice")),
        "lowPrice": num(x.get("LowestPrice")),
        "closePrice": c,
        "change": ch,
        "changePercent": rnd(change_pct(c, ch)),
        "tradeVolume": num(x.get("TradeVolume")),
        "tradeValue": num(x.get("TradeValue")),
    }


def norm_tpex(x):
    s = str(x.get("SecuritiesCompanyCode", "")).strip()

    if not (len(s) == 4 and s.isdigit()):
        return None

    c = num(x.get("Close"))
    ch = num(x.get("Change"))
    vol = num(x.get("TradingShares"))

    val = next(
        (
            num(x.get(k))
            for k in (
                "TransactionAmount",
                "TradingValue",
                "TradeValue",
                "TradeAmount",
            )
            if num(x.get(k)) is not None
        ),
        None,
    )

    if val is None and vol is not None and c is not None:
        val = vol * c

    return {
        "symbol": s,
        "name": str(x.get("CompanyName", "")).strip(),
        "market": "OTC",
        "date": roc_date(x.get("Date")),
        "openPrice": num(x.get("Open")),
        "highPrice": num(x.get("High")),
        "lowPrice": num(x.get("Low")),
        "closePrice": c,
        "change": ch,
        "changePercent": rnd(change_pct(c, ch)),
        "tradeVolume": vol,
        "tradeValue": val,
    }


def add_intraday(r):
    h = r.get("highPrice")
    l = r.get("lowPrice")
    c = r.get("closePrice")

    if None not in (h, l, c) and h != l:
        r["closePosition"] = rnd(
            (c - l) / (h - l),
            4,
        )
    else:
        r["closePosition"] = None

    return r


def load_daily(url, fn, market):
    try:
        raw = get_json(url)

        rows = [
            add_intraday(y)
            for x in raw
            if isinstance(x, dict)
            and (y := fn(x))
        ]

        return {
            "ok": bool(rows),
            "market": market,
            "raw": len(raw),
            "rows": rows,
            "error": None,
        }

    except Exception as e:
        return {
            "ok": False,
            "market": market,
            "raw": 0,
            "rows": [],
            "error": str(e),
        }


def security_master(str_mode, market):
    try:
        r = S.get(
            ISIN,
            params={"strMode": str_mode},
            timeout=30,
        )
        r.raise_for_status()

        text = r.content.decode(
            "big5",
            errors="replace",
        )

        out = {}

        for tr in re.findall(
            r"<tr[^>]*>(.*?)</tr>",
            text,
            flags=re.I | re.S,
        ):
            cells = []

            for td in re.findall(
                r"<td[^>]*>(.*?)</td>",
                tr,
                flags=re.I | re.S,
            ):
                t = re.sub(
                    r"<[^>]+>",
                    "",
                    td,
                )

                t = html.unescape(
                    re.sub(
                        r"\s+",
                        " ",
                        t,
                    )
                ).strip()

                cells.append(t)

            if not cells:
                continue

            m = re.match(
                r"^(\d{4})\s+(.+)$",
                cells[0],
            )

            if not m:
                continue

            cfi = next(
                (
                    c.upper()
                    for c in cells
                    if re.fullmatch(
                        r"ES[A-Z0-9]{4}",
                        c.upper(),
                    )
                ),
                None,
            )

            if cfi:
                out[m.group(1)] = {
                    "name": m.group(2).strip(),
                    "market": market,
                    "cfi": cfi,
                }

        return {
            "ok": bool(out),
            "count": len(out),
            "items": out,
            "error": None,
        }

    except Exception as e:
        return {
            "ok": False,
            "count": 0,
            "items": {},
            "error": str(e),
        }


def percentile(vals, v):
    if v is None or len(vals) < 2:
        return 0.0

    return (
        (
            sum(x <= v for x in vals) - 1
        )
        /
        (
            len(vals) - 1
        )
        * 100
    )


def cheap_scan(rows):
    liq = sorted(
        r["tradeValue"]
        for r in rows
        if r.get("tradeValue") is not None
    )

    mom = sorted(
        r["changePercent"]
        for r in rows
        if r.get("changePercent") is not None
    )

    for r in rows:
        lp = percentile(
            liq,
            r.get("tradeValue"),
        )

        mp = percentile(
            mom,
            r.get("changePercent"),
        )

        cp = (
            50
            if r.get("closePosition") is None
            else max(
                0,
                min(
                    100,
                    r["closePosition"] * 100,
                ),
            )
        )

        r["cheapScan"] = {
            "liquidityPercentile": rnd(
                lp,
                2,
            ),
            "momentumPercentile": rnd(
                mp,
                2,
            ),
            "closeStrengthScore": rnd(
                cp,
                2,
            ),
            "discoveryScore": rnd(
                lp * 0.40
                + mp * 0.35
                + cp * 0.25,
                2,
            ),
        }


def pick_history(rows):
    order = sorted(
        rows,
        key=lambda r:
            r["cheapScan"]["discoveryScore"],
        reverse=True,
    )

    normal = [
        r
        for r in order
        if (r.get("changePercent") or 0) < 7
    ]

    ext = [
        r
        for r in order
        if (r.get("changePercent") or 0) >= 7
    ]

    ext_cap = min(
        8,
        max(
            2,
            HISTORY_LIMIT // 5,
        ),
    )

    selected = (
        normal[
            :HISTORY_LIMIT - ext_cap
        ]
        +
        ext[
            :ext_cap
        ]
    )

    return sorted(
        selected,
        key=lambda r:
            r["cheapScan"]["discoveryScore"],
        reverse=True,
    )


def months(d):
    out = [
        (
            d.year,
            d.month,
        )
    ]

    y = d.year
    m = d.month

    if m == 1:
        y -= 1
        m = 12
    else:
        m -= 1

    out.append(
        (
            y,
            m,
        )
    )

    return list(
        reversed(out)
    )


def parse_hist(data):
    out = []

    for x in data:
        if not isinstance(x, list) or len(x) < 7:
            continue

        d = roc_date(
            x[0]
        )

        if d:
            out.append(
                {
                    "date": d,
                    "volume": num(x[1]),
                    "tradeValue": num(x[2]),
                    "open": num(x[3]),
                    "high": num(x[4]),
                    "low": num(x[5]),
                    "close": num(x[6]),
                }
            )

    return out


def history(
    symbol,
    market,
    session,
):
    out = []

    for y, m in months(session):

        if market == "TSE":

            p = get_json(
                TWSE_HIST,
                {
                    "response": "json",
                    "stockNo": symbol,
                    "date":
                        f"{y:04d}{m:02d}01",
                },
            )

            if (
                isinstance(p, dict)
                and p.get("stat") == "OK"
            ):
                out += parse_hist(
                    p.get(
                        "data",
                        [],
                    )
                )

        else:

            p = get_json(
                TPEX_HIST,
                {
                    "l": "zh-tw",
                    "date":
                        f"{y:04d}/{m:02d}/01",
                    "code": symbol,
                },
            )

            tables = (
                p.get(
                    "tables",
                    [],
                )
                if isinstance(p, dict)
                else []
            )

            if tables:
                out += parse_hist(
                    tables[0].get(
                        "data",
                        [],
                    )
                )

    return sorted(
        {
            x["date"]: x
            for x in out
        }.values(),
        key=lambda x:
            x["date"],
    )


def merged_history(
    hist,
    row,
):
    by = {
        x["date"]: x
        for x in hist
    }

    if row.get("date"):
        by[
            row["date"]
        ] = {
            "date":
                row["date"],

            "volume":
                row.get(
                    "tradeVolume"
                ),

            "tradeValue":
                row.get(
                    "tradeValue"
                ),

            "open":
                row.get(
                    "openPrice"
                ),

            "high":
                row.get(
                    "highPrice"
                ),

            "low":
                row.get(
                    "lowPrice"
                ),

            "close":
                row.get(
                    "closePrice"
                ),
        }

    return sorted(
        by.values(),
        key=lambda x:
            x["date"],
    )


def ret(
    hist,
    n,
):
    closes = [
        x["close"]
        for x in hist
        if x.get("close")
        not in (
            None,
            0,
        )
    ]

    if len(closes) < n + 1:
        return None

    return (
        closes[-1]
        /
        closes[-n - 1]
        - 1
    ) * 100


def mean_last(
    vals,
    n,
):
    if len(vals) < n:
        return None

    return statistics.fmean(
        vals[-n:]
    )


def features(
    row,
    hist,
    b5,
    b20,
):
    h = merged_history(
        hist,
        row,
    )

    valid = [
        x
        for x in h
        if x.get("close")
        not in (
            None,
            0,
        )
    ]

    r5 = ret(
        valid,
        5,
    )

    r20 = ret(
        valid,
        20,
    )

    closes = [
        float(
            x["close"]
        )
        for x in valid
    ]

    ma20 = mean_last(
        closes,
        20,
    )

    prev_vol = [
        float(
            x["volume"]
        )
        for x in valid[:-1]
        if x.get("volume")
        not in (
            None,
            0,
        )
    ]

    av20 = mean_last(
        prev_vol,
        20,
    )

    rv = (
        row["tradeVolume"]
        /
        av20
        if av20
        and row.get("tradeVolume")
        else None
    )

    prior = valid[:-1][
        -20:
    ]

    highs = [
        x["high"]
        for x in prior
        if x.get("high")
        not in (
            None,
            0,
        )
    ]

    hi20 = (
        max(highs)
        if highs
        else None
    )

    close = row.get(
        "closePrice"
    )

    dist_hi = (
        (
            close / hi20
            - 1
        )
        * 100
        if close
        and hi20
        else None
    )

    dist_ma = (
        (
            close / ma20
            - 1
        )
        * 100
        if close
        and ma20
        else None
    )

    return {
        "historySessions":
            len(valid),

        "return5":
            rnd(r5),

        "return20":
            rnd(r20),

        "rs5":
            rnd(
                r5 - b5
            )
            if r5 is not None
            and b5 is not None
            else None,

        "rs20":
            rnd(
                r20 - b20
            )
            if r20 is not None
            and b20 is not None
            else None,

        "rvol20":
            rnd(rv),

        "ma20":
            rnd(ma20),

        "prior20High":
            rnd(hi20),

        "distanceFrom20DHighPct":
            rnd(
                dist_hi
            ),

        "distanceFromMA20Pct":
            rnd(
                dist_ma
            ),
    }


def setup(r):
    f = r[
        "historyFeatures"
    ]

    ch = (
        r.get(
            "changePercent"
        )
        or 0
    )

    cp = (
        0.5
        if r.get(
            "closePosition"
        )
        is None
        else r[
            "closePosition"
        ]
    )

    r5 = f.get(
        "return5"
    )

    r20 = f.get(
        "return20"
    )

    rs20 = f.get(
        "rs20"
    )

    rv = f.get(
        "rvol20"
    )

    dh = f.get(
        "distanceFrom20DHighPct"
    )

    dm = f.get(
        "distanceFromMA20Pct"
    )

    if (
        ch >= 7
        or (
            dm is not None
            and dm >= 14
        )
    ):
        return "EXTENDED"

    if (
        dh is not None
        and dh >= -1
        and (rv or 0) >= 1.15
        and cp >= 0.70
        and (rs20 or 0) > 0
    ):
        return "BREAKOUT"

    if (
        r20 is not None
        and r20 > 3
        and dm is not None
        and -1.5 <= dm <= 6
        and dh is not None
        and -12 <= dh <= -2
        and r5 is not None
        and -6 <= r5 <= 2.5
    ):
        return "PULLBACK"

    if (
        r5 is not None
        and r5 <= 2
        and ch > 0
        and cp >= 0.75
        and dm is not None
        and dm >= -2.5
    ):
        return "REVERSAL"

    if (
        r20 is not None
        and r20 > 4
        and (rs20 or 0) > 0
        and dm is not None
        and dm > 0
        and cp >= 0.55
    ):
        return "TREND_MOMENTUM"

    return "GENERAL_WATCH"


def high_score(d):
    if d is None:
        return 0

    if d >= 0:
        return 100

    return max(
        0,
        100
        +
        d
        *
        (
            100
            /
            12
        ),
    )


def score_v2(rows):
    rs5 = sorted(
        float(
            r[
                "historyFeatures"
            ][
                "rs5"
            ]
        )
        for r in rows
        if r[
            "historyFeatures"
        ].get(
            "rs5"
        )
        is not None
    )

    rs20 = sorted(
        float(
            r[
                "historyFeatures"
            ][
                "rs20"
            ]
        )
        for r in rows
        if r[
            "historyFeatures"
        ].get(
            "rs20"
        )
        is not None
    )

    rv = sorted(
        float(
            r[
                "historyFeatures"
            ][
                "rvol20"
            ]
        )
        for r in rows
        if r[
            "historyFeatures"
        ].get(
            "rvol20"
        )
        is not None
    )

    bonus = {
        "BREAKOUT": 5,
        "PULLBACK": 5,
        "REVERSAL": 3,
        "TREND_MOMENTUM": 3,
        "GENERAL_WATCH": 0,
        "EXTENDED": -12,
    }

    for r in rows:
        f = r[
            "historyFeatures"
        ]

        st = setup(r)

        p5 = percentile(
            rs5,
            f.get(
                "rs5"
            ),
        )

        p20 = percentile(
            rs20,
            f.get(
                "rs20"
            ),
        )

        prv = percentile(
            rv,
            f.get(
                "rvol20"
            ),
        )

        hs = high_score(
            f.get(
                "distanceFrom20DHighPct"
            )
        )

        total = (
            r[
                "cheapScan"
            ][
                "discoveryScore"
            ]
            * 0.20
            +
            p5
            * 0.20
            +
            p20
            * 0.25
            +
            prv
            * 0.20
            +
            hs
            * 0.15
            +
            bonus[st]
        )

        r[
            "setup"
        ] = st

        r[
            "discoveryV2"
        ] = {
            "rs5Percentile":
                rnd(
                    p5,
                    2,
                ),

            "rs20Percentile":
                rnd(
                    p20,
                    2,
                ),

            "rvol20Percentile":
                rnd(
                    prv,
                    2,
                ),

            "highProximityScore":
                rnd(
                    hs,
                    2,
                ),

            "score":
                rnd(
                    max(
                        0,
                        min(
                            100,
                            total,
                        ),
                    ),
                    2,
                ),
        }


def dynamic_preview(rows):
    order = sorted(
        rows,
        key=lambda r:
            r[
                "discoveryV2"
            ][
                "score"
            ],
        reverse=True,
    )

    result = []
    used = set()
    ext = 0

    setup_order = (
        "BREAKOUT",
        "PULLBACK",
        "REVERSAL",
        "TREND_MOMENTUM",
        "GENERAL_WATCH",
        "EXTENDED",
    )

    for st in setup_order:

        for r in order:

            if len(result) >= DYNAMIC_LIMIT:
                return result

            if r["setup"] != st:
                continue

            if r["symbol"] in used:
                continue

            if (
                st == "EXTENDED"
                and ext >= 2
            ):
                continue

            sc = r[
                "discoveryV2"
            ][
                "score"
            ]

            if (
                st in {
                    "BREAKOUT",
                    "PULLBACK",
                }
                and sc >= 70
            ):
                tier = "A"

            elif (
                st != "EXTENDED"
                and sc >= 60
            ):
                tier = "B"

            else:
                tier = "C"

            result.append(
                {
                    "symbol":
                        r["symbol"],

                    "name":
                        r["name"],

                    "market":
                        r["market"],

                    "tier":
                        tier,

                    "score":
                        sc,

                    "setup":
                        st,
                }
            )

            used.add(
                r["symbol"]
            )

            if st == "EXTENDED":
                ext += 1

    return result


def main():

    tse = load_daily(
        TWSE_DAILY,
        norm_twse,
        "TSE",
    )

    otc = load_daily(
        TPEX_DAILY,
        norm_tpex,
        "OTC",
    )

    sm_tse = security_master(
        2,
        "TSE",
    )

    sm_otc = security_master(
        4,
        "OTC",
    )

    master = (
        {
            (
                "TSE",
                s,
            )
            for s
            in sm_tse[
                "items"
            ]
        }
        |
        {
            (
                "OTC",
                s,
            )
            for s
            in sm_otc[
                "items"
            ]
        }
    )

    raw = (
        tse[
            "rows"
        ]
        +
        otc[
            "rows"
        ]
    )

    common = [
        r
        for r in raw
        if (
            r[
                "market"
            ],
            r[
                "symbol"
            ],
        )
        in master
    ]

    eligible = [
        r
        for r in common
        if (
            r.get(
                "tradeValue"
            )
            or 0
        )
        >=
        MIN_TRADE_VALUE
        and r.get(
            "closePrice"
        )
    ]

    cheap_scan(
        eligible
    )

    candidates = pick_history(
        eligible
    )

    sessions = [
        date.fromisoformat(
            r["date"]
        )
        for r in raw
        if r.get(
            "date"
        )
    ]

    latest = max(
        sessions
    )

    bh = history(
        BENCHMARK,
        "TSE",
        latest,
    )

    b5 = ret(
        bh,
        5,
    )

    b20 = ret(
        bh,
        20,
    )

    enriched = []
    errors = []

    for r in candidates:

        try:

            f = features(
                r,
                history(
                    r[
                        "symbol"
                    ],
                    r[
                        "market"
                    ],
                    latest,
                ),
                b5,
                b20,
            )

            if (
                f[
                    "historySessions"
                ]
                < 21
            ):
                raise RuntimeError(
                    f"only {f['historySessions']} sessions"
                )

            x = dict(r)

            x[
                "historyFeatures"
            ] = f

            enriched.append(
                x
            )

        except Exception as e:

            errors.append(
                {
                    "symbol":
                        r[
                            "symbol"
                        ],

                    "market":
                        r[
                            "market"
                        ],

                    "error":
                        str(e),
                }
            )

    score_v2(
        enriched
    )

    ranked = sorted(
        enriched,
        key=lambda r:
            r[
                "discoveryV2"
            ][
                "score"
            ],
        reverse=True,
    )

    counts = {}

    for r in ranked:
        counts[
            r[
                "setup"
            ]
        ] = (
            counts.get(
                r[
                    "setup"
                ],
                0,
            )
            +
            1
        )

    payload = {
        "ok":
            bool(
                tse[
                    "ok"
                ]
                or
                otc[
                    "ok"
                ]
            ),

        "generatedAt":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "version":
            "discovery-github-v2",

        "stage":
            "history-enriched-discovery-v2",

        "sources": {
            "TSE": {
                "ok":
                    tse[
                        "ok"
                    ],

                "rawRowCount":
                    tse[
                        "raw"
                    ],

                "error":
                    tse[
                        "error"
                    ],
            },

            "OTC": {
                "ok":
                    otc[
                        "ok"
                    ],

                "rawRowCount":
                    otc[
                        "raw"
                    ],

                "error":
                    otc[
                        "error"
                    ],
            },
        },

        "securityMaster": {
            "ok":
                sm_tse[
                    "ok"
                ]
                and
                sm_otc[
                    "ok"
                ],

            "method":
                "TWSE ISIN; CFI starts with ES",

            "TSE": {
                "ok":
                    sm_tse[
                        "ok"
                    ],

                "count":
                    sm_tse[
                        "count"
                    ],

                "error":
                    sm_tse[
                        "error"
                    ],
            },

            "OTC": {
                "ok":
                    sm_otc[
                        "ok"
                    ],

                "count":
                    sm_otc[
                        "count"
                    ],

                "error":
                    sm_otc[
                        "error"
                    ],
            },
        },

        "rawUniverseCount":
            len(raw),

        "universeCount":
            len(common),

        "commonStockUniverseCount":
            len(common),

        "minTradeValue":
            MIN_TRADE_VALUE,

        "eligibleCount":
            len(
                eligible
            ),

        "historyCandidateCount":
            len(
                candidates
            ),

        "historyEnrichedCount":
            len(
                enriched
            ),

        "historyErrorCount":
            len(
                errors
            ),

        "historyErrors":
            errors[
                :20
            ],

        "benchmark": {
            "symbol":
                BENCHMARK,

            "return5":
                rnd(
                    b5
                ),

            "return20":
                rnd(
                    b20
                ),
        },

        "setupCounts":
            counts,

        "dynamicPreview":
            dynamic_preview(
                ranked
            ),

        "topDiscovery":
            ranked[
                :RESULT_LIMIT
            ],
    }

    Path(
        "data"
    ).mkdir(
        exist_ok=True
    )

    Path(
        "data/discovery-scan.json"
    ).write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "ok":
                    payload[
                        "ok"
                    ],

                "version":
                    payload[
                        "version"
                    ],

                "securityMaster":
                    payload[
                        "securityMaster"
                    ],

                "commonStockUniverseCount":
                    payload[
                        "commonStockUniverseCount"
                    ],

                "eligibleCount":
                    payload[
                        "eligibleCount"
                    ],

                "historyEnrichedCount":
                    payload[
                        "historyEnrichedCount"
                    ],

                "historyErrorCount":
                    payload[
                        "historyErrorCount"
                    ],

                "setupCounts":
                    payload[
                        "setupCounts"
                    ],

                "dynamicPreview":
                    payload[
                        "dynamicPreview"
                    ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
