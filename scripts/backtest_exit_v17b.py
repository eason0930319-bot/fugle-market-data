from __future__ import annotations

import math
import pandas as pd

from backtest_ma5 import (
    CostModel,
    INITIAL_CAPITAL,
    MAX_POSITIONS,
    _max_drawdown,
    _portfolio_summary,
    _round,
    _trade_summary,
)
from backtest_ma5_robustness import _prepare_robust
from backtest_ma5_stop import _stop_level
from backtest_execution_v17 import (
    ALLOWED_REGIMES,
    COOLDOWN_AFTER_STOP,
    STOP_CONFIG,
    _build_trades as _build_v17a_trades,
)

VERSION = "daily-execution-exit-backtest-v1.7b"

EXIT_MODES = (
    "MA5_DOWN_CROSS",
    "CLOSE_BELOW_MA10",
    "MA5_MA10_DEATH_CROSS",
    "ATR_TRAIL_2P0",
    "CLOSE_DRAWDOWN_2ATR",
    "PARTIAL_2R_THEN_MA5",
)


def _num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def _prepare_exit(data: pd.DataFrame):
    d = _prepare_robust(data)
    g = d.groupby(["market", "symbol"], sort=False)
    d["ma10"] = g["close"].transform(lambda s: s.rolling(10, min_periods=10).mean())
    d["prevMA10"] = g["ma10"].shift(1)
    return d


def _close_exit_signal(g: pd.DataFrame, j: int, mode: str, high_close: float | None):
    row = g.loc[j]
    if mode == "MA5_DOWN_CROSS":
        return bool(row.exitCrossSignal)

    if mode == "CLOSE_BELOW_MA10":
        close = _num(row.close)
        ma10 = _num(row.ma10)
        prev_close = _num(row.prevClose)
        prev_ma10 = _num(row.prevMA10)
        return bool(
            close is not None
            and ma10 is not None
            and prev_close is not None
            and prev_ma10 is not None
            and close < ma10
            and prev_close >= prev_ma10
        )

    if mode == "MA5_MA10_DEATH_CROSS":
        ma5 = _num(row.ma5)
        ma10 = _num(row.ma10)
        prev_ma5 = _num(row.prevMA5)
        prev_ma10 = _num(row.prevMA10)
        return bool(
            ma5 is not None
            and ma10 is not None
            and prev_ma5 is not None
            and prev_ma10 is not None
            and ma5 < ma10
            and prev_ma5 >= prev_ma10
        )

    if mode == "CLOSE_DRAWDOWN_2ATR":
        close = _num(row.close)
        atr = _num(row.atr14)
        return bool(
            close is not None
            and atr is not None
            and atr > 0
            and high_close is not None
            and close <= high_close - 2.0 * atr
        )

    if mode == "PARTIAL_2R_THEN_MA5":
        return bool(row.exitCrossSignal)

    return False


def _build_variant_trades(data: pd.DataFrame, exit_mode: str):
    if exit_mode == "MA5_DOWN_CROSS":
        trades, audit = _build_v17a_trades(data, "NEXT_OPEN")
        audit = dict(audit)
        audit["exitMode"] = exit_mode
        audit["baselineFromV17A"] = True
        trades = trades.copy()
        trades["exitMode"] = exit_mode
        trades["partialExitDate"] = pd.NaT
        trades["partialExitPrice"] = float("nan")
        trades["partialFraction"] = 0.0
        trades["finalFraction"] = 1.0
        return trades, audit

    trades = []
    audit = {
        "exitMode": exit_mode,
        "entrySignalsSeen": 0,
        "skippedRegimeSignals": 0,
        "skippedCooldownSignals": 0,
        "skippedWhileInTradeSignals": 0,
        "missingStopLevelCount": 0,
        "enteredTrades": 0,
        "hardStopExits": 0,
        "trailingStopExits": 0,
        "closeSignalExits": 0,
        "partialTargetHits": 0,
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
            if sig_i + 1 >= len(g):
                continue

            entry_i = sig_i + 1
            entry_px = _num(g.loc[entry_i, "open"])
            if entry_px is None or entry_px <= 0:
                continue

            stop = _stop_level(sig, entry_px, STOP_CONFIG)
            if stop is None:
                audit["missingStopLevelCount"] += 1
            initial_risk = entry_px - stop if stop is not None and stop < entry_px else None
            target_2r = entry_px + 2.0 * initial_risk if initial_risk is not None else None

            exit_i = None
            final_exit_px = None
            exit_reason = None
            partial_i = None
            partial_px = None
            partial_fraction = 0.0

            high_close = None
            trail_stop = stop

            for j in range(entry_i, len(g)):
                row = g.loc[j]
                o = _num(row.open)
                lo = _num(row.low)
                hi = _num(row.high)
                close = _num(row.close)

                active_stop = stop
                stop_reason = "STOP_HARD"
                if exit_mode == "ATR_TRAIL_2P0" and trail_stop is not None:
                    if active_stop is None or trail_stop > active_stop:
                        active_stop = trail_stop
                        stop_reason = "STOP_ATR_TRAIL"

                if active_stop is not None:
                    if o is not None and o <= active_stop:
                        exit_i = j
                        final_exit_px = o
                        exit_reason = stop_reason + "_GAP"
                        if stop_reason == "STOP_ATR_TRAIL":
                            audit["trailingStopExits"] += 1
                        else:
                            audit["hardStopExits"] += 1
                        break
                    if lo is not None and lo <= active_stop:
                        exit_i = j
                        final_exit_px = active_stop
                        exit_reason = stop_reason + "_INTRADAY"
                        if stop_reason == "STOP_ATR_TRAIL":
                            audit["trailingStopExits"] += 1
                        else:
                            audit["hardStopExits"] += 1
                        break

                if (
                    exit_mode == "PARTIAL_2R_THEN_MA5"
                    and partial_fraction == 0.0
                    and target_2r is not None
                    and hi is not None
                    and hi >= target_2r
                ):
                    partial_i = j
                    partial_px = target_2r
                    partial_fraction = 0.5
                    audit["partialTargetHits"] += 1

                if close is not None:
                    high_close = close if high_close is None else max(high_close, close)

                if _close_exit_signal(g, j, exit_mode, high_close) and j + 1 < len(g):
                    nxt = _num(g.loc[j + 1, "open"])
                    if nxt is not None and nxt > 0:
                        exit_i = j + 1
                        final_exit_px = nxt
                        if exit_mode == "CLOSE_BELOW_MA10":
                            exit_reason = "MA10_DOWN_CROSS"
                        elif exit_mode == "MA5_MA10_DEATH_CROSS":
                            exit_reason = "MA5_MA10_DEATH_CROSS"
                        elif exit_mode == "CLOSE_DRAWDOWN_2ATR":
                            exit_reason = "CLOSE_DRAWDOWN_2ATR"
                        else:
                            exit_reason = "MA5_DOWN_CROSS"
                        audit["closeSignalExits"] += 1
                        break

                if exit_mode == "ATR_TRAIL_2P0" and close is not None:
                    atr = _num(row.atr14)
                    if atr is not None and atr > 0 and high_close is not None:
                        candidate = high_close - 2.0 * atr
                        if candidate > 0:
                            trail_stop = candidate if trail_stop is None else max(trail_stop, candidate)

            if exit_i is None:
                exit_i = len(g) - 1
                final_exit_px = _num(g.loc[exit_i, "close"])
                exit_reason = "END_OF_DATA"
            if final_exit_px is None or final_exit_px <= 0:
                continue

            final_fraction = 1.0 - partial_fraction
            effective_exit_px = (
                partial_fraction * partial_px + final_fraction * final_exit_px
                if partial_fraction > 0 and partial_px is not None
                else final_exit_px
            )

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
                "exitPrice": float(effective_exit_px),
                "entryMode": "NEXT_OPEN",
                "entryDetail": "OPEN",
                "exitMode": exit_mode,
                "exitReason": exit_reason,
                "marketRegime": regime,
                "ma5SlopePct": _round(sig.ma5SlopePct),
                "tradeValue": _round(sig.tradeValue, 2),
                "stopLevel": _round(stop),
                "stopDistancePct": _round((stop / entry_px - 1) * 100) if stop else None,
                "holdingDays": int(max(1, exit_i - entry_i + 1)),
                "mfePct": _round(mfe),
                "maePct": _round(mae),
                "partialExitDate": (
                    pd.Timestamp(g.loc[partial_i, "date"])
                    if partial_i is not None
                    else pd.NaT
                ),
                "partialExitPrice": _round(partial_px),
                "partialFraction": partial_fraction,
                "finalExitPrice": float(final_exit_px),
                "finalFraction": final_fraction,
            })
            audit["enteredTrades"] += 1

    return pd.DataFrame(trades), audit


def _partial_portfolio_summary(trades: pd.DataFrame, data: pd.DataFrame, costs: CostModel):
    if trades.empty:
        return {
            "initialCapital": INITIAL_CAPITAL,
            "endingCapital": INITIAL_CAPITAL,
            "totalReturnPct": 0.0,
            "maxDrawdownPct": 0.0,
            "selectedTradeCount": 0,
            "candidateTradeCount": 0,
            "skippedCapacity": 0,
            "partialCashRecycledIntraday": True,
            "tradeCount": 0,
        }

    t = trades.copy()
    t = t.sort_values(
        ["entryDate", "ma5SlopePct", "tradeValue"],
        ascending=[True, False, False],
    )
    close_map = (
        data.drop_duplicates(["date", "market", "symbol"], keep="last")
        .set_index(["date", "market", "symbol"])["close"]
    )
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

        pre_equity = cash
        for p in positions:
            cp = close_map.get((dt, p["market"], p["symbol"]), p["entryPrice"])
            pre_equity += p["qty"] * p["remainingFraction"] * float(cp) * sell_factor

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

                events = []
                pf = _num(row.partialFraction) or 0.0
                if pf > 0 and not pd.isna(row.partialExitDate):
                    events.append({
                        "date": pd.Timestamp(row.partialExitDate),
                        "fraction": pf,
                        "price": float(row.partialExitPrice),
                    })
                events.append({
                    "date": pd.Timestamp(row.exitDate),
                    "fraction": float(row.finalFraction),
                    "price": float(row.finalExitPrice),
                })
                positions.append({
                    "idx": idx,
                    "market": row.market,
                    "symbol": row.symbol,
                    "entryPrice": float(row.entryPrice),
                    "qty": qty,
                    "remainingFraction": 1.0,
                    "events": events,
                })
                selected.append(idx)

        remaining_positions = []
        for p in positions:
            for ev in p["events"]:
                if ev["date"] == dt and ev["fraction"] > 0:
                    cash += p["qty"] * ev["fraction"] * ev["price"] * sell_factor
                    p["remainingFraction"] -= ev["fraction"]
            if p["remainingFraction"] > 1e-9:
                remaining_positions.append(p)
        positions = remaining_positions

        equity = cash
        for p in positions:
            cp = close_map.get((dt, p["market"], p["symbol"]), p["entryPrice"])
            equity += p["qty"] * p["remainingFraction"] * float(cp) * sell_factor
        equity_curve.append(float(equity))

    for p in positions:
        final_price = p["events"][-1]["price"]
        cash += p["qty"] * p["remainingFraction"] * final_price * sell_factor
    if equity_curve:
        equity_curve[-1] = float(cash)

    sel = t.loc[sorted(set(selected))].copy() if selected else t.iloc[0:0].copy()
    metrics = _trade_summary(sel, costs)
    return {
        "initialCapital": INITIAL_CAPITAL,
        "endingCapital": round(float(cash), 2),
        "totalReturnPct": _round((cash / INITIAL_CAPITAL - 1) * 100),
        "maxDrawdownPct": _max_drawdown(equity_curve),
        "selectedTradeCount": int(len(sel)),
        "candidateTradeCount": int(len(t)),
        "skippedCapacity": int(skipped_capacity),
        "partialCashRecycledIntraday": True,
        **metrics,
    }


def _portfolio_row(trades: pd.DataFrame, data: pd.DataFrame, costs: CostModel, exit_mode: str):
    if exit_mode == "PARTIAL_2R_THEN_MA5":
        p = _partial_portfolio_summary(trades, data, costs)
    else:
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


def _baseline_from_v17a(v17a_summary: dict | None):
    if not v17a_summary:
        return None
    for row in v17a_summary.get("variants") or []:
        if row.get("entryMode") == "NEXT_OPEN":
            return row
    return None


def build_exit_v17b_summary(
    data: pd.DataFrame,
    generated_at: str,
    costs: CostModel | None = None,
    v17a_summary: dict | None = None,
):
    costs = costs or CostModel()
    d = _prepare_exit(data)
    variants = []

    for mode in EXIT_MODES:
        trades, audit = _build_variant_trades(d, mode)
        if trades.empty:
            trades["entryYear"] = pd.Series(dtype=int)
        else:
            trades["entryYear"] = pd.to_datetime(trades.entryDate).dt.year

        rows = {
            "TRAIN_2024": _portfolio_row(trades[trades.entryYear == 2024], d, costs, mode),
            "VALIDATION_2025": _portfolio_row(trades[trades.entryYear == 2025], d, costs, mode),
            "TEST_2026_DESCRIPTIVE": _portfolio_row(trades[trades.entryYear >= 2026], d, costs, mode),
        }
        r24 = _num(rows["TRAIN_2024"].get("totalReturnPct"))
        r25 = _num(rows["VALIDATION_2025"].get("totalReturnPct"))
        worst = min(r24, r25) if r24 is not None and r25 is not None else None
        variants.append({
            "exitMode": mode,
            "audit": audit,
            "results": rows,
            "train2024Pass": _basic_pass(rows["TRAIN_2024"]),
            "validation2025Pass": _basic_pass(rows["VALIDATION_2025"]),
            "bothYearsBasicPass": bool(
                _basic_pass(rows["TRAIN_2024"])
                and _basic_pass(rows["VALIDATION_2025"])
            ),
            "worstTrainValidationReturnPct": None if worst is None else round(worst, 4),
        })

    baseline = next(x for x in variants if x["exitMode"] == "MA5_DOWN_CROSS")
    baseline_v17a = _baseline_from_v17a(v17a_summary)
    parity = {
        "checked": baseline_v17a is not None,
        "matches": None,
        "mismatches": [],
    }
    if baseline_v17a is not None:
        mismatches = []
        for scope in ("TRAIN_2024", "VALIDATION_2025", "TEST_2026_DESCRIPTIVE"):
            current = baseline["results"][scope]
            prior = (baseline_v17a.get("results") or {}).get(scope) or {}
            for key in ("totalReturnPct", "maxDrawdownPct", "tradeCount", "candidateTradeCount"):
                if current.get(key) != prior.get(key):
                    mismatches.append({
                        "scope": scope,
                        "metric": key,
                        "v17bBaseline": current.get(key),
                        "v17aNextOpen": prior.get(key),
                    })
        parity["matches"] = not mismatches
        parity["mismatches"] = mismatches
        if mismatches:
            raise RuntimeError(f"V1.7B baseline parity failed: {mismatches}")

    for x in variants:
        comparison = {}
        for scope in ("TRAIN_2024", "VALIDATION_2025"):
            row = x["results"][scope]
            b = baseline["results"][scope]
            rr, br = _num(row.get("totalReturnPct")), _num(b.get("totalReturnPct"))
            dd, bdd = _num(row.get("maxDrawdownPct")), _num(b.get("maxDrawdownPct"))
            comparison[scope] = {
                "returnDeltaVsMA5PctPoint": None if rr is None or br is None else round(rr - br, 4),
                "drawdownImprovementVsMA5PctPoint": None if dd is None or bdd is None else round(dd - bdd, 4),
            }
        x["vsMA5Baseline"] = comparison

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
        "purpose": "Daily Execution Backtest phase B: isolate exit logic while fixing V1.7A NEXT_OPEN entry, the MA5 signal, initial 1.25 ATR/prior-10-day-low disaster stop, NO_BEAR gate, five-session post-stop cooldown and five-slot portfolio.",
        "researchPolicy": {
            "fixedEntry": "NEXT_OPEN from V1.7A.",
            "fixedSignal": "MA5 rising cross signal.",
            "fixedInitialStop": STOP_CONFIG,
            "marketRegimeGate": "NO_BEAR: BULL and NEUTRAL signal-day entries only.",
            "cooldownAfterStopSessions": COOLDOWN_AFTER_STOP,
            "train": "2024 historical development evidence.",
            "validation": "2025 historical secondary evidence; no longer pristine.",
            "test2026": "Descriptive only.",
            "ranking": "Rank by min(2024 total return, 2025 total return). Drawdown is reported separately and is not optimized as a hidden objective.",
            "automaticProductionChanges": False,
        },
        "exitRules": {
            "MA5_DOWN_CROSS": "Baseline: MA5 is falling and close crosses below MA5; exit next session open. Initial disaster stop remains active.",
            "CLOSE_BELOW_MA10": "Close crosses from at/above MA10 to below MA10; exit next session open. Initial disaster stop remains active.",
            "MA5_MA10_DEATH_CROSS": "MA5 crosses from at/above MA10 to below MA10; exit next session open. Initial disaster stop remains active.",
            "ATR_TRAIL_2P0": "Keep the initial disaster stop and trail a stop at the highest close since entry minus 2.0 ATR14. The next session's trail uses only information known at the prior close; gap-through exits at open.",
            "CLOSE_DRAWDOWN_2ATR": "When the close is at least 2 ATR14 below the highest close since entry, exit next session open. Initial disaster stop remains active.",
            "PARTIAL_2R_THEN_MA5": "Take 50% at +2R based on initial risk; let the remaining 50% use the original disaster stop plus MA5 down-cross exit. Same-day stop/target ambiguity is resolved stop-first.",
        },
        "baselineParityWithV17A": parity,
        "exitModeCount": len(EXIT_MODES),
        "bothYearsBasicPassCount": sum(1 for x in variants if x["bothYearsBasicPass"]),
        "topByWorstTrainValidationReturn": ranked,
        "variants": variants,
        "limitations": [
            "Daily OHLC cannot reconstruct exact intraday path. Stop/target ambiguity is treated conservatively as stop-first.",
            "The ATR trail used for a session is based on the previous close/high-water information, avoiding same-day look-ahead.",
            "The partial-profit portfolio recycles modeled partial-sale cash on the target date while keeping the position slot occupied until the final leg exits.",
            "Portfolio sizing intentionally preserves the existing V1.5/V1.7A convention for comparability; V1.7C will audit slot sizing, capital allocation and same-day chronology separately.",
            "The MA5 signal is a research proxy and is not identical to production V3.3 A/B/C decisions.",
            "Survivorship bias and Yahoo-source limitations remain despite the immutable data freeze.",
            "No exit rule is promoted automatically; forward Decision Ledger evidence remains required.",
        ],
    }
