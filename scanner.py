# scanner.py
from __future__ import annotations
import datetime as dt
from typing import List, Tuple, Optional

import numpy as np
import pandas as pd
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import MACD
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

_analyzer = SentimentIntensityAnalyzer()


def _get_hist(ticker: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame:
    t = yf.Ticker(ticker)
    hist = t.history(period=period, interval=interval, auto_adjust=True)
    if hist is None or hist.empty:
        raise ValueError("no price data")
    if "Close" not in hist.columns:
        raise ValueError("missing Close column")
    return hist


def _pct_change(series: pd.Series, days: int) -> Optional[float]:
    if len(series) <= days:
        return None
    try:
        return float((series.iloc[-1] / series.iloc[-days-1] - 1.0) * 100.0)
    except Exception:
        return None


def _ema(series: pd.Series, span: int) -> Optional[float]:
    if len(series) < span:
        return None
    return float(series.ewm(span=span, adjust=False).mean().iloc[-1])


def _rsi(series: pd.Series, window: int = 14) -> Optional[float]:
    if len(series) < window + 1:
        return None
    return float(RSIIndicator(close=series, window=window).rsi().iloc[-1])


def _macd_diff(series: pd.Series) -> Optional[float]:
    if len(series) < 35:
        return None
    ind = MACD(close=series)
    return float(ind.macd_diff().iloc[-1])


def _volume_ratio(vol: pd.Series, window: int = 20) -> Optional[float]:
    if vol is None or vol.empty or len(vol) < window:
        return None
    ma = vol.rolling(window).mean().iloc[-1]
    if ma and ma > 0:
        return round(float(vol.iloc[-1] / ma), 2)
    return None


def _sentiment_from_news(ticker: str, limit: int = 15) -> float:
    try:
        t = yf.Ticker(ticker)
        news = t.news or []
        if not isinstance(news, list):
            return 0.0
        titles = [n.get("title", "") for n in news[:limit] if isinstance(n, dict)]
        if not titles:
            return 0.0
        scores = [_analyzer.polarity_scores(txt).get("compound", 0.0) for txt in titles]
        return float(np.clip(np.mean(scores), -1.0, 1.0))
    except Exception:
        return 0.0


def _bias(last: float, ema20: Optional[float], ema50: Optional[float],
          macd: Optional[float], rsi: Optional[float]) -> str:
    try:
        above20 = ema20 is not None and last > ema20
        above50 = ema50 is not None and last > ema50
        below20 = ema20 is not None and last < ema20
        below50 = ema50 is not None and last < ema50

        if above20 and above50 and (macd is None or macd >= 0):
            return "CALL"
        if below20 and below50 and (macd is None or macd <= 0):
            return "PUT"
        if rsi is not None:
            if rsi >= 65 and (macd is None or macd >= 0):
                return "CALL"
            if rsi <= 35 and (macd is None or macd <= 0):
                return "PUT"
        return "NEUTRAL"
    except Exception:
        return "NEUTRAL"


def _earnings_date_for(ticker: str) -> Optional[dt.date]:
    t = yf.Ticker(ticker)
    today = dt.date.today()

    # 1) get_earnings_dates (preferred)
    try:
        df = t.get_earnings_dates(limit=12)
        if df is not None and not df.empty:
            dates = [idx.date() for idx in df.index if isinstance(idx, pd.Timestamp)]
            if dates:
                dates.sort(key=lambda d: abs(d - today))
                return dates[0]
    except Exception:
        pass

    # 2) calendar (fallback)
    try:
        cal = t.calendar
        if isinstance(cal, pd.DataFrame) and not cal.empty and "Earnings Date" in cal.index:
            vals = cal.loc["Earnings Date"].dropna().values
            for v in vals:
                if isinstance(v, (pd.Timestamp, dt.datetime)):
                    return v.date()
                if isinstance(v, str):
                    try:
                        return pd.to_datetime(v).date()
                    except Exception:
                        continue
    except Exception:
        pass

    return None


def analyze_one_ticker(ticker: str) -> dict:
    if not ticker or not ticker.isalnum():
        raise ValueError("invalid ticker")

    hist = _get_hist(ticker, period="1y", interval="1d")
    close = hist["Close"]
    last = float(close.iloc[-1])

    change_1d = _pct_change(close, 1) or 0.0
    change_5d = _pct_change(close, 5) or 0.0
    change_1m = _pct_change(close, 21) or 0.0

    low_52 = float(close.min())
    high_52 = float(close.max())

    ema20 = _ema(close, 20)
    ema50 = _ema(close, 50)
    rsi = _rsi(close, 14)
    macd = _macd_diff(close)
    vol_ratio = _volume_ratio(hist.get("Volume"), 20)
    sentiment = _sentiment_from_news(ticker)
    rec = _bias(last, ema20, ema50, macd, rsi)
    earn = _earnings_date_for(ticker)

    return {
        "ticker": ticker.upper(),
        "last_price": last,
        "change_1d": change_1d,
        "change_5d": change_5d,
        "change_1m": change_1m,
        "low_52": low_52,
        "high_52": high_52,
        "ema20": round(ema20, 2) if ema20 is not None else None,
        "ema50": round(ema50, 2) if ema50 is not None else None,
        "rsi": round(rsi, 1) if rsi is not None else None,
        "macd": round(macd, 3) if macd is not None else None,
        "volume_ratio": vol_ratio,
        "sentiment": round(sentiment, 2),
        "rec": rec,
        "earnings_date": earn.isoformat() if earn else None,
    }


def earnings_within_window(tickers: List[str], days: int = 7) -> List[Tuple[str, dt.date]]:
    out: List[Tuple[str, dt.date]] = []
    window = dt.timedelta(days=days)
    today = dt.date.today()
    for tk in set([t.upper() for t in tickers if t and t.strip()]):
        try:
            d = _earnings_date_for(tk)
            if d and abs(d - today) <= window:
                out.append((tk, d))
        except Exception:
            continue
    out.sort(key=lambda x: x[1])
    return out
