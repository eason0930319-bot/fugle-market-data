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

COMPANY_PROFILE = (
    "https://openapi.twse.com.tw/"
    "v1/opendata/t187ap03_P"
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

INDUSTRY_LIMIT = int(
    os.getenv(
        "DYNAMIC_MAX_PER_INDUSTRY",
        "2",
    )
)

BENCHMARK = os.getenv(
    "BENCHMARK_SYMBOL",
    "0050",
)


# ============================================================
# Dynamic Setup Quotas
# ============================================================

SETUP_QUOTAS = {
    "BREAKOUT": 3,
    "PULLBACK": 3,
    "REVERSAL": 3,
    "TREND_MOMENTUM": 3,
    "GENERAL_WATCH": 1,
    "EXTENDED": 1,
}


# ============================================================
# HTTP
# ============================================================

S = requests.Session()

S.headers.update(
    {
        "User-Agent":
            "fugle-market-data-discovery/2.2",
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

        # 民國 YYYMMDD
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
# TWSE Daily
# ============================================================

def norm_twse(x):
    symbol = str(
        x.get(
            "Code",
            "",
        )
    ).strip()

    if not (
        len(symbol) == 4
        and symbol.isdigit()
    ):
        return None

    close = num(
        x.get(
            "ClosingPrice"
        )
    )

    change = num(
        x.get(
            "Change"
        )
    )

    return {
        "symbol":
            symbol,

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
            close,

        "change":
            change,

        "changePercent":
            rnd(
                change_pct(
                    close,
                    change,
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


# ============================================================
# TPEx Daily
# ============================================================

def norm_tpex(x):
    symbol = str(
        x.get(
            "SecuritiesCompanyCode",
            "",
        )
    ).strip()

    if not (
        len(symbol) == 4
        and symbol.isdigit()
    ):
        return None

    close = num(
        x.get(
            "Close"
        )
    )

    change = num(
        x.get(
            "Change"
        )
    )

    volume = num(
        x.get(
            "TradingShares"
        )
    )

    trade_value = None

    for key in (
        "TransactionAmount",
        "TradingValue",
        "TradeValue",
        "TradeAmount",
    ):
        value = num(
            x.get(
                key
            )
        )

        if value is not None:
            trade_value = value
            break

    if (
        trade_value is None
        and volume is not None
        and close is not None
    ):
        trade_value = (
            volume
            * close
        )

    return {
        "symbol":
            symbol,

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
            close,

        "change":
            change,

        "changePercent":
            rnd(
                change_pct(
                    close,
                    change,
                )
            ),

        "tradeVolume":
            volume,

        "tradeValue":
            trade_value,
    }


# ============================================================
# Intraday Derived Features
# ============================================================

def add_intraday(row):
    high = row.get(
        "highPrice"
    )

    low = row.get(
        "lowPrice"
    )

    close = row.get(
        "closePrice"
    )

    if (
        None not in (
            high,
            low,
            close,
        )
        and high != low
    ):
        row[
            "closePosition"
        ] = rnd(
            (
                close - low
            )
            /
            (
                high - low
            ),
            4,
        )

    else:
        row[
            "closePosition"
        ] = None

    return row


def load_daily(
    url,
    normalizer,
    market,
):
    try:
        raw = get_json(
            url
        )

        rows = []

        for item in raw:
            if not isinstance(
                item,
                dict,
            ):
                continue

            row = normalizer(
                item
            )

            if row:
                rows.append(
                    add_intraday(
                        row
                    )
                )

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
# Common Stock Security Master
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
# Industry Master
#
# Uses:
# TWSE OpenAPI t187ap03_P
# 公開發行公司基本資料
#
# Only one request is needed for all companies.
# ============================================================

def industry_master():
    try:
        raw = get_json(
            COMPANY_PROFILE
        )

        items = {}

        for row in raw:
            if not isinstance(
                row,
                dict,
            ):
                continue

            symbol = str(
                row.get(
                    "公司代號",
                    "",
                )
            ).strip()

            if not (
                len(symbol) == 4
                and symbol.isdigit()
            ):
                continue

            industry = str(
                row.get(
                    "產業別",
                    "",
                )
            ).strip()

            if not industry:
                industry = "UNKNOWN"

            items[
                symbol
            ] = industry

        return {
            "ok":
                bool(items),

            "count":
                len(items),

            "items":
                items,

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
# Percentile
# ============================================================

def percentile(
    vals,
    value,
):
    if (
        value is None
        or len(vals) < 2
    ):
        return 0.0

    return (
        (
            sum(
                x <= value
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


# ============================================================
# Cheap Scan
# ============================================================

def cheap_scan(rows):
    liquidity = sorted(
        r[
            "tradeValue"
        ]
        for r in rows
        if r.get(
            "tradeValue"
        ) is not None
    )

    momentum = sorted(
        r[
            "changePercent"
        ]
        for r in rows
        if r.get(
            "changePercent"
        ) is not None
    )

    for row in rows:
        lp = percentile(
            liquidity,
            row.get(
                "tradeValue"
            ),
        )

        mp = percentile(
            momentum,
            row.get(
                "changePercent"
            ),
        )

        cp = (
            50
            if row.get(
                "closePosition"
            ) is None
            else max(
                0,
                min(
                    100,
                    row[
                        "closePosition"
                    ]
                    * 100,
                ),
            )
        )

        row[
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
# Select History Candidates
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
# History
# ============================================================

def months(session):
    current = (
        session.year,
        session.month,
    )

    if (
        session.month
        == 1
    ):
        previous = (
            session.year - 1,
            12,
        )

    else:
        previous = (
            session.year,
            session.month - 1,
        )

    return [
        previous,
        current,
    ]


def parse_hist(
    data,
    unit_multiplier=1,
):
    out = []

    for row in data:
        if (
            not isinstance(
                row,
                list,
            )
            or len(row) < 7
        ):
            continue

        d = roc_date(
            row[0]
        )

        if not d:
            continue

        volume = num(
            row[1]
        )

        trade_value = num(
            row[2]
        )

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
                        row[3]
                    ),

                "high":
                    num(
                        row[4]
                    ),

                "low":
                    num(
                        row[5]
                    ),

                "close":
                    num(
                        row[6]
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

    for year, month in months(
        session
    ):

        # TWSE
        if market == "TSE":
            payload = get_json(
                TWSE_HIST,
                {
                    "response":
                        "json",

                    "stockNo":
                        symbol,

                    "date":
                        (
                            f"{year:04d}"
                            f"{month:02d}"
                            "01"
                        ),
                },
            )

            if (
                isinstance(
                    payload,
                    dict,
                )
                and payload.get(
                    "stat"
                ) == "OK"
            ):
                out += parse_hist(
                    payload.get(
                        "data",
                        [],
                    ),
                    unit_multiplier=1,
                )

        # TPEx
        else:
            payload = get_json(
                TPEX_HIST,
                {
                    "l":
                        "zh-tw",

                    "date":
                        (
                            f"{year:04d}/"
                            f"{month:02d}/01"
                        ),

                    "code":
                        symbol,
                },
            )

            tables = (
                payload.get(
                    "tables",
                    [],
                )
                if isinstance(
                    payload,
                    dict,
                )
                else []
            )

            if tables:
                # TPEx history:
                # 成交仟股 / 成交仟元
                # convert to 股 / 元
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
    by_date = {
        x[
            "date"
        ]:
            x
        for x in hist
    }

    if row.get(
        "date"
    ):
        by_date[
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
        by_date.values(),
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
        (
            closes[-1]
            /
            closes[
                -n - 1
            ]
        )
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
    merged = merged_history(
        hist,
        row,
    )

    valid = [
        x
        for x in merged
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

    previous_volume = [
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
        previous_volume,
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
            (
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
                else None
            ),

        "rs20":
            (
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
                else None
            ),

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

def setup(row):
    f = row[
        "historyFeatures"
    ]

    change = (
        row.get(
            "changePercent"
        )
        or 0
    )

    close_position = (
        0.5
        if row.get(
            "closePosition"
        ) is None
        else row[
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

    # --------------------------------------------------------
    # EXTENDED
    # --------------------------------------------------------

    if (
        change >= 7
        or (
            distance_ma
            is not None
            and distance_ma >= 14
        )
    ):
        return "EXTENDED"

    # --------------------------------------------------------
    # BREAKOUT
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # PULLBACK
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # REVERSAL
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # TREND MOMENTUM
    # --------------------------------------------------------

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

def high_score(
    distance,
):
    if distance is None:
        return 0

    if distance >= 0:
        return 100

    return max(
        0,
        100
        +
        distance
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

    for row in rows:
        f = row[
            "historyFeatures"
        ]

        st = setup(
            row
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

        proximity = high_score(
            f.get(
                "distanceFrom20DHighPct"
            )
        )

        total = (
            row[
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
            proximity
            * 0.15
            +
            setup_bonus[
                st
            ]
        )

        row[
            "setup"
        ] = st

        row[
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
                    proximity,
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
# Tier
# ============================================================

def tier_for(row):
    score = row[
        "discoveryV2"
    ][
        "score"
    ]

    st = row[
        "setup"
    ]

    if (
        st in {
            "BREAKOUT",
            "PULLBACK",
        }
        and score >= 70
    ):
        return "A"

    if (
        st != "EXTENDED"
        and score >= 60
    ):
        return "B"

    return "C"


# ============================================================
# Dynamic Preview V2.2
#
# Two hard concentration controls:
#
# 1. Setup quota
# 2. Industry quota
#
# Dynamic can contain fewer than 11.
# We prefer quality/diversification over filling every slot.
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

    setup_counts = {
        key:
            0
        for key
        in SETUP_QUOTAS
    }

    industry_counts = {}

    for row in order:
        if (
            len(result)
            >= DYNAMIC_LIMIT
        ):
            break

        symbol = row[
            "symbol"
        ]

        if symbol in used:
            continue

        st = row[
            "setup"
        ]

        # ----------------------------------------------------
        # Setup quota
        # ----------------------------------------------------

        if (
            setup_counts.get(
                st,
                0,
            )
            >=
            SETUP_QUOTAS.get(
                st,
                0,
            )
        ):
            continue

        industry = str(
            row.get(
                "industryCode"
            )
            or "UNKNOWN"
        )

        # ----------------------------------------------------
        # Industry quota
        #
        # If industry API somehow fails for one stock,
        # UNKNOWN does not block all other candidates.
        # ----------------------------------------------------

        industry_cap = (
            DYNAMIC_LIMIT
            if industry == "UNKNOWN"
            else INDUSTRY_LIMIT
        )

        if (
            industry_counts.get(
                industry,
                0,
            )
            >= industry_cap
        ):
            continue

        result.append(
            {
                "symbol":
                    symbol,

                "name":
                    row[
                        "name"
                    ],

                "market":
                    row[
                        "market"
                    ],

                "industryCode":
                    industry,

                "tier":
                    tier_for(
                        row
                    ),

                "score":
                    row[
                        "discoveryV2"
                    ][
                        "score"
                    ],

                "setup":
                    st,

                "reasonCodes": [
                    (
                        "SETUP_"
                        + st
                    ),
                    (
                        "INDUSTRY_"
                        + industry
                    ),
                ],
            }
        )

        used.add(
            symbol
        )

        setup_counts[
            st
        ] = (
            setup_counts.get(
                st,
                0,
            )
            + 1
        )

        industry_counts[
            industry
        ] = (
            industry_counts.get(
                industry,
                0,
            )
            + 1
        )

    return {
        "candidates":
            result,

        "selectedCount":
            len(
                result
            ),

        "setupCounts": {
            key:
                value
            for key, value
            in setup_counts.items()
            if value > 0
        },

        "industryCounts":
            industry_counts,
    }


# ============================================================
# Main
# ============================================================

def main():

    # --------------------------------------------------------
    # 1. Full market daily data
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
    # 2. True common-stock master
    # --------------------------------------------------------

    sm_tse = security_master(
        2,
        "TSE",
    )

    sm_otc = security_master(
        4,
        "OTC",
    )

    # --------------------------------------------------------
    # 3. Industry master
    # --------------------------------------------------------

    industries = industry_master()

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
        row
        for row in raw
        if (
            row[
                "market"
            ],
            row[
                "symbol"
            ],
        ) in master
    ]

    # --------------------------------------------------------
    # Attach official broad-industry code
    # --------------------------------------------------------

    for row in common:
        row[
            "industryCode"
        ] = (
            industries[
                "items"
            ].get(
                row[
                    "symbol"
                ],
                "UNKNOWN",
            )
        )

    # --------------------------------------------------------
    # 4. Liquidity filter
    # --------------------------------------------------------

    eligible = [
        row
        for row in common
        if (
            (
                row.get(
                    "tradeValue"
                )
                or 0
            )
            >= MIN_TRADE_VALUE
            and row.get(
                "closePrice"
            )
        )
    ]

    # --------------------------------------------------------
    # 5. Cheap scan
    # --------------------------------------------------------

    cheap_scan(
        eligible
    )

    # --------------------------------------------------------
    # 6. Top 40 expensive enrichment candidates
    # --------------------------------------------------------

    candidates = pick_history(
        eligible
    )

    sessions = [
        date.fromisoformat(
            row[
                "date"
            ]
        )
        for row in raw
        if row.get(
            "date"
        )
    ]

    latest = max(
        sessions
    )

    # --------------------------------------------------------
    # 7. Benchmark
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
    # 8. History enrichment
    # --------------------------------------------------------

    enriched = []

    errors = []

    for row in candidates:
        try:
            f = features(
                row,
                history(
                    row[
                        "symbol"
                    ],
                    row[
                        "market"
                    ],
                    latest,
                ),
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

            item = dict(
                row
            )

            item[
                "historyFeatures"
            ] = f

            enriched.append(
                item
            )

        except Exception as e:
            errors.append(
                {
                    "symbol":
                        row[
                            "symbol"
                        ],

                    "market":
                        row[
                            "market"
                        ],

                    "error":
                        str(e),
                }
            )

    # --------------------------------------------------------
    # 9. Score
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
    # Setup counts before dynamic diversification
    # --------------------------------------------------------

    setup_counts = {}

    for row in ranked:
        st = row[
            "setup"
        ]

        setup_counts[
            st
        ] = (
            setup_counts.get(
                st,
                0,
            )
            + 1
        )

    # --------------------------------------------------------
    # 10. Diversified Dynamic Preview
    # --------------------------------------------------------

    preview = dynamic_preview(
        ranked
    )

    # --------------------------------------------------------
    # 11. Output
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
            "discovery-github-v2.2",

        "stage":
            "diversified-candidate-selection-v2.2",

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

        "industryMaster": {
            "ok":
                industries[
                    "ok"
                ],

            "source":
                (
                    "TWSE OpenAPI "
                    "t187ap03_P "
                    "公開發行公司基本資料"
                ),

            "count":
                industries[
                    "count"
                ],

            "error":
                industries[
                    "error"
                ],
        },

        "rawUniverseCount":
            len(
                raw
            ),

        "commonStockUniverseCount":
            len(
                common
            ),

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
            setup_counts,

        "dynamicSelectionPolicy": {
            "maxCandidates":
                DYNAMIC_LIMIT,

            "maxPerIndustry":
                INDUSTRY_LIMIT,

            "setupQuotas":
                SETUP_QUOTAS,

            "note":
                (
                    "Preview only; "
                    "does not write "
                    "watchlist-dynamic.json."
                ),
        },

        "dynamicPreview":
            preview[
                "candidates"
            ],

        "dynamicPreviewStats": {
            "selectedCount":
                preview[
                    "selectedCount"
                ],

            "setupCounts":
                preview[
                    "setupCounts"
                ],

            "industryCounts":
                preview[
                    "industryCounts"
                ],
        },

        "topDiscovery":
            ranked[
                :
                RESULT_LIMIT
            ],
    }

    # --------------------------------------------------------
    # Save
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
    # GitHub Actions output
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

                "industryMaster":
                    payload[
                        "industryMaster"
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

                "dynamicPreviewStats":
                    payload[
                        "dynamicPreviewStats"
                    ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
