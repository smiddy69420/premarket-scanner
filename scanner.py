# scanner.py
from __future__ import annotations
import datetime as dt
import time
from typing import List, Tuple, Optional, Iterable

import numpy as np
import pandas as pd
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import MACD
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

_analyzer = SentimentIntensityAnalyzer()

# 10-minute in-process cache for earnings lookups (ticker -> (date, ts))
_EARN_CACHE: dict[str, tuple[Optional[dt.date], float]] = {}
_EARN_TTL = 600.0


def _get_hist(ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
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


def _to_dates(cands: Iterable) -> list[dt.date]:
    out: list[dt.date] = []
    for v in cands:
        if isinstance(v, (pd.Timestamp, dt.datetime, np.datetime64)):
            try:
                out.append(pd.to_datetime(v).date())
            except Exception:
                pass
        elif isinstance(v, (int, float)) and v > 0:
            # epoch seconds or ms
            try:
                if v > 10_000_000_000:  # ms
                    out.append(dt.datetime.utcfromtimestamp(v / 1000).date())
                else:
                    out.append(dt.datetime.utcfromtimestamp(v).date())
            except Exception:
                pass
        elif isinstance(v, str) and v.strip():
            try:
                out.append(pd.to_datetime(v).date())
            except Exception:
                pass
    return out


def _pick_best_date(dates: list[dt.date], today: dt.date) -> Optional[dt.date]:
    if not dates:
        return None
    fut = [d for d in dates if d >= today]
    if fut:
        return min(fut)  # earliest upcoming
    return max(dates)   # most recent past


def _earnings_date_for(ticker: str) -> Optional[dt.date]:
    # cache
    now = time.time()
    if ticker in _EARN_CACHE:
        d, ts = _EARN_CACHE[ticker]
        if now - ts < _EARN_TTL:
            return d

    t = yf.Ticker(ticker)
    today = dt.date.today()
    picked: Optional[dt.date] = None

    # 1) get_earnings_dates
    try:
        df = t.get_earnings_dates(limit=16)
        if df is not None and not df.empty:
            idx_dates = _to_dates(list(df.index))
            cand = _pick_best_date(idx_dates, today)
            if cand:
                picked = cand
    except Exception:
        pass

    # 2) calendar dataframe (index or columns)
    if not picked:
        try:
            cal = t.calendar
            if isinstance(cal, pd.DataFrame) and not cal.empty:
                cands = []
                if "Earnings Date" in cal.index:
                    cands += list(cal.loc["Earnings Date"].dropna().values)
                if "Earnings Date" in cal.columns:
                    cands += list(cal["Earnings Date"].dropna().values)
                if not cands:
                    cands = list(cal.values.ravel())
                dates = _to_dates(cands)
                cand = _pick_best_date(dates, today)
                if cand:
                    picked = cand
        except Exception:
            pass

    # 3) get_info()['calendarEvents']['earnings']['earningsDate'] (2-element range)
    if not picked:
        try:
            info = t.get_info() or {}
            ce = info.get("calendarEvents") or {}
            earn = (ce.get("earnings") or {}).get("earningsDate") or []
            dates = _to_dates(earn if isinstance(earn, (list, tuple)) else [earn])
            cand = _pick_best_date(dates, today)
            if cand:
                picked = cand
        except Exception:
            pass

    # 4) info/fast_info epoch timestamp fields
    if not picked:
        try:
            info = t.get_info() or {}
            dates = _to_dates([
                info.get("earningsTimestamp"),
                info.get("earningsTimestampStart"),
                info.get("earningsTimestampEnd"),
                info.get("nextEarningsDate"),
                info.get("next_earnings_date"),
            ])
            # fast_info can be mapping-like or object
            fi = getattr(t, "fast_info", None)
            if fi is not None:
                for k in ("earningsTimestamp", "earningsTimestampStart", "earningsTimestampEnd", "nextEarningsDate", "next_earnings_date"):
                    try:
                        v = getattr(fi, k)
                        dates += _to_dates([v])
                    except Exception:
                        try:
                            dates += _to_dates([fi.get(k)])
                        except Exception:
                            pass
            cand = _pick_best_date(dates, today)
            if cand:
                picked = cand
        except Exception:
            pass

    _EARN_CACHE[ticker] = (picked, now)
    return picked


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
    uniq = {t.strip().upper() for t in tickers if t and t.strip()}
    for tk in sorted(uniq):
        try:
            d = _earnings_date_for(tk)
            if d and abs(d - today) <= window:
                out.append((tk, d))
        except Exception:
            continue
    out.sort(key=lambda x: x[1])
    return out
