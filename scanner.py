# scanner.py
import math
import datetime as dt
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple

import numpy as np
import pandas as pd
import pytz
import requests
import yfinance as yf

from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

NY = pytz.timezone("America/New_York")

# -------- Helpers

def _now_et() -> dt.datetime:
    return dt.datetime.now(tz=NY)

def _flatten_cols(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure single-level columns."""
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = ['_'.join([str(c) for c in col if c != '']).strip('_') for col in df.columns.values]
    return df

def fetch_bars(symbol: str, period="5d", interval="5m") -> pd.DataFrame:
    d = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=True)
    if d is None or d.empty:
        raise ValueError(f"No data for {symbol}")
    d = _flatten_cols(d)
    # Standardize column names to: Open, High, Low, Close, Volume
    for c in ["Open","High","Low","Close","Volume"]:
        if c not in d.columns:
            # try alternative names like "Open_adjclose"
            match = [x for x in d.columns if x.lower().startswith(c.lower())]
            if match:
                d[c] = d[match[0]]
            else:
                raise ValueError(f"Missing column {c} for {symbol}")
    d = d.dropna(subset=["Open","High","Low","Close"])
    return d

@dataclass
class TAResult:
    symbol: str
    price: float
    bias: str            # CALL / PUT / NEUTRAL
    buy_low: float
    buy_high: float
    target: float
    stop: float
    reasons: str
    risk: str
    score: float
    rsi: float
    macd_diff: float

@dataclass
class OptionPick:
    contract: Optional[str]
    exp: Optional[str]
    strike: Optional[float]
    mid: Optional[float]
    spread_pct: Optional[float]
    vol: Optional[int]
    oi: Optional[int]
    note: Optional[str]

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close = df["Close"].astype(float)
    ema20 = EMAIndicator(close, window=20).ema_indicator()
    ema50 = EMAIndicator(close, window=50).ema_indicator()
    macd = MACD(close)
    rsi = RSIIndicator(close, window=14).rsi()
    atr = AverageTrueRange(high=df["High"], low=df["Low"], close=close, window=14).average_true_range()
    out = df.copy()
    out["EMA20"], out["EMA50"] = ema20, ema50
    out["MACD"], out["MACD_SIG"] = macd.macd(), macd.macd_signal()
    out["MACD_DIFF"] = out["MACD"] - out["MACD_SIG"]
    out["RSI"] = rsi
    out["ATR"] = atr
    return out.dropna().copy()

def _risk_from_spread_vol(spread_pct: float, oi: int) -> str:
    if spread_pct is None or oi is None:
        return "High"
    if spread_pct <= 0.05 and oi >= 1000: return "Low"
    if spread_pct <= 0.10 and oi >= 300: return "Medium"
    return "High"

def analyze_symbol(symbol: str, period="5d", interval="5m") -> Tuple[TAResult, OptionPick]:
    df = fetch_bars(symbol, period=period, interval=interval)
    df = compute_indicators(df)
    last = df.iloc[-1]
    px = float(last["Close"])
    ema20, ema50 = float(last["EMA20"]), float(last["EMA50"])
    macd_diff = float(last["MACD_DIFF"])
    rsi = float(last["RSI"])
    atr = float(last["ATR"])
    # bias
    bias = "NEUTRAL"
    if px > ema20 > ema50 and macd_diff > 0 and rsi >= 55:
        bias = "CALL"
    elif px < ema20 < ema50 and macd_diff < 0 and rsi <= 45:
        bias = "PUT"

    # simple plan using ATR
    buy_pad = 0.15 * atr if atr > 0 else max(0.001*px, 0.02)
    tgt_pad = 0.35 * atr if atr > 0 else max(0.002*px, 0.05)
    stp_pad = 0.25 * atr if atr > 0 else max(0.002*px, 0.05)

    if bias == "CALL":
        buy_low, buy_high = px - buy_pad, px - 0.5*buy_pad
        target, stop = px + tgt_pad, px - stp_pad
    elif bias == "PUT":
        buy_low, buy_high = px + 0.5*buy_pad, px + buy_pad
        target, stop = px - tgt_pad, px + stp_pad
    else:
        buy_low, buy_high = px - 0.25*buy_pad, px + 0.25*buy_pad
        target, stop = px + tgt_pad, px - stp_pad

    reasons = []
    if bias != "NEUTRAL":
        reasons.append(f"Trend: {('Up' if bias=='CALL' else 'Down')} (Close {px:.2f} vs EMA20/EMA50)")
    else:
        reasons.append(f"Trend: Mixed (Close {px:.2f} vs EMA20/EMA50)")
    reasons.append(f"MACD diff: {macd_diff:+.3f}")
    reasons.append(f"RSI: {rsi:.1f}")

    score = (1 if bias == "CALL" else -1 if bias == "PUT" else 0) * 6 \
            + (rsi - 50) / 5.0 + (macd_diff * 10)

    ta = TAResult(
        symbol=symbol.upper(),
        price=px,
        bias=bias,
        buy_low=float(buy_low),
        buy_high=float(buy_high),
        target=float(target),
        stop=float(stop),
        reasons="; ".join(reasons),
        risk="Medium",
        score=float(score),
        rsi=rsi,
        macd_diff=macd_diff
    )

    opt = choose_option(symbol, px, bias)
    # refine risk with option liquidity if we have it
    if opt and opt.spread_pct is not None and opt.oi is not None:
        ta.risk = _risk_from_spread_vol(opt.spread_pct, opt.oi)

    return ta, opt

def _pick_exp(exp_list: List[str], days_min=7, days_max=21) -> Optional[str]:
    if not exp_list: return None
    today = _now_et().date()
    def dte(exp):
        try: return (dt.datetime.strptime(exp, "%Y-%m-%d").date() - today).days
        except: return 9999
    exp_sorted = sorted(exp_list, key=lambda x: abs(max(min(dte(x), days_max), days_min) - dte(x)))
    # prefer within band, else earliest future
    for e in exp_sorted:
        if 0 < dte(e) <= 45:  # cap
            if days_min <= dte(e) <= days_max:
                return e
    # fallback: next available future
    fut = [e for e in exp_sorted if dte(e) > 0]
    return fut[0] if fut else exp_sorted[0]

def _nearest(series: pd.Series, value: float) -> float:
    return float(series.iloc[(series - value).abs().argsort().iloc[0]])

def choose_option(symbol: str, last_px: float, bias: str) -> OptionPick:
    try:
        tkr = yf.Ticker(symbol)
        exps = list(tkr.options)
        if not exps:
            return OptionPick(None,None,None,None,None,None,None,"No options listed")
        exp = _pick_exp(exps, 7, 21) or exps[0]
        oc = tkr.option_chain(exp)
        side = oc.calls if bias == "CALL" else oc.puts if bias == "PUT" else oc.calls
        df = side.copy()
        if df is None or df.empty:
            return OptionPick(None, exp, None, None, None, None, None, "Empty option chain")
        # Ensure needed fields exist
        for col in ["bid","ask","strike","volume","openInterest","contractSymbol"]:
            if col not in df.columns: df[col] = np.nan
        df["mid"] = (df["bid"].fillna(0) + df["ask"].fillna(0)) / 2.0
        df["spread_pct"] = np.where(df["mid"] > 0, (df["ask"].fillna(0) - df["bid"].fillna(0)) / df["mid"], np.inf)

        # pick nearest ATM with liquidity/spread filters
        df["dist"] = (df["strike"] - last_px).abs()
        filtered = df[(df["openInterest"].fillna(0) >= 50) & (df["spread_pct"] <= 0.20)]
        pick = None
        if not filtered.empty:
            pick = filtered.sort_values(by=["dist","spread_pct","openInterest"], ascending=[True, True, False]).iloc[0]
        else:
            pick = df.sort_values(by=["dist","spread_pct","openInterest"], ascending=[True, True, False]).iloc[0]
        return OptionPick(
            contract=str(pick.get("contractSymbol", "")),
            exp=exp,
            strike=float(pick.get("strike", np.nan)),
            mid=float(pick.get("mid", np.nan)) if np.isfinite(pick.get("mid", np.nan)) else None,
            spread_pct=float(pick.get("spread_pct", np.nan)) if np.isfinite(pick.get("spread_pct", np.nan)) else None,
            vol=int(pick.get("volume", 0)) if not pd.isna(pick.get("volume", np.nan)) else 0,
            oi=int(pick.get("openInterest", 0)) if not pd.isna(pick.get("openInterest", np.nan)) else 0,
            note=None
        )
    except Exception as e:
        return OptionPick(None, None, None, None, None, None, None, f"Option error: {e}")

def scan_many(tickers: List[str], limit: int = 10, period="5d", interval="5m") -> List[Dict]:
    rows = []
    for sym in tickers:
        try:
            ta, opt = analyze_symbol(sym, period=period, interval=interval)
            rows.append({
                "symbol": ta.symbol,
                "price": ta.price,
                "score": ta.score,
                "bias": ta.bias,
                "buy_low": ta.buy_low,
                "buy_high": ta.buy_high,
                "target": ta.target,
                "stop": ta.stop,
                "risk": ta.risk,
                "reasons": ta.reasons,
                "opt": opt
            })
        except Exception as e:
            rows.append({
                "symbol": sym.upper(),
                "error": str(e)
            })
    # sort by absolute score (strongest first)
    ok = [r for r in rows if "score" in r]
    err = [r for r in rows if "error" in r]
    ok = sorted(ok, key=lambda r: abs(r["score"]), reverse=True)[:limit]
    return ok + err

# -------- Earnings (for any symbol)

def _yahoo_calendar_event(symbol: str) -> Optional[dt.datetime]:
    try:
        url = f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}?modules=calendarEvents"
        hdrs = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=hdrs, timeout=10)
        j = r.json()
        arr = j["quoteSummary"]["result"][0]["calendarEvents"]["earnings"]["earningsDate"]
        raw = arr[0].get("raw")
        if raw:
            return dt.datetime.fromtimestamp(raw, tz=NY).replace(hour=0, minute=0, second=0, microsecond=0)
    except Exception:
        pass
    return None

def earnings_date(symbol: str) -> Optional[dt.datetime]:
    try:
        df = yf.Ticker(symbol).get_earnings_dates(limit=8)
        if df is not None and not df.empty:
            # take nearest to today
            dts = [pd.to_datetime(ix).to_pydatetime().astimezone(NY) for ix in df.index]
            d = min(dts, key=lambda d: abs((d - _now_et()).days))
            return d.replace(hour=0, minute=0, second=0, microsecond=0)
    except Exception:
        pass
    return _yahoo_calendar_event(symbol)
