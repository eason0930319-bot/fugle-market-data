from __future__ import annotations

import html, math, re, time
import numpy as np
import pandas as pd
import requests
import yfinance as yf

ISIN = "https://isin.twse.com.tw/isin/C_public.jsp"
S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 fugle-market-data-backtest/1.1"})


def security_master(mode: int, market: str):
    r = S.get(ISIN, params={"strMode": mode}, timeout=35)
    r.raise_for_status()
    text = r.content.decode("big5", errors="replace")
    out = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", text, flags=re.I | re.S):
        cells = []
        for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.I | re.S):
            cell = html.unescape(re.sub(r"<[^>]+>", "", td))
            cells.append(re.sub(r"\s+", " ", cell).strip())
        if len(cells) < 6:
            continue
        m = re.match(r"^(\d{4})\s+(.+)$", cells[0])
        if not m:
            continue
        cfi = cells[5].strip().upper()
        if not re.fullmatch(r"ES[A-Z0-9]{4}", cfi):
            continue
        symbol = m.group(1)
        out[symbol] = {
            "symbol": symbol,
            "name": m.group(2).strip(),
            "market": market,
            "industry": cells[4].strip() or "UNKNOWN",
            "ticker": f"{symbol}.TW" if market == "TSE" else f"{symbol}.TWO",
        }
    if not out:
        raise RuntimeError(f"empty security master: {market}")
    return out


def yf_download(tickers, start, end):
    return yf.download(
        tickers=tickers,
        start=start,
        end=end,
        auto_adjust=False,
        actions=False,
        group_by="ticker",
        threads=True,
        progress=False,
        timeout=30,
        multi_level_index=True,
    )


def normalize_frame(raw: pd.DataFrame, ticker: str):
    if raw is None or raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        l0 = set(map(str, raw.columns.get_level_values(0)))
        l1 = set(map(str, raw.columns.get_level_values(1)))
        if ticker in l0:
            f = raw[ticker].copy()
        elif ticker in l1:
            f = raw.xs(ticker, axis=1, level=1).copy()
        else:
            return pd.DataFrame()
    else:
        f = raw.copy()
    cols = {str(c).lower().replace(" ", ""): c for c in f.columns}
    required = {}
    for target, alias in [("Open", "open"), ("High", "high"), ("Low", "low"), ("Close", "close"), ("Volume", "volume")]:
        if alias not in cols:
            return pd.DataFrame()
        required[target] = cols[alias]
    adj = cols.get("adjclose")
    z = pd.DataFrame(index=pd.to_datetime(f.index).tz_localize(None))
    for target, source in required.items():
        z[target] = pd.to_numeric(f[source], errors="coerce")
    z["Adj Close"] = pd.to_numeric(f[adj], errors="coerce") if adj is not None else z["Close"]
    return z[~z.index.duplicated(keep="last")].sort_index()


def _attach(frame, meta):
    frame = frame.reset_index().rename(columns={frame.index.name or "index": "date", "Date": "date"})
    if "date" not in frame:
        frame = frame.rename(columns={frame.columns[0]: "date"})
    for key in ("symbol", "name", "market", "industry"):
        frame[key] = meta[key]
    return frame


def download_history(items, start, end, batch_size=80):
    meta = {x["ticker"]: x for x in items}
    tickers = sorted(meta)
    frames, failed = [], []
    total = math.ceil(len(tickers) / batch_size)
    for offset in range(0, len(tickers), batch_size):
        batch = tickers[offset:offset + batch_size]
        print(f"download {offset // batch_size + 1}/{total}")
        try:
            raw = yf_download(batch, start, end)
        except Exception:
            raw = pd.DataFrame()
        missing = []
        for ticker in batch:
            f = normalize_frame(raw, ticker)
            if f.empty:
                missing.append(ticker)
            else:
                frames.append(_attach(f, meta[ticker]))
        for ticker in missing:
            try:
                f = normalize_frame(yf_download([ticker], start, end), ticker)
            except Exception:
                f = pd.DataFrame()
            if f.empty:
                failed.append(ticker)
            else:
                frames.append(_attach(f, meta[ticker]))
        time.sleep(0.25)
    if not frames:
        raise RuntimeError("Yahoo returned no usable history")
    d = pd.concat(frames, ignore_index=True)
    d["date"] = pd.to_datetime(d["date"]).dt.tz_localize(None)
    d = d.drop_duplicates(["market", "symbol", "date"], keep="last")
    return d.sort_values(["market", "symbol", "date"]), sorted(set(failed))


def adjusted_prices(d):
    d = d.copy()
    close = pd.to_numeric(d["Close"], errors="coerce")
    adj = pd.to_numeric(d["Adj Close"], errors="coerce")
    factor = (adj / close).replace([np.inf, -np.inf], np.nan).fillna(1.0)
    for src, dst in [("Open", "open"), ("High", "high"), ("Low", "low")]:
        d[dst] = pd.to_numeric(d[src], errors="coerce") * factor
    d["close"] = adj.where(adj > 0, close)
    d["volume"] = pd.to_numeric(d["Volume"], errors="coerce")
    d["tradeValue"] = d["close"] * d["volume"]
    cols = ["date", "symbol", "name", "market", "industry", "open", "high", "low", "close", "volume", "tradeValue"]
    return d[cols].dropna(subset=["close"])


def add_features(d):
    d = d.sort_values(["market", "symbol", "date"]).copy()
    g = d.groupby(["market", "symbol"], sort=False)
    d["prev"] = g["close"].shift(1)
    d["chg"] = (d["close"] / d["prev"] - 1) * 100
    d["gapPct"] = (d["open"] / d["prev"] - 1) * 100
    d["bodyPct"] = (d["close"] / d["open"] - 1) * 100
    d["rangePct"] = (d["high"] - d["low"]) / d["prev"] * 100
    d["upperWickPct"] = (d["high"] - pd.concat([d["open"], d["close"]], axis=1).max(axis=1)) / d["prev"] * 100
    d["lowerWickPct"] = (pd.concat([d["open"], d["close"]], axis=1).min(axis=1) - d["low"]) / d["prev"] * 100
    day_range = d["high"] - d["low"]
    d["cp"] = ((d["close"] - d["low"]) / day_range).where(day_range > 0, 0.5)
    d["lowdd"] = (d["low"] / d["prev"] - 1) * 100
    d["rebound"] = (d["close"] / d["low"] - 1) * 100
    d["ret5"] = g["close"].pct_change(5, fill_method=None) * 100
    d["ret20"] = g["close"].pct_change(20, fill_method=None) * 100
    d["ma20"] = g["close"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    d["ma20Prev5"] = g["close"].transform(lambda s: s.rolling(20, min_periods=20).mean().shift(5))
    d["ma20Slope5Pct"] = (d["ma20"] / d["ma20Prev5"] - 1) * 100
    d["av20"] = g["volume"].transform(lambda s: s.shift(1).rolling(20, min_periods=20).mean())
    d["h20"] = g["high"].transform(lambda s: s.shift(1).rolling(20, min_periods=20).max())
    d["h60"] = g["high"].transform(lambda s: s.shift(1).rolling(60, min_periods=40).max())
    d["l10"] = g["low"].transform(lambda s: s.shift(1).rolling(10, min_periods=8).min())
    d["rvol20"] = d["volume"] / d["av20"]
    d["distH"] = (d["close"] / d["h20"] - 1) * 100
    d["distMA"] = (d["close"] / d["ma20"] - 1) * 100
    tr = pd.concat([(d["high"]-d["low"]).abs(), (d["high"]-d["prev"]).abs(), (d["low"]-d["prev"]).abs()], axis=1).max(axis=1)
    d["atr14"] = tr.groupby([d["market"], d["symbol"]]).transform(lambda s: s.rolling(14, min_periods=14).mean())
    for i in range(1, 6):
        d[f"fc{i}"] = g["close"].shift(-i)
        d[f"fh{i}"] = g["high"].shift(-i)
        d[f"fl{i}"] = g["low"].shift(-i)
    for h in (1, 3, 5):
        d[f"r{h}"] = (d[f"fc{h}"] / d["close"] - 1) * 100
        d[f"mfe{h}"] = (d[[f"fh{i}" for i in range(1, h+1)]].max(axis=1, skipna=False) / d["close"] - 1) * 100
        d[f"mae{h}"] = (d[[f"fl{i}" for i in range(1, h+1)]].min(axis=1, skipna=False) / d["close"] - 1) * 100
    return d
