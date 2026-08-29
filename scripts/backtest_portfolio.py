from __future__ import annotations

import math
from dataclasses import asdict

import numpy as np
import pandas as pd

from backtest_execution import CostModel, STRATEGIES

VERSION = "portfolio-backtest-v1.3"

INITIAL_CAPITAL = 100_000.0
MAX_POSITIONS = 5
SLOT_FRACTION = 1.0 / MAX_POSITIONS

COHORTS = {
    "ALL_EXTENDED": lambda x: pd.Series(True, index=x.index),
    "GAP_EXTENSION": lambda x: x.extensionSubtype.eq("GAP_EXTENSION"),
    "GAP_EXTENSION_BULL": lambda x: x.extensionSubtype.eq("GAP_EXTENSION") & x.marketRegime.eq("BULL"),
    "GAP_EXTENSION_STRONG_SECTOR": lambda x: x.extensionSubtype.eq("GAP_EXTENSION") & x.sectorBucket.eq("STRONG_70_PLUS"),
    "GAP_EXTENSION_BULL_STRONG_SECTOR": lambda x: x.extensionSubtype.eq("GAP_EXTENSION") & x.marketRegime.eq("BULL") & x.sectorBucket.eq("STRONG_70_PLUS"),
    "STRONG_CONTINUATION": lambda x: x.extensionSubtype.eq("STRONG_CONTINUATION"),
    "EXHAUSTION_RISK": lambda x: x.extensionSubtype.eq("EXHAUSTION_RISK"),
}

SECTOR_PRIORITY = {
    "STRONG_70_PLUS": 3,
    "MID_50_70": 2,
    "WEAK_LT50": 1,
    "UNKNOWN": 0,
}


def _num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def _round(x, n=4):
    v = _num(x)
    return None if v is None else round(v, n)


def _profit_factor(values):
    s = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    if s.empty:
        return None
    gains = float(s[s > 0].sum())
    losses = float(-s[s < 0].sum())
    if losses <= 0:
        return None
    return round(gains / losses, 4)


def _max_consecutive_losses(values):
    best = 0
    run = 0
    for v in values:
        if v < 0:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return int(best)


def _attach_trade_dates(trades: pd.DataFrame, data: pd.DataFrame):
    cal = data[["date", "market", "symbol"]].copy()
    cal["date"] = pd.to_datetime(cal.date).dt.tz_localize(None)
    cal = cal.drop_duplicates(["date", "market", "symbol"]).sort_values(["market", "symbol", "date"])
    g = cal.groupby(["market", "symbol"], sort=False)
    for i in range(1, 6):
        cal[f"fd{i}"] = g["date"].shift(-i)
    cal["signalDate"] = cal.date.dt.date.astype(str)
    cal = cal.drop(columns=["date"])

    t = trades.merge(cal, how="left", on=["signalDate", "market", "symbol"])
    t["entryDate"] = pd.to_datetime(t.fd1)
    t["exitDate"] = pd.NaT
    for i in range(1, 6):
        mask = pd.to_numeric(t.exitDay, errors="coerce").eq(i)
        t.loc[mask, "exitDate"] = pd.to_datetime(t.loc[mask, f"fd{i}"])
    return t.drop(columns=[f"fd{i}" for i in range(1, 6)])


def _price_series(data: pd.DataFrame, column: str):
    x = data[["date", "market", "symbol", column]].copy()
    x["date"] = pd.to_datetime(x.date).dt.tz_localize(None)
    x = x.drop_duplicates(["date", "market", "symbol"], keep="last")
    return x.set_index(["date", "market", "symbol"])[column]


def _px(series: pd.Series, dt, market, symbol):
    try:
        v = series.get((pd.Timestamp(dt), market, symbol), np.nan)
    except Exception:
        return None
    return _num(v)


def _max_drawdown(equity_values):
    if not equity_values:
        return None
    peak = -np.inf
    worst = 0.0
    for e in equity_values:
        if not math.isfinite(e) or e <= 0:
            continue
        peak = max(peak, e)
        if peak > 0:
            dd = e / peak - 1.0
            worst = min(worst, dd)
    return round(worst * 100, 4)


def _trade_summary(selected: pd.DataFrame):
    if selected.empty:
        return {
            "tradeCount": 0,
            "winRate": None,
            "avgTradeNetReturnPct": None,
            "avgWinPct": None,
            "avgLossPct": None,
            "payoffRatio": None,
            "expectancyPct": None,
            "profitFactor": None,
            "maxTradeGainPct": None,
            "maxTradeLossPct": None,
            "sumSelectedTradeReturnPct": None,
            "maxConsecutiveLosses": 0,
            "bestTrade": None,
            "worstTrade": None,
        }
    r = pd.to_numeric(selected.netReturnPct, errors="coerce").dropna()
    wins = r[r > 0]
    losses = r[r < 0]
    avg_win = float(wins.mean()) if len(wins) else None
    avg_loss = float(losses.mean()) if len(losses) else None
    payoff = (avg_win / abs(avg_loss)) if avg_win is not None and avg_loss is not None and avg_loss != 0 else None

    ordered = selected.loc[r.index].copy()
    best_i = r.idxmax()
    worst_i = r.idxmin()

    def brief(row):
        return {
            "symbol": str(row.symbol),
            "name": str(row["name"]),
            "signalDate": str(row.signalDate),
            "entryDate": None if pd.isna(row.entryDate) else pd.Timestamp(row.entryDate).date().isoformat(),
            "exitDate": None if pd.isna(row.exitDate) else pd.Timestamp(row.exitDate).date().isoformat(),
            "netReturnPct": _round(row.netReturnPct),
            "exitType": str(row.exitType),
        }

    return {
        "tradeCount": int(len(r)),
        "winRate": round(float((r > 0).mean()), 4),
        "avgTradeNetReturnPct": round(float(r.mean()), 4),
        "avgWinPct": _round(avg_win),
        "avgLossPct": _round(avg_loss),
        "payoffRatio": _round(payoff),
        "expectancyPct": round(float(r.mean()), 4),
        "profitFactor": _profit_factor(r),
        "maxTradeGainPct": round(float(r.max()), 4),
        "maxTradeLossPct": round(float(r.min()), 4),
        "sumSelectedTradeReturnPct": round(float(r.sum()), 4),
        "maxConsecutiveLosses": _max_consecutive_losses(r.tolist()),
        "bestTrade": brief(ordered.loc[best_i]),
        "worstTrade": brief(ordered.loc[worst_i]),
    }


def _simulate_portfolio(candidates: pd.DataFrame, data: pd.DataFrame, costs: CostModel, initial_capital=INITIAL_CAPITAL, max_positions=MAX_POSITIONS):
    c = candidates.copy()
    c = c[(c.entryStatus == "ENTERED") & c.netReturnPct.notna() & c.entryDate.notna() & c.exitDate.notna()].copy()
    if c.empty:
        return {
            "candidateTradeCount": 0,
            "selectedTradeCount": 0,
            "selectionRate": None,
            "initialCapital": round(initial_capital, 2),
            "endingCapital": round(initial_capital, 2),
            "totalReturnPct": 0.0,
            "cagrPct": None,
            "maxDrawdownPct": 0.0,
            "avgCapitalUtilizationPct": 0.0,
            "maxConcurrentPositions": 0,
            "skippedCapacity": 0,
            "skippedDuplicateSymbol": 0,
            "skippedCash": 0,
            **_trade_summary(pd.DataFrame()),
        }

    c["sectorPriority"] = c.sectorBucket.map(SECTOR_PRIORITY).fillna(0)
    c["riskSort"] = pd.to_numeric(c.initialRiskPct, errors="coerce").fillna(999.0)
    c = c.sort_values(["entryDate", "sectorPriority", "riskSort", "symbol"], ascending=[True, False, True, True])

    open_px = _price_series(data, "open")
    close_px = _price_series(data, "close")
    sessions = sorted(pd.to_datetime(data.date).dt.tz_localize(None).drop_duplicates())
    first_date = pd.Timestamp(c.entryDate.min())
    last_date = pd.Timestamp(c.exitDate.max())
    sessions = [d for d in sessions if first_date <= d <= last_date]

    buy_factor = (1 + costs.slippage_each_side) * (1 + costs.buy_commission)
    sell_factor = (1 - costs.slippage_each_side) * (1 - costs.sell_commission - costs.sell_tax)

    by_entry = {pd.Timestamp(dt): g for dt, g in c.groupby("entryDate")}
    cash = float(initial_capital)
    positions = []
    chosen_idx = []
    skipped_capacity = 0
    skipped_duplicate = 0
    skipped_cash = 0
    max_concurrent = 0
    equity_curve = []
    utilization = []

    for dt in sessions:
        dt = pd.Timestamp(dt)

        # Estimate portfolio equity at the opening before new entries. Existing positions
        # are marked to the current open and valued net of estimated exit friction.
        open_equity = cash
        for p in positions:
            p_open = _px(open_px, dt, p["market"], p["symbol"])
            if p_open is None:
                p_open = p["entryPrice"]
            open_equity += p["qty"] * p_open * sell_factor

        todays = by_entry.get(dt)
        if todays is not None and not todays.empty:
            for idx, row in todays.iterrows():
                if any(p["symbol"] == row.symbol and p["market"] == row.market for p in positions):
                    skipped_duplicate += 1
                    continue
                if len(positions) >= max_positions:
                    skipped_capacity += 1
                    continue
                target_cash = open_equity / max_positions
                allocation = min(float(cash), float(target_cash))
                if allocation <= max(100.0, initial_capital * 0.001):
                    skipped_cash += 1
                    continue
                entry_price = _num(row.entryPrice)
                if entry_price is None or entry_price <= 0:
                    continue
                qty = allocation / (entry_price * buy_factor)
                if qty <= 0:
                    continue
                cash -= allocation
                positions.append({
                    "rowIndex": idx,
                    "market": row.market,
                    "symbol": row.symbol,
                    "entryPrice": entry_price,
                    "qty": qty,
                    "allocation": allocation,
                    "entryDate": dt,
                    "exitDate": pd.Timestamp(row.exitDate),
                    "exitPrice": float(row.exitPrice),
                })
                chosen_idx.append(idx)
                max_concurrent = max(max_concurrent, len(positions))

        # Conservative chronology: new entries occur at the open; exits from existing
        # positions on the same date release cash only after entries have been allocated.
        still_open = []
        for p in positions:
            if p["exitDate"] == dt:
                cash += p["qty"] * p["exitPrice"] * sell_factor
            else:
                still_open.append(p)
        positions = still_open

        equity = cash
        invested = 0.0
        for p in positions:
            cp = _px(close_px, dt, p["market"], p["symbol"])
            if cp is None:
                cp = p["entryPrice"]
            liquidation_value = p["qty"] * cp * sell_factor
            equity += liquidation_value
            invested += liquidation_value
        equity_curve.append((dt, equity))
        utilization.append(invested / equity if equity > 0 else 0.0)

    # A completed candidate should normally be closed by the last simulation date. If a
    # position remains due to missing calendar/price data, conservatively liquidate it at
    # its modeled exit price so ending capital is defined, while preserving the limitation.
    for p in positions:
        cash += p["qty"] * p["exitPrice"] * sell_factor
    positions = []
    if equity_curve:
        equity_curve[-1] = (equity_curve[-1][0], cash)

    selected = c.loc[sorted(set(chosen_idx))].copy() if chosen_idx else c.iloc[0:0].copy()
    trade_metrics = _trade_summary(selected)
    eq = [float(v) for _, v in equity_curve]
    ending = float(cash)
    total_return = (ending / initial_capital - 1) * 100
    if equity_curve and equity_curve[-1][0] > equity_curve[0][0] and ending > 0:
        years = (equity_curve[-1][0] - equity_curve[0][0]).days / 365.25
        cagr = ((ending / initial_capital) ** (1 / years) - 1) * 100 if years > 0 else None
    else:
        cagr = None

    return {
        "candidateTradeCount": int(len(c)),
        "selectedTradeCount": int(len(selected)),
        "selectionRate": round(len(selected) / len(c), 4) if len(c) else None,
        "initialCapital": round(initial_capital, 2),
        "endingCapital": round(ending, 2),
        "totalReturnPct": round(total_return, 4),
        "cagrPct": _round(cagr),
        "maxDrawdownPct": _max_drawdown(eq),
        "avgCapitalUtilizationPct": round(float(np.mean(utilization)) * 100, 4) if utilization else 0.0,
        "maxConcurrentPositions": int(max_concurrent),
        "skippedCapacity": int(skipped_capacity),
        "skippedDuplicateSymbol": int(skipped_duplicate),
        "skippedCash": int(skipped_cash),
        **trade_metrics,
    }


def build_portfolio_summary(trades: pd.DataFrame, data: pd.DataFrame, generated_at: str, costs: CostModel | None = None):
    costs = costs or CostModel()
    t = _attach_trade_dates(trades, data)
    complete = t[(t.entryStatus == "ENTERED") & t.netReturnPct.notna() & t.entryDate.notna() & t.exitDate.notna()].copy()

    overall = {}
    by_split = {}
    for cohort_name, cohort_filter in COHORTS.items():
        overall[cohort_name] = {}
        mask = cohort_filter(complete)
        cohort = complete[mask].copy()
        for strategy in STRATEGIES:
            overall[cohort_name][strategy] = _simulate_portfolio(
                cohort[cohort.strategy == strategy], data, costs
            )

    for split in sorted(complete.split.dropna().astype(str).unique()):
        by_split[split] = {}
        split_data = complete[complete.split.astype(str) == split].copy()
        for cohort_name, cohort_filter in COHORTS.items():
            by_split[split][cohort_name] = {}
            cohort = split_data[cohort_filter(split_data)].copy()
            for strategy in STRATEGIES:
                by_split[split][cohort_name][strategy] = _simulate_portfolio(
                    cohort[cohort.strategy == strategy], data, costs
                )

    ranked = []
    for cohort_name, strategies in overall.items():
        for strategy, stats in strategies.items():
            ranked.append({
                "cohort": cohort_name,
                "strategy": strategy,
                "totalReturnPct": stats.get("totalReturnPct"),
                "maxDrawdownPct": stats.get("maxDrawdownPct"),
                "profitFactor": stats.get("profitFactor"),
                "expectancyPct": stats.get("expectancyPct"),
                "tradeCount": stats.get("tradeCount"),
            })
    ranked = sorted(ranked, key=lambda r: (-999999 if r["totalReturnPct"] is None else r["totalReturnPct"]), reverse=True)

    validation_ranking = []
    validation = by_split.get("VALIDATION_2025", {})
    for cohort_name, strategies in validation.items():
        for strategy, stats in strategies.items():
            validation_ranking.append({
                "cohort": cohort_name,
                "strategy": strategy,
                "totalReturnPct": stats.get("totalReturnPct"),
                "maxDrawdownPct": stats.get("maxDrawdownPct"),
                "profitFactor": stats.get("profitFactor"),
                "expectancyPct": stats.get("expectancyPct"),
                "tradeCount": stats.get("tradeCount"),
            })
    validation_ranking = sorted(validation_ranking, key=lambda r: (-999999 if r["totalReturnPct"] is None else r["totalReturnPct"]), reverse=True)

    return {
        "ok": True,
        "schemaVersion": 1,
        "version": VERSION,
        "generatedAt": generated_at,
        "purpose": "Normalized portfolio-level evaluation of V1.2 executable trades: compounding return, payoff, single-trade extremes and portfolio drawdown matter more than win rate alone.",
        "portfolioAssumptions": {
            "initialCapitalTWD": int(INITIAL_CAPITAL),
            "normalizedResearchCapital": True,
            "maxConcurrentPositions": MAX_POSITIONS,
            "targetSlotPct": round(SLOT_FRACTION * 100, 2),
            "leverage": False,
            "fractionalShares": True,
            "sameDayCashReuse": False,
            "selectionPriority": "When capacity is constrained: stronger sector bucket first, then lower initial risk %, then symbol. This is deterministic and not parameter-optimized.",
            "markToMarket": "Open positions are marked to daily close at estimated net liquidation value for drawdown/equity; modeled exits use V1.2 exit prices.",
            "costModel": {
                "buyCommissionPct": round(costs.buy_commission * 100, 4),
                "sellCommissionPct": round(costs.sell_commission * 100, 4),
                "sellTransactionTaxPct": round(costs.sell_tax * 100, 4),
                "slippageEachSidePct": round(costs.slippage_each_side * 100, 4),
            },
        },
        "metricDefinitions": {
            "totalReturnPct": "Compounded normalized portfolio return after modeled trading friction and capacity limits.",
            "maxDrawdownPct": "Worst peak-to-trough decline of daily mark-to-market liquidation equity.",
            "avgWinPct": "Average net percentage return of profitable selected trades.",
            "avgLossPct": "Average net percentage return of losing selected trades.",
            "payoffRatio": "Average win divided by absolute average loss.",
            "expectancyPct": "Average net return per selected trade; equivalent to win-rate/payoff weighted expectancy.",
            "profitFactor": "Sum of positive net trade returns divided by absolute sum of negative net trade returns.",
            "maxTradeGainPct": "Largest single selected-trade net percentage gain.",
            "maxTradeLossPct": "Largest single selected-trade net percentage loss.",
            "sumSelectedTradeReturnPct": "Arithmetic sum of selected trade returns; diagnostic only, not portfolio return.",
        },
        "cohortDefinitions": {
            "ALL_EXTENDED": "All V1.1 EXTENDED signals.",
            "GAP_EXTENSION": "Primary subtype GAP_EXTENSION.",
            "GAP_EXTENSION_BULL": "GAP_EXTENSION when signal-day market regime is BULL.",
            "GAP_EXTENSION_STRONG_SECTOR": "GAP_EXTENSION in sector strength bucket >=70.",
            "GAP_EXTENSION_BULL_STRONG_SECTOR": "GAP_EXTENSION with both BULL market and sector strength >=70.",
            "STRONG_CONTINUATION": "Primary subtype STRONG_CONTINUATION.",
            "EXHAUSTION_RISK": "Primary subtype EXHAUSTION_RISK; negative-control cohort.",
        },
        "completedTradeRowsAvailable": int(len(complete)),
        "overall": overall,
        "bySplit": by_split,
        "descriptiveRankingByFullPeriodTotalReturn": ranked,
        "validation2025RankingByTotalReturn": validation_ranking,
        "calibrationPolicy": {
            "automaticProductionChanges": False,
            "note": "2026 results have already been inspected during V1.1/V1.2 research, so they are no longer a pristine untouched test. Treat 2024/2025/2026 as historical research evidence and use the forward Decision Ledger as the clean out-of-sample check.",
        },
        "limitations": [
            "Portfolio results inherit V1.2 daily-OHLC path ambiguity and historical-data biases.",
            "Fractional shares are used and exchange lot-size constraints are ignored.",
            "No liquidity impact or market impact beyond fixed slippage is modeled.",
            "Signals are mechanical EXTENDED research signals, not a historical reconstruction of final ChatGPT V3.3 decisions.",
            "Same-day exit proceeds are not reused for new opening trades, which is conservative for capital capacity.",
            "Selection priority under capacity constraints is deterministic but still a modeling assumption.",
        ],
    }
