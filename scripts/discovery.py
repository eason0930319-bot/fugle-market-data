from __future__ import annotations

import html
import json
import math
import os
import re
import statistics
import time
from datetime import date, datetime, timedelta, timezone
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
INDUSTRY_LIMIT = int(os.getenv("DYNAMIC_MAX_PER_INDUSTRY", "2"))
MIN_DYNAMIC_SCORE = float(os.getenv("MIN_DYNAMIC_SCORE", "50"))
DYNAMIC_TTL_HOURS = int(os.getenv("DYNAMIC_TTL_HOURS", "72"))
BENCHMARK = os.getenv("BENCHMARK_SYMBOL", "0050")

TAIPEI_TZ = timezone(timedelta(hours=8))

PINNED_DEFAULT = "0050,00713,2303,2308,2330,2357,2408,2454,6757"
PINNED_SYMBOLS = {
    x.strip()
    for x in os.getenv("PINNED_SYMBOLS", PINNED_DEFAULT).split(",")
    if x.strip()
}

SETUP_QUOTAS = {
    "BREAKOUT": 3,
    "PULLBACK": 3,
    "REVERSAL": 3,
    "TREND_MOMENTUM": 3,
    "GENERAL_WATCH": 1,
    "EXTENDED": 1,
}

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 fugle-market-data-discovery/2.5",
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
})


def num(v):
    if v is None:
        return None

    if isinstance(v, (int, float)):
        if isinstance(v, float) and math.isnan(v):
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

    return (
        float(m.group())
        if m
        else None
    )


def rnd(v, n=3):
    return (
        None
        if v is None
        else round(
            float(v),
            n,
        )
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
        pass

    return None


def latest_row_date(rows):
    dates = []

    for row in rows:
        d = row.get(
            "date"
        )

        if not d:
            continue

        try:
            dates.append(
                date.fromisoformat(
                    d
                )
            )

        except ValueError:
            pass

    return (
        max(dates)
        if dates
        else None
    )


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
            headers={
                "Accept":
                    (
                        "text/html,"
                        "application/xhtml+xml"
                    ),
            },
        )

        r.raise_for_status()

        text = (
            r.content
            .decode(
                "big5",
                errors="replace",
            )
        )

        items = {}

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
                cell = html.unescape(
                    re.sub(
                        r"<[^>]+>",
                        "",
                        td,
                    )
                )

                cell = re.sub(
                    r"\s+",
                    " ",
                    cell,
                ).strip()

                cells.append(
                    cell
                )

            if len(cells) < 6:
                continue

            match = re.match(
                r"^(\d{4})\s+(.+)$",
                cells[0],
            )

            if not match:
                continue

            symbol = match.group(
                1
            )

            name = match.group(
                2
            ).strip()

            industry = (
                cells[4].strip()
                if len(cells) > 4
                else ""
            )

            cfi = (
                cells[5]
                .strip()
                .upper()
                if len(cells) > 5
                else ""
            )

            if not re.fullmatch(
                r"ES[A-Z0-9]{4}",
                cfi,
            ):
                continue

            items[
                symbol
            ] = {
                "name":
                    name,

                "market":
                    market,

                "industry":
                    (
                        industry
                        or "UNKNOWN"
                    ),

                "cfi":
                    cfi,
            }

        known = sum(
            1
            for item
            in items.values()
            if item[
                "industry"
            ] != "UNKNOWN"
        )

        return {
            "ok":
                bool(items),

            "count":
                len(items),

            "knownIndustryCount":
                known,

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

            "knownIndustryCount":
                0,

            "items":
                {},

            "error":
                str(e),
        }


def percentile(
    values,
    value,
):
    if (
        value is None
        or len(values) < 2
    ):
        return 0.0

    return (
        (
            sum(
                x <= value
                for x in values
            )
            - 1
        )
        /
        (
            len(values)
            - 1
        )
        * 100
    )


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


def months(session):
    if session.month == 1:
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
        (
            session.year,
            session.month,
        ),
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

        day = roc_date(
            row[0]
        )

        if not day:
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
                    day,

                "volume":
                    (
                        volume
                        * unit_multiplier
                        if volume is not None
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
                    1,
                )

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
                # TPEx historical:
                # 千股 / 千元 -> 股 / 元
                out += parse_hist(
                    tables[
                        0
                    ].get(
                        "data",
                        [],
                    ),
                    1000,
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
    values,
    n,
):
    if len(
        values
    ) < n:
        return None

    return statistics.fmean(
        values[
            -n:
        ]
    )


def features(
    row,
    hist,
    benchmark_return5,
    benchmark_return20,
):
    valid = [
        x
        for x in merged_history(
            hist,
            row,
        )
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

    prior20 = valid[
        :-1
    ][
        -20:
    ]

    highs = [
        x[
            "high"
        ]
        for x in prior20
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

    distance_high = (
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

    distance_ma = (
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
            len(
                valid
            ),

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
                distance_high
            ),

        "distanceFromMA20Pct":
            rnd(
                distance_ma
            ),
    }


def classify_setup(row):
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

    if (
        change >= 7
        or (
            distance_ma is not None
            and distance_ma >= 14
        )
    ):
        return "EXTENDED"

    if (
        distance_high is not None
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

    if (
        return20 is not None
        and return20 > 3
        and distance_ma is not None
        and (
            -1.5
            <= distance_ma
            <= 6
        )
        and distance_high is not None
        and (
            -12
            <= distance_high
            <= -2
        )
        and return5 is not None
        and (
            -6
            <= return5
            <= 2.5
        )
    ):
        return "PULLBACK"

    if (
        return5 is not None
        and return5 <= 2
        and change > 0
        and close_position >= 0.75
        and distance_ma is not None
        and distance_ma >= -2.5
    ):
        return "REVERSAL"

    if (
        return20 is not None
        and return20 > 4
        and (
            rs20
            or 0
        ) > 0
        and distance_ma is not None
        and distance_ma > 0
        and close_position >= 0.55
    ):
        return "TREND_MOMENTUM"

    return "GENERAL_WATCH"


def high_score(distance):
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
            row[
                "historyFeatures"
            ][
                "rs5"
            ]
        )
        for row in rows
        if row[
            "historyFeatures"
        ].get(
            "rs5"
        ) is not None
    )

    rs20_values = sorted(
        float(
            row[
                "historyFeatures"
            ][
                "rs20"
            ]
        )
        for row in rows
        if row[
            "historyFeatures"
        ].get(
            "rs20"
        ) is not None
    )

    rvol_values = sorted(
        float(
            row[
                "historyFeatures"
            ][
                "rvol20"
            ]
        )
        for row in rows
        if row[
            "historyFeatures"
        ].get(
            "rvol20"
        ) is not None
    )

    bonus = {
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

        setup = classify_setup(
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
            bonus[
                setup
            ]
        )

        row[
            "setup"
        ] = setup

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


def tier_for(row):
    score = row[
        "discoveryV2"
    ][
        "score"
    ]

    setup = row[
        "setup"
    ]

    if (
        setup in {
            "BREAKOUT",
            "PULLBACK",
        }
        and score >= 70
    ):
        return "A"

    if (
        setup != "EXTENDED"
        and score >= 60
    ):
        return "B"

    return "C"


def dynamic_selection(rows):
    order = sorted(
        rows,
        key=lambda row:
            row[
                "discoveryV2"
            ][
                "score"
            ],
        reverse=True,
    )

    selected = []

    used = set()

    setup_counts = {
        key:
            0
        for key
        in SETUP_QUOTAS
    }

    industry_counts = {}

    rejection_counts = {
        "belowMinScore":
            0,

        "pinned":
            0,

        "setupQuota":
            0,

        "industryQuota":
            0,
    }

    for row in order:
        if (
            len(
                selected
            )
            >= DYNAMIC_LIMIT
        ):
            break

        symbol = row[
            "symbol"
        ]

        setup = row[
            "setup"
        ]

        score = row[
            "discoveryV2"
        ][
            "score"
        ]

        industry = (
            str(
                row.get(
                    "industry"
                )
                or "UNKNOWN"
            )
            .strip()
            or "UNKNOWN"
        )

        if symbol in used:
            continue

        if score < MIN_DYNAMIC_SCORE:
            rejection_counts[
                "belowMinScore"
            ] += 1

            continue

        if symbol in PINNED_SYMBOLS:
            rejection_counts[
                "pinned"
            ] += 1

            continue

        if (
            setup_counts.get(
                setup,
                0,
            )
            >=
            SETUP_QUOTAS.get(
                setup,
                0,
            )
        ):
            rejection_counts[
                "setupQuota"
            ] += 1

            continue

        industry_cap = (
            1
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
            rejection_counts[
                "industryQuota"
            ] += 1

            continue

        selected.append(
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

                "industry":
                    industry,

                "tier":
                    tier_for(
                        row
                    ),

                "score":
                    score,

                "setup":
                    setup,

                "reasonCodes": [
                    (
                        "SETUP_"
                        + setup
                    ),
                    (
                        "INDUSTRY_"
                        + industry
                    ),
                    "SCORE_GATE_PASS",
                    "NOT_PINNED",
                ],
            }
        )

        used.add(
            symbol
        )

        setup_counts[
            setup
        ] = (
            setup_counts.get(
                setup,
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
            selected,

        "selectedCount":
            len(
                selected
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

        "rejectionCounts":
            rejection_counts,
    }


def main():

    # ========================================================
    # 1. Full-market daily data
    # ========================================================

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

    # ========================================================
    # 2. Common-stock + industry master
    # ========================================================

    sm_tse = security_master(
        2,
        "TSE",
    )

    sm_otc = security_master(
        4,
        "OTC",
    )

    master = {
        (
            "TSE",
            symbol,
        ):
            item
        for symbol, item
        in sm_tse[
            "items"
        ].items()
    }

    master.update(
        {
            (
                "OTC",
                symbol,
            ):
                item
            for symbol, item
            in sm_otc[
                "items"
            ].items()
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

    common = []

    for row in raw:
        meta = master.get(
            (
                row[
                    "market"
                ],
                row[
                    "symbol"
                ],
            )
        )

        if not meta:
            continue

        item = dict(
            row
        )

        item[
            "industry"
        ] = (
            meta.get(
                "industry"
            )
            or "UNKNOWN"
        )

        item[
            "cfi"
        ] = meta.get(
            "cfi"
        )

        common.append(
            item
        )

    # ========================================================
    # 3. Liquidity Gate + Cheap Scan
    # ========================================================

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

    cheap_scan(
        eligible
    )

    candidates = pick_history(
        eligible
    )

    # ========================================================
    # 4. V2.5 Market Date diagnostics
    # ========================================================

    tse_market_date = latest_row_date(
        tse[
            "rows"
        ]
    )

    otc_market_date = latest_row_date(
        otc[
            "rows"
        ]
    )

    sessions = [
        d
        for d in (
            tse_market_date,
            otc_market_date,
        )
        if d is not None
    ]

    if not sessions:
        raise RuntimeError(
            "No valid market trading date found."
        )

    latest = max(
        sessions
    )

    generated_at = datetime.now(
        timezone.utc
    )

    taipei_now = generated_at.astimezone(
        TAIPEI_TZ
    )

    expected_market_date = taipei_now.date()

    market_date_ok = (
        tse_market_date
        == expected_market_date
        and
        otc_market_date
        == expected_market_date
    )

    # ========================================================
    # 5. Benchmark
    # ========================================================

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

    # ========================================================
    # 6. Historical enrichment
    # ========================================================

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
                    (
                        "only "
                        f"{f['historySessions']} "
                        "sessions"
                    )
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

    # ========================================================
    # 7. Score + diversified candidate selection
    # ========================================================

    score_v2(
        enriched
    )

    ranked = sorted(
        enriched,
        key=lambda row:
            row[
                "discoveryV2"
            ][
                "score"
            ],
        reverse=True,
    )

    setup_counts = {}

    for row in ranked:
        setup = row[
            "setup"
        ]

        setup_counts[
            setup
        ] = (
            setup_counts.get(
                setup,
                0,
            )
            + 1
        )

    selection = dynamic_selection(
        ranked
    )

    common_known = sum(
        1
        for row in common
        if row.get(
            "industry"
        ) not in (
            "",
            "UNKNOWN",
            None,
        )
    )

    enriched_known = sum(
        1
        for row in enriched
        if row.get(
            "industry"
        ) not in (
            "",
            "UNKNOWN",
            None,
        )
    )

    # ========================================================
    # 8. Production Gates
    # ========================================================

    min_history_ok = max(
        20,
        int(
            HISTORY_LIMIT
            * 0.75
        ),
    )

    base_integrity_ok = (
        tse[
            "ok"
        ]
        and
        otc[
            "ok"
        ]
        and
        sm_tse[
            "ok"
        ]
        and
        sm_otc[
            "ok"
        ]
        and
        len(
            enriched
        )
        >= min_history_ok
    )

    data_integrity_ok = (
        base_integrity_ok
        and
        market_date_ok
    )

    expires_at = (
        generated_at
        +
        timedelta(
            hours=DYNAMIC_TTL_HOURS
        )
    )

    market_date_reasons = []

    if (
        tse_market_date
        != expected_market_date
    ):
        market_date_reasons.append(
            "TSE_NOT_CURRENT_TAIPEI_DATE"
        )

    if (
        otc_market_date
        != expected_market_date
    ):
        market_date_reasons.append(
            "OTC_NOT_CURRENT_TAIPEI_DATE"
        )

    if (
        tse_market_date is not None
        and
        otc_market_date is not None
        and
        tse_market_date
        != otc_market_date
    ):
        market_date_reasons.append(
            "TSE_OTC_DATE_MISMATCH"
        )

    if not market_date_ok:
        planned_dynamic_write = (
            "skipped_market_date"
        )

    elif not base_integrity_ok:
        planned_dynamic_write = (
            "skipped_data_integrity"
        )

    else:
        planned_dynamic_write = (
            "written"
        )

    market_date_gate = {
        "ok":
            market_date_ok,

        "expectedTaipeiDate":
            expected_market_date.isoformat(),

        "TSE":
            (
                tse_market_date.isoformat()
                if tse_market_date
                else None
            ),

        "OTC":
            (
                otc_market_date.isoformat()
                if otc_market_date
                else None
            ),

        "sameDate":
            bool(
                tse_market_date
                and
                otc_market_date
                and
                tse_market_date
                == otc_market_date
            ),

        "reasons":
            market_date_reasons,

        "rule":
            (
                "TWSE and TPEx daily snapshots "
                "must both match today's "
                "Asia/Taipei calendar date "
                "before Dynamic can be refreshed."
            ),
    }

    # ========================================================
    # 9. Discovery diagnostic payload
    # ========================================================

    discovery_payload = {
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
            generated_at.isoformat(),

        "generatedAtTaipei":
            taipei_now.isoformat(),

        "version":
            "discovery-github-v2.5",

        "stage":
            "market-date-production-gate-v2.5",

        "marketDate":
            latest.isoformat(),

        "expectedMarketDate":
            expected_market_date.isoformat(),

        "marketDateGate":
            market_date_gate,

        "dynamicWriteDecision":
            planned_dynamic_write,

        "dataIntegrityGate": {
            "ok":
                data_integrity_ok,

            "baseIntegrityOk":
                base_integrity_ok,

            "marketDateOk":
                market_date_ok,

            "minimumHistoryEnriched":
                min_history_ok,

            "actualHistoryEnriched":
                len(
                    enriched
                ),

            "rule":
                (
                    "TSE + OTC + both security "
                    "masters must be healthy; "
                    "history enrichment must reach "
                    "threshold; TWSE and TPEx "
                    "market dates must both equal "
                    "today's Taipei date."
                ),
        },

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

                "marketDate":
                    (
                        tse_market_date.isoformat()
                        if tse_market_date
                        else None
                    ),

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

                "marketDate":
                    (
                        otc_market_date.isoformat()
                        if otc_market_date
                        else None
                    ),

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
                    "TWSE ISIN: "
                    "CFI ES + industry column"
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

                "knownIndustryCount":
                    sm_tse[
                        "knownIndustryCount"
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

                "knownIndustryCount":
                    sm_otc[
                        "knownIndustryCount"
                    ],

                "error":
                    sm_otc[
                        "error"
                    ],
            },
        },

        "industryCoverage": {
            "commonStockKnown":
                common_known,

            "commonStockTotal":
                len(
                    common
                ),

            "historyEnrichedKnown":
                enriched_known,

            "historyEnrichedTotal":
                len(
                    enriched
                ),
        },

        "rawUniverseCount":
            len(
                raw
            ),

        "universeCount":
            len(
                common
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

            "minScore":
                MIN_DYNAMIC_SCORE,

            "maxPerIndustry":
                INDUSTRY_LIMIT,

            "maxUnknownIndustry":
                1,

            "setupQuotas":
                SETUP_QUOTAS,

            "pinnedExcluded":
                sorted(
                    PINNED_SYMBOLS
                ),

            "ttlHours":
                DYNAMIC_TTL_HOURS,

            "requiresCurrentTaipeiMarketDate":
                True,
        },

        "dynamicPreview":
            selection[
                "candidates"
            ],

        "dynamicPreviewStats": {
            "selectedCount":
                selection[
                    "selectedCount"
                ],

            "setupCounts":
                selection[
                    "setupCounts"
                ],

            "industryCounts":
                selection[
                    "industryCounts"
                ],

            "rejectionCounts":
                selection[
                    "rejectionCounts"
                ],
        },

        "topDiscovery":
            ranked[
                :
                RESULT_LIMIT
            ],
    }

    # ========================================================
    # 10. Production Dynamic payload
    # ========================================================

    dynamic_payload = {
        "schemaVersion":
            1,

        "generatedAt":
            generated_at.isoformat(),

        "expiresAt":
            expires_at.isoformat(),

        "marketDate":
            latest.isoformat(),

        "source":
            "discovery-github-v2.5",

        "status":
            (
                "ok"
                if selection[
                    "selectedCount"
                ] > 0
                else "no_candidates"
            ),

        "policy": {
            "minScore":
                MIN_DYNAMIC_SCORE,

            "maxCandidates":
                DYNAMIC_LIMIT,

            "maxPerIndustry":
                INDUSTRY_LIMIT,

            "ttlHours":
                DYNAMIC_TTL_HOURS,

            "marketDateGate":
                True,
        },

        "candidates":
            selection[
                "candidates"
            ],
    }

    # ========================================================
    # 11. Save
    #
    # discovery-scan:
    #   Always write for diagnosis.
    #
    # watchlist-dynamic:
    #   ONLY write when both integrity and market-date gates pass.
    # ========================================================

    Path(
        "data"
    ).mkdir(
        exist_ok=True
    )

    Path(
        "data/discovery-scan.json"
    ).write_text(
        json.dumps(
            discovery_payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    dynamic_write = (
        planned_dynamic_write
    )

    if (
        dynamic_write
        == "written"
    ):
        Path(
            "data/watchlist-dynamic.json"
        ).write_text(
            json.dumps(
                dynamic_payload,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    # ========================================================
    # 12. GitHub Actions summary
    # ========================================================

    print(
        json.dumps(
            {
                "ok":
                    discovery_payload[
                        "ok"
                    ],

                "version":
                    discovery_payload[
                        "version"
                    ],

                "generatedAtTaipei":
                    discovery_payload[
                        "generatedAtTaipei"
                    ],

                "marketDate":
                    discovery_payload[
                        "marketDate"
                    ],

                "expectedMarketDate":
                    discovery_payload[
                        "expectedMarketDate"
                    ],

                "marketDateGate":
                    discovery_payload[
                        "marketDateGate"
                    ],

                "commonStockUniverseCount":
                    discovery_payload[
                        "commonStockUniverseCount"
                    ],

                "eligibleCount":
                    discovery_payload[
                        "eligibleCount"
                    ],

                "historyEnrichedCount":
                    discovery_payload[
                        "historyEnrichedCount"
                    ],

                "historyErrorCount":
                    discovery_payload[
                        "historyErrorCount"
                    ],

                "industryCoverage":
                    discovery_payload[
                        "industryCoverage"
                    ],

                "dataIntegrityGate":
                    discovery_payload[
                        "dataIntegrityGate"
                    ],

                "dynamicWrite":
                    dynamic_write,

                "dynamicSelectedCount":
                    selection[
                        "selectedCount"
                    ],

                "dynamicCandidates": [
                    {
                        "symbol":
                            x[
                                "symbol"
                            ],

                        "name":
                            x[
                                "name"
                            ],

                        "score":
                            x[
                                "score"
                            ],

                        "setup":
                            x[
                                "setup"
                            ],

                        "industry":
                            x[
                                "industry"
                            ],
                    }
                    for x
                    in selection[
                        "candidates"
                    ]
                ],

                "expiresAt":
                    (
                        dynamic_payload[
                            "expiresAt"
                        ]
                        if dynamic_write
                        == "written"
                        else None
                    ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
