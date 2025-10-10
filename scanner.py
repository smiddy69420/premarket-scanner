# scanner.py
import datetime as dt
from typing import Tuple, Optional, Dict, Any, List

import numpy as np
import pandas as pd
import yfinance as yf
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator


# ---------- Utilities ----------

def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [' '.join([str(x) for x in tup if x is not None]).strip() for tup in df.columns]
    return df


def _standardize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    df = _flatten_columns(df).copy()
    cols = {c.lower().strip(): c for c in df.columns}
    # Map common variations
    if 'close' not in cols and 'adj close' in cols:
        df['Close'] = df[cols['adj close']]
    elif 'close' in cols:
        df['Close'] = df[cols['close']]

    if 'open' in cols:   df['Open']   = df[cols['open']]
    if 'high' in cols:   df['High']   = df[cols['high']]
    if 'low' in cols:    df['Low']    = df[cols['low']]
    if 'volume' in cols: df['Volume'] = df[cols['volume']]

    needed = ['Open', 'High', 'Low', 'Close', 'Volume']
    if not set(needed).issubset(df.columns):
        return pd.DataFrame()

    return df[needed].dropna(how='any')


def fetch_price_df(symbol: str, auto_adjust: bool = False) -> pd.DataFrame:
    """
    Robust yfinance fetch with multiple fallbacks:
    1) 1d/1m → 2) 5d/5m → 3) 30d/1h → 4) 1y/1d
    """
    symbol = symbol.upper().strip()
    t = yf.Ticker(symbol)
    attempts: List[Tuple[str, str]] = [
        ("1d",  "1m"),
        ("5d",  "5m"),
        ("30d", "1h"),
        ("1y",  "1d"),
    ]
    for period, interval in attempts:
        try:
            df = t.history(period=period, interval=interval, auto_adjust=auto_adjust,
                           prepost=True, actions=False)
            df = _standardize_ohlcv(df)
            if not df.empty and df['Close'].ndim == 1:
                return df
        except Exception:
            continue
    return pd.DataFrame()


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    n = len(out)

    ema20_win = min(20, max(2, n // 6))
    ema50_win = min(50, max(3, n // 3))
    macd_fast = 12 if n >= 12 else max(2, n // 4)
    macd_slow = 26 if n >= 26 else max(3, n // 3)
    macd_sig  = 9  if n >= 9  else max(2, n // 5)

    out['EMA20'] = EMAIndicator(close=out['Close'], window=ema20_win).ema_indicator()
    out['EMA50'] = EMAIndicator(close=out['Close'], window=ema50_win).ema_indicator()

    macd = MACD(close=out['Close'], window_slow=macd_slow, window_fast=macd_fast, window_sign=macd_sig)
    out['MACD'] = macd.macd()
    out['MACD_SIG'] = macd.macd_signal()

    out['RSI'] = RSIIndicator(close=out['Close'], window=min(14, max(2, n // 8))).rsi()
    return out.dropna(how='any')


def classify_signal(df: pd.DataFrame) -> Tuple[str, List[str]]:
    if df.empty:
        return "NEUTRAL", ["No data"]
    last = df.iloc[-1]
    close, ema20, ema50 = last['Close'], last['EMA20'], last['EMA50']
    macd, macd_sig = last['MACD'], last['MACD_SIG']
    rsi = float(last['RSI'])

    reasons = []
    if close > ema20 > ema50:
        trend = "Uptrend";   reasons.append("Close > EMA20 > EMA50")
    elif close < ema20 < ema50:
        trend = "Downtrend"; reasons.append("Close < EMA20 < EMA50")
    else:
        trend = "Sideways";  reasons.append("Mixed EMAs")

    reasons.append("MACD momentum up" if macd > macd_sig else "MACD momentum down")
    reasons.append(f"RSI {rsi:.1f}")

    if trend == "Uptrend" and macd > macd_sig and rsi >= 50:
        sig = "CALL"
    elif trend == "Downtrend" and macd < macd_sig and rsi <= 50:
        sig = "PUT"
    else:
        sig = "NEUTRAL"
    return sig, reasons


def analyze_one(symbol: str) -> Dict[str, Any]:
    df = fetch_price_df(symbol)
    if df.empty:
        return {"ok": False, "symbol": symbol.upper(), "error": "No price data returned"}

    df = compute_indicators(df)
    if df.empty or 'Close' not in df.columns:
        return {"ok": False, "symbol": symbol.upper(), "error": "Close series unavailable after processing"}

    price = float(df['Close'].iloc[-1])
    signal, reasons = classify_signal(df)
    return {"ok": True, "symbol": symbol.upper(), "price": price, "signal": signal, "reasons": reasons}


# Backwards-compat: keep the old name that your bot was calling
def analyze_one_ticker(symbol: str) -> Dict[str, Any]:
    return analyze_one(symbol)


# ---------- Earnings (± window) ----------

def _nearest_earnings_from_df(edf: pd.DataFrame, days_window: int) -> Optional[pd.Timestamp]:
    if edf is None or edf.empty:
        return None

    # Accept either datetime index or a column that looks like a date
    if isinstance(edf.index, pd.DatetimeIndex):
        dates = edf.index
    else:
        col = next((c for c in edf.columns if str(c).lower().startswith("earnings")), None)
        if col is None:
            return None
        dates = pd.to_datetime(edf[col], errors="coerce").dropna()

    if len(dates) == 0:
        return None

    today = pd.Timestamp.utcnow().normalize()
    win_lo = today - pd.Timedelta(days=days_window)
    win_hi = today + pd.Timedelta(days=days_window)
    in_win = [pd.Timestamp(d).normalize() for d in list(dates) if win_lo <= pd.Timestamp(d).normalize() <= win_hi]
    if not in_win:
        return None
    return min(in_win, key=lambda d: abs((d - today).days))


def earnings_watch_text(symbol: str, days_window: int = 7) -> str:
    symbol = symbol.upper().strip()
    t = yf.Ticker(symbol)

    target: Optional[pd.Timestamp] = None
    try:
        edf = t.get_earnings_dates(limit=12)
        target = _nearest_earnings_from_df(edf, days_window)
    except Exception:
        target = None

    if target is None:
        try:
            cal = t.calendar
            if cal is not None and not cal.empty:
                values = []
                for col in cal.columns:
                    values.extend(list(cal[col].values))
                dates = pd.to_datetime(pd.Series(values), errors="coerce").dropna()
                if not dates.empty:
                    candidate = pd.Timestamp(dates.iloc[0]).normalize()
                    today = pd.Timestamp.utcnow().normalize()
                    if abs((candidate - today).days) <= days_window:
                        target = candidate
        except Exception:
            pass

    if target is None:
        return f"🗓️ **{symbol}** — No earnings found within ±{days_window} days."

    iso = target.strftime("%Y-%m-%d")
    delta = (target - pd.Timestamp.utcnow().normalize()).days
    when = "today" if delta == 0 else (f"in **{delta}** days" if delta > 0 else f"**{abs(delta)}** days ago")
    return f"🗓️ **{symbol}** — Earnings on **{iso}** ({when})."
