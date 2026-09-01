from __future__ import annotations

import math
from dataclasses import asdict

import numpy as np
import pandas as pd

from backtest_data import add_features
from backtest_ma5 import CostModel, INITIAL_CAPITAL, _max_drawdown, _net_return, _round, _trade_summary
from backtest_exit_v17b import _build_variant_trades, _prepare_exit
from backtest_allocation_v17c import _simulate_allocation

VERSION = "engine-market-state-audit-v1.9a"
FIXED_EXIT = "MA5_MA10_DEATH_CROSS"
FIXED_ALLOCATION = {
    "name": "EQUAL_5_FIXED", "sizingMode": "EQUAL_SLOT", "maxPositions": 5,
    "riskBudgetPct": None, "maxWeightPct": 20.0,
}
MIN_STATE_STOCKS = 700
YEARS = (2022, 2023, 2024, 2025, 2026)


def _num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def _pf(r):
    r = pd.to_numeric(pd.Series(r), errors="coerce").dropna()
    gains, losses = float(r[r > 0].sum()), float(-r[r < 0].sum())
    return None if losses <= 0 else round(gains / losses, 4)


def _group_stats(g: pd.DataFrame):
    if g is None or g.empty:
        return {"tradeCount": 0, "winRate": None, "expectancyPct": None, "profitFactor": None, "payoffRatio": None, "avgHoldingDays": None}
    r = pd.to_numeric(g.netReturnPct, errors="coerce").dropna()
    wins, losses = r[r > 0], r[r < 0]
    payoff = None if wins.empty or losses.empty else round(float(wins.mean()) / abs(float(losses.mean())), 4)
    return {
        "tradeCount": int(len(r)),
        "winRate": None if r.empty else round(float((r > 0).mean()), 4),
        "expectancyPct": None if r.empty else round(float(r.mean()), 4),
        "profitFactor": _pf(r),
        "payoffRatio": payoff,
        "avgHoldingDays": None if g.holdingDays.empty else round(float(pd.to_numeric(g.holdingDays, errors="coerce").mean()), 2),
        "avgWinPct": None if wins.empty else round(float(wins.mean()), 4),
        "avgLossPct": None if losses.empty else round(float(losses.mean()), 4),
    }


def _prepare_strategy(data: pd.DataFrame):
    # Freeze stores OHLCV, so deterministic indicators are rebuilt from frozen rows.
    return _prepare_exit(add_features(data[data.market != "BENCH"].copy()))


def _market_states(data: pd.DataFrame):
    eq = data[data.market != "BENCH"].copy().sort_values(["market", "symbol", "date"])
    eq["date"] = pd.to_datetime(eq.date).dt.tz_localize(None)
    g = eq.groupby(["market", "symbol"], sort=False)
    eq["prevStateClose"] = g.close.shift(1)
    eq["ma60State"] = g.close.transform(lambda s: s.rolling(60, min_periods=60).mean())
    eq["upState"] = eq.close > eq.prevStateClose
    eq["above60State"] = eq.close > eq.ma60State

    rows = []
    for dt, x in eq.groupby("date", sort=True):
        av, bv = x.prevStateClose.notna(), x.ma60State.notna()
        rows.append({
            "date": pd.Timestamp(dt),
            "stockCount": int(x.close.notna().sum()),
            "breadthValidCount": int(bv.sum()),
            "advanceRatio": None if int(av.sum()) == 0 else float(x.loc[av, "upState"].mean()),
            "breadthAboveMA60": None if int(bv.sum()) == 0 else float(x.loc[bv, "above60State"].mean()),
        })
    m = pd.DataFrame(rows).sort_values("date")
    m["advanceRatio20"] = pd.to_numeric(m.advanceRatio, errors="coerce").rolling(20, min_periods=10).mean()

    b = data[data.market == "BENCH"].copy().sort_values("date")
    b["date"] = pd.to_datetime(b.date).dt.tz_localize(None)
    b["benchMA60"] = pd.to_numeric(b.close, errors="coerce").rolling(60, min_periods=60).mean()
    b["benchMA120"] = pd.to_numeric(b.close, errors="coerce").rolling(120, min_periods=120).mean()
    b["benchMA60Slope20Pct"] = (b.benchMA60 / b.benchMA60.shift(20) - 1) * 100
    b = b[["date", "close", "benchMA120", "benchMA60Slope20Pct"]].rename(columns={"close": "benchClose"})
    m = m.merge(b, on="date", how="inner").sort_values("date").reset_index(drop=True)

    def classify(r):
        vals = [_num(r.benchClose), _num(r.benchMA120), _num(r.benchMA60Slope20Pct), _num(r.breadthAboveMA60)]
        if r.breadthValidCount < MIN_STATE_STOCKS or any(v is None for v in vals):
            return "UNKNOWN"
        close, ma120, slope, breadth = vals
        if close > ma120 and slope > 0 and breadth >= 0.55:
            return "RISK_ON"
        if close < ma120 and slope < 0 and breadth <= 0.45:
            return "RISK_OFF"
        return "TRANSITION"

    m["marketStateV19A"] = m.apply(classify, axis=1)
    m["year"] = m.date.dt.year
    m["month"] = m.date.dt.to_period("M").astype(str)
    return m


def _clean_sim(trades: pd.DataFrame, data: pd.DataFrame, costs: CostModel, recycle: bool):
    """Prior-close sizing, exit-before-entry slot chronology, integer shares."""
    if trades.empty:
        return {"initialCapital": INITIAL_CAPITAL, "endingCapital": INITIAL_CAPITAL, "totalReturnPct": 0.0, "maxDrawdownPct": 0.0, "selectedTradeCount": 0, "candidateTradeCount": 0, "tradeCount": 0}

    t = trades.copy().sort_values(["entryDate", "ma5SlopePct", "tradeValue", "symbol"], ascending=[True, False, False, True])
    d = data.copy().sort_values(["market", "symbol", "date"])
    d["prevCloseForOpen"] = d.groupby(["market", "symbol"], sort=False).close.shift(1)
    key = ["date", "market", "symbol"]
    prev_map = d.drop_duplicates(key, keep="last").set_index(key).prevCloseForOpen
    close_map = d.drop_duplicates(key, keep="last").set_index(key).close
    sessions = sorted(pd.to_datetime(d.date).dt.tz_localize(None).drop_duplicates())
    by_entry = {pd.Timestamp(k): v for k, v in t.groupby("entryDate")}
    bf = (1 + costs.slippage_each_side) * (1 + costs.buy_commission)
    sf = (1 - costs.slippage_each_side) * (1 - costs.sell_commission - costs.sell_tax)

    cash, positions, selected = float(INITIAL_CAPITAL), [], []
    curve, util, allocs = [], [], []
    skipped_capacity = skipped_cash = skipped_one = max_concurrent = 0

    for dt in sessions:
        dt = pd.Timestamp(dt)
        remain, exit_cash = [], 0.0
        for p in positions:
            if p["exitDate"] == dt:
                exit_cash += p["qty"] * p["exitPrice"] * sf
            else:
                remain.append(p)
        positions = remain
        if recycle:
            cash += exit_cash
            exit_cash = 0.0

        pre_equity = cash + exit_cash
        for p in positions:
            cp = _num(prev_map.get((dt, p["market"], p["symbol"]))) or p["entryPrice"]
            pre_equity += p["qty"] * cp * sf

        new_ids = set()
        todays = by_entry.get(dt)
        if todays is not None:
            for idx, row in todays.iterrows():
                if any(p["market"] == row.market and p["symbol"] == row.symbol for p in positions):
                    continue
                if len(positions) >= 5:
                    skipped_capacity += 1
                    continue
                ep = _num(row.entryPrice)
                if ep is None or ep <= 0:
                    continue
                cap = min(cash, pre_equity * 0.20)
                per_share = ep * bf
                qty = math.floor(cap / per_share)
                if qty < 1:
                    skipped_one += 1
                    continue
                spend = qty * per_share
                if spend > cash + 1e-8:
                    skipped_cash += 1
                    continue
                cash -= spend
                positions.append({"idx": idx, "market": row.market, "symbol": row.symbol, "entryPrice": ep, "exitDate": pd.Timestamp(row.exitDate), "exitPrice": float(row.exitPrice), "qty": int(qty)})
                selected.append(idx)
                new_ids.add(idx)
                allocs.append(spend / pre_equity * 100 if pre_equity > 0 else np.nan)
                max_concurrent = max(max_concurrent, len(positions))

        cash += exit_cash
        remain = []
        for p in positions:
            if p["idx"] in new_ids and p["exitDate"] == dt:
                cash += p["qty"] * p["exitPrice"] * sf
            else:
                remain.append(p)
        positions = remain

        equity, invested = cash, 0.0
        for p in positions:
            cp = _num(close_map.get((dt, p["market"], p["symbol"]))) or p["entryPrice"]
            value = p["qty"] * cp * sf
            equity += value
            invested += value
        curve.append(float(equity))
        util.append(invested / equity if equity > 0 else 0.0)

    for p in positions:
        cash += p["qty"] * p["exitPrice"] * sf
    if curve:
        curve[-1] = float(cash)
    sel = t.loc[sorted(set(selected))].copy() if selected else t.iloc[0:0].copy()
    metrics = _trade_summary(sel, costs)
    return {
        "initialCapital": INITIAL_CAPITAL,
        "endingCapital": round(float(cash), 2),
        "totalReturnPct": _round((cash / INITIAL_CAPITAL - 1) * 100),
        "maxDrawdownPct": _max_drawdown(curve),
        "selectedTradeCount": int(len(sel)),
        "candidateTradeCount": int(len(t)),
        "selectionRate": round(len(sel) / len(t), 4) if len(t) else None,
        "skippedCapacity": int(skipped_capacity),
        "skippedCash": int(skipped_cash),
        "skippedBelowOneShare": int(skipped_one),
        "maxConcurrentPositions": int(max_concurrent),
        "avgAllocationPctAtEntry": None if not allocs else _round(float(pd.Series(allocs).dropna().mean())),
        "avgCapitalUtilizationPct": _round(float(np.mean(util)) * 100 if util else 0.0),
        **metrics,
    }


def _engine_audit(trades, d, costs):
    out = {}
    for year in YEARS:
        g = trades[pd.to_datetime(trades.entryDate).dt.year == year].copy()
        legacy = _simulate_allocation(g, d, costs, FIXED_ALLOCATION)
        recycle = _clean_sim(g, d, costs, True)
        conservative = _clean_sim(g, d, costs, False)
        out[str(year)] = {
            "legacyV17C": legacy,
            "cleanPriorCloseRecycle": recycle,
            "cleanPriorCloseNoRecycle": conservative,
            "impactVsLegacy": {
                "recycleReturnDeltaPctPoint": round(float(recycle["totalReturnPct"]) - float(legacy["totalReturnPct"]), 4),
                "noRecycleReturnDeltaPctPoint": round(float(conservative["totalReturnPct"]) - float(legacy["totalReturnPct"]), 4),
                "recycleDrawdownDeltaPctPoint": round(float(recycle["maxDrawdownPct"]) - float(legacy["maxDrawdownPct"]), 4),
                "noRecycleDrawdownDeltaPctPoint": round(float(conservative["maxDrawdownPct"]) - float(legacy["maxDrawdownPct"]), 4),
            },
        }
    return out


def _state_diagnostics(trades: pd.DataFrame, states: pd.DataFrame, costs: CostModel):
    t = trades.copy()
    t["netReturnPct"] = [_net_return(e, x, costs) for e, x in zip(t.entryPrice, t.exitPrice)]
    t["signalDate"] = pd.to_datetime(t.signalDate).dt.tz_localize(None)
    cols = ["date", "marketStateV19A", "benchClose", "benchMA120", "benchMA60Slope20Pct", "breadthAboveMA60", "advanceRatio20"]
    t = t.merge(states[cols], left_on="signalDate", right_on="date", how="left")
    t["marketStateV19A"] = t.marketStateV19A.fillna("UNKNOWN")
    t["entryYear"] = pd.to_datetime(t.entryDate).dt.year
    by_state = {s: _group_stats(g) for s, g in t.groupby("marketStateV19A", sort=True)}
    by_year = {}
    for year in YEARS:
        y = t[t.entryYear == year]
        by_year[str(year)] = {s: _group_stats(g) for s, g in y.groupby("marketStateV19A", sort=True)}
    return t, by_state, by_year


def _monthly(states):
    rows = []
    for month, g in states.groupby("month", sort=True):
        counts = g.marketStateV19A.value_counts().to_dict()
        valid = g[g.marketStateV19A != "UNKNOWN"]
        rows.append({
            "month": month,
            "sessionCount": int(len(g)),
            "dominantState": None if valid.empty else str(valid.marketStateV19A.value_counts().index[0]),
            "stateCounts": {k: int(v) for k, v in counts.items()},
            "avgBreadthAboveMA60": None if pd.to_numeric(g.breadthAboveMA60, errors="coerce").dropna().empty else round(float(pd.to_numeric(g.breadthAboveMA60, errors="coerce").mean()), 4),
            "avgAdvanceRatio20": None if pd.to_numeric(g.advanceRatio20, errors="coerce").dropna().empty else round(float(pd.to_numeric(g.advanceRatio20, errors="coerce").mean()), 4),
        })
    return rows


def build_v19a_summary(data: pd.DataFrame, generated_at: str):
    costs = CostModel()
    d = _prepare_strategy(data)
    trades, audit = _build_variant_trades(d, FIXED_EXIT)
    states = _market_states(data)
    annotated, by_state, by_year = _state_diagnostics(trades, states, costs)
    latest = states.iloc[-1] if not states.empty else None
    return {
        "ok": True,
        "schemaVersion": 1,
        "version": VERSION,
        "generatedAt": generated_at,
        "purpose": "Preregistered V1.9A audit of portfolio chronology/share granularity plus slow market-state diagnosis of the fixed V1.7C strategy.",
        "researchPolicy": {
            "preregisteredFile": "research/v19a-preregister.json",
            "noParameterSearch": True,
            "noGatingOptimization": True,
            "automaticProductionChanges": False,
            "important": "2022-2026 are already inspected; these state results are diagnostic, not clean OOS evidence."
        },
        "fixedStrategy": {
            "entry": "NEXT_OPEN daily-open proxy", "legacyRegime": "NO_BEAR",
            "initialStop": "max(entry - 1.25*ATR14, prior 10-session low)",
            "cooldownAfterHardStopSessions": 5, "exit": FIXED_EXIT,
            "allocation": FIXED_ALLOCATION,
            "selectionPriority": "higher MA5 slope, then higher signal-day trade value, then symbol",
        },
        "costModel": asdict(costs),
        "tradeAudit": audit,
        "engineAudit": {
            "legacyIssue": "V1.7C sizes opening trades using same-session closes and releases same-day exit cash after entries.",
            "cleanRule": "Prior-session close equity for opening sizing; scheduled opening exits free slots first; integer shares; report recycle and no-recycle bounds.",
            "dailyOpenProxyLimitation": "Daily OHLC cannot represent Taiwan 09:10 odd-lot matching.",
            "yearly": _engine_audit(trades, d, costs),
        },
        "marketStateDefinition": {
            "benchmarkProxy": "0050",
            "RISK_ON": "0050 close > MA120 AND MA60 20-session slope > 0 AND breadthAboveMA60 >= 55%",
            "RISK_OFF": "0050 close < MA120 AND MA60 20-session slope < 0 AND breadthAboveMA60 <= 45%",
            "TRANSITION": "all other valid observations",
            "UNKNOWN": f"insufficient lookback or breadthValidCount < {MIN_STATE_STOCKS}",
            "stateAssignedOn": "signal-day close using only data available by that close",
        },
        "tradePerformanceBySignalState": by_state,
        "tradePerformanceByYearAndSignalState": by_year,
        "monthlyMarketStates": _monthly(states),
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
            "strategyTrades": int(len(trades)), "annotatedTrades": int(len(annotated)),
            "marketStateSessions": int(len(states)),
            "stateSessionCounts": {k: int(v) for k, v in states.marketStateV19A.value_counts().to_dict().items()},
        },
        "knownLimitations": [
            "Current-universe survivorship bias remains in this frozen Yahoo-based snapshot.",
            "Historical liquidity filter still inherits adjusted-price times unadjusted-volume tradeValue from earlier research code.",
            "Daily OHLC cannot model 09:10 odd-lot matching, queue position or exact intraday slippage.",
            "State thresholds are predeclared diagnostic rules and must not be retuned on these inspected years.",
            "Point-in-time universe and actual historical transaction-value data are still required for production-grade inference."
        ],
        "automaticProductionChanges": False,
    }
