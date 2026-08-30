from __future__ import annotations

import math
import pandas as pd

from backtest_ma5 import CostModel, _portfolio_summary, _round
from backtest_ma5_robustness import _prepare_robust
from backtest_ma5_stop import _stop_level

VERSION = "daily-execution-entry-backtest-v1.7a"

ENTRY_MODES = (
    "NEXT_OPEN",
    "NO_CHASE_3PCT",
    "PULLBACK_1PCT",
    "BREAKOUT_0_5PCT",
)

# Freeze the risk architecture at the best reproducible V1.6.3 research candidate.
# This step changes entry execution only; it does not re-tune stop/regime/cooldown.
STOP_CONFIG = {
    "kind": "ATR_STRUCT",
    "atr_mult": 1.25,
    "use_structure": True,
    "struct_col": "l10",
    "struct_atr_offset": 0.0,
}
ALLOWED_REGIMES = {"BULL", "NEUTRAL"}
COOLDOWN_AFTER_STOP = 5


def _num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def _entry_fill(g: pd.DataFrame, sig_i: int, mode: str):
    if sig_i + 1 >= len(g):
        return None, None, None, "MISSING_NEXT_DAY"

    sig = g.loc[sig_i]
    nxt = g.loc[sig_i + 1]
    close = _num(sig.close)
    high = _num(sig.high)
    o = _num(nxt.open)
    h = _num(nxt.high)
    lo = _num(nxt.low)
    if close is None or o is None or h is None or lo is None:
        return None, None, None, "MISSING_NEXT_DAY"

    if mode == "NEXT_OPEN":
        return sig_i + 1, o, "OPEN", "ENTERED"

    if mode == "NO_CHASE_3PCT":
        if o > close * 1.03:
            return None, None, None, "SKIP_CHASE"
        return sig_i + 1, o, "OPEN", "ENTERED"

    if mode == "PULLBACK_1PCT":
        limit_px = close * 0.99
        if o <= limit_px:
            return sig_i + 1, o, "OPEN_BELOW_LIMIT", "ENTERED"
        if lo <= limit_px <= h:
            return sig_i + 1, limit_px, "INTRADAY_LIMIT", "ENTERED"
        return None, None, None, "NO_PULLBACK_FILL"

    if mode == "BREAKOUT_0_5PCT":
        if high is None:
            return None, None, None, "MISSING_SIGNAL_HIGH"
        trigger = high * 1.005
        if o >= trigger:
            return sig_i + 1, o, "GAP_THROUGH_TRIGGER", "ENTERED"
        if h >= trigger:
            return sig_i + 1, trigger, "INTRADAY_STOP_BUY", "ENTERED"
        return None, None, None, "NO_BREAKOUT_FILL"

    raise ValueError(f"unknown entry mode {mode}")


def _build_trades(data: pd.DataFrame, entry_mode: str):
    trades = []
    audit = {
        "entryMode": entry_mode,
        "entrySignalsSeen": 0,
        "skippedRegimeSignals": 0,
        "skippedCooldownSignals": 0,
        "skippedWhileInTradeSignals": 0,
        "noFillSignals": 0,
        "missingStopLevelCount": 0,
        "enteredTrades": 0,
        "sameDayIntradayEntryStopCount": 0,
    }

    for (market, symbol), g in data.groupby(["market", "symbol"], sort=False):
        g = g.sort_values("date").reset_index(drop=True)
        entries = list(g.index[g.entrySignal.fillna(False)])
        last_exit_i = -1
        cooldown_until_i = -1

        for sig_i in entries:
            audit["entrySignalsSeen"] += 1
            if sig_i <= last_exit_i:
                audit["skippedWhileInTradeSignals"] += 1
                continue
            if sig_i <= cooldown_until_i:
                audit["skippedCooldownSignals"] += 1
                continue

            sig = g.loc[sig_i]
            regime = str(sig.marketRegime)
            if regime not in ALLOWED_REGIMES:
                audit["skippedRegimeSignals"] += 1
                continue

            entry_i, entry_px, entry_detail, status = _entry_fill(g, sig_i, entry_mode)
            if status != "ENTERED" or entry_i is None or entry_px is None or entry_px <= 0:
                audit["noFillSignals"] += 1
                continue

            stop = _stop_level(sig, entry_px, STOP_CONFIG)
            if stop is None:
                audit["missingStopLevelCount"] += 1

            exit_i = None
            exit_px = None
            exit_reason = None
            intraday_entry = entry_detail in {"INTRADAY_LIMIT", "INTRADAY_STOP_BUY"}

            for j in range(entry_i, len(g)):
                row = g.loc[j]
                if stop is not None:
                    o = _num(row.open)
                    lo = _num(row.low)
                    # On the entry day, an intraday fill happens after the open. Therefore
                    # an opening gap below the stop cannot stop a position that did not yet
                    # exist. Daily OHLC still cannot order the later fill vs low, so if the
                    # day's low reaches the stop we conservatively assume the stop happened
                    # after the intraday entry.
                    if not (j == entry_i and intraday_entry):
                        if o is not None and o <= stop:
                            exit_i, exit_px, exit_reason = j, o, "STOP_GAP"
                            break
                    if lo is not None and lo <= stop:
                        exit_i, exit_px, exit_reason = j, stop, "STOP_INTRADAY"
                        if j == entry_i and intraday_entry:
                            audit["sameDayIntradayEntryStopCount"] += 1
                        break

                if bool(row.exitCrossSignal) and j + 1 < len(g):
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
            if str(exit_reason).startswith("STOP_"):
                cooldown_until_i = exit_i + COOLDOWN_AFTER_STOP

            hold = g.loc[entry_i:exit_i]
            mae = (hold.low.min() / entry_px - 1) * 100 if len(hold) else None
            mfe = (hold.high.max() / entry_px - 1) * 100 if len(hold) else None
            trades.append({
                "market": market,
                "symbol": str(symbol),
                "name": str(sig["name"]),
                "industry": str(sig["industry"]),
                "signalDate": pd.Timestamp(sig.date),
                "entryDate": pd.Timestamp(g.loc[entry_i, "date"]),
                "exitDate": pd.Timestamp(g.loc[exit_i, "date"]),
                "signalClose": float(sig.close),
                "entryPrice": float(entry_px),
                "exitPrice": float(exit_px),
                "entryMode": entry_mode,
                "entryDetail": entry_detail,
                "exitReason": exit_reason,
                "marketRegime": regime,
                "ma5SlopePct": _round(sig.ma5SlopePct),
                "tradeValue": _round(sig.tradeValue, 2),
                "stopLevel": _round(stop),
                "stopDistancePct": _round((stop / entry_px - 1) * 100) if stop else None,
                "holdingDays": int(max(1, exit_i - entry_i + 1)),
                "mfePct": _round(mfe),
                "maePct": _round(mae),
            })
            audit["enteredTrades"] += 1

    return pd.DataFrame(trades), audit


def _portfolio_row(trades: pd.DataFrame, data: pd.DataFrame, costs: CostModel):
    p = _portfolio_summary(trades, data, costs)
    return {
        "totalReturnPct": p.get("totalReturnPct"),
        "maxDrawdownPct": p.get("maxDrawdownPct"),
        "expectancyPct": p.get("expectancyPct"),
        "profitFactor": p.get("profitFactor"),
        "payoffRatio": p.get("payoffRatio"),
        "winRate": p.get("winRate"),
        "avgWinPct": p.get("avgWinPct"),
        "avgLossPct": p.get("avgLossPct"),
        "avgHoldingDays": p.get("avgHoldingDays"),
        "maxTradeGainPct": p.get("maxTradeGainPct"),
        "maxTradeLossPct": p.get("maxTradeLossPct"),
        "tradeCount": p.get("tradeCount"),
        "candidateTradeCount": p.get("candidateTradeCount"),
        "skippedCapacity": p.get("skippedCapacity"),
    }


def _basic_pass(row: dict):
    r = _num(row.get("totalReturnPct"))
    e = _num(row.get("expectancyPct"))
    pf = _num(row.get("profitFactor"))
    return bool(r is not None and e is not None and pf is not None and r > 0 and e > 0 and pf > 1)


def build_execution_v17_summary(data: pd.DataFrame, generated_at: str, costs: CostModel | None = None):
    costs = costs or CostModel()
    d = _prepare_robust(data)
    variants = []

    for mode in ENTRY_MODES:
        trades, audit = _build_trades(d, mode)
        if trades.empty:
            trades["entryYear"] = pd.Series(dtype=int)
        else:
            trades["entryYear"] = pd.to_datetime(trades.entryDate).dt.year

        rows = {
            "TRAIN_2024": _portfolio_row(trades[trades.entryYear == 2024], d, costs),
            "VALIDATION_2025": _portfolio_row(trades[trades.entryYear == 2025], d, costs),
            "TEST_2026_DESCRIPTIVE": _portfolio_row(trades[trades.entryYear >= 2026], d, costs),
        }
        r24 = _num(rows["TRAIN_2024"].get("totalReturnPct"))
        r25 = _num(rows["VALIDATION_2025"].get("totalReturnPct"))
        worst = min(r24, r25) if r24 is not None and r25 is not None else None
        variants.append({
            "entryMode": mode,
            "audit": audit,
            "results": rows,
            "train2024Pass": _basic_pass(rows["TRAIN_2024"]),
            "validation2025Pass": _basic_pass(rows["VALIDATION_2025"]),
            "bothYearsBasicPass": bool(_basic_pass(rows["TRAIN_2024"]) and _basic_pass(rows["VALIDATION_2025"])),
            "worstTrainValidationReturnPct": None if worst is None else round(worst, 4),
        })

    baseline = next(x for x in variants if x["entryMode"] == "NEXT_OPEN")
    for x in variants:
        comparison = {}
        for scope in ("TRAIN_2024", "VALIDATION_2025"):
            row = x["results"][scope]
            b = baseline["results"][scope]
            rr, br = _num(row.get("totalReturnPct")), _num(b.get("totalReturnPct"))
            dd, bdd = _num(row.get("maxDrawdownPct")), _num(b.get("maxDrawdownPct"))
            comparison[scope] = {
                "returnDeltaVsNextOpenPct": None if rr is None or br is None else round(rr - br, 4),
                "drawdownImprovementVsNextOpenPctPoint": None if dd is None or bdd is None else round(dd - bdd, 4),
            }
        x["vsNextOpen"] = comparison

    ranked = sorted(
        variants,
        key=lambda x: -999999 if x["worstTrainValidationReturnPct"] is None else x["worstTrainValidationReturnPct"],
        reverse=True,
    )

    return {
        "ok": True,
        "schemaVersion": 1,
        "version": VERSION,
        "generatedAt": generated_at,
        "purpose": "Daily Execution Backtest phase A: isolate next-session entry timing while holding the signal, risk stop, regime gate, cooldown, MA5 trend exit and portfolio capacity rules fixed.",
        "researchPolicy": {
            "signal": "MA5 rising cross signal from V1.5 is held fixed across entry variants.",
            "fixedRiskArchitecture": {
                "stop": STOP_CONFIG,
                "marketRegimeGate": "NO_BEAR: allow BULL and NEUTRAL signal-day entries only.",
                "cooldownAfterStopSessions": COOLDOWN_AFTER_STOP,
                "trendExit": "MA5 down-cross confirmed at close; exit next session open.",
            },
            "train": "2024 historical development evidence",
            "validation": "2025 historical secondary evidence; no longer pristine because earlier research has inspected it.",
            "test2026": "Descriptive only.",
            "ranking": "Rank by min(2024 total return, 2025 total return), not by best single-year result.",
            "automaticProductionChanges": False,
        },
        "entryRules": {
            "NEXT_OPEN": "Enter next session open.",
            "NO_CHASE_3PCT": "Enter next open only if it is <= 3% above signal close.",
            "PULLBACK_1PCT": "On next session only, buy at 1% below signal close; if open is below limit, fill at open.",
            "BREAKOUT_0_5PCT": "On next session only, buy at 0.5% above signal-day high; gap through fills at the open.",
        },
        "entryModeCount": len(ENTRY_MODES),
        "bothYearsBasicPassCount": sum(1 for x in variants if x["bothYearsBasicPass"]),
        "topByWorstTrainValidationReturn": ranked,
        "variants": variants,
        "limitations": [
            "This phase tests entry timing only; it is not yet a full V3.3 execution backtest.",
            "The MA5 signal is a clean mechanical research signal and is not identical to production A/B/C candidate decisions.",
            "Daily OHLC cannot reveal intraday path. For intraday limit/stop-buy fills, a same-day stop touch is conservatively treated as occurring after the fill.",
            "Portfolio results use the existing normalized 100,000 capital, maximum five positions and deterministic MA5-slope/trade-value priority.",
            "The fixed 1.25 ATR + prior-10-day-low, NO_BEAR, 5-session cooldown architecture was selected in prior research and is not re-tuned here.",
            "Survivorship bias and frozen Yahoo-source limitations remain; forward Decision Ledger evidence is still required before production promotion.",
        ],
    }
