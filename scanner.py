import datetime as dt
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yfinance as yf
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# --------- Utilities

def _now_utc_date() -> dt.date:
    # timezone-aware safe "today"
    return dt.datetime.now(dt.timezone.utc).date()

def _flatten_yf_df(df: pd.DataFrame) -> pd.DataFrame:
    """Make sure yfinance data has simple single-level columns."""
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        # For a single ticker, columns can be like ('Close','NVDA')
        df.columns = [c[0] for c in df.columns]
    # Normalize expected names
    rename_map = {c.lower(): c for c in ["Open", "High", "Low", "Close", "Adj Close", "Volume"]}
    for low, proper in rename_map.items():
        if proper not in df.columns and low in [x.lower() for x in df.columns]:
            match = [x for x in df.columns if x.lower() == low][0]
            df[proper] = df[match]
    return df.dropna(subset=["Close"])

@lru_cache(maxsize=256)
def _history_cached(ticker: str, period: str, interval: str) -> pd.DataFrame:
    # group_by='column' prevents MultiIndex surprises
    df = yf.download(
        ticker,
        period=period,
        interval=interval,
        auto_adjust=True,
        progress=False,
        group_by="column",
        threads=False,
    )
    return _flatten_yf_df(df)

def get_history(ticker: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame:
    try:
        return _history_cached(ticker.upper(), period, interval).copy()
    except Exception:
        return pd.DataFrame()

def rsi(series: pd.Series, length: int = 14) -> Optional[float]:
    if series is None or series.size < length + 1:
        return None
    delta = series.diff()
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    roll_up = pd.Series(gain, index=series.index).rolling(length).mean()
    roll_down = pd.Series(loss, index=series.index).rolling(length).mean()
    rs = roll_up / roll_down.replace(0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    return float(np.round(out.iloc[-1], 2)) if not np.isnan(out.iloc[-1]) else None

def macd_diff(series: pd.Series) -> Optional[float]:
    if series is None or series.size < 35:
        return None
    ema12 = series.ewm(span=12, adjust=False).mean()
    ema26 = series.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    sig = macd.ewm(span=9, adjust=False).mean()
    diff = macd - sig
    return float(np.round(diff.iloc[-1], 3))

# --------- News Sentiment

_analyzer = SentimentIntensityAnalyzer()

def news_sentiment(ticker: str) -> float:
    try:
        news = yf.Ticker(ticker).news  # can be None or list[dict]
        if not news:
            return 0.0
        titles: List[str] = []
        for n in news[:8]:
            # defensive parsing; some entries are dict-like, some objects
            title = None
            if isinstance(n, dict):
                title = n.get("title") or n.get("headline")
            else:
                # very rare edge case
                title = getattr(n, "title", None) or getattr(n, "headline", None)
            if title:
                titles.append(str(title))
        if not titles:
            return 0.0
        score = np.mean([_analyzer.polarity_scores(t)["compound"] for t in titles])
        return float(np.round(score, 3))
    except Exception:
        return 0.0

# --------- Main single-ticker analysis

def analyze_one_ticker(ticker: str) -> Optional[Dict]:
    t = ticker.upper().strip()
    df = get_history(t, period="6mo", interval="1d")
    if df.empty:
        return None

    close = df["Close"]
    last = float(close.iloc[-1])
    # percent changes (guard length)
    ch_1d = float(np.round((last / float(close.iloc[-2]) - 1) * 100, 2)) if len(close) > 1 else 0.0
    ch_5d = float(np.round((last / float(close.iloc[-6]) - 1) * 100, 2)) if len(close) > 6 else 0.0
    ch_1m = float(np.round((last / float(close.iloc[-21]) - 1) * 100, 2)) if len(close) > 21 else 0.0

    high_52 = float(np.round(df["High"].max(), 2))
    low_52 = float(np.round(df["Low"].min(), 2))

    # EMAs
    ema20 = float(np.round(close.ewm(span=20, adjust=False).mean().iloc[-1], 4)) if len(close) >= 20 else None
    ema50 = float(np.round(close.ewm(span=50, adjust=False).mean().iloc[-1], 4)) if len(close) >= 50 else None

    rsi_val = rsi(close, 14)
    macd_val = macd_diff(close)

    # Volume vs 20d average
    vol_ratio = None
    if "Volume" in df.columns and df["Volume"].tail(20).mean() > 0:
        vol_ratio = float(np.round(df["Volume"].iloc[-1] / df["Volume"].tail(20).mean(), 2))

    sent = news_sentiment(t)

    # Simple bias heuristic
    rec = "HOLD"
    if ema20 and ema50 and rsi_val is not None and macd_val is not None:
        if last > ema20 > ema50 and macd_val > 0 and rsi_val < 70:
            rec = "CALL"
        elif last < ema20 < ema50 and macd_val < 0 and rsi_val > 30:
            rec = "PUT"

    return {
        "ticker": t,
        "last_price": last,
        "change_1d": ch_1d,
        "change_5d": ch_5d,
        "change_1m": ch_1m,
        "high_52": high_52,
        "low_52": low_52,
        "ema20": ema20,
        "ema50": ema50,
        "rsi": rsi_val,
        "macd": macd_val,
        "volume_ratio": vol_ratio,
        "sentiment": sent,
        "rec": rec,
    }

# --------- Earnings (robust and fast)

def next_earnings_date(ticker: str) -> Optional[dt.date]:
    """Try multiple yfinance paths; return a single date if known."""
    t = ticker.upper().strip()
    # 1) Preferred: get_earnings_dates (new API; index is DatetimeIndex)
    try:
        df = yf.Ticker(t).get_earnings_dates(limit=8)
        if df is not None and not df.empty:
            # pick the nearest future date
            today = _now_utc_date()
            # index often is DatetimeIndex
            dates = list(pd.to_datetime(df.index).date)
            fut = [d for d in dates if d >= today]
            if fut:
                return min(fut)
            # if none in future, return most recent past
            return max(dates)
    except Exception:
        pass

    # 2) Fallback: .calendar (can be DataFrame with "Earnings Date" row)
    try:
        cal = yf.Ticker(t).calendar
        if isinstance(cal, pd.DataFrame) and not cal.empty:
            # Some builds store 1-2 values (start/end)
            if "Earnings Date" in cal.index:
                vals = [pd.to_datetime(x, errors="coerce") for x in cal.loc["Earnings Date"].to_list()]
                vals = [v.date() for v in vals if pd.notnull(v)]
                if vals:
                    return min(vals)
    except Exception:
        pass
    return None

def earnings_within_window(tickers: List[str], days: int = 7) -> List[Tuple[str, dt.date]]:
    out: List[Tuple[str, dt.date]] = []
    today = _now_utc_date()
    lo = today - dt.timedelta(days=days)
    hi = today + dt.timedelta(days=days)
    for t in sorted(set([x.upper().strip() for x in tickers if x])):
        try:
            d = next_earnings_date(t)
            if d and lo <= d <= hi:
                out.append((t, d))
        except Exception:
            continue
    return sorted(out, key=lambda x: x[1])
