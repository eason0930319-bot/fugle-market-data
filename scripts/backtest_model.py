from __future__ import annotations

import math
import numpy as np
import pandas as pd


def rnd(x, n=3):
    try:
        x=float(x); return round(x,n) if math.isfinite(x) else None
    except Exception:return None


def percentile(s):
    out=pd.Series(np.nan,index=s.index,dtype=float); valid=s.notna(); n=int(valid.sum())
    if n==0:return out
    if n==1:out.loc[valid]=100;return out
    out.loc[valid]=(s.loc[valid].rank(method="max")-1)/(n-1)*100
    return out


def setup(row, rs20=None):
    ch=0 if pd.isna(row.chg) else row.chg; cp=.5 if pd.isna(row.cp) else row.cp; rs=0 if rs20 is None or pd.isna(rs20) else rs20
    if ch>=7 or (pd.notna(row.distMA) and row.distMA>=14):return "EXTENDED"
    if pd.notna(row.distH) and row.distH>=-1 and (0 if pd.isna(row.rvol20) else row.rvol20)>=1.15 and cp>=.70 and rs>0:return "BREAKOUT"
    if pd.notna(row.ret20) and row.ret20>3 and pd.notna(row.distMA) and -1.5<=row.distMA<=6 and pd.notna(row.distH) and -12<=row.distH<=-2 and pd.notna(row.ret5) and -6<=row.ret5<=2.5:return "PULLBACK"
    if pd.notna(row.ret5) and row.ret5<=2 and ch>0 and cp>=.75 and pd.notna(row.distMA) and row.distMA>=-2.5:return "REVERSAL"
    if pd.notna(row.ret20) and row.ret20>4 and rs>0 and pd.notna(row.distMA) and row.distMA>0 and cp>=.55:return "TREND_MOMENTUM"
    return "GENERAL_WATCH"


def regime(advance_ratio, median_change):
    if pd.isna(advance_ratio) or pd.isna(median_change):return "UNKNOWN"
    if advance_ratio>=.55 and median_change>0:return "BULL"
    if advance_ratio<=.40 and median_change<0:return "BEAR"
    return "NEUTRAL"


def rr_proxy(row):
    if pd.isna(row.atr14) or row.atr14<=0:return None
    supports=[x for x in (row.ma20,row.l10) if pd.notna(x) and x<row.close]
    if not supports or pd.isna(row.h60) or row.h60<=row.close:return None
    stop=max(supports)-.25*row.atr14; risk=row.close-stop; reward=row.h60-row.close
    return reward/risk if stop>0 and risk>0 and reward>0 else None


def _outcomes(row):
    return {"close":rnd(row.close,4),"ret1dPct":rnd(row.r1),"ret3dPct":rnd(row.r3),"ret5dPct":rnd(row.r5),
            "mfe1dPct":rnd(row.mfe1),"mae1dPct":rnd(row.mae1),"mfe3dPct":rnd(row.mfe3),"mae3dPct":rnd(row.mae3),"mfe5dPct":rnd(row.mfe5),"mae5dPct":rnd(row.mae5)}


def split_name(dt):
    return "TRAIN_2024" if dt.year<=2024 else ("VALIDATION_2025" if dt.year==2025 else "TEST_2026_PLUS")


def build_day(day, benchmark_return5, benchmark_return20, min_trade_value=50_000_000, top_limit=50, history_limit=40):
    day=day.dropna(subset=["prev","chg"]).copy()
    if len(day)<700:return [],None
    medm=day.groupby("market")["chg"].median().to_dict()
    day["rs1"]=day.apply(lambda r:r.chg-medm.get(r.market,np.nan),axis=1)
    for col,name in [("tradeValue","liqP"),("chg","chgP"),("rs1","rsP"),("rebound","rebP")]:day[name]=percentile(day[col])
    eligible=day[(day.tradeValue>=min_trade_value)&day.close.notna()].copy()
    ar=(day.chg>.001).mean(); med=day.chg.median(); market_regime=regime(ar,med)

    sectors=[]
    for industry,members in day.groupby("industry"):
        if industry=="UNKNOWN" or len(members)<3:continue
        changes=members.chg.dropna()
        if changes.empty:continue
        sectors.append({"industry":industry,"med":changes.median(),"ar":(changes>.001).mean(),"vps":members.tradeValue.fillna(0).sum()/len(members)})
    sdf=pd.DataFrame(sectors); sector_map={}; top_sectors=set()
    if not sdf.empty:
        sdf["mp"]=percentile(sdf.med);sdf["vp"]=percentile(sdf.vps);sdf["ss"]=(sdf.mp*.55+sdf.ar*100*.25+sdf.vp*.20).clip(0,100)
        sdf=sdf.sort_values("ss",ascending=False);sector_map=sdf.set_index("industry").ss.to_dict();top_sectors=set(sdf.head(10).industry)

    eligible["mom"]=(eligible.chgP.fillna(0)*.35+eligible.rsP.fillna(0)*.25+eligible.cp.fillna(.5)*100*.20+eligible.liqP.fillna(0)*.20)
    eligible["rec"]=(eligible.rebP.fillna(0)*.35+eligible.cp.fillna(.5)*100*.25+eligible.liqP.fillna(0)*.20+eligible.rsP.fillna(0)*.20)
    eligible["ss"]=eligible.industry.map(sector_map);eligible["sl"]=(eligible.ss.fillna(0)*.40+eligible.rsP.fillna(0)*.25+eligible.liqP.fillna(0)*.20+eligible.cp.fillna(.5)*100*.15)
    buckets=[(eligible[(eligible.chg>=.8)&(eligible.cp>=.65)].nlargest(30,"mom"),"MOMENTUM","mom"),
             (eligible[(eligible.lowdd<=-1.5)&(eligible.cp>=.65)&(eligible.rebound>=1.2)].nlargest(30,"rec"),"RECOVERY","rec"),
             (eligible[eligible.industry.isin(top_sectors)&(eligible.rs1>0)].nlargest(30,"sl"),"SECTOR_LEADER","sl")]
    merged={}
    for frame,signal,score_col in buckets:
        for _,r in frame.iterrows():
            key=(r.market,r.symbol)
            if key not in merged:merged[key]=[r,float(r[score_col]),[signal]]
            else:
                merged[key][1]=max(merged[key][1],float(r[score_col]))
                if signal not in merged[key][2]:merged[key][2].append(signal)
    screener=sorted(merged.values(),key=lambda x:x[1],reverse=True)[:top_limit]

    day["cheap"]=(percentile(day.tradeValue)*.40+percentile(day.chg)*.35+day.cp.fillna(.5).clip(0,1)*100*.25)
    ordered=day.sort_values("cheap",ascending=False);extcap=min(8,max(2,history_limit//5))
    selected=pd.concat([ordered[ordered.chg.fillna(0)<7].head(history_limit-extcap),ordered[ordered.chg.fillna(0)>=7].head(extcap)]).sort_values("cheap",ascending=False).copy()
    selected["rs5"]=selected.ret5-benchmark_return5;selected["rs20"]=selected.ret20-benchmark_return20
    selected["setup"]=[setup(r,r.rs20) for _,r in selected.iterrows()]
    selected["p5"]=percentile(selected.rs5);selected["p20"]=percentile(selected.rs20);selected["prv"]=percentile(selected.rvol20)
    selected["prox"]=selected.distH.map(lambda x:0 if pd.isna(x) else (100 if x>=0 else max(0,100+x*(100/12))))
    bonus={"BREAKOUT":5,"PULLBACK":5,"REVERSAL":3,"TREND_MOMENTUM":3,"GENERAL_WATCH":0,"EXTENDED":-12}
    selected["score"]=(selected.cheap*.20+selected.p5.fillna(0)*.20+selected.p20.fillna(0)*.25+selected.prv.fillna(0)*.20+selected.prox.fillna(0)*.15+selected.setup.map(bonus)).clip(0,100)
    selected["tier"]=["A" if r.setup in {"BREAKOUT","PULLBACK"} and r.score>=70 else ("B" if r.setup!="EXTENDED" and r.score>=60 else "C") for _,r in selected.iterrows()]

    dt=day.date.iloc[0]; signals=[]
    for rank,(r,score,signal_names) in enumerate(screener,1):
        rs20=(r.ret20-benchmark_return20) if pd.notna(r.ret20) and pd.notna(benchmark_return20) else None
        signals.append({"source":"SCREENER","signalDate":dt.date().isoformat(),"split":split_name(dt),"rank":rank,"symbol":r.symbol,"name":r.name,"market":r.market,"industry":r.industry,"signals":signal_names,"score":rnd(score,2),"tier":None,"setup":setup(r,rs20),"marketRegime":market_regime,"sectorStrengthScore":rnd(sector_map.get(r.industry),2),"rvol20":rnd(r.rvol20),"distanceFromMA20Pct":rnd(r.distMA),"distanceFrom20DHighPct":rnd(r.distH),"rrProxy":rnd(rr_proxy(r),3),**_outcomes(r)})
    for rank,(_,r) in enumerate(selected.sort_values("score",ascending=False).iterrows(),1):
        signals.append({"source":"DISCOVERY_V2","signalDate":dt.date().isoformat(),"split":split_name(dt),"rank":rank,"symbol":r.symbol,"name":r.name,"market":r.market,"industry":r.industry,"signals":["HISTORY_ENRICHED"],"score":rnd(r.score,2),"tier":r.tier,"setup":r.setup,"marketRegime":market_regime,"sectorStrengthScore":rnd(sector_map.get(r.industry),2),"rvol20":rnd(r.rvol20),"distanceFromMA20Pct":rnd(r.distMA),"distanceFrom20DHighPct":rnd(r.distH),"rrProxy":rnd(rr_proxy(r),3),**_outcomes(r)})
    day_stats={"date":dt.date().isoformat(),"marketRegime":market_regime,"stockCount":len(day),"eligibleCount":len(eligible),"advanceRatio":rnd(ar,4),"medianChangePct":rnd(med),"topSector":None if sdf.empty else sdf.iloc[0].industry,"topSectorStrength":None if sdf.empty else rnd(sdf.iloc[0].ss,1)}
    return signals,day_stats


def perf_stats(df,h=5):
    key=f"ret{h}dPct";v=df[df[key].notna()] if key in df else pd.DataFrame()
    if v.empty:return {"count":0,"avgReturnPct":None,"medianReturnPct":None,"winRate":None,"avgMfePct":None,"avgMaePct":None}
    return {"count":int(len(v)),"avgReturnPct":rnd(v[key].mean()),"medianReturnPct":rnd(v[key].median()),"winRate":rnd((v[key]>0).mean(),4),"avgMfePct":rnd(v[f"mfe{h}dPct"].mean()),"avgMaePct":rnd(v[f"mae{h}dPct"].mean())}


def grouped_stats(df,key,min_group=30,explode=False):
    if explode:df=df.explode(key)
    out={}
    for name,g in df.groupby(key,dropna=False):
        s={"recordCount":int(len(g)),"1d":perf_stats(g,1),"3d":perf_stats(g,3),"5d":perf_stats(g,5)}
        if max(s["1d"]["count"],s["3d"]["count"],s["5d"]["count"])>=min_group:out["UNKNOWN" if pd.isna(name) else str(name)]=s
    return out
