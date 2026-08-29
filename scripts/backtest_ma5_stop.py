from __future__ import annotations

import math
import numpy as np
import pandas as pd

from backtest_ma5 import (
    CostModel,
    _num,
    _round,
    _portfolio_summary,
    _prepare,
    _trade_summary,
)

VERSION = "ma5-risk-stop-backtest-v1.6"

STOP_VARIANTS = {
    "MA5_ONLY": {"kind": "NONE"},
    "FIXED_STOP_5PCT": {"kind": "FIXED", "pct": 0.05},
    "FIXED_STOP_7PCT": {"kind": "FIXED", "pct": 0.07},
    "ATR_STRUCT_STOP": {"kind": "ATR_STRUCT"},
}


def _stop_level(signal, entry_price: float, cfg: dict):
    kind = cfg["kind"]
    if kind == "NONE":
        return None
    if kind == "FIXED":
        return entry_price * (1.0 - float(cfg["pct"]))
    if kind == "ATR_STRUCT":
        atr = _num(signal.atr14)
        low10 = _num(signal.l10)
        if atr is None or atr <= 0:
            return None
        levels = [entry_price - 1.5 * atr]
        if low10 is not None:
            structural = low10 - 0.25 * atr
            if 0 < structural < entry_price:
                levels.append(structural)
        stop = max(levels)
        return stop if 0 < stop < entry_price else None
    raise ValueError(kind)


def _post_stop_recovery(g: pd.DataFrame, exit_i: int, exit_price: float, entry_price: float):
    out = {}
    for h in (5, 10, 20):
        fut = g.iloc[exit_i + 1: exit_i + 1 + h]
        if fut.empty:
            out[f"reboundMax{h}dPct"] = None
            out[f"close{h}dPct"] = None
            out[f"reclaimEntry{h}d"] = None
            continue
        mx = _num(fut.high.max())
        out[f"reboundMax{h}dPct"] = _round((mx / exit_price - 1) * 100) if mx else None
        if len(fut) >= h:
            c = _num(fut.iloc[h - 1].close)
            out[f"close{h}dPct"] = _round((c / exit_price - 1) * 100) if c else None
        else:
            out[f"close{h}dPct"] = None
        out[f"reclaimEntry{h}d"] = bool(mx is not None and mx >= entry_price)
    return out


def _build_trades(data: pd.DataFrame, cfg: dict):
    trades = []
    missing_stop = 0
    for (market, symbol), g in data.groupby(["market", "symbol"], sort=False):
        g = g.sort_values("date").reset_index(drop=True)
        entries = list(g.index[g.entrySignal.fillna(False)])
        last_exit_i = -1
        for sig_i in entries:
            if sig_i <= last_exit_i or sig_i + 1 >= len(g):
                continue
            sig = g.loc[sig_i]
            entry_i = sig_i + 1
            entry = g.loc[entry_i]
            entry_px = _num(entry.open)
            if entry_px is None or entry_px <= 0:
                continue

            stop = _stop_level(sig, entry_px, cfg)
            if cfg["kind"] != "NONE" and stop is None:
                missing_stop += 1

            exit_i = None
            exit_px = None
            exit_reason = None
            for j in range(entry_i, len(g)):
                row = g.loc[j]
                if stop is not None:
                    o, lo = _num(row.open), _num(row.low)
                    if o is not None and o <= stop:
                        exit_i, exit_px, exit_reason = j, o, "STOP_GAP"
                        break
                    if lo is not None and lo <= stop:
                        exit_i, exit_px, exit_reason = j, stop, "STOP_INTRADAY"
                        break
                if bool(row.exitCrossSignal):
                    if j + 1 < len(g):
                        nxt = _num(g.loc[j + 1, "open"])
                        if nxt is not None and nxt > 0:
                            exit_i, exit_px, exit_reason = j + 1, nxt, "MA5_DOWN_CROSS"
                            break

            if exit_i is None:
                exit_i = len(g) - 1
                exit_px = _num(g.loc[exit_i, "close"])
                exit_reason = "END_OF_DATA"
            if exit_px is None or exit_px <= 0:
                continue

            last_exit_i = exit_i
            hold = g.loc[entry_i:exit_i]
            mae = (hold.low.min() / entry_px - 1) * 100 if len(hold) else np.nan
            mfe = (hold.high.max() / entry_px - 1) * 100 if len(hold) else np.nan
            rec = {}
            if exit_reason.startswith("STOP_"):
                rec = _post_stop_recovery(g, exit_i, exit_px, entry_px)

            trades.append({
                "market": market,
                "symbol": str(symbol),
                "name": str(sig["name"]),
                "industry": str(sig["industry"]),
                "signalDate": pd.Timestamp(sig.date),
                "entryDate": pd.Timestamp(entry.date),
                "exitDate": pd.Timestamp(g.loc[exit_i, "date"]),
                "signalClose": float(sig.close),
                "entryPrice": float(entry_px),
                "exitPrice": float(exit_px),
                "exitReason": exit_reason,
                "marketRegime": str(sig.marketRegime),
                "ma5SlopePct": _round(sig.ma5SlopePct),
                "tradeValue": _round(sig.tradeValue, 2),
                "stopLevel": _round(stop),
                "stopDistancePct": _round((stop / entry_px - 1) * 100) if stop else None,
                "holdingDays": int(max(1, exit_i - entry_i + 1)),
                "mfePct": _round(mfe),
                "maePct": _round(mae),
                **rec,
            })
    return pd.DataFrame(trades), int(missing_stop)


def _recovery_summary(trades: pd.DataFrame):
    if trades.empty:
        return {"stopTradeCount": 0}
    s = trades[trades.exitReason.astype(str).str.startswith("STOP_")].copy()
    if s.empty:
        return {"stopTradeCount": 0}
    out = {"stopTradeCount": int(len(s))}
    for h in (5, 10, 20):
        r = pd.to_numeric(s.get(f"reboundMax{h}dPct"), errors="coerce").dropna()
        c = pd.to_numeric(s.get(f"close{h}dPct"), errors="coerce").dropna()
        reclaim = s.get(f"reclaimEntry{h}d")
        out[f"avgMaxRebound{h}dPct"] = _round(r.mean()) if len(r) else None
        out[f"medianMaxRebound{h}dPct"] = _round(r.median()) if len(r) else None
        out[f"avgCloseReturn{h}dPct"] = _round(c.mean()) if len(c) else None
        if reclaim is not None:
            rr = pd.Series(reclaim).dropna()
            out[f"reclaimEntryRate{h}d"] = _round(rr.astype(bool).mean(), 4) if len(rr) else None
        out[f"rebound10PctRate{h}d"] = _round((r >= 10).mean(), 4) if len(r) else None
    return out


def _scope_payload(g: pd.DataFrame, data: pd.DataFrame, costs: CostModel):
    return {
        "tradeDistribution": _trade_summary(g, costs),
        "portfolio": _portfolio_summary(g, data, costs),
        "stopRecovery": _recovery_summary(g),
    }


def build_ma5_stop_summary(data: pd.DataFrame, generated_at: str, costs: CostModel | None = None):
    costs = costs or CostModel()
    d = _prepare(data)
    results = {}
    validation = []

    for name, cfg in STOP_VARIANTS.items():
        trades, missing_stop = _build_trades(d, cfg)
        if not trades.empty:
            trades["entryYear"] = pd.to_datetime(trades.entryDate).dt.year
        else:
            trades["entryYear"] = pd.Series(dtype=int)
        scopes = {
            "FULL": trades,
            "TRAIN_2024": trades[trades.entryYear == 2024],
            "VALIDATION_2025": trades[trades.entryYear == 2025],
            "TEST_2026_DESCRIPTIVE": trades[trades.entryYear >= 2026],
            "BULL_ONLY_FULL": trades[trades.marketRegime == "BULL"],
        }
        results[name] = {
            "definition": cfg,
            "missingStopLevelCount": missing_stop,
            "scopes": {k: _scope_payload(v, d, costs) for k, v in scopes.items()},
        }
        p = results[name]["scopes"]["VALIDATION_2025"]["portfolio"]
        validation.append({
            "variant": name,
            "totalReturnPct": p.get("totalReturnPct"),
            "maxDrawdownPct": p.get("maxDrawdownPct"),
            "expectancyPct": p.get("expectancyPct"),
            "profitFactor": p.get("profitFactor"),
            "payoffRatio": p.get("payoffRatio"),
            "avgWinPct": p.get("avgWinPct"),
            "avgLossPct": p.get("avgLossPct"),
            "maxTradeGainPct": p.get("maxTradeGainPct"),
            "maxTradeLossPct": p.get("maxTradeLossPct"),
            "tradeCount": p.get("tradeCount"),
        })

    validation.sort(key=lambda x: (-999999 if x["totalReturnPct"] is None else x["totalReturnPct"]), reverse=True)
    return {
        "ok": True,
        "schemaVersion": 1,
        "version": VERSION,
        "generatedAt": generated_at,
        "purpose": "Test whether a fixed or volatility/structure-aware disaster stop improves the MA5 trend-following rule without destroying its large-winner payoff profile.",
        "baseRule": {
            "entry": "MA5 rising and close crosses above MA5 at close; enter next session open.",
            "normalExit": "MA5 falling and close crosses below MA5 at close; exit next session open.",
            "selectionPriority": "Higher MA5 slope, then higher signal-day trade value; max 5 concurrent positions.",
        },
        "stopRules": {
            "MA5_ONLY": "No additional hard stop.",
            "FIXED_STOP_5PCT": "5% below raw entry price; gap below stop exits at the open, otherwise intraday touch exits at stop.",
            "FIXED_STOP_7PCT": "7% below raw entry price; gap below stop exits at the open, otherwise intraday touch exits at stop.",
            "ATR_STRUCT_STOP": "max(entry - 1.5*ATR14, prior10Low - 0.25*ATR14) when valid; static initial stop.",
        },
        "recoveryAudit": "For stop exits, measure post-exit maximum rebound and close return over 5/10/20 sessions plus entry-price reclaim rate to detect stops that are too tight.",
        "costModel": {
            "buy_commission": costs.buy_commission,
            "sell_commission": costs.sell_commission,
            "sell_tax": costs.sell_tax,
            "slippage_each_side": costs.slippage_each_side,
        },
        "results": results,
        "validation2025Ranking": validation,
        "promotionPolicy": {
            "automaticProductionChanges": False,
            "minimum": "Prefer positive 2025 total return and expectancy, profit factor >1, materially lower drawdown and worst loss than MA5_ONLY, while retaining a strong payoff ratio and without excessive post-stop recoveries.",
        },
        "limitations": [
            "Daily OHLC cannot determine intraday path when multiple price levels are touched; hard-stop fills use conservative gap/open logic and stop-touch assumptions.",
            "ATR/structure stop is static from entry and is not a trailing stop.",
            "Current-listed security master and Yahoo history retain the prior survivorship/data-source limitations.",
            "2026 is descriptive because it has already been examined in earlier research; forward Decision Ledger remains the cleanest live validation.",
        ],
    }
