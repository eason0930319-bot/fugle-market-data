from __future__ import annotations

import math
import pandas as pd

from backtest_ma5 import CostModel, _portfolio_summary, _prepare
from backtest_ma5_stop import _build_trades

VERSION = "ma5-stop-robustness-v1.6.1"
ATR_MULTS = (1.0, 1.25, 1.5, 1.75, 2.0)
STRUCT_VARIANTS = (
    ("ATR_ONLY", False, "l10", 0.25),
    ("L5_OFF025", True, "l5", 0.25),
    ("L10_OFF000", True, "l10", 0.00),
    ("L10_OFF025", True, "l10", 0.25),
    ("L10_OFF050", True, "l10", 0.50),
    ("L20_OFF025", True, "l20", 0.25),
)


def _n(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def _prepare_robust(data: pd.DataFrame):
    d = _prepare(data)
    g = d.groupby(["market", "symbol"], sort=False)
    d["l5"] = g["low"].transform(lambda s: s.shift(1).rolling(5, min_periods=4).min())
    # l10 already exists in the historical feature set, but recompute here so the
    # robustness module remains explicit and internally comparable.
    d["l10"] = g["low"].transform(lambda s: s.shift(1).rolling(10, min_periods=8).min())
    d["l20"] = g["low"].transform(lambda s: s.shift(1).rolling(20, min_periods=15).min())
    return d


def _portfolio_row(trades: pd.DataFrame, d: pd.DataFrame, costs: CostModel):
    p = _portfolio_summary(trades, d, costs)
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
    }


def _passes_basic(row: dict):
    r = _n(row.get("totalReturnPct"))
    e = _n(row.get("expectancyPct"))
    pf = _n(row.get("profitFactor"))
    return bool(r is not None and e is not None and pf is not None and r > 0 and e > 0 and pf > 1)


def _better_drawdown(row: dict, baseline: dict):
    dd = _n(row.get("maxDrawdownPct"))
    b = _n(baseline.get("maxDrawdownPct"))
    return bool(dd is not None and b is not None and dd > b)


def _variant_name(mult: float, struct_name: str):
    m = str(mult).replace(".", "p")
    return f"ATR{m}_{struct_name}"


def build_ma5_robustness_summary(data: pd.DataFrame, generated_at: str, costs: CostModel | None = None):
    costs = costs or CostModel()
    d = _prepare_robust(data)

    baseline_trades, _ = _build_trades(d, {"kind": "NONE"})
    if baseline_trades.empty:
        baseline_trades["entryYear"] = pd.Series(dtype=int)
    else:
        baseline_trades["entryYear"] = pd.to_datetime(baseline_trades.entryDate).dt.year
    baseline = {
        "TRAIN_2024": _portfolio_row(baseline_trades[baseline_trades.entryYear == 2024], d, costs),
        "VALIDATION_2025": _portfolio_row(baseline_trades[baseline_trades.entryYear == 2025], d, costs),
        "TEST_2026_DESCRIPTIVE": _portfolio_row(baseline_trades[baseline_trades.entryYear >= 2026], d, costs),
    }

    variants = []
    for mult in ATR_MULTS:
        for struct_name, use_structure, struct_col, offset in STRUCT_VARIANTS:
            cfg = {
                "kind": "ATR_STRUCT",
                "atr_mult": mult,
                "use_structure": use_structure,
                "struct_col": struct_col,
                "struct_atr_offset": offset,
            }
            trades, missing = _build_trades(d, cfg)
            if trades.empty:
                trades["entryYear"] = pd.Series(dtype=int)
            else:
                trades["entryYear"] = pd.to_datetime(trades.entryDate).dt.year

            rows = {
                "TRAIN_2024": _portfolio_row(trades[trades.entryYear == 2024], d, costs),
                "VALIDATION_2025": _portfolio_row(trades[trades.entryYear == 2025], d, costs),
                "TEST_2026_DESCRIPTIVE": _portfolio_row(trades[trades.entryYear >= 2026], d, costs),
            }
            train_ok = _passes_basic(rows["TRAIN_2024"])
            val_ok = _passes_basic(rows["VALIDATION_2025"])
            train_dd = _better_drawdown(rows["TRAIN_2024"], baseline["TRAIN_2024"])
            val_dd = _better_drawdown(rows["VALIDATION_2025"], baseline["VALIDATION_2025"])
            r24 = _n(rows["TRAIN_2024"].get("totalReturnPct"))
            r25 = _n(rows["VALIDATION_2025"].get("totalReturnPct"))
            worst_tv = min(r24, r25) if r24 is not None and r25 is not None else None
            variants.append({
                "variant": _variant_name(mult, struct_name),
                "parameters": cfg,
                "missingStopLevelCount": int(missing),
                "results": rows,
                "train2024Pass": train_ok,
                "validation2025Pass": val_ok,
                "train2024DrawdownImprovedVsNoStop": train_dd,
                "validation2025DrawdownImprovedVsNoStop": val_dd,
                "robustTrainValidationPass": bool(train_ok and val_ok and train_dd and val_dd),
                "worstTrainValidationReturnPct": None if worst_tv is None else round(worst_tv, 4),
            })

    robust = [x for x in variants if x["robustTrainValidationPass"]]
    ranked = sorted(
        variants,
        key=lambda x: -999999 if x["worstTrainValidationReturnPct"] is None else x["worstTrainValidationReturnPct"],
        reverse=True,
    )

    by_mult = []
    for mult in ATR_MULTS:
        xs = [x for x in variants if float(x["parameters"]["atr_mult"]) == mult]
        by_mult.append({
            "atrMult": mult,
            "variantCount": len(xs),
            "robustPassCount": sum(1 for x in xs if x["robustTrainValidationPass"]),
            "median2024ReturnPct": round(pd.Series([_n(x["results"]["TRAIN_2024"].get("totalReturnPct")) for x in xs]).dropna().median(), 4),
            "median2025ReturnPct": round(pd.Series([_n(x["results"]["VALIDATION_2025"].get("totalReturnPct")) for x in xs]).dropna().median(), 4),
        })

    by_structure = []
    for struct_name, _, _, _ in STRUCT_VARIANTS:
        xs = [x for x in variants if x["variant"].endswith(struct_name)]
        by_structure.append({
            "structureVariant": struct_name,
            "variantCount": len(xs),
            "robustPassCount": sum(1 for x in xs if x["robustTrainValidationPass"]),
            "median2024ReturnPct": round(pd.Series([_n(x["results"]["TRAIN_2024"].get("totalReturnPct")) for x in xs]).dropna().median(), 4),
            "median2025ReturnPct": round(pd.Series([_n(x["results"]["VALIDATION_2025"].get("totalReturnPct")) for x in xs]).dropna().median(), 4),
        })

    return {
        "ok": True,
        "schemaVersion": 1,
        "version": VERSION,
        "generatedAt": generated_at,
        "purpose": "Robustness test of the V1.6 ATR/structure disaster stop. The goal is to identify a broad stable parameter region, not the single best historical point.",
        "researchPolicy": {
            "train": "2024",
            "validation": "2025",
            "test2026": "Descriptive only; already examined in prior research.",
            "automaticProductionChanges": False,
            "selectionRule": "Do not promote a parameter merely because it has the highest 2025 return. Prefer a plateau where neighboring ATR/structure settings also pass train+validation gates.",
        },
        "grid": {
            "atrMultipliers": list(ATR_MULTS),
            "structureVariants": [
                {"name": n, "useStructure": u, "column": c, "atrOffset": o}
                for n, u, c, o in STRUCT_VARIANTS
            ],
            "variantCount": len(variants),
        },
        "baselineNoStop": baseline,
        "robustGate": {
            "perYearBasic": "totalReturnPct > 0 AND expectancyPct > 0 AND profitFactor > 1",
            "drawdown": "maxDrawdownPct must be less severe than MA5_ONLY in both 2024 and 2025",
            "trainValidation": "All basic and drawdown conditions must pass in both 2024 and 2025.",
        },
        "robustPassCount": len(robust),
        "robustPassRate": round(len(robust) / len(variants), 4) if variants else None,
        "topByWorstTrainValidationReturn": ranked[:10],
        "atrMultiplierPlateau": by_mult,
        "structurePlateau": by_structure,
        "variants": variants,
        "limitations": [
            "This is a parameter sensitivity study, not permission to optimize on 2025 or 2026.",
            "Daily OHLC stop-touch assumptions cannot reconstruct exact intraday path.",
            "The structure low uses prior-session rolling lows only, avoiding same-day look-ahead.",
            "Current-listed security master and Yahoo history retain survivorship/data-source limitations.",
            "Forward Decision Ledger remains the cleanest validation for production promotion.",
        ],
    }
