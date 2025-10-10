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
    missing = [c for c in needed if c not in df.columns]
    if missing:
        # Try to coerce if possible; otherwise return empty
        return pd.DataFrame()

    # Clean NaNs
    df = df[needed].dropna(how='any')
    return df


def fetch_price_df(
    symbol: str,
    auto_adjust: bool = False,
    max_attempts: int = 4
) -> pd.DataFrame:
    """
    Robust yfinance fetch with multiple fallbacks:
    1) 1d / 1m
    2) 5d / 5m
    3) 30d / 1h
    4) 1y / 1d
    Returns standardized OHLCV or empty DataFrame.
    """
    symbol = symbol.upper().strip()
    t = yf.Ticker(symbol)
    attempts: List[Tuple[str, str]] = [
        ("1d",  "1m"),
        ("5d",  "5m"),
        ("30d", "1h"),
        ("1y",  "1d"),
    ]
    tried = 0
    for period, interval in attempts:
        try:
            df = t.history(period=period, interval=interval, auto_adjust=auto_adjust, prepost=True, actions=False)
            df = _standardize_ohlcv(df)
            if not df.empty and df['Close'].ndim == 1:
                return df
        except Exception:
            pass
        tried += 1
        if tried >= max_attempts:
            break
    return pd.DataFrame()


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Adds EMA20, EMA50, MACD, MACD Signal, RSI(14)
    """
    if df.empty:
        return df

    out = df.copy()
    # Use safe lengths relative to available rows
    n = len(out)
    span_fast = 12 if n >= 12 else max(2, n // 4)
    span_slow = 26 if n >= 26 else max(3, n // 3)
    span_signal = 9 if n >= 9 else max(2, n // 5)

    out['EMA20'] = EMAIndicator(close=out['Close'], window=min(20, max(2, n // 6))).ema_indicator()
    out['EMA50'] = EMAIndicator(close=out['Close'], window=min(50, max(3, n // 3))).ema_indicator()

    macd = MACD(close=out['Close'], window_slow=span_slow, window_fast=span_fast, window_sign=span_signal)
    out['MACD'] = macd.macd()
    out['MACD_SIG'] = macd.macd_signal()

    out['RSI'] = RSIIndicator(close=out['Close'], window=min(14, max(2, n // 8))).rsi()

    return out.dropna(how='any')


def classify_signal(df: pd.DataFrame) -> Tuple[str, List[str]]:
    """
    Simple trend/momentum classification with reasons.
    """
    reasons = []
    if df.empty:
        return "NEUTRAL", ["No data"]

    last = df.iloc[-1]
    close, ema20, ema50 = last['Close'], last['EMA20'], last['EMA50']
    macd, macd_sig = last['MACD'], last['MACD_SIG']
    rsi = float(last['RSI'])

    # Trend
    if close > ema20 > ema50:
        trend = "Uptrend"
        reasons.append("Close > EMA20 > EMA50")
    elif close < ema20 < ema50:
        trend = "Downtrend"
        reasons.append("Close < EMA20 < EMA50")
    else:
        trend = "Sideways"
        reasons.append("Mixed EMAs")

    # Momentum
    if macd > macd_sig:
        reasons.append("MACD momentum up")
    else:
        reasons.append("MACD momentum down")

    # RSI
    reasons.append(f"RSI {rsi:.1f}")

    # Final signal
    if trend == "Uptrend" and macd > macd_sig and rsi >= 50:
        sig = "CALL"
    elif trend == "Downtrend" and macd < macd_sig and rsi <= 50:
        sig = "PUT"
    else:
        sig = "NEUTRAL"

    return sig, reasons


def analyze_one(symbol: str) -> Dict[str, Any]:
    """
    Returns dict with fields:
      ok, error, symbol, price, signal, reasons
    """
    df = fetch_price_df(symbol)
    if df.empty:
        return {"ok": False, "symbol": symbol.upper(), "error": "No price data returned"}

    df = compute_indicators(df)
    if df.empty or 'Close' not in df.columns:
        return {"ok": False, "symbol": symbol.upper(), "error": "Close series unavailable after processing"}

    price = float(df['Close'].iloc[-1])
    signal, reasons = classify_signal(df)
    return {
        "ok": True,
        "symbol": symbol.upper(),
        "price": price,
        "signal": signal,
        "reasons": reasons,
    }


# ---------- Earnings (± window) ----------

def _nearest_earnings_from_df(edf: pd.DataFrame, days_window: int) -> Optional[pd.Timestamp]:
    if edf is None or edf.empty:
        return None
    # yfinance get_earnings_dates can return index as DatetimeIndex
    if isinstance(edf.index, pd.DatetimeIndex):
        dates = edf.index.to_pydatetime()
    else:
        # or a column named 'Earnings Date'
        col_name = None
        for c in edf.columns:
            if str(c).lower().startswith("earnings"):
                col_name = c
                break
        if col_name is None:
            return None
        dates = pd.to_datetime(edf[col_name], errors='coerce').dropna().to_pydatetime()

    if not len(dates):
        return None

    today = pd.Timestamp.now(tz="UTC").normalize()
    window_start = today - pd.Timedelta(days=days_window)
    window_end = today + pd.Timedelta(days=days_window)

    # keep within window, pick the closest by absolute delta
    in_win = [pd.Timestamp(d, tz="UTC").normalize() for d in dates if window_start <= pd.Timestamp(d, tz="UTC").normalize() <= window_end]
    if not in_win:
        return None

    closest = min(in_win, key=lambda d: abs((d - today).days))
    return closest


def earnings_watch_text(symbol: str, days_window: int = 7) -> str:
    """
    Returns a user-friendly sentence summarizing earnings within ±days_window.
    """
    symbol = symbol.upper().strip()
    t = yf.Ticker(symbol)

    # Try new API first
    edate: Optional[pd.Timestamp] = None
    try:
        edf = t.get_earnings_dates(limit=12)
        edate = _nearest_earnings_from_df(edf, days_window)
    except Exception:
        edate = None

    # Fallback to calendar
    if edate is None:
        try:
            cal = t.calendar
            # calendar may be DataFrame with index including 'Earnings Date'
            if cal is not None and not cal.empty:
                # Pull anything that looks like a date
                vals = []
                for col in cal.columns:
                    vals.extend(list(cal[col].values))
                dates = pd.to_datetime(pd.Series(vals), errors='coerce').dropna()
                if not dates.empty:
                    edate_candidate = pd.Timestamp(dates.iloc[0]).tz_localize("UTC") if dates.dt.tz is None else dates.iloc[0]
                    # treat as event and check window
                    today = pd.Timestamp.now(tz="UTC").normalize()
                    if abs((edate_candidate.normalize() - today).days) <= days_window:
                        edate = edate_candidate.normalize()
        except Exception:
            pass

    if edate is None:
        return f"🗓️ **{symbol}** — No earnings found within ±{days_window} days."

    iso = edate.tz_convert("UTC").strftime("%Y-%m-%d") if edate.tzinfo else edate.strftime("%Y-%m-%d")
    delta = (edate.normalize() - pd.Timestamp.now(tz="UTC").normalize()).days
    when = "today" if delta == 0 else ("in **{}** days".format(delta) if delta > 0 else "**{}** days ago".format(abs(delta)))
    return f"🗓️ **{symbol}** — Earnings on **{iso}** ({when})."
