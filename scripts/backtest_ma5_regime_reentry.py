from __future__ import annotations

import math
import numpy as np
import pandas as pd

from backtest_ma5 import CostModel, _net_return, _num, _portfolio_summary, _round, _trade_summary
from backtest_ma5_robustness import _prepare_robust
from backtest_ma5_stop import _post_stop_recovery, _stop_level

VERSION = "ma5-regime-reentry-v1.6.2"

STOP_CONFIGS = {
    "ROBUST_1P25_L10_OFF000": {
        "kind": "ATR_STRUCT",
        "atr_mult": 1.25,
        "use_structure": True,
        "struct_col": "l10",
        "struct_atr_offset": 0.0,
    },
    "ORIGINAL_1P5_L10_OFF025": {
        "kind": "ATR_STRUCT",
        "atr_mult": 1.5,
        "use_structure": True,
        "struct_col": "l10",
        "struct_atr_offset": 0.25,
    },
}

REGIME_FILTERS = {
    "ALL_REGIMES": None,
    "NO_BEAR": {"BULL", "NEUTRAL"},
    "BULL_ONLY": {"BULL"},
}

COOLDOWNS = (0, 5, 10, 20)


def _n(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def _build_filtered_trades(
    data: pd.DataFrame,
    stop_cfg: dict,
    allowed_regimes: set[str] | None,
    cooldown_after_stop_sessions: int,
):
    trades = []
    missing_stop = 0
    skipped_regime = 0
    skipped_cooldown = 0

    for (market, symbol), g in data.groupby(["market", "symbol"], sort=False):
        g = g.sort_values("date").reset_index(drop=True)
        entries = list(g.index[g.entrySignal.fillna(False)])
        last_exit_i = -1
        cooldown_until_i = -1

        for sig_i in entries:
            if sig_i <= last_exit_i or sig_i + 1 >= len(g):
                continue
            if sig_i <= cooldown_until_i:
                skipped_cooldown += 1
                continue

            sig = g.loc[sig_i]
            regime = str(sig.marketRegime)
            if allowed_regimes is not None and regime not in allowed_regimes:
                skipped_regime += 1
                continue

            entry_i = sig_i + 1
            entry = g.loc[entry_i]
            entry_px = _num(entry.open)
            if entry_px is None or entry_px <= 0:
                continue

            stop = _stop_level(sig, entry_px, stop_cfg)
            if stop_cfg["kind"] != "NONE" and stop is None:
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
            if str(exit_reason).startswith("STOP_") and cooldown_after_stop_sessions > 0:
                cooldown_until_i = exit_i + int(cooldown_after_stop_sessions)

            hold = g.loc[entry_i:exit_i]
            mae = (hold.low.min() / entry_px - 1) * 100 if len(hold) else np.nan
            mfe = (hold.high.max() / entry_px - 1) * 100 if len(hold) else np.nan
            rec = {}
            if str(exit_reason).startswith("STOP_"):
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
                "marketRegime": regime,
                "ma5SlopePct": _round(sig.ma5SlopePct),
                "tradeValue": _round(sig.tradeValue, 2),
                "stopLevel": _round(stop),
                "stopDistancePct": _round((stop / entry_px - 1) * 100) if stop else None,
                "holdingDays": int(max(1, exit_i - entry_i + 1)),
                "mfePct": _round(mfe),
                "maePct": _round(mae),
                **rec,
            })

    return pd.DataFrame(trades), {
        "missingStopLevelCount": int(missing_stop),
        "skippedRegimeSignals": int(skipped_regime),
        "skippedCooldownSignals": int(skipped_cooldown),
    }


def _portfolio_row(trades: pd.DataFrame, data: pd.DataFrame, costs: CostModel):
    p = _portfolio_summary(trades, data, costs)
    return {
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
        "candidateTradeCount": p.get("candidateTradeCount"),
        "skippedCapacity": p.get("skippedCapacity"),
    }


def _basic_pass(row: dict):
    r = _n(row.get("totalReturnPct"))
    e = _n(row.get("expectancyPct"))
    pf = _n(row.get("profitFactor"))
    return bool(r is not None and e is not None and pf is not None and r > 0 and e > 0 and pf > 1)


def _regime_diagnostic(trades: pd.DataFrame, costs: CostModel):
    if trades.empty:
        return {}
    out = {}
    for regime, g in trades.groupby("marketRegime"):
        net = pd.Series([
            _net_return(e, x, costs) for e, x in zip(g.entryPrice, g.exitPrice)
        ], index=g.index, dtype=float)
        stop_mask = g.exitReason.astype(str).str.startswith("STOP_")
        out[str(regime)] = {
            "tradeCount": int(len(g)),
            "stopRate": _round(stop_mask.mean(), 4),
            "avgNetReturnPct": _round(net.mean()),
            "winRate": _round((net > 0).mean(), 4),
            "profitFactor": _trade_summary(g, costs).get("profitFactor"),
            "avgMaePct": _round(pd.to_numeric(g.maePct, errors="coerce").mean()),
        }
    return out


def _reentry_diagnostic(trades: pd.DataFrame, sessions: list[pd.Timestamp], costs: CostModel):
    if trades.empty:
        return {"stopTradeCount": 0}
    t = trades.copy().sort_values(["market", "symbol", "entryDate"])
    t["netReturnPct"] = [
        _net_return(e, x, costs) for e, x in zip(t.entryPrice, t.exitPrice)
    ]
    session_idx = {pd.Timestamp(dt): i for i, dt in enumerate(sessions)}
    rows = []
    max_chain = 0

    for (_, _), g in t.groupby(["market", "symbol"], sort=False):
        g = g.sort_values("entryDate").reset_index(drop=True)
        chain = 0
        for i, row in g.iterrows():
            if not str(row.exitReason).startswith("STOP_"):
                chain = 0
                continue
            chain += 1
            max_chain = max(max_chain, chain)
            if i + 1 >= len(g):
                rows.append({"hasNext": False})
                continue
            nxt = g.loc[i + 1]
            a = session_idx.get(pd.Timestamp(row.exitDate))
            b = session_idx.get(pd.Timestamp(nxt.entryDate))
            gap = None if a is None or b is None else max(0, b - a)
            next_stop = str(nxt.exitReason).startswith("STOP_")
            if gap is None or gap > 20:
                chain = 0
            rows.append({
                "hasNext": True,
                "gapSessions": gap,
                "nextTradeNetReturnPct": float(nxt.netReturnPct),
                "nextTradeStop": bool(next_stop),
            })

    s = pd.DataFrame(rows)
    stops = int(len(s))
    out = {"stopTradeCount": stops, "maxConsecutiveStopChain": int(max_chain)}
    if stops == 0:
        return out
    for h in (5, 10, 20):
        q = s[(s.hasNext == True) & pd.to_numeric(s.get("gapSessions"), errors="coerce").le(h)].copy()
        out[f"reentryWithin{h}dCount"] = int(len(q))
        out[f"reentryWithin{h}dRate"] = _round(len(q) / stops, 4)
        if len(q):
            r = pd.to_numeric(q.nextTradeNetReturnPct, errors="coerce").dropna()
            out[f"reentryWithin{h}dNextTradeAvgNetPct"] = _round(r.mean()) if len(r) else None
            out[f"reentryWithin{h}dNextTradeWinRate"] = _round((r > 0).mean(), 4) if len(r) else None
            out[f"reentryWithin{h}dNextTradeStopRate"] = _round(q.nextTradeStop.astype(bool).mean(), 4)
        else:
            out[f"reentryWithin{h}dNextTradeAvgNetPct"] = None
            out[f"reentryWithin{h}dNextTradeWinRate"] = None
            out[f"reentryWithin{h}dNextTradeStopRate"] = None

    stop_counts = t[t.exitReason.astype(str).str.startswith("STOP_")].groupby(["market", "symbol"]).size()
    out["symbolsWith2PlusStops"] = int((stop_counts >= 2).sum())
    out["symbolsWith3PlusStops"] = int((stop_counts >= 3).sum())
    out["maxStopsSingleSymbol"] = int(stop_counts.max()) if len(stop_counts) else 0
    return out


def build_ma5_regime_reentry_summary(data: pd.DataFrame, generated_at: str, costs: CostModel | None = None):
    costs = costs or CostModel()
    d = _prepare_robust(data)
    sessions = sorted(pd.to_datetime(d.date).dt.tz_localize(None).drop_duplicates())

    variants = []
    diagnostics = {}

    for stop_name, stop_cfg in STOP_CONFIGS.items():
        base_trades, _ = _build_filtered_trades(d, stop_cfg, None, 0)
        base_trades["entryYear"] = pd.to_datetime(base_trades.entryDate).dt.year if not base_trades.empty else pd.Series(dtype=int)
        diagnostics[stop_name] = {
            "byEntryRegime2024": _regime_diagnostic(base_trades[base_trades.entryYear == 2024], costs),
            "byEntryRegime2025": _regime_diagnostic(base_trades[base_trades.entryYear == 2025], costs),
            "reentry2024": _reentry_diagnostic(base_trades[base_trades.entryYear == 2024], sessions, costs),
            "reentry2025": _reentry_diagnostic(base_trades[base_trades.entryYear == 2025], sessions, costs),
        }

        for regime_name, allowed in REGIME_FILTERS.items():
            for cooldown in COOLDOWNS:
                trades, audit = _build_filtered_trades(d, stop_cfg, allowed, cooldown)
                trades["entryYear"] = pd.to_datetime(trades.entryDate).dt.year if not trades.empty else pd.Series(dtype=int)
                rows = {
                    "TRAIN_2024": _portfolio_row(trades[trades.entryYear == 2024], d, costs),
                    "VALIDATION_2025": _portfolio_row(trades[trades.entryYear == 2025], d, costs),
                    "TEST_2026_DESCRIPTIVE": _portfolio_row(trades[trades.entryYear >= 2026], d, costs),
                }
                r24 = _n(rows["TRAIN_2024"].get("totalReturnPct"))
                r25 = _n(rows["VALIDATION_2025"].get("totalReturnPct"))
                worst = min(r24, r25) if r24 is not None and r25 is not None else None
                variants.append({
                    "variant": f"{stop_name}__{regime_name}__CD{cooldown}",
                    "stopConfig": stop_name,
                    "regimeFilter": regime_name,
                    "cooldownSessions": cooldown,
                    "audit": audit,
                    "results": rows,
                    "train2024Pass": _basic_pass(rows["TRAIN_2024"]),
                    "validation2025Pass": _basic_pass(rows["VALIDATION_2025"]),
                    "bothYearsBasicPass": bool(_basic_pass(rows["TRAIN_2024"]) and _basic_pass(rows["VALIDATION_2025"])),
                    "worstTrainValidationReturnPct": None if worst is None else round(worst, 4),
                })

    ranked = sorted(
        variants,
        key=lambda x: -999999 if x["worstTrainValidationReturnPct"] is None else x["worstTrainValidationReturnPct"],
        reverse=True,
    )

    both_pass = [x for x in variants if x["bothYearsBasicPass"]]
    return {
        "ok": True,
        "schemaVersion": 1,
        "version": VERSION,
        "generatedAt": generated_at,
        "purpose": "Test whether V1.6/V1.6.1 weakness in 2024 is driven by repeated stop-and-reentry whipsaws and/or unfavorable market regimes.",
        "researchPolicy": {
            "train": "2024",
            "validation": "2025",
            "test2026": "Descriptive only; already examined in earlier research.",
            "automaticProductionChanges": False,
            "pointInTimeRule": "Signal-day market regime is known at the close; any permitted entry occurs next session open.",
            "parameterPolicy": "Use only the pre-declared V1.6 original stop and V1.6.1 best worst-year candidate; do not retune ATR in this step.",
        },
        "stopConfigs": STOP_CONFIGS,
        "regimeFilters": {
            "ALL_REGIMES": "Allow all signal-day regimes.",
            "NO_BEAR": "Allow BULL and NEUTRAL only; reject BEAR/UNKNOWN new entries.",
            "BULL_ONLY": "Allow only BULL signal-day new entries.",
        },
        "cooldownSessions": list(COOLDOWNS),
        "variantCount": len(variants),
        "bothYearsBasicPassCount": len(both_pass),
        "rankingRule": "Rank by min(2024 total return, 2025 total return), not by best single-year return.",
        "topByWorstTrainValidationReturn": ranked[:12],
        "diagnostics": diagnostics,
        "variants": variants,
        "limitations": [
            "Regime is a broad market breadth state, not a sector-specific regime.",
            "Cooldown is measured in that security's row/session index after a stop and can sacrifice valid fast recoveries.",
            "Daily OHLC cannot reconstruct exact intraday stop path.",
            "Current-listed security master and Yahoo history retain survivorship/data-source limitations.",
            "2026 remains descriptive; forward Decision Ledger is the cleanest production validation.",
        ],
    }
