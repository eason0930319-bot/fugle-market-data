from __future__ import annotations

import math
from dataclasses import asdict

import numpy as np
import pandas as pd

from backtest_execution import CostModel

VERSION = "ma5-timing-backtest-v1.5"
INITIAL_CAPITAL = 100_000.0
MAX_POSITIONS = 5
MIN_TRADE_VALUE = 50_000_000

VARIANTS = {
    "MA5_CROSS_ONLY": {"bias25_exit": False},
    "MA5_CROSS_OR_BIAS25": {"bias25_exit": True},
}
ENTRY_MODES = ("NEXT_OPEN", "NO_CHASE_3PCT")


def _num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def _round(x, n=4):
    v = _num(x)
    return None if v is None else round(v, n)


def _net_return(entry_raw, exit_raw, costs: CostModel):
    buy_cash = entry_raw * (1 + costs.slippage_each_side) * (1 + costs.buy_commission)
    sell_cash = exit_raw * (1 - costs.slippage_each_side) * (1 - costs.sell_commission - costs.sell_tax)
    return (sell_cash / buy_cash - 1) * 100


def _profit_factor(values):
    s = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    gains = float(s[s > 0].sum())
    losses = float(-s[s < 0].sum())
    return None if losses <= 0 else round(gains / losses, 4)


def _max_consecutive_losses(values):
    best = run = 0
    for v in values:
        if v < 0:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return int(best)


def _max_drawdown(values):
    peak = -np.inf
    worst = 0.0
    for e in values:
        if not math.isfinite(e) or e <= 0:
            continue
        peak = max(peak, e)
        worst = min(worst, e / peak - 1.0)
    return round(worst * 100, 4)


def _regime_map(data: pd.DataFrame):
    z = data[["date", "market", "symbol", "close"]].copy()
    z = z.sort_values(["market", "symbol", "date"])
    z["prev"] = z.groupby(["market", "symbol"], sort=False)["close"].shift(1)
    z["chg"] = (z.close / z.prev - 1) * 100
    out = {}
    for dt, g in z.dropna(subset=["chg"]).groupby("date"):
        if len(g) < 700:
            out[pd.Timestamp(dt)] = "UNKNOWN"
            continue
        ar = float((g.chg > 0.001).mean())
        med = float(g.chg.median())
        if ar >= 0.55 and med > 0:
            r = "BULL"
        elif ar <= 0.40 and med < 0:
            r = "BEAR"
        else:
            r = "NEUTRAL"
        out[pd.Timestamp(dt)] = r
    return out


def _prepare(data: pd.DataFrame):
    d = data.copy().sort_values(["market", "symbol", "date"])
    d["date"] = pd.to_datetime(d.date).dt.tz_localize(None)
    g = d.groupby(["market", "symbol"], sort=False)
    d["ma5"] = g["close"].transform(lambda s: s.rolling(5, min_periods=5).mean())
    d["ma5Prev"] = g["ma5"].shift(1)
    d["prevClose"] = g["close"].shift(1)
    d["prevMA5"] = g["ma5"].shift(1)
    d["ma5SlopePct"] = (d.ma5 / d.ma5Prev - 1) * 100
    d["bias5Pct"] = (d.close / d.ma5 - 1) * 100
    d["nextOpen"] = g["open"].shift(-1)
    d["nextDate"] = g["date"].shift(-1)
    d["entrySignal"] = (
        (d.ma5 > d.ma5Prev)
        & (d.close > d.ma5)
        & (d.prevClose <= d.prevMA5)
        & (pd.to_numeric(d.tradeValue, errors="coerce") >= MIN_TRADE_VALUE)
    )
    d["exitCrossSignal"] = (
        (d.ma5 < d.ma5Prev)
        & (d.close < d.ma5)
        & (d.prevClose >= d.prevMA5)
    )
    d["bias25Signal"] = d.bias5Pct > 25.0
    regimes = _regime_map(d)
    d["marketRegime"] = d.date.map(regimes).fillna("UNKNOWN")
    return d


def _build_trades(data: pd.DataFrame, bias25_exit: bool, entry_mode: str):
    trades = []
    for (market, symbol), g in data.groupby(["market", "symbol"], sort=False):
        g = g.sort_values("date").reset_index(drop=True)
        entries = list(g.index[g.entrySignal.fillna(False)])
        last_exit_idx = -1
        for sig_i in entries:
            if sig_i <= last_exit_idx:
                continue
            if sig_i + 1 >= len(g):
                continue
            sig = g.loc[sig_i]
            entry_i = sig_i + 1
            entry = g.loc[entry_i]
            entry_px = _num(entry.open)
            if entry_px is None or entry_px <= 0:
                continue
            if entry_mode == "NO_CHASE_3PCT" and entry_px > float(sig.close) * 1.03:
                continue

            exit_sig_i = None
            exit_reason = None
            for j in range(entry_i, len(g)):
                row = g.loc[j]
                if bias25_exit and bool(row.bias25Signal):
                    exit_sig_i = j
                    exit_reason = "BIAS25"
                    break
                if bool(row.exitCrossSignal):
                    exit_sig_i = j
                    exit_reason = "MA5_DOWN_CROSS"
                    break

            if exit_sig_i is not None and exit_sig_i + 1 < len(g):
                exit_i = exit_sig_i + 1
                exit_px = _num(g.loc[exit_i, "open"])
                exit_date = g.loc[exit_i, "date"]
                last_exit_idx = exit_i
            else:
                exit_i = len(g) - 1
                exit_px = _num(g.loc[exit_i, "close"])
                exit_date = g.loc[exit_i, "date"]
                exit_reason = "END_OF_DATA"
                last_exit_idx = exit_i

            if exit_px is None or exit_px <= 0:
                continue
            hold_slice = g.loc[entry_i:exit_i]
            mae = (hold_slice.low.min() / entry_px - 1) * 100 if len(hold_slice) else np.nan
            mfe = (hold_slice.high.max() / entry_px - 1) * 100 if len(hold_slice) else np.nan
            trades.append({
                "market": market,
                "symbol": str(symbol),
                "name": str(sig["name"]),
                "industry": str(sig["industry"]),
                "signalDate": pd.Timestamp(sig.date),
                "entryDate": pd.Timestamp(entry.date),
                "exitDate": pd.Timestamp(exit_date),
                "signalClose": float(sig.close),
                "entryPrice": float(entry_px),
                "exitPrice": float(exit_px),
                "entryMode": entry_mode,
                "exitReason": exit_reason,
                "marketRegime": str(sig.marketRegime),
                "ma5SlopePct": _round(sig.ma5SlopePct),
                "bias5PctAtSignal": _round(sig.bias5Pct),
                "tradeValue": _round(sig.tradeValue, 2),
                "holdingDays": int(max(1, exit_i - entry_i + 1)),
                "mfePct": _round(mfe),
                "maePct": _round(mae),
            })
    return pd.DataFrame(trades)


def _trade_summary(trades: pd.DataFrame, costs: CostModel):
    if trades.empty:
        return {"tradeCount": 0}
    t = trades.copy()
    t["netReturnPct"] = [
        _net_return(e, x, costs) for e, x in zip(t.entryPrice, t.exitPrice)
    ]
    r = t.netReturnPct.astype(float)
    wins = r[r > 0]
    losses = r[r < 0]
    avg_win = float(wins.mean()) if len(wins) else None
    avg_loss = float(losses.mean()) if len(losses) else None
    payoff = avg_win / abs(avg_loss) if avg_win is not None and avg_loss is not None and avg_loss != 0 else None
    best = t.loc[r.idxmax()]
    worst = t.loc[r.idxmin()]
    return {
        "tradeCount": int(len(t)),
        "winRate": round(float((r > 0).mean()), 4),
        "avgTradeNetReturnPct": _round(r.mean()),
        "avgWinPct": _round(avg_win),
        "avgLossPct": _round(avg_loss),
        "payoffRatio": _round(payoff),
        "expectancyPct": _round(r.mean()),
        "profitFactor": _profit_factor(r),
        "maxTradeGainPct": _round(r.max()),
        "maxTradeLossPct": _round(r.min()),
        "avgHoldingDays": _round(t.holdingDays.mean(), 2),
        "medianHoldingDays": _round(t.holdingDays.median(), 2),
        "avgMfePct": _round(t.mfePct.mean()),
        "avgMaePct": _round(t.maePct.mean()),
        "worstInTradeMaePct": _round(t.maePct.min()),
        "maxConsecutiveLosses": _max_consecutive_losses(r.tolist()),
        "exitReasonCounts": {str(k): int(v) for k, v in t.exitReason.value_counts().to_dict().items()},
        "bestTrade": {
            "symbol": str(best.symbol), "signalDate": best.signalDate.date().isoformat(),
            "entryDate": best.entryDate.date().isoformat(), "exitDate": best.exitDate.date().isoformat(),
            "netReturnPct": _round(r.loc[best.name]), "exitReason": str(best.exitReason),
        },
        "worstTrade": {
            "symbol": str(worst.symbol), "signalDate": worst.signalDate.date().isoformat(),
            "entryDate": worst.entryDate.date().isoformat(), "exitDate": worst.exitDate.date().isoformat(),
            "netReturnPct": _round(r.loc[worst.name]), "exitReason": str(worst.exitReason),
        },
    }


def _portfolio_summary(trades: pd.DataFrame, data: pd.DataFrame, costs: CostModel):
    if trades.empty:
        return {"initialCapital": INITIAL_CAPITAL, "endingCapital": INITIAL_CAPITAL, "totalReturnPct": 0.0, "tradeCount": 0}
    t = trades.copy()
    t["netReturnPct"] = [_net_return(e, x, costs) for e, x in zip(t.entryPrice, t.exitPrice)]
    t = t.sort_values(["entryDate", "ma5SlopePct", "tradeValue"], ascending=[True, False, False])
    close_map = data.drop_duplicates(["date", "market", "symbol"], keep="last").set_index(["date", "market", "symbol"])["close"]
    sessions = sorted(pd.to_datetime(data.date).dt.tz_localize(None).drop_duplicates())
    by_entry = {pd.Timestamp(k): v for k, v in t.groupby("entryDate")}
    buy_factor = (1 + costs.slippage_each_side) * (1 + costs.buy_commission)
    sell_factor = (1 - costs.slippage_each_side) * (1 - costs.sell_commission - costs.sell_tax)
    cash = INITIAL_CAPITAL
    positions = []
    selected = []
    equity_curve = []
    skipped_capacity = 0

    for dt in sessions:
        dt = pd.Timestamp(dt)
        # Existing positions marked at close for sizing context.
        pre_equity = cash
        for p in positions:
            cp = close_map.get((dt, p["market"], p["symbol"]), p["entryPrice"])
            pre_equity += p["qty"] * float(cp) * sell_factor

        todays = by_entry.get(dt)
        if todays is not None:
            for idx, row in todays.iterrows():
                if len(positions) >= MAX_POSITIONS:
                    skipped_capacity += 1
                    continue
                if any(p["market"] == row.market and p["symbol"] == row.symbol for p in positions):
                    continue
                allocation = min(cash, pre_equity / MAX_POSITIONS)
                if allocation <= 100:
                    continue
                qty = allocation / (float(row.entryPrice) * buy_factor)
                cash -= allocation
                positions.append({
                    "idx": idx, "market": row.market, "symbol": row.symbol,
                    "entryPrice": float(row.entryPrice), "exitPrice": float(row.exitPrice),
                    "exitDate": pd.Timestamp(row.exitDate), "qty": qty,
                })
                selected.append(idx)

        remaining = []
        for p in positions:
            if p["exitDate"] == dt:
                cash += p["qty"] * p["exitPrice"] * sell_factor
            else:
                remaining.append(p)
        positions = remaining

        equity = cash
        for p in positions:
            cp = close_map.get((dt, p["market"], p["symbol"]), p["entryPrice"])
            equity += p["qty"] * float(cp) * sell_factor
        equity_curve.append(float(equity))

    for p in positions:
        cash += p["qty"] * p["exitPrice"] * sell_factor
    if equity_curve:
        equity_curve[-1] = float(cash)

    sel = t.loc[sorted(set(selected))].copy() if selected else t.iloc[0:0]
    metrics = _trade_summary(sel, costs)
    return {
        "initialCapital": INITIAL_CAPITAL,
        "endingCapital": round(float(cash), 2),
        "totalReturnPct": _round((cash / INITIAL_CAPITAL - 1) * 100),
        "maxDrawdownPct": _max_drawdown(equity_curve),
        "selectedTradeCount": int(len(sel)),
        "candidateTradeCount": int(len(t)),
        "skippedCapacity": int(skipped_capacity),
        "selectionPriority": "Higher MA5 slope first, then higher signal-day trade value.",
        **metrics,
    }


def build_ma5_summary(data: pd.DataFrame, generated_at: str, costs: CostModel | None = None):
    costs = costs or CostModel()
    d = _prepare(data)
    results = {}
    for variant, cfg in VARIANTS.items():
        results[variant] = {}
        for entry_mode in ENTRY_MODES:
            trades = _build_trades(d, cfg["bias25_exit"], entry_mode)
            trades["entryYear"] = pd.to_datetime(trades.entryDate).dt.year if not trades.empty else pd.Series(dtype=int)
            scopes = {
                "FULL": trades,
                "TRAIN_2024": trades[trades.entryYear == 2024] if not trades.empty else trades,
                "VALIDATION_2025": trades[trades.entryYear == 2025] if not trades.empty else trades,
                "TEST_2026_DESCRIPTIVE": trades[trades.entryYear >= 2026] if not trades.empty else trades,
                "BULL_ONLY_FULL": trades[trades.marketRegime == "BULL"] if not trades.empty else trades,
            }
            results[variant][entry_mode] = {}
            for scope, g in scopes.items():
                results[variant][entry_mode][scope] = {
                    "tradeDistribution": _trade_summary(g, costs),
                    "portfolio": _portfolio_summary(g, d, costs),
                }

    return {
        "ok": True,
        "schemaVersion": 1,
        "version": VERSION,
        "generatedAt": generated_at,
        "ruleDefinition": {
            "entrySignal": "At close: MA5 rising, prior close <= prior MA5, current close > current MA5; signal-day trade value >= NT$50m. Execute next session open.",
            "exitCross": "At close: MA5 falling, prior close >= prior MA5, current close < current MA5. Execute next session open.",
            "bias25Exit": "Optional overlay: positive 5-day MA deviation (close/MA5 - 1) > 25%; execute next session open.",
            "entryModes": {
                "NEXT_OPEN": "Always enter next open after a valid signal.",
                "NO_CHASE_3PCT": "Skip if next open is more than 3% above signal-day close.",
            },
        },
        "costModel": asdict(costs),
        "portfolioPolicy": {
            "initialCapital": INITIAL_CAPITAL,
            "maxConcurrentPositions": MAX_POSITIONS,
            "targetSlotPct": 20.0,
            "noLeverage": True,
            "sameDayPriority": "Higher MA5 slope, then higher signal-day trade value.",
        },
        "results": results,
        "promotionPolicy": {
            "automaticProductionChanges": False,
            "note": "This is a separate timing-rule experiment. Do not change V3.3/V3.4 production logic unless 2025 validation and forward Decision Ledger support it.",
        },
        "limitations": [
            "Uses daily OHLC; intraday crossing time is unknown, so signals are confirmed at the close and executed next open to avoid look-ahead.",
            "Uses current surviving security universe/Yahoo historical data and inherits survivorship/data-source bias.",
            "No hard stop is imposed beyond the requested MA5/bias exit rules; this intentionally exposes their true tail risk.",
            "Portfolio selection among simultaneous signals is a modeling assumption and is reported explicitly.",
        ],
    }
