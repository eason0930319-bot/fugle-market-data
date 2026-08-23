from __future__ import annotations

import json
import os
from pathlib import Path

import requests


TWSE_URL = (
    "https://openapi.twse.com.tw/"
    "v1/exchangeReport/STOCK_DAY_ALL"
)

TPEX_URL = (
    "https://www.tpex.org.tw/"
    "openapi/v1/"
    "tpex_mainboard_daily_close_quotes"
)

MIN_TRADE_VALUE = int(
    os.getenv(
        "MIN_TRADE_VALUE",
        "50000000"
    )
)

RESULT_LIMIT = int(
    os.getenv(
        "RESULT_LIMIT",
        "30"
    )
)


def to_number(value):
    if value is None:
        return None

    if isinstance(
        value,
        (int, float)
    ):
        return float(value)

    text = (
        str(value)
        .strip()
        .replace(",", "")
        .replace("%", "")
        .replace("+", "")
    )

    if text in {
        "",
        "-",
        "--",
        "---",
        "N/A",
        "null",
        "None"
    }:
        return None

    try:
        return float(text)
    except ValueError:
        return None


def round_number(
    value,
    digits=3
):
    if value is None:
        return None

    try:
        return round(
            float(value),
            digits
        )
    except (
        TypeError,
        ValueError
    ):
        return None


def first_number(
    row,
    *keys
):
    for key in keys:
        value = to_number(
            row.get(key)
        )

        if value is not None:
            return value

    return None


def is_common_stock(
    symbol
):
    text = str(
        symbol or ""
    ).strip()

    return (
        len(text) == 4 and
        text.isdigit()
    )


def fetch_json(url):
    response = requests.get(
        url,
        timeout=30,
        headers={
            "User-Agent":
                "fugle-market-data-discovery/1.0",

            "Accept":
                "application/json"
        }
    )

    response.raise_for_status()

    return response.json()


def calculate_change_pct(
    close_price,
    change
):
    if (
        close_price is None or
        change is None
    ):
        return None

    previous_close = (
        close_price -
        change
    )

    if previous_close == 0:
        return None

    return (
        change /
        previous_close *
        100
    )


def normalize_twse(row):
    symbol = str(
        row.get(
            "Code",
            ""
        )
    ).strip()

    if not is_common_stock(
        symbol
    ):
        return None

    close_price = to_number(
        row.get(
            "ClosingPrice"
        )
    )

    change = to_number(
        row.get(
            "Change"
        )
    )

    return {
        "symbol":
            symbol,

        "name":
            str(
                row.get(
                    "Name",
                    ""
                )
            ).strip(),

        "market":
            "TSE",

        "exchange":
            "TWSE",

        "date":
            row.get(
                "Date"
            ),

        "openPrice":
            to_number(
                row.get(
                    "OpeningPrice"
                )
            ),

        "highPrice":
            to_number(
                row.get(
                    "HighestPrice"
                )
            ),

        "lowPrice":
            to_number(
                row.get(
                    "LowestPrice"
                )
            ),

        "closePrice":
            close_price,

        "change":
            change,

        "changePercent":
            round_number(
                calculate_change_pct(
                    close_price,
                    change
                )
            ),

        "tradeVolume":
            to_number(
                row.get(
                    "TradeVolume"
                )
            ),

        "tradeValue":
            to_number(
                row.get(
                    "TradeValue"
                )
            ),

        "transaction":
            to_number(
                row.get(
                    "Transaction"
                )
            )
    }


def normalize_tpex(row):
    symbol = str(
        row.get(
            "SecuritiesCompanyCode",
            ""
        )
    ).strip()

    if not is_common_stock(
        symbol
    ):
        return None

    close_price = to_number(
        row.get(
            "Close"
        )
    )

    change = to_number(
        row.get(
            "Change"
        )
    )

    trade_volume = first_number(
        row,
        "TradingShares",
        "TradeVolume",
        "Volume"
    )

    trade_value = first_number(
        row,
        "TransactionAmount",
        "TradingValue",
        "TradeValue",
        "TradeAmount"
    )

    # 若 TPEx schema 某天沒有成交金額欄，
    # 使用收盤價 × 成交股數做保守估算。
    if (
        trade_value is None and
        trade_volume is not None and
        close_price is not None
    ):
        trade_value = (
            trade_volume *
            close_price
        )

    return {
        "symbol":
            symbol,

        "name":
            str(
                row.get(
                    "CompanyName",
                    ""
                )
            ).strip(),

        "market":
            "OTC",

        "exchange":
            "TPEx",

        "date":
            row.get(
                "Date"
            ),

        "openPrice":
            to_number(
                row.get(
                    "Open"
                )
            ),

        "highPrice":
            to_number(
                row.get(
                    "High"
                )
            ),

        "lowPrice":
            to_number(
                row.get(
                    "Low"
                )
            ),

        "closePrice":
            close_price,

        "change":
            change,

        "changePercent":
            round_number(
                calculate_change_pct(
                    close_price,
                    change
                )
            ),

        "tradeVolume":
            trade_volume,

        "tradeValue":
            trade_value,

        "transaction":
            first_number(
                row,
                "Transaction",
                "TransactionCount",
                "NumberOfTransactions"
            )
    }


def add_derived(row):
    high_price = row.get(
        "highPrice"
    )

    low_price = row.get(
        "lowPrice"
    )

    close_price = row.get(
        "closePrice"
    )

    intraday_range_pct = None
    close_position = None

    if (
        high_price is not None and
        low_price is not None and
        close_price not in (
            None,
            0
        )
    ):
        intraday_range_pct = (
            (
                high_price -
                low_price
            ) /
            close_price *
            100
        )

    if (
        high_price is not None and
        low_price is not None and
        close_price is not None and
        high_price != low_price
    ):
        close_position = (
            (
                close_price -
                low_price
            ) /
            (
                high_price -
                low_price
            )
        )

    row[
        "intradayRangePct"
    ] = round_number(
        intraday_range_pct
    )

    row[
        "closePosition"
    ] = round_number(
        close_position,
        4
    )

    return row


def load_market(
    name,
    url,
    normalizer
):
    try:
        raw = fetch_json(
            url
        )

        if not isinstance(
            raw,
            list
        ):
            raise RuntimeError(
                "API response is not a list"
            )

        rows = []

        for item in raw:
            row = normalizer(
                item
            )

            if row:
                rows.append(
                    add_derived(
                        row
                    )
                )

        return {
            "ok":
                len(rows) > 0,

            "market":
                name,

            "rawRowCount":
                len(raw),

            "commonStockRowCount":
                len(rows),

            "error":
                None,

            "rows":
                rows
        }

    except Exception as error:
        return {
            "ok":
                False,

            "market":
                name,

            "rawRowCount":
                0,

            "commonStockRowCount":
                0,

            "error":
                str(error),

            "rows":
                []
        }


def percentile_rank(
    sorted_values,
    value
):
    if (
        value is None or
        len(sorted_values) <= 1
    ):
        return 0

    count = sum(
        1
        for item
        in sorted_values
        if item <= value
    )

    return (
        (
            count - 1
        ) /
        (
            len(
                sorted_values
            ) - 1
        ) *
        100
    )


def classify_candidate(
    row
):
    change = (
        row.get(
            "changePercent"
        ) or
        0
    )

    close_position = (
        row.get(
            "closePosition"
        )
    )

    if close_position is None:
        close_position = 0.5

    liquidity = (
        row
        .get(
            "cheapScan",
            {}
        )
        .get(
            "liquidityPercentile",
            0
        )
    )

    if change >= 7:
        return (
            "EXTENDED_MOMENTUM"
        )

    if (
        1.5 <= change <= 6.5 and
        close_position >= 0.65 and
        liquidity >= 50
    ):
        return (
            "BALANCED_MOMENTUM"
        )

    if (
        -2 <= change <= 1.5 and
        close_position >= 0.75 and
        liquidity >= 60
    ):
        return (
            "REVERSAL_WATCH"
        )

    return "GENERAL_WATCH"


def main():
    twse = load_market(
        "TSE",
        TWSE_URL,
        normalize_twse
    )

    tpex = load_market(
        "OTC",
        TPEX_URL,
        normalize_tpex
    )

    universe = (
        twse[
            "rows"
        ] +
        tpex[
            "rows"
        ]
    )

    eligible = [
        row
        for row
        in universe
        if (
            row.get(
                "tradeValue"
            ) or
            0
        ) >=
        MIN_TRADE_VALUE
    ]

    liquidity_values = sorted(
        [
            row[
                "tradeValue"
            ]
            for row
            in eligible
            if row.get(
                "tradeValue"
            )
            is not None
        ]
    )

    momentum_values = sorted(
        [
            row[
                "changePercent"
            ]
            for row
            in eligible
            if row.get(
                "changePercent"
            )
            is not None
        ]
    )

    for row in eligible:

        liquidity_pct = (
            percentile_rank(
                liquidity_values,
                row.get(
                    "tradeValue"
                )
            )
        )

        momentum_pct = (
            percentile_rank(
                momentum_values,
                row.get(
                    "changePercent"
                )
            )
        )

        close_position = (
            row.get(
                "closePosition"
            )
        )

        if close_position is None:
            close_position = 0.5

        close_strength = (
            max(
                0,
                min(
                    100,
                    close_position *
                    100
                )
            )
        )

        discovery_score = (
            liquidity_pct *
            0.40 +
            momentum_pct *
            0.35 +
            close_strength *
            0.25
        )

        row[
            "cheapScan"
        ] = {
            "liquidityPercentile":
                round_number(
                    liquidity_pct,
                    2
                ),

            "momentumPercentile":
                round_number(
                    momentum_pct,
                    2
                ),

            "closeStrengthScore":
                round_number(
                    close_strength,
                    2
                ),

            "discoveryScore":
                round_number(
                    discovery_score,
                    2
                )
        }

        row[
            "candidateType"
        ] = classify_candidate(
            row
        )

    top_discovery = sorted(
        eligible,
        key=lambda row:
            (
                row
                .get(
                    "cheapScan",
                    {}
                )
                .get(
                    "discoveryScore",
                    0
                )
            ),
        reverse=True
    )[
        :RESULT_LIMIT
    ]

    def select_type(
        candidate_type,
        limit=10
    ):
        return [
            row
            for row
            in top_discovery
            if (
                row.get(
                    "candidateType"
                ) ==
                candidate_type
            )
        ][
            :limit
        ]

    payload = {
        "ok":
            twse[
                "ok"
            ] or
            tpex[
                "ok"
            ],

        "generatedAt":
            __import__(
                "datetime"
            )
            .datetime
            .now(
                __import__(
                    "datetime"
                ).timezone.utc
            )
            .isoformat(),

        "version":
            "discovery-github-v1",

        "stage":
            "cheap-scan-github-v1",

        "sources": {
            "TSE": {
                key:
                    twse[key]
                for key in (
                    "ok",
                    "rawRowCount",
                    "commonStockRowCount",
                    "error"
                )
            },

            "OTC": {
                key:
                    tpex[key]
                for key in (
                    "ok",
                    "rawRowCount",
                    "commonStockRowCount",
                    "error"
                )
            }
        },

        "universeDefinition":
            "4-digit common-stock-like symbols only",

        "universeCount":
            len(
                universe
            ),

        "minTradeValue":
            MIN_TRADE_VALUE,

        "eligibleCount":
            len(
                eligible
            ),

        "resultLimit":
            RESULT_LIMIT,

        "scoreDefinition": {
            "note":
                (
                    "Discovery Score is a "
                    "candidate-ranking score, "
                    "not a buy signal."
                ),

            "liquidityPercentile":
                0.40,

            "intradayMomentumPercentile":
                0.35,

            "closeStrength":
                0.25
        },

        "candidatePreview": {
            "balancedMomentum":
                select_type(
                    "BALANCED_MOMENTUM"
                ),

            "reversalWatch":
                select_type(
                    "REVERSAL_WATCH"
                ),

            "extendedMomentum":
                select_type(
                    "EXTENDED_MOMENTUM"
                ),

            "generalWatch":
                select_type(
                    "GENERAL_WATCH"
                )
        },

        "topDiscovery":
            top_discovery
    }

    Path(
        "data"
    ).mkdir(
        parents=True,
        exist_ok=True
    )

    Path(
        "data/discovery-scan.json"
    ).write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    print(
        json.dumps(
            {
                "ok":
                    payload[
                        "ok"
                    ],

                "TSE":
                    payload[
                        "sources"
                    ][
                        "TSE"
                    ],

                "OTC":
                    payload[
                        "sources"
                    ][
                        "OTC"
                    ],

                "universeCount":
                    payload[
                        "universeCount"
                    ],

                "eligibleCount":
                    payload[
                        "eligibleCount"
                    ],

                "topSymbols":
                    [
                        row[
                            "symbol"
                        ]
                        for row
                        in top_discovery[
                            :10
                        ]
                    ]
            },
            ensure_ascii=False,
            indent=2
        )
    )


if __name__ == "__main__":
    main()
