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


# ============================================================
# Data Sources
# ============================================================

TWSE_DAILY = (
    "https://openapi.twse.com.tw/"
    "v1/exchangeReport/STOCK_DAY_ALL"
)

TPEX_DAILY = (
    "https://www.tpex.org.tw/"
    "openapi/v1/"
    "tpex_mainboard_daily_close_quotes"
)

TWSE_HIST = (
    "https://www.twse.com.tw/"
    "exchangeReport/STOCK_DAY"
)

TPEX_HIST = (
    "https://www.tpex.org.tw/"
    "www/zh-tw/afterTrading/"
    "tradingStock/st43_result.php"
)

ISIN = (
    "https://isin.twse.com.tw/"
    "isin/C_public.jsp"
)


# ============================================================
# Config
# ============================================================

MIN_TRADE_VALUE = int(
    os.getenv(
        "MIN_TRADE_VALUE",
        "50000000",
    )
)

RESULT_LIMIT = int(
    os.getenv(
        "RESULT_LIMIT",
        "30",
    )
)

HISTORY_LIMIT = int(
    os.getenv(
        "HISTORY_CANDIDATE_LIMIT",
        "40",
    )
)

DYNAMIC_LIMIT = int(
    os.getenv(
        "DYNAMIC_PREVIEW_LIMIT",
        "11",
    )
)

BENCHMARK = os.getenv(
    "BENCHMARK_SYMBOL",
    "0050",
)


# ============================================================
# HTTP Session
# ============================================================

S = requests.Session()

S.headers.update(
    {
        "User-Agent":
            "fugle-market-data-discovery/2.1",
    }
)


# ============================================================
# Basic Helpers
# ============================================================

def num(v):
    if v is None:
        return None

    if isinstance(
        v,
        (int, float),
    ):
        if (
            isinstance(v, float)
            and math.isnan(v)
        ):
            return None

        return float(v)

    t = (
        str(v)
        .strip()
        .replace(",", "")
        .replace("%", "")
        .replace("+", "")
    )

    if t in {
        "",
        "-",
        "--",
        "---",
        "N/A",
        "null",
        "None",
    }:
        return None

    m = re.search(
        r"-?\d+(?:\.\d+)?",
        t,
    )

    if not m:
        return None

    return float(
        m.group()
    )


def rnd(
    v,
    n=3,
):
    if v is None:
        return None

    return round(
        float(v),
        n,
    )


def get_json(
    url,
    params=None,
):
    last = None

    for i in range(3):
        try:
            r = S.get(
                url,
                params=params,
                timeout=30,
                headers={
                    "Accept":
                        "application/json",
                },
            )

            r.raise_for_status()

            time.sleep(
                0.12
            )

            return r.json()

        except Exception as e:
            last = e

            time.sleep(
                0.7
                * (i + 1)
            )

    raise RuntimeError(
        f"GET failed {url}: {last}"
    )


def roc_date(v):
    if v is None:
        return None

    d = re.sub(
        r"\D",
        "",
        str(v),
    )

    try:
        # YYYYMMDD
        if len(d) == 8:
            return date(
                int(d[:4]),
                int(d[4:6]),
                int(d[6:8]),
            ).isoformat()

        # YYYMMDD 民國年
        if len(d) == 7:
            return date(
                int(d[:3]) + 1911,
                int(d[3:5]),
                int(d[5:7]),
            ).isoformat()

    except ValueError:
        return None

    return None


def change_pct(
    close,
    change,
):
    if (
        close is None
        or change is None
    ):
        return None

    prev = (
        close
        - change
    )

    if prev == 0:
        return None

    return (
        change
        / prev
        * 100
    )


# ============================================================
# Daily Market Normalization
# ============================================================

def norm_twse(x):
    s = str(
        x.get(
            "Code",
            "",
        )
    ).strip()

    if not (
        len(s) == 4
        and s.isdigit()
    ):
        return None

    c = num(
        x.get(
            "ClosingPrice"
        )
    )

    ch = num(
        x.get(
            "Change"
        )
    )

    return {
        "symbol":
            s,

        "name":
            str(
                x.get(
                    "Name",
                    "",
                )
            ).strip(),

        "market":
            "TSE",

        "date":
            roc_date(
                x.get(
                    "Date"
                )
            ),

        "openPrice":
            num(
                x.get(
                    "OpeningPrice"
                )
            ),

        "highPrice":
            num(
                x.get(
                    "HighestPrice"
                )
            ),

        "lowPrice":
            num(
                x.get(
                    "LowestPrice"
                )
            ),

        "closePrice":
            c,

        "change":
            ch,

        "changePercent":
            rnd(
                change_pct(
                    c,
                    ch,
                )
            ),

        "tradeVolume":
            num(
                x.get(
                    "TradeVolume"
                )
            ),

        "tradeValue":
            num(
                x.get(
                    "TradeValue"
                )
            ),
    }


def norm_tpex(x):
    s = str(
        x.get(
            "SecuritiesCompanyCode",
            "",
        )
    ).strip()

    if not (
        len(s) == 4
        and s.isdigit()
    ):
        return None

    c = num(
        x.get(
            "Close"
        )
    )

    ch = num(
        x.get(
            "Change"
        )
    )

    vol = num(
        x.get(
            "TradingShares"
        )
    )

    val = next(
        (
            num(
                x.get(k)
            )
            for k in (
                "TransactionAmount",
                "TradingValue",
                "TradeValue",
                "TradeAmount",
            )
            if num(
                x.get(k)
            ) is not None
        ),
        None,
    )

    # 若 TPEx OpenAPI 沒有成交金額欄位，
    # 用收盤價 × 成交股數估算成交額。
    if (
        val is None
        and vol is not None
        and c is not None
    ):
        val = (
            vol
            * c
        )

    return {
        "symbol":
            s,

        "name":
            str(
                x.get(
                    "CompanyName",
                    "",
                )
            ).strip(),

        "market":
            "OTC",

        "date":
            roc_date(
                x.get(
                    "Date"
                )
            ),

        "openPrice":
            num(
                x.get(
                    "Open"
                )
            ),

        "highPrice":
            num(
                x.get(
                    "High"
                )
            ),

        "lowPrice":
            num(
                x.get(
                    "Low"
                )
            ),

        "closePrice":
            c,

        "change":
            ch,

        "changePercent":
            rnd(
                change_pct(
                    c,
                    ch,
                )
            ),

        "tradeVolume":
            vol,

        "tradeValue":
            val,
    }


def add_intraday(r):
    h = r.get(
        "highPrice"
    )

    l = r.get(
        "lowPrice"
    )

    c = r.get(
        "closePrice"
    )

    if (
        None not in (
            h,
            l,
            c,
        )
        and h != l
    ):
        r[
            "closePosition"
        ] = rnd(
            (
                c - l
            )
            /
            (
                h - l
            ),
            4,
        )

    else:
        r[
            "closePosition"
        ] = None

    return r


def load_daily(
    url,
    fn,
    market,
):
    try:
        raw = get_json(
            url
        )

        rows = [
            add_intraday(y)
            for x in raw
            if (
                isinstance(
                    x,
                    dict,
                )
                and
                (
                    y := fn(x)
                )
            )
        ]

        return {
            "ok":
                bool(rows),

            "market":
                market,

            "raw":
                len(raw),

            "rows":
                rows,

            "error":
                None,
        }

    except Exception as e:
        return {
            "ok":
                False,

            "market":
                market,

            "raw":
                0,

            "rows":
                [],

            "error":
                str(e),
        }


# ============================================================
# Security Master
# 真正普通股過濾：CFI code ESxxxx
# ============================================================

def security_master(
    str_mode,
    market,
):
    try:
        r = S.get(
            ISIN,
            params={
                "strMode":
                    str_mode,
            },
            timeout=30,
        )

        r.raise_for_status()

        text = (
            r.content
            .decode(
                "big5",
                errors="replace",
            )
        )

        out = {}

        for tr in re.findall(
            r"<tr[^>]*>(.*?)</tr>",
            text,
            flags=(
                re.I
                | re.S
            ),
        ):
            cells = []

            for td in re.findall(
                r"<td[^>]*>(.*?)</td>",
                tr,
                flags=(
                    re.I
                    | re.S
                ),
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

                cells.append(
                    t
                )

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
                    for c
                    in cells
                    if re.fullmatch(
                        r"ES[A-Z0-9]{4}",
                        c.upper(),
                    )
                ),
                None,
            )

            if cfi:
                out[
                    m.group(1)
                ] = {
                    "name":
                        m.group(
                            2
                        ).strip(),

                    "market":
                        market,

                    "cfi":
                        cfi,
                }

        return {
            "ok":
                bool(out),

            "count":
                len(out),

            "items":
                out,

            "error":
                None,
        }

    except Exception as e:
        return {
            "ok":
                False,

            "count":
                0,

            "items":
                {},

            "error":
                str(e),
        }


# ============================================================
# Cheap Scan
# ============================================================

def percentile(
    vals,
    v,
):
    if (
        v is None
        or len(vals) < 2
    ):
        return 0.0

    return (
        (
            sum(
                x <= v
                for x in vals
            )
            - 1
        )
        /
        (
            len(vals)
            - 1
        )
        * 100
    )


def cheap_scan(rows):
    liq = sorted(
        r[
            "tradeValue"
        ]
        for r in rows
        if r.get(
            "tradeValue"
        ) is not None
    )

    mom = sorted(
        r[
            "changePercent"
        ]
        for r in rows
        if r.get(
            "changePercent"
        ) is not None
    )

    for r in rows:
        lp = percentile(
            liq,
            r.get(
                "tradeValue"
            ),
        )

        mp = percentile(
            mom,
            r.get(
                "changePercent"
            ),
        )

        cp = (
            50
            if r.get(
                "closePosition"
            ) is None
            else max(
                0,
                min(
                    100,
                    r[
                        "closePosition"
                    ]
                    * 100,
                ),
            )
        )

        r[
            "cheapScan"
        ] = {
            "liquidityPercentile":
                rnd(
                    lp,
                    2,
                ),

            "momentumPercentile":
                rnd(
                    mp,
                    2,
                ),

            "closeStrengthScore":
                rnd(
                    cp,
                    2,
                ),

            "discoveryScore":
                rnd(
                    lp
                    * 0.40
                    +
                    mp
                    * 0.35
                    +
                    cp
                    * 0.25,
                    2,
                ),
        }


# ============================================================
# Pick only ~40 stocks for expensive history enrichment
# ============================================================

def pick_history(rows):
    order = sorted(
        rows,
        key=lambda r:
            r[
                "cheapScan"
            ][
                "discoveryScore"
            ],
        reverse=True,
    )

    normal = [
        r
        for r in order
        if (
            r.get(
                "changePercent"
            )
            or 0
        ) < 7
    ]

    extended = [
        r
        for r in order
        if (
            r.get(
                "changePercent"
            )
            or 0
        ) >= 7
    ]

    # 最多約 20% 名額給單日過熱股
    extended_cap = min(
        8,
        max(
            2,
            HISTORY_LIMIT
            // 5,
        ),
    )

    selected = (
        normal[
            :
            HISTORY_LIMIT
            - extended_cap
        ]
        +
        extended[
            :
            extended_cap
        ]
    )

    return sorted(
        selected,
        key=lambda r:
            r[
                "cheapScan"
            ][
                "discoveryScore"
            ],
        reverse=True,
    )


# ============================================================
# Historical Data
# ============================================================

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


def parse_hist(
    data,
    unit_multiplier=1,
):
    """
    TWSE historical endpoint:
      成交股數 = 股
      成交金額 = 元
      multiplier = 1

    TPEx historical endpoint:
      成交仟股 = 千股
      成交仟元 = 千元
      multiplier = 1000

    這樣最後全部標準化成：
      volume = 股
      tradeValue = 元
    """

    out = []

    for x in data:
        if (
            not isinstance(
                x,
                list,
            )
            or len(x) < 7
        ):
            continue

        d = roc_date(
            x[0]
        )

        volume = num(
            x[1]
        )

        trade_value = num(
            x[2]
        )

        if d:
            out.append(
                {
                    "date":
                        d,

                    "volume":
                        (
                            volume
                            * unit_multiplier
                            if volume
                            is not None
                            else None
                        ),

                    "tradeValue":
                        (
                            trade_value
                            * unit_multiplier
                            if trade_value
                            is not None
                            else None
                        ),

                    "open":
                        num(
                            x[3]
                        ),

                    "high":
                        num(
                            x[4]
                        ),

                    "low":
                        num(
                            x[5]
                        ),

                    "close":
                        num(
                            x[6]
                        ),
                }
            )

    return out


def history(
    symbol,
    market,
    session,
):
    out = []

    for y, m in months(
        session
    ):

        # --------------------------
        # TWSE
        # --------------------------
        if market == "TSE":
            p = get_json(
                TWSE_HIST,
                {
                    "response":
                        "json",

                    "stockNo":
                        symbol,

                    "date":
                        f"{y:04d}{m:02d}01",
                },
            )

            if (
                isinstance(
                    p,
                    dict,
                )
                and p.get(
                    "stat"
                ) == "OK"
            ):
                out += parse_hist(
                    p.get(
                        "data",
                        [],
                    ),
                    unit_multiplier=1,
                )

        # --------------------------
        # TPEx
        # --------------------------
        else:
            p = get_json(
                TPEX_HIST,
                {
                    "l":
                        "zh-tw",

                    "date":
                        f"{y:04d}/{m:02d}/01",

                    "code":
                        symbol,
                },
            )

            tables = (
                p.get(
                    "tables",
                    [],
                )
                if isinstance(
                    p,
                    dict,
                )
                else []
            )

            if tables:
                # TPEx 歷史資料：
                # 成交仟股 / 成交仟元
                # 必須 ×1000 才能與今日 OpenAPI 單位一致
                out += parse_hist(
                    tables[
                        0
                    ].get(
                        "data",
                        [],
                    ),
                    unit_multiplier=1000,
                )

    return sorted(
        {
            x[
                "date"
            ]:
                x
            for x in out
        }.values(),
        key=lambda x:
            x[
                "date"
            ],
    )


def merged_history(
    hist,
    row,
):
    by = {
        x[
            "date"
        ]:
            x
        for x in hist
    }

    if row.get(
        "date"
    ):
        by[
            row[
                "date"
            ]
        ] = {
            "date":
                row[
                    "date"
                ],

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
            x[
                "date"
            ],
    )


# ============================================================
# Historical Features
# ============================================================

def ret(
    hist,
    n,
):
    closes = [
        x[
            "close"
        ]
        for x in hist
        if x.get(
            "close"
        ) not in (
            None,
            0,
        )
    ]

    if len(
        closes
    ) < n + 1:
        return None

    return (
        closes[-1]
        /
        closes[
            -n - 1
        ]
        - 1
    ) * 100


def mean_last(
    vals,
    n,
):
    if len(
        vals
    ) < n:
        return None

    return statistics.fmean(
        vals[
            -n:
        ]
    )


def features(
    row,
    hist,
    benchmark_return5,
    benchmark_return20,
):
    h = merged_history(
        hist,
        row,
    )

    valid = [
        x
        for x in h
        if x.get(
            "close"
        ) not in (
            None,
            0,
        )
    ]

    return5 = ret(
        valid,
        5,
    )

    return20 = ret(
        valid,
        20,
    )

    closes = [
        float(
            x[
                "close"
            ]
        )
        for x in valid
    ]

    ma20 = mean_last(
        closes,
        20,
    )

    # 不包含今天，避免今天成交量被放進平均值
    prev_vol = [
        float(
            x[
                "volume"
            ]
        )
        for x in valid[
            :-1
        ]
        if x.get(
            "volume"
        ) not in (
            None,
            0,
        )
    ]

    avg_volume20 = mean_last(
        prev_vol,
        20,
    )

    rvol20 = (
        row[
            "tradeVolume"
        ]
        /
        avg_volume20
        if (
            avg_volume20
            and row.get(
                "tradeVolume"
            )
        )
        else None
    )

    # Prior 20D High 不包含今天
    prior = valid[
        :-1
    ][
        -20:
    ]

    highs = [
        x[
            "high"
        ]
        for x in prior
        if x.get(
            "high"
        ) not in (
            None,
            0,
        )
    ]

    high20 = (
        max(
            highs
        )
        if highs
        else None
    )

    close = row.get(
        "closePrice"
    )

    distance_high20 = (
        (
            close
            /
            high20
            - 1
        )
        * 100
        if (
            close
            and high20
        )
        else None
    )

    distance_ma20 = (
        (
            close
            /
            ma20
            - 1
        )
        * 100
        if (
            close
            and ma20
        )
        else None
    )

    return {
        "historySessions":
            len(valid),

        "return5":
            rnd(
                return5
            ),

        "return20":
            rnd(
                return20
            ),

        "rs5":
            rnd(
                return5
                -
                benchmark_return5
            )
            if (
                return5 is not None
                and benchmark_return5
                is not None
            )
            else None,

        "rs20":
            rnd(
                return20
                -
                benchmark_return20
            )
            if (
                return20 is not None
                and benchmark_return20
                is not None
            )
            else None,

        "rvol20":
            rnd(
                rvol20
            ),

        "avgVolume20":
            rnd(
                avg_volume20
            ),

        "ma20":
            rnd(
                ma20
            ),

        "prior20High":
            rnd(
                high20
            ),

        "distanceFrom20DHighPct":
            rnd(
                distance_high20
            ),

        "distanceFromMA20Pct":
            rnd(
                distance_ma20
            ),
    }


# ============================================================
# Setup Classification
# ============================================================

def setup(r):
    f = r[
        "historyFeatures"
    ]

    change = (
        r.get(
            "changePercent"
        )
        or 0
    )

    close_position = (
        0.5
        if r.get(
            "closePosition"
        ) is None
        else r[
            "closePosition"
        ]
    )

    return5 = f.get(
        "return5"
    )

    return20 = f.get(
        "return20"
    )

    rs20 = f.get(
        "rs20"
    )

    rvol20 = f.get(
        "rvol20"
    )

    distance_high = f.get(
        "distanceFrom20DHighPct"
    )

    distance_ma = f.get(
        "distanceFromMA20Pct"
    )

    # ========================================================
    # EXTENDED
    # 單日太強或離 MA20 太遠
    # ========================================================
    if (
        change >= 7
        or (
            distance_ma
            is not None
            and distance_ma >= 14
        )
    ):
        return "EXTENDED"

    # ========================================================
    # BREAKOUT
    # ========================================================
    if (
        distance_high
        is not None
        and distance_high >= -1
        and (
            rvol20
            or 0
        ) >= 1.15
        and close_position >= 0.70
        and (
            rs20
            or 0
        ) > 0
    ):
        return "BREAKOUT"

    # ========================================================
    # PULLBACK
    # ========================================================
    if (
        return20
        is not None
        and return20 > 3
        and distance_ma
        is not None
        and (
            -1.5
            <= distance_ma
            <= 6
        )
        and distance_high
        is not None
        and (
            -12
            <= distance_high
            <= -2
        )
        and return5
        is not None
        and (
            -6
            <= return5
            <= 2.5
        )
    ):
        return "PULLBACK"

    # ========================================================
    # REVERSAL
    # ========================================================
    if (
        return5
        is not None
        and return5 <= 2
        and change > 0
        and close_position >= 0.75
        and distance_ma
        is not None
        and distance_ma >= -2.5
    ):
        return "REVERSAL"

    # ========================================================
    # TREND MOMENTUM
    # ========================================================
    if (
        return20
        is not None
        and return20 > 4
        and (
            rs20
            or 0
        ) > 0
        and distance_ma
        is not None
        and distance_ma > 0
        and close_position >= 0.55
    ):
        return "TREND_MOMENTUM"

    return "GENERAL_WATCH"


# ============================================================
# V2 Score
# ============================================================

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
    rs5_values = sorted(
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
        ) is not None
    )

    rs20_values = sorted(
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
        ) is not None
    )

    rvol_values = sorted(
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
        ) is not None
    )

    setup_bonus = {
        "BREAKOUT":
            5,

        "PULLBACK":
            5,

        "REVERSAL":
            3,

        "TREND_MOMENTUM":
            3,

        "GENERAL_WATCH":
            0,

        "EXTENDED":
            -12,
    }

    for r in rows:
        f = r[
            "historyFeatures"
        ]

        st = setup(
            r
        )

        p5 = percentile(
            rs5_values,
            f.get(
                "rs5"
            ),
        )

        p20 = percentile(
            rs20_values,
            f.get(
                "rs20"
            ),
        )

        p_rvol = percentile(
            rvol_values,
            f.get(
                "rvol20"
            ),
        )

        high_proximity = high_score(
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
            p_rvol
            * 0.20
            +
            high_proximity
            * 0.15
            +
            setup_bonus[
                st
            ]
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
                    p_rvol,
                    2,
                ),

            "highProximityScore":
                rnd(
                    high_proximity,
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


# ============================================================
# Dynamic Preview
# 尚未正式寫 watchlist-dynamic.json
# ============================================================

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

    extended_count = 0

    setup_order = (
        "BREAKOUT",
        "PULLBACK",
        "REVERSAL",
        "TREND_MOMENTUM",
        "GENERAL_WATCH",
        "EXTENDED",
    )

    for setup_type in setup_order:

        for r in order:

            if (
                len(result)
                >= DYNAMIC_LIMIT
            ):
                return result

            if (
                r[
                    "setup"
                ]
                != setup_type
            ):
                continue

            if (
                r[
                    "symbol"
                ]
                in used
            ):
                continue

            if (
                setup_type
                == "EXTENDED"
                and extended_count >= 2
            ):
                continue

            score = r[
                "discoveryV2"
            ][
                "score"
            ]

            if (
                setup_type
                in {
                    "BREAKOUT",
                    "PULLBACK",
                }
                and score >= 70
            ):
                tier = "A"

            elif (
                setup_type
                != "EXTENDED"
                and score >= 60
            ):
                tier = "B"

            else:
                tier = "C"

            result.append(
                {
                    "symbol":
                        r[
                            "symbol"
                        ],

                    "name":
                        r[
                            "name"
                        ],

                    "market":
                        r[
                            "market"
                        ],

                    "tier":
                        tier,

                    "score":
                        score,

                    "setup":
                        setup_type,
                }
            )

            used.add(
                r[
                    "symbol"
                ]
            )

            if (
                setup_type
                == "EXTENDED"
            ):
                extended_count += 1

    return result


# ============================================================
# Main
# ============================================================

def main():

    # --------------------------------------------------------
    # 1. Full Market Daily
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # 2. True Common-Stock Security Master
    # --------------------------------------------------------

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
                symbol,
            )
            for symbol
            in sm_tse[
                "items"
            ]
        }
        |
        {
            (
                "OTC",
                symbol,
            )
            for symbol
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
        ) in master
    ]

    # --------------------------------------------------------
    # 3. Liquidity Filter
    # --------------------------------------------------------

    eligible = [
        r
        for r in common
        if (
            (
                r.get(
                    "tradeValue"
                )
                or 0
            )
            >= MIN_TRADE_VALUE
            and r.get(
                "closePrice"
            )
        )
    ]

    # --------------------------------------------------------
    # 4. Cheap Scan
    # --------------------------------------------------------

    cheap_scan(
        eligible
    )

    # --------------------------------------------------------
    # 5. Top ~40 for history enrichment
    # --------------------------------------------------------

    candidates = pick_history(
        eligible
    )

    # --------------------------------------------------------
    # Latest trading session
    # --------------------------------------------------------

    sessions = [
        date.fromisoformat(
            r[
                "date"
            ]
        )
        for r in raw
        if r.get(
            "date"
        )
    ]

    latest = max(
        sessions
    )

    # --------------------------------------------------------
    # 6. Benchmark 0050
    # --------------------------------------------------------

    benchmark_history = history(
        BENCHMARK,
        "TSE",
        latest,
    )

    benchmark_return5 = ret(
        benchmark_history,
        5,
    )

    benchmark_return20 = ret(
        benchmark_history,
        20,
    )

    # --------------------------------------------------------
    # 7. History Enrichment
    # --------------------------------------------------------

    enriched = []
    errors = []

    for r in candidates:

        try:
            hist = history(
                r[
                    "symbol"
                ],
                r[
                    "market"
                ],
                latest,
            )

            f = features(
                r,
                hist,
                benchmark_return5,
                benchmark_return20,
            )

            if (
                f[
                    "historySessions"
                ]
                < 21
            ):
                raise RuntimeError(
                    "only "
                    f"{f['historySessions']} "
                    "sessions"
                )

            x = dict(
                r
            )

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

    # --------------------------------------------------------
    # 8. V2 Scoring
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # 9. Setup Counts
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # 10. Output
    # --------------------------------------------------------

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
            "discovery-github-v2.1",

        "stage":
            "history-enriched-discovery-v2.1",

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
                (
                    sm_tse[
                        "ok"
                    ]
                    and
                    sm_otc[
                        "ok"
                    ]
                ),

            "method":
                (
                    "TWSE ISIN; "
                    "CFI starts with ES"
                ),

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
                    benchmark_return5
                ),

            "return20":
                rnd(
                    benchmark_return20
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
                :
                RESULT_LIMIT
            ],
    }

    # --------------------------------------------------------
    # Write JSON
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # GitHub Actions Summary
    # --------------------------------------------------------

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

                "benchmark":
                    payload[
                        "benchmark"
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
