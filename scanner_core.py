# scanner_core.py — shared scan logic (v4.4)
import logging
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

import yfinance as yf, pandas as pd, numpy as np
import datetime as dt, pytz
from ta.momentum import RSIIndicator
from ta.trend import MACD, EMAIndicator

NY = pytz.timezone("America/New_York")

UNIVERSE = [
    "SPY","QQQ","NVDA","TSLA","AAPL","MSFT","META","AMD","AMZN","GOOGL",
    "NFLX","AVGO","JPM","BA","SMCI","ORCL","CRM","ADBE","COST","WMT",
    "XOM","CVX","UNH","HD","KO","PEP","INTC","NKE","PYPL","MRNA",
    "T","V","MA","PLTR","MU","ABBV","LLY","UNP","CAT","GS","SHOP"
]

MIN_PRICE = 5.0
MIN_AVG_DAILY_VOL = 2e6

MAX_SPREAD_PCT = 8.0
OPT_VOL_MIN     = 300
OPT_OI_MIN      = 500
RELAX_SPREAD_PCT = 12.0
RELAX_VOL_MIN    = 100
RELAX_OI_MIN     = 100

TARGET_EXP_MIN_DAYS = 5
TARGET_EXP_MAX_DAYS = 14

ETF_TICKERS = {"SPY","QQQ","IWM","DIA","XLK","XLE","XLF","XLV","XLY","XLI","XLP","XLB","XLU","XLC"}
POS = {"surge","beat","beats","strong","upgrade","record","growth","bull","rally","up"}
NEG = {"miss","misses","downgrade","weak","lawsuit","probe","fall","drop","down","cuts","cut"}

def _to_float(x): 
    return float(x.item() if hasattr(x,"item") else x)

def dl_prices(tickers, period="5d", interval="5m"):
    return yf.download(" ".join(tickers), period=period, interval=interval,
                       auto_adjust=False, threads=True, group_by="ticker", progress=False)

def normalize(raw, tickers):
    out = {}
    if isinstance(raw, pd.DataFrame) and not isinstance(raw.columns, pd.MultiIndex):
        tkr = tickers[0]
        need=["Open","High","Low","Close","Volume"]
        if all(c in raw.columns for c in need):
            out[tkr] = raw[need].dropna().copy()
        return out
    if isinstance(raw, pd.DataFrame) and isinstance(raw.columns, pd.MultiIndex):
        for t in sorted({lv0 for lv0,_ in raw.columns}):
            df = raw[t].dropna().copy()
            need=["Open","High","Low","Close","Volume"]
            if all(c in df.columns for c in need):
                out[t] = df[need]
    return out

def safe_download(tickers):
    for period, interval in [("5d","5m"), ("15d","15m"), ("60d","1d")]:
        raw = dl_prices(tickers, period, interval)
        data = {k:v for k,v in normalize(raw, tickers).items() if not v.dropna().empty}
        if data:
            return data, period, interval
    return {}, None, None

def add_indicators(df):
    close, vol = df["Close"], df["Volume"]
    df["EMA20"] = EMAIndicator(close, 20).ema_indicator()
    df["EMA50"] = EMAIndicator(close, 50).ema_indicator()
    macd = MACD(close, 26, 12, 9)
    df["MACD_H"] = macd.macd_diff()
    df["RSI"] = RSIIndicator(close, 14).rsi()
    df["VOL20"] = vol.rolling(20).mean()
    df["ATRp"] = (df["High"]-df["Low"]).rolling(14).mean()/close.rolling(14).mean()*100
    return df

def news_score(tkr, n=12):
    try:
        ttl = [(x.get("title","") or "").lower() for x in (yf.Ticker(tkr).news or [])[:n]]
        return (sum(any(w in t for w in POS) for t in ttl)
               -sum(any(w in t for w in NEG) for t in ttl)), (ttl[0] if ttl else "")
    except:
        return 0, ""

def trading_days_between(a,b):
    if isinstance(a,dt.datetime): a=a.date()
    if isinstance(b,dt.datetime): b=b.date()
    if a>b: a,b=b,a
    return len(pd.bdate_range(a,b))-1

def is_etf(tkr):
    if tkr.upper() in ETF_TICKERS:
        return True
    try:
        info = yf.Ticker(tkr).info or {}
        return str(info.get("quoteType","")).upper()=="ETF"
    except:
        return False

def earnings_window_flag(tkr, window_days=3):
    try:
        if is_etf(tkr): return (False,"")
        tk = yf.Ticker(tkr)
        try:
            df = tk.get_earnings_dates(limit=6)
            if df is not None and not df.empty:
                dates = sorted([d.to_pydatetime().date() for d in df.index])
                today=dt.date.today()
                nearest=min(dates,key=lambda d:abs((d-today).days))
                return (trading_days_between(today,nearest)<=window_days, nearest.isoformat())
        except: pass
        try:
            cal = tk.calendar
            if cal is not None and not cal.empty:
                poss=[]
                for col in cal.columns:
                    for v in cal[col].values:
                        if isinstance(v,(pd.Timestamp,dt.datetime,dt.date)):
                            poss.append(v.date() if isinstance(v,(pd.Timestamp,dt.datetime)) else v)
                if poss:
                    today=dt.date.today()
                    nearest=min(poss,key=lambda d:abs((d-today).days))
                    return (trading_days_between(today,nearest)<=window_days, nearest.isoformat())
        except: pass
        return (False,"")
    except:
        return (False,"")

def daily_liquidity_ok(tkr):
    try:
        d=yf.download(tickers=tkr,period="60d",interval="1d",auto_adjust=False,progress=False)
        if d.empty: return False
        last = d.iloc[-1]
        px   = _to_float(last["Close"])
        avgv = _to_float(d["Volume"].tail(20).mean())
        return (px >= MIN_PRICE) and (avgv >= MIN_AVG_DAILY_VOL)
    except:
        return False

def score_row(row, nscore):
    trend = (2 if row["Close"]>row["EMA20"]>row["EMA50"] else -2 if row["Close"]<row["EMA20"]<row["EMA50"] else 0)
    momentum = 1 if row["MACD_H"]>0 else -1
    rsi_dist = (row["RSI"]-50)/10.0
    vsurge = (2 if row["Volume"]>2*row["VOL20"] else 1 if row["Volume"]>1.5*row["VOL20"] else 0)
    nsc = max(-2, min(2, nscore))
    return (trend*2) + (momentum*1.5) + rsi_dist + (vsurge*1.2) + nsc

def bias_from_score(s): 
    return "CALL" if s>=0 else "PUT"

def levels_from_atr(price, atrp, bias):
    er = price*(atrp/100)*0.35
    tg = price*(atrp/100)*1.10
    st = price*(atrp/100)*0.70
    if bias=="CALL":
        return (round(price-er,2), round(price+er,2)), round(price+tg,2), round(price-st,2)
    return (round(price-er,2), round(price+er,2)), round(price-tg,2), round(price+st,2)

def nearest_target_expiration(ticker, min_days=TARGET_EXP_MIN_DAYS, max_days=TARGET_EXP_MAX_DAYS):
    try:
        exps = yf.Ticker(ticker).options
        if not exps: return None
        today=dt.date.today()
        to_date=lambda s: dt.date(*map(int, s.split("-")))
        c=[((to_date(e)-today).days, e) for e in exps if to_date(e)>=today]
        c_in=[(d,e) for d,e in c if min_days<=d<=max_days]
        if c_in: return sorted(c_in)[0][1]
        return sorted(c)[0][1] if c else exps[0]
    except:
        return None

def pick_option_contract(ticker, bias, spot):
    try:
        exp = nearest_target_expiration(ticker)
        if not exp: return None
        chain = yf.Ticker(ticker).option_chain(exp)
        tbl = chain.puts if bias=="PUT" else chain.calls
        if tbl.empty: return None
        t=tbl.copy()
        t["mid"]=(t["bid"]+t["ask"])/2
        t=t[(t["mid"]>0) & (t["ask"]>=t["bid"])]
        t["dist"]=(t["strike"]-spot).abs()
        t["spread_pct"]=(t["ask"]-t["bid"])/t["mid"]*100
        strict=t[(t["volume"]>=OPT_VOL_MIN)&(t["openInterest"]>=OPT_OI_MIN)&(t["spread_pct"]<=MAX_SPREAD_PCT)]
        use = strict if not strict.empty else t[(t["volume"]>=RELAX_VOL_MIN)&(t["openInterest"]>=RELAX_OI_MIN)&(t["spread_pct"]<=RELAX_SPREAD_PCT)]
        if use.empty: return {"expiration":exp,"note":"No liquid ATM (strict or relaxed)"}
        row=use.sort_values(["dist","spread_pct"]).iloc[0]
        return {"expiration":exp,"contract":str(row.get("contractSymbol","")), "strike":float(row["strike"]),
                "bid":float(row["bid"]), "ask":float(row["ask"]),
                "mid":round(float((row["bid"]+row["ask"])/2),2),
                "spread_pct":round(float((row["ask"]-row["bid"])/((row["bid"]+row["ask"])/2))*100,1),
                "volume":int(row["volume"]), "openInterest":int(row["openInterest"])}
    except:
        return None

def run_scan(top_k=10):
    data, used_period, used_interval = safe_download(UNIVERSE)
    rows=[]
    for tkr, df in data.items():
        try:
            df=add_indicators(df).dropna()
            if df.empty or not daily_liquidity_ok(tkr):
                continue
            last=df.iloc[-1]
            price=_to_float(last["Close"]); atrp=_to_float(last["ATRp"])
            nsc, ex = news_score(tkr); 
            total = score_row(last,nsc); 
            bias  = bias_from_score(total)
            entry, target, stop = levels_from_atr(price, atrp, bias)
            earn_flag, earn_date = earnings_window_flag(tkr, 3)

            reasons=[]
            reasons.append("Uptrend (Close>EMA20>EMA50)" if last["Close"]>last["EMA20"]>last["EMA50"]
                           else ("Downtrend (Close<EMA20<EMA50)" if last["Close"]<last["EMA20"]<last["EMA50"] else "Mixed trend"))
            reasons.append("MACD momentum up" if last["MACD_H"]>0 else "MACD momentum down")
            reasons.append(f"RSI {float(last['RSI']):.1f}")
            if last["Volume"]>2*last["VOL20"]: reasons.append("Unusual volume")
            elif last["Volume"]>1.5*last["VOL20"]: reasons.append("Volume > 1.5× avg")
            else: reasons.append("Moderate volume")
            if atrp>3: reasons.append(f"High range ~{atrp:.1f}%")
            reasons.append("News positive" if nsc>0 else ("News negative" if nsc<0 else "News neutral/low-signal"))
            if ex: reasons.append(f"Ex: {ex[:60]}…")
            if earn_flag: reasons.append(f"Earnings window (±3d: {earn_date})")

            pick=pick_option_contract(tkr, bias, price)
            ok=False; exp="N/A"; note="no pick"
            sym=strike=mid=sp=ov=oi=""
            if pick:
                exp=pick.get("expiration","N/A")
                if "note" in pick: note=pick["note"]
                else:
                    sym=pick["contract"]; strike=round(pick["strike"],2)
                    mid=pick["mid"]; sp=pick["spread_pct"]; ov=pick["volume"]; oi=pick["openInterest"]; ok=True

            rows.append({"Ticker":tkr,"Price":round(price,2),"Type":bias,
                         "Target Expiration":exp,"Buy Range":f"${entry[0]}–${entry[1]}",
                         "Sell Target":f"${target}","Stop Idea":f"${stop}",
                         "Risk":("High" if (atrp>=4 or abs(nsc)>=2 or earn_flag) else "Medium"),
                         "Why":"; ".join(reasons),
                         "Option Contract":sym,"Strike":strike,"Opt Mid":mid,"Spread %":sp,
                         "Opt Vol":ov,"Opt OI":oi,"Opt Note":note,"ScoreAbs":abs(total),"ok_contract":ok})
        except:
            continue

    if not rows:
        return pd.DataFrame(), f"Used data: period={used_period}, interval={used_interval}. No candidates."

    df=pd.DataFrame(rows)
    good=df[df["ok_contract"]==True].copy()
    if good.empty:
        view=df.sort_values(["ScoreAbs","Risk"],ascending=[False,True]).head(top_k)
    else:
        view=good.sort_values(["ScoreAbs","Risk"],ascending=[False,True]).head(top_k)

    return view, f"Used data: period={used_period}, interval={used_interval}"
