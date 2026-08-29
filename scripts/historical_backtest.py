from __future__ import annotations

import json, os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from backtest_data import add_features, adjusted_prices, download_history, normalize_frame, security_master, yf_download
from backtest_model import grouped_stats, perf_stats, rnd, build_day

VERSION="historical-backtest-v1.0"
TZ=timezone(timedelta(hours=8))
START=os.getenv("BACKTEST_START","2024-01-01")
END=os.getenv("BACKTEST_END","").strip()
MIN_VALUE=float(os.getenv("BACKTEST_MIN_TRADE_VALUE","50000000"))
BATCH=int(os.getenv("BACKTEST_DOWNLOAD_BATCH","80"))
TOP=int(os.getenv("BACKTEST_TOP_LIMIT","50"))
HIST_TOP=int(os.getenv("BACKTEST_HISTORY_LIMIT","40"))
MIN_GROUP=int(os.getenv("BACKTEST_MIN_GROUP_SAMPLE","30"))
OUT=Path("data/backtest-summary.json")
SAMPLE=Path("data/backtest-signal-sample.json")


def benchmark(start,end):
    raw=yf_download(["0050.TW"],start,end)
    f=normalize_frame(raw,"0050.TW")
    if f.empty:raise RuntimeError("0050 benchmark unavailable")
    f=f.reset_index().rename(columns={f.index.name or "index":"date","Date":"date"})
    if "date" not in f:f=f.rename(columns={f.columns[0]:"date"})
    for k,v in {"symbol":"0050","name":"0050","market":"BENCH","industry":"BENCH"}.items():f[k]=v
    return add_features(adjusted_prices(f))


def main():
    start=pd.Timestamp(START).normalize();end=pd.Timestamp(END).normalize() if END else pd.Timestamp(datetime.now(TZ).date())
    if end<start:raise RuntimeError("BACKTEST_END before BACKTEST_START")
    fetch_start=(start-pd.Timedelta(days=120)).date().isoformat();fetch_end=(end+pd.Timedelta(days=7)).date().isoformat()
    tse=security_master(2,"TSE");otc=security_master(4,"OTC");items=list(tse.values())+list(otc.values())
    print(f"current universe {len(items)}; TSE={len(tse)} OTC={len(otc)}")
    raw,failed=download_history(items,fetch_start,fetch_end,BATCH)
    data=add_features(adjusted_prices(raw))
    actual=data[["market","symbol"]].drop_duplicates();coverage=len(actual)/len(items)
    if coverage<.70:raise RuntimeError(f"historical ticker coverage too low: {coverage:.1%}")
    b=benchmark(fetch_start,fetch_end);bm={r.date.date().isoformat():r for _,r in b.iterrows()}
    target=data[(data.date>=start)&(data.date<=end)&data.prev.notna()].copy()
    signals=[];days=[];sessions=sorted(target.date.unique())
    for i,dt in enumerate(sessions,1):
        if i==1 or i%25==0 or i==len(sessions):print(f"backtest {i}/{len(sessions)} {pd.Timestamp(dt).date()}")
        key=pd.Timestamp(dt).date().isoformat();br=bm.get(key);b5=np.nan if br is None else br.ret5;b20=np.nan if br is None else br.ret20
        ss,st=build_day(target[target.date==dt],b5,b20,MIN_VALUE,TOP,HIST_TOP)
        if st:signals.extend(ss);days.append(st)
    if not signals:raise RuntimeError("backtest produced no signals")
    f=pd.DataFrame(signals);d=pd.DataFrame(days);dv=f[f.source=="DISCOVERY_V2"]
    thresholds={}
    for sp,g in dv.groupby("split"):
        thresholds[sp]={}
        for t in [50,55,60,65,70,75,80,85]:
            x=g[g.score>=t]
            thresholds[sp][str(t)]={"recordCount":len(x),"1d":perf_stats(x,1),"3d":perf_stats(x,3),"5d":perf_stats(x,5)}
    summary={
      "ok":True,"schemaVersion":1,"version":VERSION,"generatedAt":datetime.now(timezone.utc).isoformat(),
      "period":{"requestedStart":START,"effectiveStart":days[0]["date"],"effectiveEnd":days[-1]["date"],"splits":{"TRAIN_2024":"2024","VALIDATION_2025":"2025","TEST_2026_PLUS":"2026+; reporting only"}},
      "coverage":{"currentUniverse":len(items),"TSE":len(tse),"OTC":len(otc),"tickersWithData":len(actual),"coverageRatio":rnd(coverage,4),"failedYahooCount":len(failed),"failedYahooSample":failed[:50],"rawRows":len(raw),"signalCount":len(signals),"minTradeValue":int(MIN_VALUE)},
      "dailyCoverage":{"sessionCount":len(d),"medianStocks":rnd(d.stockCount.median(),0),"medianEligible":rnd(d.eligibleCount.median(),0),"marketRegimeCounts":d.marketRegime.value_counts().to_dict()},
      "methodology":{"purpose":"Historical calibration of mechanical Screener/Discovery V2/setup/regime/sector skeleton; NOT historical full V3.3.","walkForward":"2024 train, 2025 validation, 2026+ untouched test reporting","priceBasis":"Yahoo OHLC scaled by Adj Close/Close; unadjusted volume","rrProxy":"Exploratory only: nearest MA20/prior10Low - 0.25 ATR vs prior60High; never production V3.3 R/R"},
      "knownBiases":["SURVIVORSHIP_BIAS: current security master; delisted historical names absent","CURRENT_INDUSTRY_LABELS reused historically","Yahoo/yfinance is not official archival bulk data","No historical fundamentals/catalysts/valuation/flows","Not a backfill of final V3.3 Opportunity or ChatGPT A/B/C decisions"],
      "overall":{src:{"recordCount":len(g),"1d":perf_stats(g,1),"3d":perf_stats(g,3),"5d":perf_stats(g,5)} for src,g in f.groupby("source")},
      "bySplit":grouped_stats(f,"split",MIN_GROUP),"bySetup":grouped_stats(f,"setup",MIN_GROUP),"byMarketRegime":grouped_stats(f,"marketRegime",MIN_GROUP),"byDiscoveryTier":grouped_stats(dv,"tier",MIN_GROUP),"byScreenerSignal":grouped_stats(f[f.source=="SCREENER"],"signals",MIN_GROUP,True),"discoveryScoreThresholdsBySplit":thresholds,
      "calibrationPolicy":{"automaticProductionChanges":False,"rule":"Only consider changes consistent in 2024 train and 2025 validation; never tune on 2026+ test; confirm with forward Decision Ledger."}
    }
    OUT.parent.mkdir(exist_ok=True);OUT.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    sample={"schemaVersion":1,"version":VERSION,"generatedAt":summary["generatedAt"],"first":f.head(40).to_dict("records"),"latest":f.tail(80).to_dict("records")}
    SAMPLE.write_text(json.dumps(sample,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"ok":True,"version":VERSION,"period":summary["period"],"coverage":summary["coverage"],"overall":summary["overall"]},ensure_ascii=False,indent=2))

if __name__=="__main__":main()
