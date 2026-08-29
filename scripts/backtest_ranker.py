from __future__ import annotations

import math

import numpy as np
import pandas as pd

from backtest_execution import CostModel, STRATEGIES
from backtest_portfolio import _attach_trade_dates, _simulate_portfolio

VERSION = "candidate-ranker-v1.4"
RIDGE_ALPHA = 25.0

NUMERIC_FEATURES = [
    "sectorStrengthScore",
    "rvol20",
    "distanceFromMA20Pct",
    "distanceFrom20DHighPct",
    "changePct",
    "gapPct",
    "closePosition",
    "upperWickPct",
    "rangePct",
    "ma20Slope5Pct",
    "rrProxy",
]

CATEGORICAL_LEVELS = {
    "extensionSubtype": [
        "EXHAUSTION_RISK",
        "STRONG_CONTINUATION",
        "GAP_EXTENSION",
        "HIGH_VOLUME_EXTENSION",
        "OTHER_EXTENSION",
    ],
    "marketRegime": ["BULL", "NEUTRAL", "BEAR", "UNKNOWN"],
    "sectorBucket": ["STRONG_70_PLUS", "MID_50_70", "WEAK_LT50", "UNKNOWN"],
    "rvolBucket": ["LT1", "1_TO_1_5", "1_5_TO_2", "GE2", "UNKNOWN"],
    "distMABucket": ["LT14", "14_TO_18", "18_TO_25", "GE25", "UNKNOWN"],
}

MODEL_COHORTS = {
    "GLOBAL_EXTENDED": lambda x: pd.Series(True, index=x.index),
    "GAP_EXTENSION_BULL": lambda x: x.extensionSubtype.eq("GAP_EXTENSION") & x.marketRegime.eq("BULL"),
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


def _merge_signal_features(trades: pd.DataFrame, ext: pd.DataFrame):
    keys = ["signalDate", "split", "market", "symbol"]
    feature_cols = keys + NUMERIC_FEATURES + list(CATEGORICAL_LEVELS.keys())
    f = ext[feature_cols].drop_duplicates(keys, keep="last").copy()
    overlap = [c for c in feature_cols if c not in keys and c in trades.columns]
    base = trades.drop(columns=overlap, errors="ignore")
    return base.merge(f, how="left", on=keys)


def _daily_rank_numeric(df: pd.DataFrame):
    out = pd.DataFrame(index=df.index)
    for col in NUMERIC_FEATURES:
        s = pd.to_numeric(df[col], errors="coerce")
        rank = s.groupby(df.signalDate).rank(method="average", pct=True)
        out[f"RANK_{col}"] = rank.fillna(0.5).astype(float) - 0.5
    return out


def _design(df: pd.DataFrame):
    parts = [_daily_rank_numeric(df)]
    for col, levels in CATEGORICAL_LEVELS.items():
        values = df[col].fillna("UNKNOWN").astype(str)
        d = pd.DataFrame(index=df.index)
        for level in levels:
            d[f"{col}={level}"] = (values == level).astype(float)
        parts.append(d)
    x = pd.concat(parts, axis=1)
    return x.astype(float)


def _weighted_ridge_fit(x: pd.DataFrame, y: pd.Series, dates: pd.Series, alpha=RIDGE_ALPHA):
    y = pd.to_numeric(y, errors="coerce")
    valid = y.notna() & np.isfinite(x).all(axis=1)
    x = x.loc[valid]
    y = y.loc[valid].astype(float)
    dates = dates.loc[valid].astype(str)
    if len(y) < 100:
        raise RuntimeError(f"ranker training sample too small: {len(y)}")

    lo, hi = y.quantile([0.02, 0.98])
    yw = y.clip(lower=float(lo), upper=float(hi))
    counts = dates.value_counts()
    w = dates.map(lambda d: 1.0 / counts[d]).astype(float)
    w = w / w.mean()

    a = x.to_numpy(dtype=float)
    a = np.column_stack([np.ones(len(a)), a])
    sw = np.sqrt(w.to_numpy(dtype=float))[:, None]
    aw = a * sw
    bw = yw.to_numpy(dtype=float) * sw[:, 0]

    penalty = np.eye(a.shape[1]) * float(alpha)
    penalty[0, 0] = 0.0
    beta = np.linalg.solve(aw.T @ aw + penalty, aw.T @ bw)
    pred = a @ beta
    corr = pd.Series(pred).corr(pd.Series(y.to_numpy()), method="spearman")
    return {
        "columns": list(x.columns),
        "beta": beta,
        "clipLowPct": _round(lo),
        "clipHighPct": _round(hi),
        "trainCount": int(len(y)),
        "trainDayCount": int(dates.nunique()),
        "trainTargetMeanPct": _round(y.mean()),
        "trainWinsorizedMeanPct": _round(yw.mean()),
        "trainSpearman": _round(corr),
        "alpha": float(alpha),
    }


def _predict(df: pd.DataFrame, model):
    x = _design(df)
    x = x.reindex(columns=model["columns"], fill_value=0.0)
    a = np.column_stack([np.ones(len(x)), x.to_numpy(dtype=float)])
    return a @ model["beta"]


def _coefficients(model, topn=12):
    rows = []
    for name, coef in zip(model["columns"], model["beta"][1:]):
        rows.append({"feature": name, "coefficient": _round(coef)})
    rows.sort(key=lambda r: abs(r["coefficient"] or 0), reverse=True)
    return rows[:topn]


def _ranked_portfolio(candidates: pd.DataFrame, data: pd.DataFrame, costs: CostModel):
    c = candidates.copy()
    # Reuse the V1.3 portfolio engine while replacing its deterministic selection key.
    # All rows receive the same sector priority; lower synthetic riskSort means higher
    # trained score because _simulate_portfolio sorts risk ascending.
    c["sectorBucket"] = "UNKNOWN"
    c["initialRiskPct"] = -pd.to_numeric(c["rankScore"], errors="coerce").fillna(-999.0)
    return _simulate_portfolio(c, data, costs)


def _baseline_portfolio(candidates: pd.DataFrame, data: pd.DataFrame, costs: CostModel):
    return _simulate_portfolio(candidates, data, costs)


def _compact(stats):
    keep = [
        "totalReturnPct", "cagrPct", "maxDrawdownPct", "tradeCount", "winRate",
        "avgTradeNetReturnPct", "avgWinPct", "avgLossPct", "payoffRatio",
        "expectancyPct", "profitFactor", "maxTradeGainPct", "maxTradeLossPct",
        "maxConsecutiveLosses", "avgCapitalUtilizationPct",
    ]
    return {k: stats.get(k) for k in keep}


def build_ranking_summary(trades: pd.DataFrame, ext: pd.DataFrame, data: pd.DataFrame, generated_at: str, costs: CostModel | None = None):
    costs = costs or CostModel()
    merged = _merge_signal_features(trades, ext)
    t = _attach_trade_dates(merged, data)
    complete = t[(t.entryStatus == "ENTERED") & t.netReturnPct.notna() & t.entryDate.notna() & t.exitDate.notna()].copy()

    out_models = {}
    comparisons = {}

    for model_name, cohort_filter in MODEL_COHORTS.items():
        cohort = complete[cohort_filter(complete)].copy()
        out_models[model_name] = {}
        comparisons[model_name] = {}

        for strategy in STRATEGIES:
            s = cohort[cohort.strategy == strategy].copy()
            train = s[s.split.astype(str) == "TRAIN_2024"].copy()
            model = _weighted_ridge_fit(_design(train), train.netReturnPct, train.signalDate)
            s["rankScore"] = _predict(s, model)

            out_models[model_name][strategy] = {
                "trainCount": model["trainCount"],
                "trainDayCount": model["trainDayCount"],
                "trainTargetMeanPct": model["trainTargetMeanPct"],
                "trainWinsorizedMeanPct": model["trainWinsorizedMeanPct"],
                "trainSpearman": model["trainSpearman"],
                "alpha": model["alpha"],
                "targetClipPct": [model["clipLowPct"], model["clipHighPct"]],
                "topCoefficients": _coefficients(model),
            }

            comparisons[model_name][strategy] = {}
            scopes = {
                "FULL_RESEARCH_PERIOD": s,
                "TRAIN_2024": s[s.split.astype(str) == "TRAIN_2024"],
                "VALIDATION_2025": s[s.split.astype(str) == "VALIDATION_2025"],
                "TEST_2026_PLUS_DESCRIPTIVE": s[s.split.astype(str) == "TEST_2026_PLUS"],
            }
            for scope_name, g in scopes.items():
                baseline = _baseline_portfolio(g, data, costs)
                ranked = _ranked_portfolio(g, data, costs)
                comparisons[model_name][strategy][scope_name] = {
                    "baselineV13": _compact(baseline),
                    "trainedRanker": _compact(ranked),
                    "deltaTotalReturnPct": _round((ranked.get("totalReturnPct") or 0) - (baseline.get("totalReturnPct") or 0)),
                    "deltaMaxDrawdownPct": _round((ranked.get("maxDrawdownPct") or 0) - (baseline.get("maxDrawdownPct") or 0)),
                    "deltaExpectancyPct": _round((ranked.get("expectancyPct") or 0) - (baseline.get("expectancyPct") or 0)),
                }

    validation_ranking = []
    for model_name, strategies in comparisons.items():
        for strategy, scopes in strategies.items():
            v = scopes["VALIDATION_2025"]
            r = v["trainedRanker"]
            validation_ranking.append({
                "model": model_name,
                "strategy": strategy,
                "totalReturnPct": r.get("totalReturnPct"),
                "maxDrawdownPct": r.get("maxDrawdownPct"),
                "expectancyPct": r.get("expectancyPct"),
                "profitFactor": r.get("profitFactor"),
                "payoffRatio": r.get("payoffRatio"),
                "avgWinPct": r.get("avgWinPct"),
                "avgLossPct": r.get("avgLossPct"),
                "maxTradeGainPct": r.get("maxTradeGainPct"),
                "maxTradeLossPct": r.get("maxTradeLossPct"),
                "tradeCount": r.get("tradeCount"),
                "improvementVsV13ReturnPct": v.get("deltaTotalReturnPct"),
            })
    validation_ranking.sort(key=lambda r: (-999999 if r["totalReturnPct"] is None else r["totalReturnPct"]), reverse=True)

    return {
        "ok": True,
        "schemaVersion": 1,
        "version": VERSION,
        "generatedAt": generated_at,
        "purpose": "Train a point-in-time candidate ranking model on 2024 only, then test whether choosing limited portfolio slots improves 2025 portfolio economics. Win rate is secondary to total return, expectancy, payoff and drawdown.",
        "trainingPolicy": {
            "fitSplit": "TRAIN_2024 only",
            "validationSplit": "VALIDATION_2025",
            "test2026Usage": "descriptive only; already inspected in prior research and not a pristine holdout",
            "target": "strategy-specific netReturnPct after V1.2 trading friction",
            "dailyWeighting": "Each signal day has equal aggregate training weight so crowded days do not dominate.",
            "targetWinsorization": "2nd/98th percentiles computed from 2024 training only.",
            "ridgeAlpha": RIDGE_ALPHA,
            "automaticProductionChanges": False,
        },
        "featurePolicy": {
            "timing": "Only signal-day fields available after the close are used. Entry price, future OHLC, exit type and realized outcomes are excluded from ranking features.",
            "numericFeatures": NUMERIC_FEATURES,
            "numericTransform": "Within-signal-date percentile ranks centered at zero; missing values neutral at 0.5 percentile.",
            "categoricalFeatures": CATEGORICAL_LEVELS,
        },
        "portfolioPolicy": {
            "engine": "V1.3 portfolio engine",
            "maxConcurrentPositions": 5,
            "targetSlotPct": 20.0,
            "trainedSelection": "Higher predicted 2024-trained net-return score receives priority when capacity is constrained.",
            "baselineSelection": "Existing V1.3 strong-sector then lower-initial-risk deterministic priority.",
        },
        "models": out_models,
        "comparisons": comparisons,
        "validation2025Ranking": validation_ranking,
        "promotionGate": {
            "minimum": "A ranking rule is not eligible for production merely because it improves in-sample results. At minimum 2025 validation should show positive total return, positive expectancy, profit factor > 1, and materially tolerable drawdown versus baseline; forward Decision Ledger must still confirm it.",
            "automaticPromotion": False,
        },
        "limitations": [
            "Inherits V1.2/V1.3 survivorship bias, Yahoo data limitations and daily-OHLC path ambiguity.",
            "The model ranks mechanical EXTENDED research signals, not historical ChatGPT V3.3 A/B/C decisions.",
            "Linear ridge on percentile/categorical features is intentionally simple and interpretable; it is not claimed to be an optimal ML model.",
            "Feature discovery and prior analysis have already examined 2026, so forward Decision Ledger remains the clean out-of-sample check.",
        ],
    }
