# scanner.py
import os
import datetime as dt
from typing import List, Dict, Optional, Tuple

import numpy as np
import pandas as pd
import yfinance as yf
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator


# ----------------------------
# Config / Universe helpers
# ----------------------------

def get_universe_from_env() -> List[str]:
    """
    Read a comma-separated universe from env UNIVERSE_CSV, or use a reasonable default.
    You can set UNIVERSE_CSV in Render like: AAPL,MSFT,AMZN,GOOGL,META,NVDA,TSLA,AMD,BA,KO,PEP,WMT,ORCL,INTC,CRM,UNH,T
    """
    csv = os.environ.get("UNIVERSE_CSV", "")
    if csv.strip():
        u = [s.strip().upper() for s in csv.split(",") if s.strip()]
        return sorted(list(dict.fromkeys(u)))
    # Default large-cap core (stable + liquid)
    return [
        "AAPL","MSFT","AMZN","GOOGL","META","NVDA","TSLA","AMD","INTC","AVGO",
        "KO","PEP","WMT","COST","BA","ORCL","CRM","DIS","NFLX","T","V","MA",
        "JPM","BAC","XOM","CVX","WFC","HD","LOW","UNH","PFE","MRK","ABBV",
        "ADBE","SHOP","QCOM","PYPL","CSCO","IBM","NKE","MCD","GE","CAT",
    ]


# ----------------------------
# Data / Indicators
# ----------------------------

def fetch_price_history(ticker: str, period: str = "5d", interval: str = "5m") -> Optional[pd.DataFrame]:
    """
    Return OHLCV with a clean DatetimeIndex. None if empty.
    """
    try:
        df = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=False)
        if df is None or df.empty:
            return None
        # Normalize columns (yfinance sometimes gives lowercase)
        df = df.rename(columns={c: c.title() for c in df.columns})
        # Ensure expected columns exist
        for col in ["Open","High","Low","Close","Volume"]:
            if col not in df.columns:
                return None
        # Drop dupes, sort
        df = df[["Open","High","Low","Close","Volume"]].copy()
        df = df[~df.index.duplicated(keep="last")]
        df = df.sort_index()
        return df
    except Exception:
        return None


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add EMA20, EMA50, RSI, MACD and return same-length dataframe.
    """
    close = pd.Series(df["Close"].astype(float), index=df.index)
    ema20 = EMAIndicator(close=close, window=20, fillna=False).ema_indicator()
    ema50 = EMAIndicator(close=close, window=50, fillna=False).ema_indicator()

    macd = MACD(close=close, window_slow=26, window_fast=12, window_sign=9, fillna=False)
    rsi = RSIIndicator(close=close, window=14, fillna=False).rsi()

    df = df.copy()
    df["EMA20"] = ema20
    df["EMA50"] = ema50
    df["MACD"] = macd.macd()
    df["MACD_Signal"] = macd.macd_signal()
    df["MACD_Hist"] = macd.macd_diff()
    df["RSI"] = rsi
    return df


# ----------------------------
# Earnings (± window days)
# ----------------------------

def get_next_earnings_date(ticker: str) -> Optional[pd.Timestamp]:
    """
    Best-effort: yfinance get_earnings_dates can break for some tickers.
    Return a pandas Timestamp (UTC-naive) or None.
    """
    try:
        t = yf.Ticker(ticker)
        # yfinance has get_earnings_dates(limit=...) returning a df with index as dates
        df = t.get_earnings_dates(limit=8)
        if df is None or df.empty:
            return None
        # Choose nearest upcoming (>= today) else most recent
        today = dt.date.today()
        dates = [pd.Timestamp(d).date() for d in df.index.to_list()]
        upcoming = [d for d in dates if d >= today]
        chosen = (min(upcoming) if upcoming else max(dates))
        return pd.Timestamp(chosen)
    except Exception:
        return None


def earnings_watch_text(symbols: List[str], days_window: int = 7) -> str:
    """
    Return a friendly text block of tickers with earnings within ±days_window of today.
    """
    today = dt.date.today()
    start = today - dt.timedelta(days=days_window)
    end = today + dt.timedelta(days=days_window)

    lines = []
    for sym in symbols:
        ed = get_next_earnings_date(sym)
        if ed is None:
            continue
        d = ed.date()
        if start <= d <= end:
            sign = "🟢 Today" if d == today else ("🔜 Upcoming" if d > today else "🟡 Recent")
            lines.append(f"• **{sym}** — {d.isoformat()} ({sign})")

    if not lines:
        return f"No earnings within ±{days_window} days."
    lines.sort()
    return "**Earnings (±{0} days)**\n".format(days_window) + "\n".join(lines)


# ----------------------------
# Simple Scorer & Signal Builder
# ----------------------------

def last_row(df: pd.DataFrame) -> pd.Series:
    return df.iloc[-1]

def build_signal(ticker: str, df: pd.DataFrame) -> Optional[Dict]:
    """
    Build a single signal dict for the latest bar.
    CALL if Close > EMA20 > EMA50, PUT if Close < EMA20 < EMA50; else None.
    """
    try:
        row = last_row(df)
        c = float(row["Close"])
        ema20 = float(row["EMA20"])
        ema50 = float(row["EMA50"])
        rsi = float(row["RSI"])
        macd_hist = float(row["MACD_Hist"])

        # Trend logic
        if c > ema20 > ema50:
            bias = "CALL"
        elif c < ema20 < ema50:
            bias = "PUT"
        else:
            return None

        # Basic score (positive for CALL strength, negative for PUT strength)
        score = 0.0
        score += (abs(ema20 - ema50) / max(1e-9, ema50)) * 100
        score += (macd_hist * 10.0)
        score += (70 - abs(50 - rsi)) * 0.05  # RSI away from 50 adds a little conviction

        # Lightweight buy/target/stop around the last price
        # CALL: slightly above; PUT: slightly below
        if bias == "CALL":
            buy_low, buy_high = c - 0.10, c - 0.02
            target = c + 0.40
            stop = c - 0.25
        else:
            buy_low, buy_high = c + 0.02, c + 0.10
            target = c - 0.40
            stop = c + 0.25

        # Why string
        reasons = []
        if bias == "CALL":
            reasons.append("Uptrend (Close>EMA20>EMA50)")
            if macd_hist > 0: reasons.append("MACD momentum up")
        else:
            reasons.append("Downtrend (Close<EMA20<EMA50)")
            if macd_hist < 0: reasons.append("MACD momentum down")
        reasons.append(f"RSI {rsi:.1f}")

        return {
            "ticker": ticker,
            "price": c,
            "score": round(score, 2),
            "type": bias,
            "buy_range": (round(buy_low, 2), round(buy_high, 2)),
            "target": round(target, 2),
            "stop": round(stop, 2),
            "risk": "Medium",
            "why": "; ".join(reasons),
            # We keep options info optional; filled by try_fetch_option_block
        }
    except Exception:
        return None


def try_fetch_option_block(ticker: str, bias: str) -> Optional[Dict]:
    """
    Best-effort: fetch a nearby monthly contract and return a light liquidity/spread summary.
    If anything fails, return None (don’t crash scans).
    """
    try:
        t = yf.Ticker(ticker)
        exps = t.options
        if not exps:
            return None

        # Prefer the 3rd Friday ~monthly if available, else first listed
        # yfinance gives 'YYYY-MM-DD' strings
        chosen = None
        for e in exps:
            d = pd.Timestamp(e).date()
            # heuristic: pick the nearest >= 14 days out (gives options some time)
            if d >= (dt.date.today() + dt.timedelta(days=14)):
                chosen = e
                break
        if chosen is None:
            chosen = exps[0]

        chain = t.option_chain(chosen)
        if bias == "CALL":
            tbl = chain.calls
        else:
            tbl = chain.puts

        if tbl is None or tbl.empty:
            return None

        # Pick strike near the money (median by abs(strike - last_price))
        last_price = float(t.fast_info["last_price"]) if "last_price" in t.fast_info else None
        if last_price is None:
            # fallback to close from history
            h = fetch_price_history(ticker, period="1d", interval="1m")
            if h is None or h.empty:
                return None
            last_price = float(h["Close"].iloc[-1])

        tbl = tbl.copy()
        tbl["dist"] = (tbl["strike"] - last_price).abs()
        near = tbl.sort_values("dist").head(1).squeeze()

        # Compute mid, spread%, Vol, OI
        bid = float(near.get("bid", np.nan))
        ask = float(near.get("ask", np.nan))
        vol = int(near.get("volume", 0)) if not np.isnan(near.get("volume", np.nan)) else 0
        oi = int(near.get("openInterest", 0)) if not np.isnan(near.get("openInterest", np.nan)) else 0
        if np.isnan(bid) or np.isnan(ask) or bid <= 0 or ask <= 0:
            return None

        mid = round((bid + ask) / 2.0, 2)
        spread_pct = round(((ask - bid) / mid) * 100.0, 1) if mid > 0 else 0.0

        symbol = near.get("contractSymbol")
        strike = float(near.get("strike", 0.0))
        return {
            "expiry": chosen,
            "contract": symbol,
            "strike": strike,
            "mid": mid,
            "spread_pct": spread_pct,
            "volume": vol,
            "oi": oi
        }
    except Exception:
        return None


# ----------------------------
# Public API used by bot.py
# ----------------------------

def analyze_one_ticker(ticker: str, period: str = "5d", interval: str = "5m") -> Dict:
    """
    Analyze a single ticker and return a dict ready to embed.
    Raises Exception with a helpful message if something blocks analysis.
    """
    tk = ticker.upper().strip()
    df = fetch_price_history(tk, period=period, interval=interval)
    if df is None or df.empty:
        raise Exception("No price data returned")

    df = compute_indicators(df)
    sig = build_signal(tk, df)
    if not sig:
        raise Exception("No clear trend signal (not strictly up/down EMA stack)")

    # Attach earnings note
    ed = get_next_earnings_date(tk)
    if ed is not None:
        # ±7 days flag
        window = 7
        d = ed.date()
        today = dt.date.today()
        if abs((d - today).days) <= window:
            sig["risk"] = "High"
            sig.setdefault("why", "")
            sig["why"] += f"; Earnings window (±{window}d: {d.isoformat()})"

    # Try options block (best-effort)
    ob = try_fetch_option_block(tk, sig["type"])
    if ob:
        sig["options"] = ob

    return sig


def scan_universe_and_rank(universe: List[str], top_n: int = 10,
                           period: str = "5d", interval: str = "5m") -> List[Dict]:
    """
    Analyze a list of tickers, keep those with a clear bias, sort by |score| desc, return top_n
    """
    results = []
    for tk in universe:
        try:
            info = analyze_one_ticker(tk, period=period, interval=interval)
            results.append(info)
        except Exception:
            continue

    if not results:
        return []
    results.sort(key=lambda x: abs(x.get("score", 0.0)), reverse=True)
    return results[:top_n]


def signal_to_embed_fields(sig: Dict) -> Tuple[str, str]:
    """
    Return (title, body) for Discord embed.
    """
    t = sig["ticker"]
    c = sig["price"]
    bias = sig["type"]
    tgt = sig["target"]
    stp = sig["stop"]
    br_lo, br_hi = sig["buy_range"]
    risk = sig["risk"]
    why = sig["why"]

    title = f"{t} @ ${c:.2f} → {bias}"
    body = (
        f"**Buy Range:** ${br_lo:.2f} – ${br_hi:.2f}\n"
        f"**Target:** ${tgt:.2f}   |   **Stop:** ${stp:.2f}\n"
        f"**Risk:** {risk}\n"
        f"**Why:** {why}\n"
    )

    if "options" in sig:
        ob = sig["options"]
        body += (
            f"\n**Option Block**\n"
            f"• `{ob['contract']}` (strike {ob['strike']:.1f}, exp {ob['expiry']})\n"
            f"• Mid ~ ${ob['mid']:.2f}   |   Spread ~ {ob['spread_pct']:.1f}%\n"
            f"• Vol {ob['volume']}   |   OI {ob['oi']}\n"
        )
    return title, body
