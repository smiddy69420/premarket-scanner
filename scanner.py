# scanner.py — v4.3 Final with liquidity + expiration logic + cleaner reasons
import yfinance as yf
import pandas as pd
import numpy as np
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
import datetime as dt
import pytz
import requests

# Discord webhook (optional if posting from GitHub workflow)
WEBHOOK = None  # or set manually: "https://discord.com/api/webhooks/..."

# ==============================
# CONFIGURATION
# ==============================
UNIVERSE = [
    "SPY","QQQ","AAPL","MSFT","GOOGL","AMZN","META","NVDA","TSLA",
    "NFLX","ORCL","CRM","ADBE","COST","WMT","BA","PEP","KO","INTC",
    "SHOP","AMD","PYPL","V","MA","JPM","UNH","XOM","CVX","HD","CAT",
    "ABBV","LLY","GS","NKE","PLTR","SMCI","MRNA","T","VZ","DIS"
]

NY = pytz.timezone("America/New_York")
TARGET_EXP_MIN_DAYS = 5
TARGET_EXP_MAX_DAYS = 14

MIN_PRICE = 5
MIN_AVG_VOL = 2e6
MAX_SPREAD_PCT = 8.0
OPT_VOL_MIN = 300
OPT_OI_MIN = 500
RELAX_SPREAD_PCT = 12.0
RELAX_VOL_MIN = 100
RELAX_OI_MIN = 100


# ==============================
# HELPERS
# ==============================

def _to_float(x):
    try:
        return float(x.item() if hasattr(x, "item") else x)
    except:
        return float(x)

def now_et():
    return dt.datetime.now(NY)

def daily_liquidity_ok(tkr):
    try:
        d = yf.download(tkr, period="60d", interval="1d", progress=False)
        if d.empty: return False
        px = _to_float(d["Close"].iloc[-1])
        avgv = _to_float(d["Volume"].tail(20).mean())
        return px >= MIN_PRICE and avgv >= MIN_AVG_VOL
    except:
        return False

def add_indicators(df):
    close = df["Close"]
    vol = df["Volume"]
    df["EMA20"] = EMAIndicator(close, 20).ema_indicator()
    df["EMA50"] = EMAIndicator(close, 50).ema_indicator()
    macd = MACD(close, 26, 12, 9)
    df["MACD_H"] = macd.macd_diff()
    df["RSI"] = RSIIndicator(close, 14).rsi()
    df["VOL20"] = vol.rolling(20).mean()
    df["ATRp"] = (df["High"] - df["Low"]).rolling(14).mean() / close.rolling(14).mean() * 100
    return df

def news_score(tkr, n=10):
    try:
        news = yf.Ticker(tkr).news or []
        titles = [x.get("title","").lower() for x in news[:n]]
        pos = {"surge","beat","beats","strong","upgrade","record","growth","bull","rally","up"}
        neg = {"miss","misses","downgrade","weak","lawsuit","probe","fall","drop","down","cuts","cut"}
        score = sum(any(w in t for w in pos) for t in titles) - sum(any(w in t for w in neg) for t in titles)
        return score, (titles[0] if titles else "")
    except:
        return 0, ""

def score_row(r, nscore):
    trend = (2 if r["Close"] > r["EMA20"] > r["EMA50"] else -2 if r["Close"] < r["EMA20"] < r["EMA50"] else 0)
    momentum = 1 if r["MACD_H"] > 0 else -1
    rsi_adj = (r["RSI"] - 50) / 10
    vol_boost = 1.5 if r["Volume"] > 1.5 * r["VOL20"] else 0
    nsc = max(-2, min(2, nscore))
    return (trend * 2) + (momentum * 1.5) + rsi_adj + vol_boost + nsc

def bias_from_score(s): return "CALL" if s >= 0 else "PUT"

def levels_from_atr(price, atrp, bias):
    er = price * (atrp / 100) * 0.35
    tg = price * (atrp / 100) * 1.10
    st = price * (atrp / 100) * 0.70
    if bias == "CALL":
        return (round(price - er, 2), round(price + er, 2)), round(price + tg, 2), round(price - st, 2)
    else:
        return (round(price - er, 2), round(price + er, 2)), round(price - tg, 2), round(price + st, 2)

def nearest_exp(tkr):
    try:
        exps = yf.Ticker(tkr).options
        if not exps: return None
        today = dt.date.today()
        to_date = lambda s: dt.date(*map(int, s.split("-")))
        c = [((to_date(e) - today).days, e) for e in exps if to_date(e) >= today]
        c_in = [(d, e) for d, e in c if TARGET_EXP_MIN_DAYS <= d <= TARGET_EXP_MAX_DAYS]
        if c_in: return sorted(c_in)[0][1]
        return sorted(c)[0][1]
    except:
        return None

def pick_option_contract(tkr, bias, price):
    try:
        exp = nearest_exp(tkr)
        if not exp: return None
        ch = yf.Ticker(tkr).option_chain(exp)
        tbl = ch.puts if bias == "PUT" else ch.calls
        if tbl.empty: return None
        t = tbl.copy()
        t["mid"] = (t["bid"] + t["ask"]) / 2
        t = t[(t["mid"] > 0) & (t["ask"] >= t["bid"])]
        t["dist"] = abs(t["strike"] - price)
        t["spread_pct"] = (t["ask"] - t["bid"]) / t["mid"] * 100
        strict = t[(t["volume"] >= OPT_VOL_MIN) & (t["openInterest"] >= OPT_OI_MIN) & (t["spread_pct"] <= MAX_SPREAD_PCT)]
        relaxed = t[(t["volume"] >= RELAX_VOL_MIN) & (t["openInterest"] >= RELAX_OI_MIN) & (t["spread_pct"] <= RELAX_SPREAD_PCT)]
        use = strict if not strict.empty else relaxed
        if use.empty: return {"expiration": exp, "note": "No liquid ATM contract"}
        row = use.sort_values(["dist", "spread_pct"]).iloc[0]
        return {
            "expiration": exp,
            "contract": str(row.get("contractSymbol", "")),
            "strike": float(row["strike"]),
            "mid": round(float(row["mid"]), 2),
            "spread_pct": round(float(row["spread_pct"]), 1),
            "vol": int(row["volume"]),
            "oi": int(row["openInterest"])
        }
    except:
        return None


# ==============================
# MAIN SCAN FUNCTION
# ==============================

def run_scan(top_k=10):
    all_data = yf.download(" ".join(UNIVERSE), period="5d", interval="5m", group_by="ticker", progress=False)
    results = []
    for tkr in UNIVERSE:
        try:
            df = all_data[tkr]
            if df.empty: continue
            df = add_indicators(df).dropna()
            if df.empty or not daily_liquidity_ok(tkr): continue
            last = df.iloc[-1]
            price = _to_float(last["Close"])
            atrp = _to_float(last["ATRp"])
            nscore, ex = news_score(tkr)
            total = score_row(last, nscore)
            bias = bias_from_score(total)
            entry, target, stop = levels_from_atr(price, atrp, bias)

            reasons = []
            if last["Close"] > last["EMA20"] > last["EMA50"]:
                reasons.append("Uptrend (Close>EMA20>EMA50)")
            elif last["Close"] < last["EMA20"] < last["EMA50"]:
                reasons.append("Downtrend (Close<EMA20<EMA50)")
            else:
                reasons.append("Mixed trend")

            reasons.append("MACD momentum up" if last["MACD_H"] > 0 else "MACD momentum down")
            reasons.append(f"RSI {float(last['RSI']):.1f}")

            if last["Volume"] > 2 * last["VOL20"]:
                reasons.append("Unusual volume")
            elif last["Volume"] > 1.5 * last["VOL20"]:
                reasons.append("Volume > 1.5× avg")
            else:
                reasons.append("Moderate volume")

            if atrp > 3:
                reasons.append(f"High range {atrp:.1f}%")

            reasons.append(
                "News positive" if nscore > 0 else ("News negative" if nscore < 0 else "News neutral/low-signal")
            )
            if ex:
                reasons.append(f"News ex: {ex[:60]}…")

            opt = pick_option_contract(tkr, bias, price)
            exp = opt.get("expiration") if opt else None
            if not exp: continue

            if "contract" not in opt:
                opt["contract"] = ""
                opt["note"] = opt.get("note", "no pick")

            results.append({
                "Ticker": tkr,
                "Price": round(price, 2),
                "Score": round(total, 2),
                "Type": bias,
                "Target Expiration": exp,
                "Buy Range": f"${entry[0]}–${entry[1]}",
                "Sell Target": f"${target}",
                "Stop Idea": f"${stop}",
                "Risk": "High" if atrp >= 4 else "Medium",
                "Option Contract": opt["contract"],
                "Strike": opt.get("strike", ""),
                "Opt Mid": opt.get("mid", ""),
                "Spread %": opt.get("spread_pct", ""),
                "Opt Vol": opt.get("vol", ""),
                "Opt OI": opt.get("oi", ""),
                "Why": "; ".join(reasons),
                "Opt Note": opt.get("note", ""),
            })
        except Exception as e:
            print(f"[WARN] {tkr}: {e}")
            continue

    if not results:
        print("No valid signals.")
        return pd.DataFrame(), "No valid signals."

    df = pd.DataFrame(results)
    ranked = df.sort_values("Score", ascending=False).head(top_k)
    return ranked, f"Used data: period=5d, interval=5m. Top {top_k} ranked candidates."

# ==============================
# DISCORD POSTING (optional)
# ==============================

def post_to_discord(df, meta):
    if not WEBHOOK: 
        print("No Discord webhook set, skipping post.")
        return
    msg = f"**📢 Ranked Scan ({len(df)} picks)**\n"
    for _, r in df.iterrows():
        msg += (f"- **{r['Ticker']}** @ ${r['Price']} → **{r['Type']}** | "
                f"Exp: {r['Target Expiration']} | Buy: {r['Buy Range']} | "
                f"Target: {r['Sell Target']} | Stop: {r['Stop Idea']} | Risk: {r['Risk']}\n"
                f"   ↳ {r['Option Contract']} (strike {r['Strike']}, mid ~${r['Opt Mid']}, "
                f"spread ~{r['Spread %']}%, vol {r['Opt Vol']}, OI {r['Opt OI']})\n")
    requests.post(WEBHOOK, json={"content": msg})

# ==============================
# MAIN
# ==============================

if __name__ == "__main__":
    df, meta = run_scan()
    print(meta)
    if not df.empty:
        print(df)
        if WEBHOOK:
            post_to_discord(df, meta)

