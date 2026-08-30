from __future__ import annotations

import math

import numpy as np
import pandas as pd

from backtest_ma5 import (
    CostModel,
    INITIAL_CAPITAL,
    _max_drawdown,
    _round,
    _trade_summary,
)
from backtest_exit_v17b import _build_variant_trades, _prepare_exit

VERSION = "daily-execution-allocation-backtest-v1.7c"
FIXED_EXIT_MODE = "MA5_MA10_DEATH_CROSS"

# Compact, pre-declared sizing set. Equal-slot variants test concentration/capacity.
# Risk-budget variants cap each name at 20% and allow up to eight simultaneous names.
ALLOCATION_VARIANTS = (
    {
        "name": "EQUAL_3",
        "sizingMode": "EQUAL_SLOT",
        "maxPositions": 3,
        "riskBudgetPct": None,
        "maxWeightPct": 100.0 / 3.0,
    },
    {
        "name": "EQUAL_5_BASELINE",
        "sizingMode": "EQUAL_SLOT",
        "maxPositions": 5,
        "riskBudgetPct": None,
        "maxWeightPct": 20.0,
    },
    {
        "name": "EQUAL_8",
        "sizingMode": "EQUAL_SLOT",
        "maxPositions": 8,
        "riskBudgetPct": None,
        "maxWeightPct": 12.5,
    },
    {
        "name": "RISK_0P50_CAP20_MAX8",
        "sizingMode": "INITIAL_STOP_RISK",
        "maxPositions": 8,
        "riskBudgetPct": 0.50,
        "maxWeightPct": 20.0,
    },
    {
        "name": "RISK_0P75_CAP20_MAX8",
        "sizingMode": "INITIAL_STOP_RISK",
        "maxPositions": 8,
        "riskBudgetPct": 0.75,
        "maxWeightPct": 20.0,
    },
    {
        "name": "RISK_1P00_CAP20_MAX8",
        "sizingMode": "INITIAL_STOP_RISK",
        "maxPositions": 8,
        "riskBudgetPct": 1.00,
        "maxWeightPct": 20.0,
    },
)


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


def _simulate_allocation(
    trades: pd.DataFrame,
    data: pd.DataFrame,
    costs: CostModel,
    cfg: dict,
):
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
            "skippedDuplicateSymbol": 0,
            "skippedCash": 0,
            "skippedInvalidRisk": 0,
            "avgCapitalUtilizationPct": 0.0,
            "avgAllocationPctAtEntry": None,
            "avgPlannedInitialRiskPctOfEquity": None,
            "maxConcurrentPositions": 0,
            "tradeCount": 0,
        }

    t = trades.copy()
    t = t.sort_values(
        ["entryDate", "ma5SlopePct", "tradeValue", "symbol"],
        ascending=[True, False, False, True],
    )

    close_map = (
        data.drop_duplicates(["date", "market", "symbol"], keep="last")
        .set_index(["date", "market", "symbol"])["close"]
    )
    sessions = sorted(pd.to_datetime(data.date).dt.tz_localize(None).drop_duplicates())
    by_entry = {pd.Timestamp(k): v for k, v in t.groupby("entryDate")}

    buy_factor = (1 + costs.slippage_each_side) * (1 + costs.buy_commission)
    sell_factor = (1 - costs.slippage_each_side) * (1 - costs.sell_commission - costs.sell_tax)

    cash = float(INITIAL_CAPITAL)
    positions = []
    selected = []
    equity_curve = []
    utilization = []
    allocation_pcts = []
    planned_risk_pcts = []
    skipped_capacity = 0
    skipped_duplicate = 0
    skipped_cash = 0
    skipped_invalid_risk = 0
    max_concurrent = 0

    max_positions = int(cfg["maxPositions"])
    sizing_mode = str(cfg["sizingMode"])
    max_weight = float(cfg["maxWeightPct"]) / 100.0
    risk_budget = None if cfg.get("riskBudgetPct") is None else float(cfg["riskBudgetPct"]) / 100.0

    for dt in sessions:
        dt = pd.Timestamp(dt)

        # Preserve V1.7A/B portfolio chronology so this phase isolates sizing only:
        # existing positions are marked at the current session close for the sizing base,
        # new entries are allocated, and same-day exits release cash afterward.
        pre_equity = cash
        for p in positions:
            cp = close_map.get((dt, p["market"], p["symbol"]), p["entryPrice"])
            pre_equity += p["qty"] * float(cp) * sell_factor

        todays = by_entry.get(dt)
        if todays is not None:
            for idx, row in todays.iterrows():
                if any(p["market"] == row.market and p["symbol"] == row.symbol for p in positions):
                    skipped_duplicate += 1
                    continue
                if len(positions) >= max_positions:
                    skipped_capacity += 1
                    continue

                entry_price = _num(row.entryPrice)
                if entry_price is None or entry_price <= 0:
                    continue

                if sizing_mode == "EQUAL_SLOT":
                    desired = pre_equity / max_positions
                    planned_risk_pct = None
                elif sizing_mode == "INITIAL_STOP_RISK":
                    stop = _num(row.stopLevel)
                    if stop is None or stop <= 0 or stop >= entry_price or risk_budget is None:
                        skipped_invalid_risk += 1
                        continue
                    risk_fraction = (entry_price - stop) / entry_price
                    if risk_fraction <= 0:
                        skipped_invalid_risk += 1
                        continue
                    desired = min(
                        pre_equity * max_weight,
                        pre_equity * risk_budget / risk_fraction,
                    )
                    planned_risk_pct = desired * risk_fraction / pre_equity * 100 if pre_equity > 0 else None
                else:
                    raise ValueError(f"unknown sizing mode {sizing_mode}")

                desired = min(desired, pre_equity * max_weight)
                allocation = min(float(cash), float(desired))
                if allocation <= max(100.0, INITIAL_CAPITAL * 0.001):
                    skipped_cash += 1
                    continue

                qty = allocation / (entry_price * buy_factor)
                if qty <= 0:
                    continue
                cash -= allocation
                positions.append({
                    "idx": idx,
                    "market": row.market,
                    "symbol": row.symbol,
                    "entryPrice": entry_price,
                    "exitDate": pd.Timestamp(row.exitDate),
                    "exitPrice": float(row.exitPrice),
                    "qty": qty,
                })
                selected.append(idx)
                allocation_pcts.append(allocation / pre_equity * 100 if pre_equity > 0 else np.nan)
                if planned_risk_pct is not None:
                    planned_risk_pcts.append(planned_risk_pct)
                max_concurrent = max(max_concurrent, len(positions))

        remaining = []
        for p in positions:
            if p["exitDate"] == dt:
                cash += p["qty"] * p["exitPrice"] * sell_factor
            else:
                remaining.append(p)
        positions = remaining

        equity = cash
        invested = 0.0
        for p in positions:
            cp = close_map.get((dt, p["market"], p["symbol"]), p["entryPrice"])
            value = p["qty"] * float(cp) * sell_factor
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
        "totalReturnPct": _round((cash / INITIAL_CAPITAL - 1) * 100),
        "maxDrawdownPct": _max_drawdown(equity_curve),
        "selectedTradeCount": int(len(sel)),
        "candidateTradeCount": int(len(t)),
        "selectionRate": round(len(sel) / len(t), 4) if len(t) else None,
        "skippedCapacity": int(skipped_capacity),
        "skippedDuplicateSymbol": int(skipped_duplicate),
        "skippedCash": int(skipped_cash),
        "skippedInvalidRisk": int(skipped_invalid_risk),
        "avgCapitalUtilizationPct": _round(float(np.mean(utilization)) * 100 if utilization else 0.0),
        "avgAllocationPctAtEntry": _round(float(pd.Series(allocation_pcts).dropna().mean()) if allocation_pcts else None),
        "avgPlannedInitialRiskPctOfEquity": _round(float(pd.Series(planned_risk_pcts).dropna().mean()) if planned_risk_pcts else None),
        "maxConcurrentPositions": int(max_concurrent),
        **metrics,
    }


def _basic_pass(row: dict):
    r = _num(row.get("totalReturnPct"))
    e = _num(row.get("expectancyPct"))
    pf = _num(row.get("profitFactor"))
    return bool(r is not None and e is not None and pf is not None and r > 0 and e > 0 and pf > 1)


def _find_v17b_exit(v17b_summary: dict | None, exit_mode: str):
    if not v17b_summary:
        return None
    for row in v17b_summary.get("variants") or []:
        if row.get("exitMode") == exit_mode:
            return row
    return None


def build_allocation_v17c_summary(
    data: pd.DataFrame,
    generated_at: str,
    costs: CostModel | None = None,
    v17b_summary: dict | None = None,
):
    costs = costs or CostModel()
    d = _prepare_exit(data)
    trades, trade_audit = _build_variant_trades(d, FIXED_EXIT_MODE)
    if trades.empty:
        trades["entryYear"] = pd.Series(dtype=int)
    else:
        trades["entryYear"] = pd.to_datetime(trades.entryDate).dt.year

    scopes = {
        "TRAIN_2024": trades[trades.entryYear == 2024],
        "VALIDATION_2025": trades[trades.entryYear == 2025],
        "TEST_2026_DESCRIPTIVE": trades[trades.entryYear >= 2026],
    }

    variants = []
    for cfg in ALLOCATION_VARIANTS:
        rows = {
            scope: _simulate_allocation(g, d, costs, cfg)
            for scope, g in scopes.items()
        }
        r24 = _num(rows["TRAIN_2024"].get("totalReturnPct"))
        r25 = _num(rows["VALIDATION_2025"].get("totalReturnPct"))
        worst_return = min(r24, r25) if r24 is not None and r25 is not None else None
        dd24 = _num(rows["TRAIN_2024"].get("maxDrawdownPct"))
        dd25 = _num(rows["VALIDATION_2025"].get("maxDrawdownPct"))
        worst_dd = min(dd24, dd25) if dd24 is not None and dd25 is not None else None
        variants.append({
            "variant": cfg["name"],
            "parameters": cfg,
            "results": rows,
            "train2024Pass": _basic_pass(rows["TRAIN_2024"]),
            "validation2025Pass": _basic_pass(rows["VALIDATION_2025"]),
            "bothYearsBasicPass": bool(
                _basic_pass(rows["TRAIN_2024"])
                and _basic_pass(rows["VALIDATION_2025"])
            ),
            "worstTrainValidationReturnPct": None if worst_return is None else round(worst_return, 4),
            "worstTrainValidationDrawdownPct": None if worst_dd is None else round(worst_dd, 4),
        })

    baseline = next(x for x in variants if x["variant"] == "EQUAL_5_BASELINE")
    prior = _find_v17b_exit(v17b_summary, FIXED_EXIT_MODE)
    parity = {"checked": prior is not None, "matches": None, "mismatches": []}
    if prior is not None:
        mismatches = []
        for scope in ("TRAIN_2024", "VALIDATION_2025", "TEST_2026_DESCRIPTIVE"):
            current = baseline["results"][scope]
            old = (prior.get("results") or {}).get(scope) or {}
            for key in ("totalReturnPct", "maxDrawdownPct", "tradeCount", "candidateTradeCount"):
                if current.get(key) != old.get(key):
                    mismatches.append({
                        "scope": scope,
                        "metric": key,
                        "v17cBaseline": current.get(key),
                        "v17bWinner": old.get(key),
                    })
        parity["matches"] = not mismatches
        parity["mismatches"] = mismatches
        if mismatches:
            raise RuntimeError(f"V1.7C equal-5 baseline does not match V1.7B winner: {mismatches[:5]}")

    for x in variants:
        x["vsEqual5Baseline"] = {}
        for scope in ("TRAIN_2024", "VALIDATION_2025", "TEST_2026_DESCRIPTIVE"):
            row = x["results"][scope]
            b = baseline["results"][scope]
            rr, br = _num(row.get("totalReturnPct")), _num(b.get("totalReturnPct"))
            dd, bdd = _num(row.get("maxDrawdownPct")), _num(b.get("maxDrawdownPct"))
            x["vsEqual5Baseline"][scope] = {
                "returnDeltaPctPoint": None if rr is None or br is None else round(rr - br, 4),
                "drawdownImprovementPctPoint": None if dd is None or bdd is None else round(dd - bdd, 4),
            }

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
        "purpose": "Daily Execution Backtest phase C: test position count, equal-slot concentration and initial-stop risk budgeting while holding the V1.7A NEXT_OPEN entry and V1.7B MA5/MA10 death-cross exit fixed.",
        "researchPolicy": {
            "fixedEntry": "NEXT_OPEN from V1.7A.",
            "fixedExit": FIXED_EXIT_MODE,
            "fixedSignalRiskRegime": "MA5 rising-cross signal + 1.25 ATR/prior-10-day-low initial stop + NO_BEAR + five-session post-stop cooldown.",
            "train": "2024 historical development evidence.",
            "validation": "2025 historical secondary evidence; no longer pristine.",
            "test2026": "Descriptive only.",
            "ranking": "Rank by min(2024 total return, 2025 total return); drawdown and utilization are reported separately.",
            "automaticProductionChanges": False,
        },
        "sizingRules": {
            "EQUAL_SLOT": "Target one equal portfolio slot per position; unused slots remain cash.",
            "INITIAL_STOP_RISK": "Target a fixed percentage of current portfolio equity lost if the initial stop is hit, capped at 20% portfolio weight per name; actual realized gap-through loss can exceed the planned risk.",
            "selectionPriority": "When candidates compete for capacity, higher MA5 slope first, then higher signal-day trade value, then symbol.",
            "cash": "No leverage; allocations are capped by available cash.",
        },
        "variantCount": len(ALLOCATION_VARIANTS),
        "tradeAudit": trade_audit,
        "baselineParityWithV17B": parity,
        "bothYearsBasicPassCount": sum(1 for x in variants if x["bothYearsBasicPass"]),
        "topByWorstTrainValidationReturn": ranked,
        "variants": variants,
        "limitations": [
            "This phase changes portfolio sizing/capacity only; the candidate signal, entry, stop, cooldown and exit are fixed.",
            "Portfolio sizing chronology intentionally matches V1.7A/B for strict baseline parity; a separate chronology audit should test prior-close/open-equity sizing before production promotion.",
            "Risk-budget sizing uses the modeled opening fill and initial stop distance; a live market-on-open implementation would need a pre-open price proxy or post-open sizing.",
            "No sector-concentration cap is applied in V1.7C; sector risk should be tested separately rather than mixed into this compact sizing experiment.",
            "Survivorship bias and frozen Yahoo-source limitations remain; forward Decision Ledger evidence remains required before production promotion.",
        ],
    }
