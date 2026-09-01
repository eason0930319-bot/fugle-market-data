from __future__ import annotations

import math
from dataclasses import asdict

import numpy as np
import pandas as pd

from backtest_data import add_features
from backtest_ma5 import CostModel, INITIAL_CAPITAL, _max_drawdown, _round, _trade_summary
from backtest_exit_v17b import _build_variant_trades, _prepare_exit
from backtest_allocation_v17c import _simulate_allocation

VERSION = "engine-market-state-audit-v1.9a"
FIXED_EXIT = "MA5_MA10_DEATH_CROSS"
FIXED_ALLOCATION = {
    "name": "EQUAL_5_FIXED",
    "sizingMode": "EQUAL_SLOT",
    "maxPositions": 5,
    "riskBudgetPct": None,
    "maxWeightPct": 20.0,
}
MIN_STATE_STOCKS = 700
YEARS = (2022, 2023, 2024, 2025, 2026)


def _num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def _profit_factor(values):
    s = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    if s.empty:
        return None
    gains = float(s[s > 0].sum())
    losses = float(-s[s < 0].sum())
    return None if losses <= 0 else round(gains / losses, 4)


def _payoff(values):
    s = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    wins = s[s > 0]
    losses = s[s < 0]
    if wins.empty or losses.empty:
        return None
    return round(float(wins.mean()) / abs(float(losses.mean())), 4)


def _trade_group_stats(g: pd.DataFrame):
    if g is None or g.empty:
        return {
            "tradeCount": 0,
            "winRate": None,
            "expectancyPct": None,
            "profitFactor": None,
            "payoffRatio": None,
            "avgHoldingDays": None,
            "avgWinPct": None,
            "avgLossPct": None,
        }
    r = pd.to_numeric(g["netReturnPct"], errors="coerce").dropna()
    if r.empty:
        return {
            "tradeCount": int(len(g)),
            "winRate": None,
            "expectancyPct": None,
            "profitFactor": None,
            "payoffRatio": None,
            "avgHoldingDays": None,
            "avgWinPct": None,
            "avgLossPct": None,
        }
    wins = r[r > 0]
    losses = r[r < 0]
    hold = pd.to_numeric(g.get("holdingDays"), errors="coerce") if "holdingDays" in g else pd.Series(dtype=float)
    return {
        "tradeCount": int(len(r)),
        "winRate": round(float((r > 0).mean()), 4),
        "expectancyPct": round(float(r.mean()), 4),
        "profitFactor": _profit_factor(r),
        "payoffRatio": _payoff(r),
        "avgHoldingDays": None if hold.dropna().empty else round(float(hold.dropna().mean()), 2),
        "avgWinPct": None if wins.empty else round(float(wins.mean()), 4),
        "avgLossPct": None if losses.empty else round(float(losses.mean()), 4),
    }


def _prepare_strategy(data: pd.DataFrame):
    equities = data[data.market != "BENCH"].copy()
    # Frozen rows contain canonical OHLCV only. Rebuild technical features from
    # frozen history before applying the already-fixed V1.7C trade rules.
    return _prepare_exit(add_features(equities))


def _market_state_table(data: pd.DataFrame):
    eq = data[data.market != "BENCH"].copy()
    eq = eq.sort_values(["market", "symbol", "date"])
    eq["date"] = pd.to_datetime(eq.date).dt.tz_localize(None)
    g = eq.groupby(["market", "symbol"], sort=False)
    eq["prevCloseState"] = g["close"].shift(1)
    eq["ma60State"] = g["close"].transform(lambda s: s.rolling(60, min_periods=60).mean())
    eq["upState"] = eq.close > eq.prevCloseState
    eq["aboveMA60State"] = eq.close > eq.ma60State

    daily_rows = []
    for dt, x in eq.groupby("date", sort=True):
        adv_valid = x.prevCloseState.notna()
        breadth_valid = x.ma60State.notna()
        adv_n = int(adv_valid.sum())
        breadth_n = int(breadth_valid.sum())
        daily_rows.append({
            "date": pd.Timestamp(dt),
            "stockCount": int(x.close.notna().sum()),
            "advanceValidCount": adv_n,
            "breadthValidCount": breadth_n,
            "advanceRatio": None if adv_n == 0 else float(x.loc[adv_valid, "upState"].mean()),
            "breadthAboveMA60": None if breadth_n == 0 else float(x.loc[breadth_valid, "aboveMA60State"].mean()),
        })
    m = pd.DataFrame(daily_rows).sort_values("date")
    m["advanceRatio20"] = pd.to_numeric(m.advanceRatio, errors="coerce").rolling(20, min_periods=10).mean()

    b = data[data.market == "BENCH"].copy().sort_values("date")
    b["date"] = pd.to_datetime(b.date).dt.tz_localize(None)
    b["benchMA60"] = pd.to_numeric(b.close, errors="coerce").rolling(60, min_periods=60).mean()
    b["benchMA120"] = pd.to_numeric(b.close, errors="coerce").rolling(120, min_periods=120).mean()
    b["benchMA60Prev20"] = b.benchMA60.shift(20)
    b["benchMA60Slope20Pct"] = (b.benchMA60 / b.benchMA60Prev20 - 1.0) * 100.0
    b = b[["date", "close", "benchMA60", "benchMA120", "benchMA60Slope20Pct"]].rename(columns={"close": "benchClose"})

    m = m.merge(b, on="date", how="inner").sort_values("date").reset_index(drop=True)

    def classify(r):
        bc = _num(r.benchClose)
        ma120 = _num(r.benchMA120)
        slope = _num(r.benchMA60Slope20Pct)
        breadth = _num(r.breadthAboveMA60)
        if (
            r.breadthValidCount < MIN_STATE_STOCKS
            or bc is None
            or ma120 is None
            or slope is None
            or breadth is None
        ):
            return "UNKNOWN"
        if bc > ma120 and slope > 0 and breadth >= 0.55:
            return "RISK_ON"
        if bc < ma120 and slope < 0 and breadth <= 0.45:
            return "RISK_OFF"
        return "TRANSITION"

    m["marketStateV19A"] = m.apply(classify, axis=1)
    m["year"] = m.date.dt.year
    m["month"] = m.date.dt.to_period("M").astype(str)
    return m


def _clean_simulator(
    trades: pd.DataFrame,
    data: pd.DataFrame,
    costs: CostModel,
    recycle_same_open_exits: bool,
):
    """Chronology-clean equal-5 simulator.

    Sizing information is restricted to prior-session closes. Existing positions
    scheduled to exit on today's open are removed before capacity checks. The
    recycle variant makes their modeled opening-sale proceeds available for new
    opening buys; the conservative variant withholds those proceeds until after
    entries. Quantities are integer shares. Daily open is still only a proxy for
    odd-lot execution because 09:10 historical odd-lot prices are unavailable.
    """
    if trades.empty:
        return {
            "initialCapital": INITIAL_CAPITAL,
            "endingCapital": INITIAL_CAPITAL,
            "totalReturnPct": 0.0,
            "maxDrawdownPct": 0.0,
            "selectedTradeCount": 0,
            "candidateTradeCount": 0,
            "selectionRate": None,
            "skippedCapacity": 0,
            "skippedCash": 0,
            "skippedBelowOneShare": 0,
            "maxConcurrentPositions": 0,
            "avgAllocationPctAtEntry": None,
            "avgCapitalUtilizationPct": 0.0,
            "tradeCount": 0,
        }

    t = trades.copy().sort_values(
        ["entryDate", "ma5SlopePct", "tradeValue", "symbol"],
        ascending=[True, False, False, True],
    )
    d = data.copy().sort_values(["market", "symbol", "date"])
    d["prevCloseForOpenSizing"] = d.groupby(["market", "symbol"], sort=False)["close"].shift(1)
    prev_close_map = (
        d.drop_duplicates(["date", "market", "symbol"], keep="last")
        .set_index(["date", "market", "symbol"])["prevCloseForOpenSizing"]
    )
    close_map = (
        d.drop_duplicates(["date", "market", "symbol"], keep="last")
        .set_index(["date", "market", "symbol"])["close"]
    )
    sessions = sorted(pd.to_datetime(d.date).dt.tz_localize(None).drop_duplicates())
    by_entry = {pd.Timestamp(k): v for k, v in t.groupby("entryDate")}

    buy_factor = (1 + costs.slippage_each_side) * (1 + costs.buy_commission)
    sell_factor = (1 - costs.slippage_each_side) * (1 - costs.sell_commission - costs.sell_tax)

    cash = float(INITIAL_CAPITAL)
    positions = []
    selected = []
    equity_curve = []
    utilization = []
    allocation_pcts = []
    skipped_capacity = 0
    skipped_cash = 0
    skipped_below_one = 0
    max_concurrent = 0

    for dt in sessions:
        dt = pd.Timestamp(dt)

        # Opening exits from positions held before today. Remove them from slots
        # first. Depending on audit variant, their cash is either immediately
        # recyclable or withheld until after new opening entries.
        remaining = []
        opening_exit_cash = 0.0
        for p in positions:
            if p["exitDate"] == dt:
                opening_exit_cash += p["qty"] * p["exitPrice"] * sell_factor
            else:
                remaining.append(p)
        positions = remaining
        if recycle_same_open_exits:
            cash += opening_exit_cash
            opening_exit_cash = 0.0

        # Equity used for sizing is known before today's close: cash plus prior
        # session close marks for positions that remain open.
        pre_equity = cash + opening_exit_cash
        for p in positions:
            cp = _num(prev_close_map.get((dt, p["market"], p["symbol"])))
            if cp is None or cp <= 0:
                cp = p["entryPrice"]
            pre_equity += p["qty"] * cp * sell_factor

        todays = by_entry.get(dt)
        new_position_ids = set()
        if todays is not None:
            for idx, row in todays.iterrows():
                if any(p["market"] == row.market and p["symbol"] == row.symbol for p in positions):
                    continue
                if len(positions) >= 5:
                    skipped_capacity += 1
                    continue
                entry_price = _num(row.entryPrice)
                if entry_price is None or entry_price <= 0:
                    continue
                desired = pre_equity * 0.20
                allocation_cap = min(float(cash), float(desired))
                per_share_cash = entry_price * buy_factor
                if allocation_cap < per_share_cash:
                    skipped_below_one += 1
                    continue
                qty = math.floor(allocation_cap / per_share_cash)
                if qty < 1:
                    skipped_below_one += 1
                    continue
                spend = qty * per_share_cash
                if spend > cash + 1e-8:
                    skipped_cash += 1
                    continue
                cash -= spend
                p = {
                    "idx": idx,
                    "market": row.market,
                    "symbol": row.symbol,
                    "entryPrice": entry_price,
                    "exitDate": pd.Timestamp(row.exitDate),
                    "exitPrice": float(row.exitPrice),
                    "qty": int(qty),
                }
                positions.append(p)
                new_position_ids.add(idx)
                selected.append(idx)
                allocation_pcts.append(spend / pre_equity * 100 if pre_equity > 0 else np.nan)
                max_concurrent = max(max_concurrent, len(positions))

        if opening_exit_cash:
            cash += opening_exit_cash

        # A newly entered trade can hit its hard stop on the entry session. Such
        # exits must occur after the entry has been created, not before it.
        remaining = []
        for p in positions:
            if p["idx"] in new_position_ids and p["exitDate"] == dt:
                cash += p["qty"] * p["exitPrice"] * sell_factor
            else:
                remaining.append(p)
        positions = remaining

        equity = cash
        invested = 0.0
        for p in positions:
            cp = _num(close_map.get((dt, p["market"], p["symbol"])))
            if cp is None or cp <= 0:
                cp = p["entryPrice"]
            value = p["qty"] * cp * sell_factor
            equity += value
            invested += value
        equity_curve.append(float(equity))
        utilization.append(invested / equity if equity > 0 else 0.0)

    for p in positions:
        cash += p["qty"] * p["exitPrice"] * sell_factor
    if equity_curve:
        equity_curve[-1] = float(cash)

    sel = t.loc[sorted(set(selected))].copy() if selected else t.iloc[0:0].copy()
    metrics = _trade_summary(sel, costs)
    return {
        "initialCapital": INITIAL_CAPITAL,
        "endingCapital": round(float(cash), 2),
        "totalReturnPct": _round((cash / INITIAL_CAPITAL - 1.0) * 100.0),
        "maxDrawdownPct": _max_drawdown(equity_curve),
        "selectedTradeCount": int(len(sel)),
        "candidateTradeCount": int(len(t)),
        "selectionRate": round(len(sel) / len(t), 4) if len(t) else None,
        "skippedCapacity": int(skipped_capacity),
        "skippedCash": int(skipped_cash),
        "skippedBelowOneShare": int(skipped_below_one),
        "maxConcurrentPositions": int(max_concurrent),
        "avgAllocationPctAtEntry": None if not allocation_pcts else _round(float(pd.Series(allocation_pcts).dropna().mean())),
        "avgCapitalUtilizationPct": _round(float(np.mean(utilization)) * 100.0 if utilization else 0.0),
        **metrics,
    }


def _yearly_engine_audit(trades: pd.DataFrame, d: pd.DataFrame, costs: CostModel):
    out = {}
    for year in YEARS:
        g = trades[pd.to_datetime(trades.entryDate).dt.year == year].copy()
        legacy = _simulate_allocation(g, d, costs, FIXED_ALLOCATION)
        clean_recycle = _clean_simulator(g, d, costs, recycle_same_open_exits=True)
        clean_no_recycle = _clean_simulator(g, d, costs, recycle_same_open_exits=False)
        out[str(year)] = {
            "legacyV17C": legacy,
            "cleanPriorCloseRecycle": clean_recycle,
            "cleanPriorCloseNoRecycle": clean_no_recycle,
            "impactVsLegacy": {
                "recycleReturnDeltaPctPoint": None if legacy.get("totalReturnPct") is None else round(float(clean_recycle["totalReturnPct"]) - float(legacy["totalReturnPct"]), 4),
                "noRecycleReturnDeltaPctPoint": None if legacy.get("totalReturnPct") is None else round(float(clean_no_recycle["totalReturnPct"]) - float(legacy["totalReturnPct"]), 4),
                "recycleDrawdownDeltaPctPoint": None if legacy.get("maxDrawdownPct") is None else round(float(clean_recycle["maxDrawdownPct"]) - float(legacy["maxDrawdownPct"]), 4),
                "noRecycleDrawdownDeltaPctPoint": None if legacy.get("maxDrawdownPct") is None else round(float(clean_no_recycle["maxDrawdownPct"]) - float(legacy["maxDrawdownPct"]), 4),
            },
        }
    return out


def _trade_state_diagnostics(trades: pd.DataFrame, states: pd.DataFrame):
    cols = [
        "date", "marketStateV19A", "benchClose", "benchMA120",
        "benchMA60Slope20Pct", "breadthAboveMA60", "advanceRatio20",
        "stockCount", "breadthValidCount",
    ]
    t = trades.copy()
    t["signalDate"] = pd.to_datetime(t.signalDate).dt.tz_localize(None)
    t = t.merge(states[cols], left_on="signalDate", right_on="date", how="left")
    t["marketStateV19A"] = t.marketStateV19A.fillna("UNKNOWN")
    t["entryYear"] = pd.to_datetime(t.entryDate).dt.year

    by_state = {
        state: _trade_group_stats(g)
        for state, g in t.groupby("marketStateV19A", sort=True)
    }
    by_year_state = {}
    for year in YEARS:
        y = t[t.entryYear == year]
        by_year_state[str(year)] = {
            state: _trade_group_stats(g)
            for state, g in y.groupby("marketStateV19A", sort=True)
        }

    return t, by_state, by_year_state


def _monthly_state_summary(states: pd.DataFrame):
    rows = []
    for month, g in states.groupby("month", sort=True):
        counts = g.marketStateV19A.value_counts().to_dict()
        valid = g[g.marketStateV19A != "UNKNOWN"]
        dominant = None if valid.empty else str(valid.marketStateV19A.value_counts().index[0])
        rows.append({
            "month": month,
            "sessionCount": int(len(g)),
            "dominantState": dominant,
            "stateCounts": {k: int(v) for k, v in counts.items()},
            "avgBreadthAboveMA60": None if pd.to_numeric(g.breadthAboveMA60, errors="coerce").dropna().empty else round(float(pd.to_numeric(g.breadthAboveMA60, errors="coerce").mean()), 4),
            "avgAdvanceRatio20": None if pd.to_numeric(g.advanceRatio20, errors="coerce").dropna().empty else round(float(pd.to_numeric(g.advanceRatio20, errors="coerce").mean()), 4),
        })
    return rows


def build_v19a_summary(data: pd.DataFrame, generated_at: str):
    costs = CostModel()
    d = _prepare_strategy(data)
    trades, trade_audit = _build_variant_trades(d, FIXED_EXIT)
    states = _market_state_table(data)
    annotated, by_state, by_year_state = _trade_state_diagnostics(trades, states)

    latest = states.iloc[-1] if not states.empty else None
    engine = _yearly_engine_audit(trades, d, costs)

    return {
        "ok": True,
        "schemaVersion": 1,
        "version": VERSION,
        "generatedAt": generated_at,
        "purpose": "Preregistered V1.9A audit: quantify V1.7C portfolio chronology/share-granularity effects and diagnose fixed-strategy trade expectancy across a slow market-state classification without tuning any threshold.",
        "researchPolicy": {
            "preregisteredFile": "research/v19a-preregister.json",
            "noParameterSearch": True,
            "noGatingOptimization": True,
            "automaticProductionChanges": False,
            "important": "2022-2026 have already been inspected. State results are diagnostic evidence only, not clean OOS evidence and not permission to retrofit a filter."
        },
        "fixedStrategy": {
            "entry": "NEXT_OPEN daily-open proxy",
            "legacyRegime": "NO_BEAR",
            "initialStop": "max(entry - 1.25*ATR14, prior 10-session low)",
            "cooldownAfterHardStopSessions": 5,
            "exit": FIXED_EXIT,
            "allocation": FIXED_ALLOCATION,
            "selectionPriority": "higher MA5 slope, then higher signal-day trade value, then symbol",
        },
        "costModel": asdict(costs),
        "tradeAudit": trade_audit,
        "engineAudit": {
            "legacyIssue": "V1.7C sizes opening trades using same-session closes and releases same-day exit cash after entries.",
            "cleanRule": "Use prior-session close equity for opening sizing; integer shares only. Report both same-open exit-cash recycling and conservative no-recycling as bounds.",
            "dailyOpenProxyLimitation": "The freeze has daily OHLC only. Taiwan odd-lot first matching is not represented; no claim is made that the daily official open equals a realizable odd-lot fill.",
            "yearly": engine,
        },
        "marketStateDefinition": {
            "benchmarkProxy": "0050",
            "RISK_ON": "0050 close > MA120 AND MA60 20-session slope > 0 AND breadthAboveMA60 >= 55%",
            "RISK_OFF": "0050 close < MA120 AND MA60 20-session slope < 0 AND breadthAboveMA60 <= 45%",
            "TRANSITION": "all other valid observations",
            "UNKNOWN": f"insufficient lookback or breadthValidCount < {MIN_STATE_STOCKS}",
            "stateAssignedOn": "signal-day close using only information available by that close",
        },
        "tradePerformanceBySignalState": by_state,
        "tradePerformanceByYearAndSignalState": by_year_state,
        "monthlyMarketStates": _monthly_state_summary(states),
        "latestFrozenMarketState": None if latest is None else {
            "date": pd.Timestamp(latest.date).date().isoformat(),
            "state": str(latest.marketStateV19A),
            "benchClose": _round(latest.benchClose),
            "benchMA120": _round(latest.benchMA120),
            "benchMA60Slope20Pct": _round(latest.benchMA60Slope20Pct),
            "breadthAboveMA60": _round(latest.breadthAboveMA60),
            "advanceRatio20": _round(latest.advanceRatio20),
        },
        "counts": {
            "strategyTrades": int(len(trades)),
            "annotatedTrades": int(len(annotated)),
            "marketStateSessions": int(len(states)),
            "stateSessionCounts": {k: int(v) for k, v in states.marketStateV19A.value_counts().to_dict().items()},
        },
        "knownLimitations": [
            "Current-universe survivorship bias remains in this frozen Yahoo-based snapshot.",
            "The historical liquidity filter still inherits the adjusted-price times unadjusted-volume tradeValue definition from earlier research code.",
            "Daily OHLC cannot model Taiwan 09:10 odd-lot matching or exact intraday queue/slippage.",
            "The slow state thresholds are diagnostic and predeclared; they are not optimized and must not be retuned on these inspected years.",
            "A future point-in-time universe and historical actual transaction-value dataset are required before production-grade inference.",
        ],
        "automaticProductionChanges": False,
    }
