import logging
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

import os, requests, datetime as dt, pytz, sys
import yfinance as yf
import pandas as pd
import numpy as np
from ta.momentum import RSIIndicator
from ta.trend import MACD, EMAIndicator

# ---------------- CONFIG ----------------
WEBHOOK = os.environ.get("DISCORD_WEBHOOK","")
NY = pytz.timezone("America/New_York")

MEGACAPS = [
    "SPY","QQQ","NVDA","TSLA","AAPL","MSFT","META","AMD","AMZN","GOOGL",
    "NFLX","AVGO","JPM","BA","SMCI","ORCL","CRM","ADBE","COST","WMT",
    "XOM","CVX","UNH","HD","KO","PEP","INTC","NKE","PYPL","MRNA",
    "T","V","MA","PLTR","MU","ABBV","LLY","UNP","CAT","GS","SHOP"
]

MIN_PRICE = 5.0
MIN_AVG_DAILY_VOL = 2e6

# Strict filters
MAX_SPREAD_PCT = 8.0
OPT_VOL_MIN     = 300
OPT_OI_MIN      = 500

# Relaxed fallback
RELAX_SPREAD_PCT = 12.0
RELAX_VOL_MIN    = 100
RELAX_OI_MIN     = 100

TARGET_EXP_MIN_DAYS = 5
TARGET_EXP_MAX_DAYS = 14
TOP_K = 10

ETF_TICKERS = {"SPY","QQQ","IWM","DIA","XLK","XLE","XLF","XLV","XLY","XLI","XLP","XLB","XLU","XLC"}

def ny_now(): return dt.datetime.now(tz=NY)

def should_run_now():
    """Gate to ~9:15am ET weekdays."""
    now = ny_now()
    if now.weekday() >= 5:
        return False
    mins = now.hour*60 + now.minute
    return 9*60 + 8 <= mins <= 9*60 + 22

def send_discord(msg):
    if not WEBHOOK:
        print("No webhook configured. Message:\n", msg)
        return
    for chunk in [msg[i:i+1800] for i in range(0, len(msg), 1800)]:
        requests.post(WEBHOOK, json={"content": chunk})

# ---------------- DOWNLOAD & NORMALIZE ----------------
def dl_prices(tickers, period="5d", interval="5m"):
    return yf.download(
        tickers=" ".join(tickers),
        period=period,
        interval=interval,
        auto_adjust=False,
        threads=True,
        group_by='ticker',
        progress=False
    )

def normalize(raw, tickers):
    out = {}
    if isinstance(raw, pd.DataFrame) and not isinstance(raw.columns, pd.MultiIndex):
        tkr = tickers[0]
        need = ["Open","High","Low","Close","Volume"]
        if all(c in raw.columns for c in need):
            out[tkr] = raw[need].dropna().copy()
        return out
    if isinstance(raw, pd.DataFrame) and isinstance(raw.columns, pd.MultiIndex):
        level0 = sorted({lv0 for lv0,_ in raw.columns})
        for t in level0:
            df = raw[t].dropna().copy()
            need = ["Open","High","Low","Close","Volume"]
            if all(c in df.columns for c in need):
                out[t] = df[need]
    return out

def safe_download(tickers):
    for period, interval in [("5d","5m"), ("15d","15m"), ("60d","1d")]:
        raw = dl_prices(tickers, period=period, interval=interval)
        data = normalize(raw, tickers)
        data = {k:v for k,v in data.items() if not v.dropna().empty}
        if data:
            return data, period, interval
    return {}, None, None

# ---------------- INDICATORS ----------------
def add_indicators(df):
    close = df['Close']; vol = df['Volume']
    df['EMA20'] = EMAIndicator(close, window=20).ema_indicator()
    df['EMA50'] = EMAIndicator(close, window=50).ema_indicator()
    macd = MACD(close, window_slow=26, window_fast=12, window_sign=9)
    df['MACD'] = macd.macd()
    df['MACD_SIG'] = macd.macd_signal()
    df['MACD_H'] = macd.macd_diff()
    df['RSI'] = RSIIndicator(close, window=14).rsi()
    df['VOL20'] = vol.rolling(20).mean()
    df['ATRp'] = (df['High']-df['Low']).rolling(14).mean()/close.rolling(14).mean()*100
    return df

# ---------------- NEWS & EARNINGS ----------------
POS = {"surge","beat","beats","strong","upgrade","record","growth","bull","rally","up"}
NEG = {"miss","misses","downgrade","weak","lawsuit","probe","fall","drop","down","cuts","cut"}

def news_score(tkr, n=12):
    try:
        news = yf.Ticker(tkr).news or []
        ttl = [x.get('title','').lower() for x in news[:n]]
        score = sum(any(w in t for w in POS) for t in ttl) - sum(any(w in t for w in NEG) for t in ttl)
        ex = ttl[0] if ttl else ""
        return score, ex
    except:
        return 0, ""

def trading_days_between(a, b):
    if isinstance(a, dt.datetime): a = a.date()
    if isinstance(b, dt.datetime): b = b.date()
    if a > b: a, b = b, a
    rng = pd.bdate_range(a, b)
    return len(rng) - 1

def is_etf(tkr):
    if tkr.upper() in ETF_TICKERS:
        return True
    try:
        info = yf.Ticker(tkr).info or {}
        return str(info.get("quoteType","")).upper() == "ETF"
    except Exception:
        return False

def earnings_window_flag(tkr, window_days=3):
    try:
        if is_etf(tkr):
            return (False, "")
        tk = yf.Ticker(tkr)
        try:
            df = tk.get_earnings_dates(limit=6)
            if df is not None and not df.empty:
                dates = sorted([d.to_pydatetime().date() for d in df.index])
                today = dt.date.today()
                nearest = min(dates, key=lambda d: abs((d - today).days))
                td = trading_days_between(today, nearest)
                return (td <= window_days, nearest.isoformat())
        except Exception:
            pass
        try:
            cal = tk.calendar
            if cal is not None and not cal.empty:
                poss = []
                for col in cal.columns:
                    for v in cal[col].values:
                        if isinstance(v, (pd.Timestamp, dt.datetime, dt.date)):
                            poss.append(v.date() if isinstance(v, (pd.Timestamp, dt.datetime)) else v)
                if poss:
                    today = dt.date.today()
                    nearest = min(poss, key=lambda d: abs((d - today).days))
                    td = trading_days_between(today, nearest)
                    return (td <= window_days, nearest.isoformat())
        except Exception:
            pass
        return (False, "")
    except Exception:
        return (False, "")

# ---------------- LIQUIDITY & SCORING ----------------
def _to_float(x): return float(x.item() if hasattr(x,"item") else x)

def daily_liquidity_ok(tkr):
    try:
        d = yf.download(tickers=tkr, period="60d", interval="1d", auto_adjust=False, progress=False)
        if d.empty: return False
        last = d.iloc[-1]
        px = _to_float(last["Close"])
        avgv = _to_float(d["Volume"].tail(20).mean())
        return (px >= MIN_PRICE) and (avgv >= MIN_AVG_DAILY_VOL)
    except:
        return False

def score_row(row, nscore):
    trend = (2 if row["Close"]>row["EMA20"]>row["EMA50"] else -2 if row["Close"]<row["EMA20"]<row["EMA50"] else 0)
    momentum = 1 if row["MACD_H"]>0 else -1
    rsi_dist = (row["RSI"]-50)/10.0
    vsurge = (2 if row["Volume"]>2.0*row["VOL20"] else 1 if row["Volume"]>1.5*row["VOL20"] else 0)
    nsc = max(-2, min(2, nscore))
    return (trend*2) + (momentum*1.5) + rsi_dist + (vsurge*1.2) + (nsc*1.0)

def bias_from_score(total): return "CALL" if total>=0 else "PUT"

def levels_from_atr(price, atrp, bias):
    atr_mult_entry, atr_mult_target, atr_mult_stop = 0.35, 1.10, 0.70
    er = price * (atrp/100.0) * atr_mult_entry
    tg = price * (atrp/100.0) * atr_mult_target
    st = price * (atrp/100.0) * atr_mult_stop
    if bias=="CALL":
        return (round(price-er,2), round(price+er,2)), round(price+tg,2), round(price-st,2)
    return (round(price-er,2), round(price+er,2)), round(price-tg,2), round(price+st,2)

# ---------------- EXPIRY & CONTRACT PICKER ----------------
def nearest_target_expiration(ticker, min_days=TARGET_EXP_MIN_DAYS, max_days=TARGET_EXP_MAX_DAYS):
    try:
        tk = yf.Ticker(ticker); exps = tk.options
        if not exps: return None
        today = dt.date.today()
        def to_date(s): y,m,d = map(int, s.split("-")); return dt.date(y,m,d)
        cands = []
        for e in exps:
            ed = to_date(e)
            if ed >= today:
                delta = (ed - today).days
                if min_days <= delta <= max_days:
                    cands.append((delta, e))
        if cands:
            cands.sort()
            return cands[0][1]
        fut = [(to_date(e), e) for e in exps if to_date(e) >= today]
        if fut:
            fut.sort(); return fut[0][1]
        return exps[0]
    except:
        return None

def pick_option_contract(ticker, bias, spot):
    try:
        exp = nearest_target_expiration(ticker)
        if not exp: return None
        chain = yf.Ticker(ticker).option_chain(exp)
        tbl = chain.puts if bias=="PUT" else chain.calls
        if tbl.empty: return None
        t = tbl.copy()
        t["dist"] = (t["strike"] - spot).abs()
        t["mid"]  = (t["bid"] + t["ask"]) / 2
        t = t[(t["mid"] > 0) & (t["ask"] >= t["bid"])]
        t["spread_pct"] = (t["ask"] - t["bid"]) / t["mid"] * 100

        t_strict = t[(t["volume"] >= OPT_VOL_MIN) & (t["openInterest"] >= OPT_OI_MIN) & (t["spread_pct"] <= MAX_SPREAD_PCT)]
        if not t_strict.empty:
            t_use = t_strict.sort_values(["dist","spread_pct"])
        else:
            t_relaxed = t[(t["volume"] >= RELAX_VOL_MIN) & (t["openInterest"] >= RELAX_OI_MIN) & (t["spread_pct"] <= RELAX_SPREAD_PCT)]
            if t_relaxed.empty:
                return {"expiration": exp, "note": "No liquid ATM (strict or relaxed)"}
            t_use = t_relaxed.sort_values(["dist","spread_pct"])

        row = t_use.iloc[0]
        return {
            "expiration": exp,
            "contract": str(row.get("contractSymbol","")),
            "strike": float(row["strike"]),
            "bid": float(row["bid"]),
            "ask": float(row["ask"]),
            "mid": round(float((row["bid"]+row["ask"])/2), 2),
            "spread_pct": round(float((row["ask"]-row["bid"])/((row["bid"]+row["ask"])/2))*100, 1),
            "volume": int(row["volume"]),
            "openInterest": int(row["openInterest"]),
        }
    except:
        return None

# ---------------- MAIN ----------------
def main():
    if not should_run_now():
        print("Not within pre-market window; exiting.")
        return

    data, used_period, used_interval = safe_download(MEGACAPS)
    if not data:
        send_discord("Premarket scan: data download failed.")
        return

    rows = []
    for tkr, df in data.items():
        try:
            df = add_indicators(df).dropna()
            if df.empty or not daily_liquidity_ok(tkr):
                continue
            last = df.iloc[-1]
            price = _to_float(last["Close"])
            atrp  = _to_float(last["ATRp"])
            nsc, ex = news_score(tkr)
            total = score_row(last, nsc)
            bias = bias_from_score(total)
            entry, target, stop = levels_from_atr(price, atrp, bias)
            earn_flag, earn_date = earnings_window_flag(tkr, window_days=3)

            reasons = []
            reasons.append("Uptrend (Close>EMA20>EMA50)" if last["Close"]>last["EMA20"]>last["EMA50"]
                           else ("Downtrend (Close<EMA20<EMA50)" if last["Close"]<last["EMA20"]<last["EMA50"] else "Mixed trend"))
            reasons.append("MACD momentum up" if last["MACD_H"]>0 else "MACD momentum down")
            reasons.append(f"RSI {float(last['RSI']):.1f}")
            if last["Volume"]>2*last["VOL20"]:
                reasons.append("Unusual volume")
            elif last["Volume"]>1.5*last["VOL20"]:
                reasons.append("Volume > 1.5× avg")
            else:
                reasons.append("Moderate volume")
            if atrp>3: reasons.append(f"High range ~{atrp:.1f}%")
            if nsc>0: reasons.append("News positive")
            elif nsc<0: reasons.append("News negative")
            else: reasons.append("News neutral/low-signal")
            if ex: reasons.append(f"Ex: {ex[:60]}…")
            if earn_flag: reasons.append(f"Earnings window (±3d: {earn_date})")

            pick = pick_option_contract(tkr, bias, price)
            ok_contract = False
            exp = "N/A"; opt_note = "no pick"
            c_symbol=c_strike=c_mid=c_spread=c_vol=c_oi=""

            if pick:
                exp = pick.get("expiration","N/A")
                if "note" in pick:
                    opt_note = pick["note"]
                else:
                    c_symbol = pick.get("contract","")
                    c_strike = round(pick.get("strike",0.0),2)
                    c_mid    = pick.get("mid","")
                    c_spread = pick.get("spread_pct","")
                    c_vol    = pick.get("volume","")
                    c_oi     = pick.get("openInterest","")
                    ok_contract = True

            rows.append({
                "Ticker": tkr, "Price": round(price,2), "Score": round(float(total),2), "Type": bias,
                "Target Expiration": exp, "Buy Range": f"${entry[0]}–${entry[1]}",
                "Sell Target": f"${target}", "Stop Idea": f"${stop}",
                "Risk": ("High" if (atrp>=4 or abs(nsc)>=2 or earn_flag) else "Medium"),
                "Why": "; ".join(reasons),
                "ok_contract": ok_contract,
                "Option Contract": c_symbol, "Strike": c_strike, "Opt Mid": c_mid, "Spread %": c_spread,
                "Opt Vol": c_vol, "Opt OI": c_oi, "Opt Note": opt_note
            })
        except Exception:
            continue

    if not rows:
        send_discord("**📢 Premarket Scan**\n_No candidates passed filters today._")
        return

    df = pd.DataFrame(rows)
    good = df[df["ok_contract"]==True].copy()
    if good.empty:
        df["absScore"] = df["Score"].abs()
        view = df.sort_values(["absScore","Risk"], ascending=[False, True]).drop(columns=["absScore"]).head(TOP_K)
    else:
        good["absScore"] = good["Score"].abs()
        view = good.sort_values(["absScore","Risk"], ascending=[False, True]).drop(columns=["absScore"]).head(TOP_K)

    lines = [f"**📢 Premarket Ranked Scan ({len(view)} picks)**"]
    for _, r in view.iterrows():
        core = (f"- **{r['Ticker']}** @ ${r['Price']} → **{r['Type']}** "
                f"| Exp: {r['Target Expiration']} | Buy: {r['Buy Range']} | Target: {r['Sell Target']} "
                f"| Stop: {r['Stop Idea']} | Risk: {r['Risk']} | Why: {r['Why']}")
        if r["Option Contract"]:
            core += (f"\n   ↳ {r['Option Contract']} (strike {r['Strike']}, mid ~${r['Opt Mid']}, "
                     f"spread ~{r['Spread %']}%, vol {r['Opt Vol']}, OI {r['Opt OI']})")
        elif r["Opt Note"]:
            core += f"\n   ↳ {r['Opt Note']}"
        lines.append(core)

    send_discord("\n".join(lines))

if __name__ == "__main__":
    main()
