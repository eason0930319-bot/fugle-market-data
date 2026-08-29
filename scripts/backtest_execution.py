from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

VERSION = "execution-backtest-v1.2"


@dataclass(frozen=True)
class CostModel:
    buy_commission: float = 0.001425
    sell_commission: float = 0.001425
    sell_tax: float = 0.003
    slippage_each_side: float = 0.0005


STRATEGIES = (
    "NEXT_OPEN",
    "OPEN_NO_CHASE_3PCT",
    "PULLBACK_1PCT",
    "BREAKOUT_0_5PCT",
)


def _num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def _round(x, n=4):
    v = _num(x)
    return None if v is None else round(v, n)


def _entry_for_strategy(row, strategy):
    signal_close = _num(row.signalClose)
    signal_high = _num(row.signalHigh)
    next_open = _num(row.fo1)
    next_high = _num(row.fh1)
    next_low = _num(row.fl1)
    if signal_close is None or next_open is None or next_high is None or next_low is None:
        return None, None, "MISSING_NEXT_DAY"

    if strategy == "NEXT_OPEN":
        return next_open, "OPEN", "ENTERED"

    if strategy == "OPEN_NO_CHASE_3PCT":
        gap = next_open / signal_close - 1
        if gap > 0.03:
            return None, None, "SKIP_CHASE"
        return next_open, "OPEN", "ENTERED"

    if strategy == "PULLBACK_1PCT":
        limit_price = signal_close * 0.99
        if next_open <= limit_price:
            return next_open, "OPEN_BELOW_LIMIT", "ENTERED"
        if next_low <= limit_price <= next_high:
            return limit_price, "INTRADAY_LIMIT", "ENTERED"
        return None, None, "NO_PULLBACK_FILL"

    if strategy == "BREAKOUT_0_5PCT":
        if signal_high is None:
            return None, None, "MISSING_SIGNAL_HIGH"
        trigger = signal_high * 1.005
        if next_open >= trigger:
            return next_open, "GAP_THROUGH_TRIGGER", "ENTERED"
        if next_high >= trigger:
            return trigger, "INTRADAY_STOP_BUY", "ENTERED"
        return None, None, "NO_BREAKOUT_FILL"

    raise ValueError(f"unknown strategy: {strategy}")


def _levels(row, entry):
    atr = _num(row.signalAtr14)
    signal_low = _num(row.signalLow)
    if atr is None or atr <= 0 or signal_low is None or entry <= 0:
        return None, None, None

    atr_stop = entry - 1.5 * atr
    structural_stop = signal_low - 0.25 * atr
    stop_candidates = [atr_stop]
    if 0 < structural_stop < entry:
        stop_candidates.append(structural_stop)
    stop = max(stop_candidates)
    risk = entry - stop
    if stop <= 0 or risk <= 0:
        return None, None, None
    target = entry + 2.0 * risk
    return stop, target, risk


def _net_return(entry_raw, exit_raw, costs: CostModel):
    buy_cash = entry_raw * (1 + costs.slippage_each_side) * (1 + costs.buy_commission)
    sell_cash = exit_raw * (1 - costs.slippage_each_side) * (1 - costs.sell_commission - costs.sell_tax)
    return (sell_cash / buy_cash - 1) * 100


def _simulate_one(row, strategy, costs: CostModel):
    entry, entry_mode, status = _entry_for_strategy(row, strategy)
    base = {
        "strategy": strategy,
        "signalDate": row.signalDate,
        "split": row.split,
        "symbol": row.symbol,
        "name": row["name"],
        "market": row.market,
        "industry": row.industry,
        "extensionSubtype": row.extensionSubtype,
        "marketRegime": row.marketRegime,
        "sectorBucket": row.sectorBucket,
        "rvolBucket": row.rvolBucket,
        "distMABucket": row.distMABucket,
        "signalClose": _round(row.signalClose),
        "signalChangePct": _round(row.changePct),
        "entryStatus": status,
        "entryMode": entry_mode,
        "entryPrice": _round(entry),
        "stopPrice": None,
        "targetPrice": None,
        "initialRiskPct": None,
        "exitType": None,
        "exitDay": None,
        "exitPrice": None,
        "grossReturnPct": None,
        "netReturnPct": None,
        "rMultipleGross": None,
        "win": None,
    }
    if status != "ENTERED" or entry is None:
        return base

    stop, target, risk = _levels(row, entry)
    if stop is None:
        base["entryStatus"] = "INVALID_LEVELS"
        return base

    base["stopPrice"] = _round(stop)
    base["targetPrice"] = _round(target)
    base["initialRiskPct"] = _round(risk / entry * 100)

    exit_type = None
    exit_day = None
    exit_price = None
    for day in range(1, 6):
        hi = _num(row[f"fh{day}"])
        lo = _num(row[f"fl{day}"])
        if hi is None or lo is None:
            break

        stop_hit = lo <= stop
        target_hit = hi >= target

        # Daily OHLC cannot resolve intraday ordering. If both touch, assume stop first.
        # For a pullback limit filled intraday, a same-day target could have occurred before
        # the limit fill, so we ignore target on day 1 unless the order filled at the open.
        if day == 1 and strategy == "PULLBACK_1PCT" and entry_mode == "INTRADAY_LIMIT":
            target_hit = False

        if stop_hit:
            exit_type = "STOP"
            exit_day = day
            exit_price = stop
            break
        if target_hit:
            exit_type = "TARGET"
            exit_day = day
            exit_price = target
            break

    if exit_type is None:
        close5 = _num(row.fc5)
        if close5 is None:
            base["entryStatus"] = "INCOMPLETE_5D"
            return base
        exit_type = "TIME_EXIT"
        exit_day = 5
        exit_price = close5

    gross = (exit_price / entry - 1) * 100
    net = _net_return(entry, exit_price, costs)
    base.update({
        "exitType": exit_type,
        "exitDay": exit_day,
        "exitPrice": _round(exit_price),
        "grossReturnPct": _round(gross),
        "netReturnPct": _round(net),
        "rMultipleGross": _round((exit_price - entry) / risk),
        "win": bool(net > 0),
    })
    return base


def _profit_factor(values):
    s = pd.to_numeric(values, errors="coerce").dropna()
    if s.empty:
        return None
    wins = s[s > 0].sum()
    losses = -s[s < 0].sum()
    if losses <= 0:
        return None
    return round(float(wins / losses), 4)


def trade_stats(df):
    signal_count = int(len(df))
    entered = df[(df.entryStatus == "ENTERED") & df.netReturnPct.notna()].copy()
    if entered.empty:
        return {
            "signalCount": signal_count,
            "enteredCount": 0,
            "entryRate": 0.0 if signal_count else None,
            "winRate": None,
            "avgNetReturnPct": None,
            "medianNetReturnPct": None,
            "avgGrossReturnPct": None,
            "profitFactor": None,
            "targetHitRate": None,
            "stopHitRate": None,
            "timeExitRate": None,
            "avgInitialRiskPct": None,
            "avgRMultipleGross": None,
        }
    return {
        "signalCount": signal_count,
        "enteredCount": int(len(entered)),
        "entryRate": round(len(entered) / signal_count, 4) if signal_count else None,
        "winRate": round(float(entered.win.mean()), 4),
        "avgNetReturnPct": round(float(entered.netReturnPct.mean()), 4),
        "medianNetReturnPct": round(float(entered.netReturnPct.median()), 4),
        "avgGrossReturnPct": round(float(entered.grossReturnPct.mean()), 4),
        "profitFactor": _profit_factor(entered.netReturnPct),
        "targetHitRate": round(float((entered.exitType == "TARGET").mean()), 4),
        "stopHitRate": round(float((entered.exitType == "STOP").mean()), 4),
        "timeExitRate": round(float((entered.exitType == "TIME_EXIT").mean()), 4),
        "avgInitialRiskPct": round(float(entered.initialRiskPct.mean()), 4),
        "avgRMultipleGross": round(float(entered.rMultipleGross.mean()), 4),
    }


def _grouped(df, key, min_count=30):
    out = {}
    for value, g in df.groupby(key, dropna=False):
        stats = trade_stats(g)
        if stats["enteredCount"] >= min_count:
            out["UNKNOWN" if pd.isna(value) else str(value)] = stats
    return out


def _grouped_by_strategy(df, key, min_count=30):
    out = {}
    for strategy, g in df.groupby("strategy"):
        out[strategy] = _grouped(g, key, min_count)
    return out


def build_execution_summary(ext: pd.DataFrame, data: pd.DataFrame, generated_at: str, min_group=30, costs: CostModel | None = None):
    costs = costs or CostModel()
    needed = [
        "date", "market", "symbol", "high", "low", "close", "atr14", "ma20", "l10",
        "fo1", "fh1", "fl1", "fc1", "fh2", "fl2", "fc2", "fh3", "fl3", "fc3",
        "fh4", "fl4", "fc4", "fh5", "fl5", "fc5",
    ]
    missing = [c for c in needed if c not in data.columns]
    if missing:
        raise RuntimeError(f"execution backtest missing data columns: {missing}")

    px = data[needed].copy()
    px["signalDate"] = pd.to_datetime(px.date).dt.date.astype(str)
    px = px.drop(columns=["date"]).rename(columns={
        "high": "signalHigh",
        "low": "signalLow",
        "close": "signalClose",
        "atr14": "signalAtr14",
        "ma20": "signalMA20",
        "l10": "signalL10",
    })
    cols = [
        "signalDate", "split", "symbol", "name", "market", "industry", "extensionSubtype",
        "marketRegime", "sectorBucket", "rvolBucket", "distMABucket", "changePct",
    ]
    sig = ext[cols].merge(px, how="left", on=["signalDate", "market", "symbol"])

    records = []
    total = len(sig)
    for i, (_, row) in enumerate(sig.iterrows(), 1):
        if i == 1 or i % 10000 == 0 or i == total:
            print(f"execution simulation {i}/{total}")
        for strategy in STRATEGIES:
            records.append(_simulate_one(row, strategy, costs))

    trades = pd.DataFrame(records)
    trades["subtypeRegime"] = trades.extensionSubtype.astype(str) + "|" + trades.marketRegime.astype(str)
    trades["subtypeSector"] = trades.extensionSubtype.astype(str) + "|" + trades.sectorBucket.astype(str)

    overall = {strategy: trade_stats(trades[trades.strategy == strategy]) for strategy in STRATEGIES}
    by_split = _grouped_by_strategy(trades, "split", min_group)
    by_subtype = _grouped_by_strategy(trades, "extensionSubtype", min_group)
    by_regime = _grouped_by_strategy(trades, "marketRegime", min_group)
    by_sector = _grouped_by_strategy(trades, "sectorBucket", min_group)
    by_subtype_regime = _grouped_by_strategy(trades, "subtypeRegime", min_group)
    by_subtype_sector = _grouped_by_strategy(trades, "subtypeSector", min_group)

    focus_filters = {
        "GAP_EXTENSION": trades.extensionSubtype == "GAP_EXTENSION",
        "GAP_EXTENSION_BULL": (trades.extensionSubtype == "GAP_EXTENSION") & (trades.marketRegime == "BULL"),
        "GAP_EXTENSION_STRONG_SECTOR": (trades.extensionSubtype == "GAP_EXTENSION") & (trades.sectorBucket == "STRONG_70_PLUS"),
        "STRONG_CONTINUATION": trades.extensionSubtype == "STRONG_CONTINUATION",
        "EXHAUSTION_RISK": trades.extensionSubtype == "EXHAUSTION_RISK",
    }
    focus = {}
    for name, mask in focus_filters.items():
        focus[name] = {strategy: trade_stats(trades[mask & (trades.strategy == strategy)]) for strategy in STRATEGIES}

    complete = trades[trades.entryStatus.isin(["ENTERED", "INCOMPLETE_5D"])].copy()
    summary = {
        "ok": True,
        "schemaVersion": 1,
        "version": VERSION,
        "generatedAt": generated_at,
        "purpose": "Next-trading-day executable simulation for V1.1 EXTENDED research signals; compares entry styles and reports net trade win rate.",
        "signalCount": int(len(sig)),
        "strategySimulationCount": int(len(trades)),
        "costModel": {
            "buyCommissionPct": round(costs.buy_commission * 100, 4),
            "sellCommissionPct": round(costs.sell_commission * 100, 4),
            "sellTransactionTaxPct": round(costs.sell_tax * 100, 4),
            "slippageEachSidePct": round(costs.slippage_each_side * 100, 4),
            "approxRoundTripFrictionPct": round((costs.buy_commission + costs.sell_commission + costs.sell_tax + 2 * costs.slippage_each_side) * 100, 4),
        },
        "executionRules": {
            "NEXT_OPEN": "Buy next trading day's open.",
            "OPEN_NO_CHASE_3PCT": "Buy next open only when it is no more than +3% above signal close.",
            "PULLBACK_1PCT": "Next-day-only buy limit at 1% below signal close; if opening below limit, fill at open.",
            "BREAKOUT_0_5PCT": "Next-day-only stop buy at 0.5% above signal-day high; a gap through trigger fills at the open.",
            "stop": "Higher (tighter) of entry - 1.5 ATR14 and signal low - 0.25 ATR14, using signal-day information only.",
            "target": "2R from raw entry price.",
            "timeExit": "If neither stop nor target occurs, exit at the fifth trading-day close after the signal.",
            "sameDayAmbiguity": "Daily OHLC cannot order high/low. If stop and target are both touched, STOP is assumed first. For intraday pullback fills, same-day target is ignored because it may have occurred before entry.",
            "winRate": "Among completed entered trades, percentage with net return > 0 after the stated commission, tax and slippage assumptions.",
        },
        "overallByStrategy": overall,
        "bySplit": by_split,
        "bySubtype": by_subtype,
        "byMarketRegime": by_regime,
        "bySectorBucket": by_sector,
        "bySubtypeRegime": by_subtype_regime,
        "bySubtypeSector": by_subtype_sector,
        "focusComparisons": focus,
        "dataQuality": {
            "mergedSignalRows": int(len(sig)),
            "rowsWithNextOpen": int(sig.fo1.notna().sum()),
            "rowsWithComplete5D": int(sig.fc5.notna().sum()),
            "completedOrIncompleteEntries": int(len(complete)),
        },
        "limitations": [
            "Daily OHLC cannot resolve exact intraday path; same-day ambiguity is handled conservatively.",
            "Current security master creates survivorship bias; delisted historical stocks are absent.",
            "Yahoo/yfinance is not official archival exchange data.",
            "The execution rules are standardized research rules, not the final discretionary V3.3 trigger/stop/target for each stock.",
            "No historical fundamentals, catalysts, valuation or point-in-time news are reconstructed here.",
        ],
    }

    valid = trades[trades.netReturnPct.notna()].copy()
    sample_cols = [
        "strategy", "signalDate", "split", "symbol", "name", "extensionSubtype", "marketRegime",
        "sectorBucket", "entryMode", "entryPrice", "stopPrice", "targetPrice", "exitType", "exitDay",
        "exitPrice", "grossReturnPct", "netReturnPct", "rMultipleGross", "win",
    ]
    sample = {
        "schemaVersion": 1,
        "version": VERSION,
        "generatedAt": generated_at,
        "latestCompleted": valid.sort_values(["signalDate", "symbol", "strategy"]).tail(160)[sample_cols].to_dict("records"),
    }
    return summary, sample, trades
