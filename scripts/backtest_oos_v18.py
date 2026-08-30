from __future__ import annotations

from datetime import datetime

import pandas as pd

from backtest_data import add_features
from backtest_ma5 import CostModel
from backtest_exit_v17b import _build_variant_trades, _prepare_exit
from backtest_allocation_v17c import _basic_pass, _simulate_allocation

VERSION = "fixed-strategy-oos-backtest-v1.8"
FIXED_ENTRY = "NEXT_OPEN"
FIXED_EXIT = "MA5_MA10_DEATH_CROSS"
FIXED_ALLOCATION = {
    "name": "EQUAL_5_FIXED",
    "sizingMode": "EQUAL_SLOT",
    "maxPositions": 5,
    "riskBudgetPct": None,
    "maxWeightPct": 20.0,
}
OOS_YEARS = (2022, 2023)


def _benchmark_return(data: pd.DataFrame, year: int):
    b = data[(data.market == "BENCH") & (pd.to_datetime(data.date).dt.year == year)].copy()
    b = b.sort_values("date")
    closes = pd.to_numeric(b.close, errors="coerce").dropna()
    if len(closes) < 2 or float(closes.iloc[0]) <= 0:
        return None
    return round((float(closes.iloc[-1]) / float(closes.iloc[0]) - 1.0) * 100.0, 4)


def build_oos_v18_summary(
    data: pd.DataFrame,
    generated_at: str | None = None,
    costs: CostModel | None = None,
):
    """Evaluate the already-frozen V1.7C winner on previously unseen 2022/2023.

    This module intentionally exposes no strategy parameter search. The purpose is
    external historical robustness, not optimization. Results are split by entry
    year and also compounded across the full 2022-2023 entry stream.
    """
    costs = costs or CostModel()
    generated_at = generated_at or datetime.utcnow().isoformat() + "Z"

    # The immutable snapshot stores canonical adjusted OHLCV rows, not derived
    # indicators. Rebuild the exact point-in-time daily features deterministically
    # from the frozen rows before feeding the already-fixed V1.7C logic. Keep the
    # 0050 benchmark outside the stock universe so it cannot affect breadth/regime.
    equities = data[data.market != "BENCH"].copy()
    d = _prepare_exit(add_features(equities))

    trades, trade_audit = _build_variant_trades(d, FIXED_EXIT)
    if trades.empty:
        trades["entryYear"] = pd.Series(dtype=int)
    else:
        trades["entryYear"] = pd.to_datetime(trades.entryDate).dt.year

    yearly = {}
    for year in OOS_YEARS:
        g = trades[trades.entryYear == year].copy()
        row = _simulate_allocation(g, d, costs, FIXED_ALLOCATION)
        row["basicPass"] = _basic_pass(row)
        row["benchmark0050PriceReturnPct"] = _benchmark_return(data, year)
        yearly[str(year)] = row

    combined_trades = trades[trades.entryYear.isin(OOS_YEARS)].copy()
    combined = _simulate_allocation(combined_trades, d, costs, FIXED_ALLOCATION)
    combined["basicPass"] = _basic_pass(combined)

    year_passes = [bool(yearly[str(y)]["basicPass"]) for y in OOS_YEARS]
    returns = [float(yearly[str(y)]["totalReturnPct"]) for y in OOS_YEARS]
    drawdowns = [float(yearly[str(y)]["maxDrawdownPct"]) for y in OOS_YEARS]

    return {
        "ok": True,
        "schemaVersion": 1,
        "version": VERSION,
        "generatedAt": generated_at,
        "purpose": "Untuned historical out-of-sample robustness check of the fixed V1.7C winner on 2022 and 2023.",
        "researchPolicy": {
            "oosYears": list(OOS_YEARS),
            "noRetuningOnOos": True,
            "automaticProductionChanges": False,
            "interpretation": "2022-2023 were not used to choose the V1.7A/B/C entry, exit or sizing winner before this test. Preserve them as validation evidence; do not optimize parameters on these results.",
        },
        "fixedStrategy": {
            "signal": "MA5 rising cross: MA5 rising, close above MA5, prior close at/below prior MA5",
            "entry": FIXED_ENTRY,
            "regime": "NO_BEAR",
            "initialStop": "max(entry - 1.25*ATR14, prior 10-session low)",
            "postStopCooldownSessions": 5,
            "exit": FIXED_EXIT,
            "allocation": FIXED_ALLOCATION,
            "selectionPriority": "higher MA5 slope, then higher signal-day trade value, then symbol",
            "leverage": False,
        },
        "tradeAudit": trade_audit,
        "yearly": yearly,
        "combined2022To2023": combined,
        "allOosYearsBasicPass": all(year_passes),
        "worstOosYearReturnPct": round(min(returns), 4),
        "worstOosYearDrawdownPct": round(min(drawdowns), 4),
        "automaticProductionChanges": False,
    }
