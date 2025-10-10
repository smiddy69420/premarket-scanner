import os
import json
import math
import time
import datetime as dt
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD

# ---------
# Paths (Render’s filesystem is ephemeral, but fine for short-lived caches)
# ---------
CACHE_DIR = os.environ.get("CACHE_DIR", "/tmp/premarket_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

UNIVERSE_CACHE = os.path.join(CACHE_DIR, "universe.csv")
EARNINGS_CACHE = os.path.join(CACHE_DIR, "earnings_cache.json")

# ---------
# Utilities
# ---------

def _now_utc_date() -> dt.date:
    return dt.datetime.utcnow().date()

def chunk(items, size):
    for i in range(0, len(items), size):
        yield items[i:i+size]

# ---------
# Universe
# ---------

def ensure_universe() -> List[str]:
    """
    Build a broad U.S. equity universe using yfinance helper lists.
    Cached to avoid rebuilding every run.
    """
    if os.path.exists(UNIVERSE_CACHE):
        try:
            df = pd.read_csv(UNIVERSE_CACHE)
            syms = [s for s in df["symbol"].astype(str).str.upper().tolist() if s.isalnum()]
            if syms:
                return sorted(set(syms))
        except Exception:
            pass

    # Fallback: pull from yfinance helper lists
    # Note: these include ETFs; we accept that trade-off for coverage without a paid API.
    lists = []
    try:
        lists.append(yf.tickers_nasdaq())
    except Exception:
        pass
    try:
        lists.append(yf.tickers_sp500())
    except Exception:
        pass
    try:
        lists.append(yf.tickers_dow())
    except Exception:
        pass

    symbols = set()
    for lst in lists:
        for s in lst:
            s = str(s).upper().strip()
            if s and s.isalnum():
                symbols.add(s)

    # Minimal safety fallback if everything else failed
    if not symbols:
        symbols = {"AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META", "JPM"}

    df = pd.DataFrame({"symbol": sorted(symbols)})
    df.to_csv(UNIVERSE_CACHE, index=False)
    return df["symbol"].tolist()

# ---------
# Pricing & Indicators
# ---------

@dataclass
class TickerCard:
    symbol: str
    last: Optional[float]
    d1: Optional[float]
    d5: Optional[float]
    d21: Optional[float]
    range52: Optional[Tuple[float, float]]
    ema20: Optional[float]
    ema50: Optional[float]
    rsi14: Optional[float]
    macd_diff: Optional[float]
    vol_vs_avg20: Optional[float]
    bias: str  # "CALL" | "PUT" | "NEUTRAL"
    why: str
    option: Optional[Dict]  # {'code','expiry','strike','mid','spread','oi','vol','side'}

def _pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    try:
        return (a - b) / b * 100.0 if (a is not None and b and b != 0) else None
    except Exception:
        return None

def _safe_last(hist: pd.DataFrame) -> Optional[float]:
    try:
        return float(hist["Close"].iloc[-1])
    except Exception:
        return None

def _compute_indicators(hist: pd.DataFrame) -> Dict[str, Optional[float]]:
    if hist is None or hist.empty:
        return dict(ema20=None, ema50=None, rsi=None, macd_diff=None)

    close = hist["Close"]
    ema20 = EMAIndicator(close=close, window=20, fillna=False).ema_indicator().iloc[-1]
    ema50 = EMAIndicator(close=close, window=50, fillna=False).ema_indicator().iloc[-1]
    rsi = RSIIndicator(close=close, window=14, fillna=False).rsi().iloc[-1]
    macd = MACD(close=close, window_slow=26, window_fast=12, window_sign=9).macd_diff().iloc[-1]
    return dict(ema20=float(ema20), ema50=float(ema50), rsi=float(rsi), macd_diff=float(macd))

def _volume_vs_avg20(hist: pd.DataFrame) -> Optional[float]:
    try:
        v = float(hist["Volume"].iloc[-1])
        v20 = float(hist["Volume"].rolling(20).mean().iloc[-1])
        return v / v20 if v20 else None
    except Exception:
        return None

def _range_52w() -> dt.timedelta:
    return dt.timedelta(days=365)

def _history(symbol: str, period="1y", interval="1d") -> pd.DataFrame:
    df = yf.download(symbol, period=period, interval=interval, auto_adjust=False, progress=False, threads=False)
    if isinstance(df, pd.DataFrame) and not df.empty:
        # yfinance can return a column MultiIndex sometimes; normalize
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        return df.dropna(how="all")
    return pd.DataFrame()

def _bias(last: Optional[float], ema20: Optional[float], ema50: Optional[float], macd_diff: Optional[float]) -> str:
    score = 0
    if last and ema20 and ema50:
        if last > ema20 > ema50:
            score += 1
        if last < ema20 < ema50:
            score -= 1
    if macd_diff is not None:
        if macd_diff > 0:
            score += 1
        elif macd_diff < 0:
            score -= 1
    if score >= 1:
        return "CALL"
    if score <= -1:
        return "PUT"
    return "NEUTRAL"

def analyze_one_ticker(symbol: str) -> Optional[TickerCard]:
    hist_d = _history(symbol, period="6mo", interval="1d")
    if hist_d.empty:
        return None

    last = _safe_last(hist_d)
    d1 = _pct(last, float(hist_d["Close"].iloc[-2])) if len(hist_d) >= 2 else None
    d5 = _pct(last, float(hist_d["Close"].iloc[-6])) if len(hist_d) >= 6 else None
    d21 = _pct(last, float(hist_d["Close"].iloc[-21])) if len(hist_d) >= 21 else None

    ind = _compute_indicators(hist_d)
    volx = _volume_vs_avg20(hist_d)

    # 52W range
    hist_52 = _history(symbol, period="1y", interval="1d")
    r52 = None
    if not hist_52.empty:
        r52 = (float(hist_52["Low"].min()), float(hist_52["High"].max()))

    bias = _bias(last, ind["ema20"], ind["ema50"], ind["macd_diff"])
    why = f"Close {'>' if last and ind['ema20'] and last>ind['ema20'] else '<'} EMA20; " \
          f"EMA20 {'>' if ind['ema20'] and ind['ema50'] and ind['ema20']>ind['ema50'] else '<'} EMA50; " \
          f"MACD Δ: {None if ind['macd_diff'] is None else round(ind['macd_diff'], 3)}; " \
          f"RSI: {None if ind['rsi'] is None else round(ind['rsi'], 1)}"

    option = _find_atm_option(symbol, last, bias)

    return TickerCard(
        symbol=symbol,
        last=last,
        d1=d1, d5=d5, d21=d21,
        range52=r52,
        ema20=ind["ema20"], ema50=ind["ema50"],
        rsi14=ind["rsi"], macd_diff=ind["macd_diff"],
        vol_vs_avg20=volx,
        bias=bias,
        why=why,
        option=option
    )

def _nearest_expiry(expiries: List[str], target_days: int = 14) -> Optional[str]:
    if not expiries:
        return None
    today = _now_utc_date()
    best = None
    best_diff = 10**9
    for e in expiries:
        try:
            d = dt.datetime.strptime(e, "%Y-%m-%d").date()
            diff = abs((d - today).days - target_days)
            if diff < best_diff:
                best = e
                best_diff = diff
        except Exception:
            continue
    return best

def _find_atm_option(symbol: str, last: Optional[float], bias: str) -> Optional[Dict]:
    try:
        t = yf.Ticker(symbol)
        expiries = t.options
        if not expiries:
            return None
        expiry = _nearest_expiry(expiries, target_days=14)
        if not expiry or last is None:
            return None

        chain = t.option_chain(expiry)
        side = "call" if bias == "CALL" else "put"
        opts = chain.calls if side == "call" else chain.puts
        if opts is None or opts.empty:
            return None

        # ATM strike
        opts["dist"] = (opts["strike"] - last).abs()
        row = opts.sort_values("dist").iloc[0]

        bid = float(row.get("bid", np.nan))
        ask = float(row.get("ask", np.nan))
        mid = None
        spread = None
        if (not math.isnan(bid)) and (not math.isnan(ask)) and ask > 0:
            mid = round((bid + ask) / 2.0, 2)
            spread = round((ask - bid) / ask * 100.0, 1)

        return {
            "code": row.get("contractSymbol", ""),
            "expiry": expiry,
            "strike": float(row.get("strike", np.nan)),
            "mid": mid,
            "spread_pct": spread,
            "oi": int(row.get("openInterest", 0)),
            "vol": int(row.get("volume", 0)),
            "side": side.upper()
        }
    except Exception:
        return None

# ---------
# Earnings
# ---------

def _load_earnings_cache() -> Dict[str, Dict]:
    if os.path.exists(EARNINGS_CACHE):
        try:
            with open(EARNINGS_CACHE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_earnings_cache(data: Dict[str, Dict]):
    tmp = EARNINGS_CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, EARNINGS_CACHE)

def _next_earnings_from_df(df: pd.DataFrame) -> Optional[dt.date]:
    """
    yfinance.get_earnings_dates returns a DF with index being the earnings datetime.
    We pick the **next future** one.
    """
    try:
        if df is None or df.empty:
            return None
        idx = df.index
        # index may be DatetimeIndex
        future = [d.date() for d in idx if d.to_pydatetime().date() >= _now_utc_date()]
        return min(future) if future else None
    except Exception:
        return None

def earnings_for_ticker(symbol: str, days: int) -> Optional[Dict]:
    """Return upcoming earnings within ±days for one ticker (dict) or None."""
    try:
        t = yf.Ticker(symbol)
        df = t.get_earnings_dates(limit=8)
        nxt = _next_earnings_from_df(df)
        if not nxt:
            return None
        if abs((nxt - _now_utc_date()).days) <= days:
            return {
                "symbol": symbol,
                "date": nxt,
                "eps_est": float(df.iloc[0]["EPS Estimate"]) if "EPS Estimate" in df.columns and not pd.isna(df.iloc[0]["EPS Estimate"]) else None,
                "reported_eps": float(df.iloc[0]["Reported EPS"]) if "Reported EPS" in df.columns and not pd.isna(df.iloc[0]["Reported EPS"]) else None,
                "surprise": float(df.iloc[0]["Surprise(%)"]) if "Surprise(%)" in df.columns and not pd.isna(df.iloc[0]["Surprise(%)"]) else None
            }
        return None
    except Exception:
        return None

def earnings_universe_window(days: int) -> List[Dict]:
    """
    Return all tickers with earnings within ±days using cached map.
    If cache stale or missing, we lazily augment it ad-hoc for any missing names
    by scanning in small batches (rate-friendly).
    """
    universe = ensure_universe()
    cache = _load_earnings_cache()

    # Refresh any entries older than 24h
    stale_cut = time.time() - 24 * 3600
    # Build a working list of tickers to (re)fetch
    to_fetch = [s for s in universe if (s not in cache) or (cache[s].get("ts", 0) < stale_cut)]

    # Fetch missing/stale in batches to avoid hammering Yahoo
    # This call is used during an interaction, so keep it modest.
    # The background task will fully refresh twice a day.
    batch = to_fetch[:500]  # safety cap
    for sym in batch:
        data = _earnings_fetch_one(sym)
        cache[sym] = {"date": data, "ts": time.time()}

        # light pacing
        time.sleep(0.03)

    _save_earnings_cache(cache)

    # Filter within window
    out = []
    for sym in universe:
        d = cache.get(sym, {}).get("date")
        if isinstance(d, str):
            d = dt.datetime.strptime(d, "%Y-%m-%d").date()
        if isinstance(d, dt.date):
            if abs((d - _now_utc_date()).days) <= days:
                out.append({"symbol": sym, "date": d})

    # sort by date then symbol
    out.sort(key=lambda x: (x["date"], x["symbol"]))
    return out

def _earnings_fetch_one(symbol: str) -> Optional[str]:
    """Return ISO date string or None for next earnings."""
    try:
        df = yf.Ticker(symbol).get_earnings_dates(limit=8)
        nxt = _next_earnings_from_df(df)
        return nxt.isoformat() if nxt else None
    except Exception:
        return None

def refresh_all_caches():
    """
    Background job: fully refresh earnings cache for the whole universe.
    Runs every 12h from bot.py without blocking interactions.
    """
    universe = ensure_universe()
    cache = _load_earnings_cache()
    for i, sym in enumerate(universe, start=1):
        cache[sym] = {"date": _earnings_fetch_one(sym), "ts": time.time()}
        if i % 50 == 0:
            _save_earnings_cache(cache)
        time.sleep(0.03)
    _save_earnings_cache(cache)

# ---------
# Rendering (Discord Embeds)
# ---------

import discord

def _fmt_pct(x: Optional[float]) -> str:
    return "—" if x is None else f"{x:+.2f}%"

def _fmt_f(v: Optional[float], prec=2) -> str:
    return "—" if v is None or (isinstance(v,float) and (math.isnan(v))) else f"{v:.{prec}f}"

def render_ticker_embed(card: TickerCard) -> discord.Embed:
    c = 0x2ECC71 if card.bias == "CALL" else (0xE74C3C if card.bias == "PUT" else 0x999999)
    e = discord.Embed(title=f"{card.symbol} • {card.bias}", color=c)

    e.add_field(name="Last", value=f"${_fmt_f(card.last)}", inline=True)
    e.add_field(name="1D / 5D / 1M", value=f"{_fmt_pct(card.d1)} / {_fmt_pct(card.d5)} / {_fmt_pct(card.d21)}", inline=True)

    r52 = "—"
    if card.range52:
        r52 = f"${_fmt_f(card.range52[0])} – ${_fmt_f(card.range52[1])}"
    e.add_field(name="52W Range", value=r52, inline=True)

    e.add_field(name="EMA20/50", value=f"{_fmt_f(card.ema20)} / {_fmt_f(card.ema50)}", inline=True)
    e.add_field(name="RSI14 | MACD Δ", value=f"{_fmt_f(card.rsi14,1)} | {_fmt_f(card.macd_diff,3)}", inline=True)
    e.add_field(name="Vol/Avg20", value=f"{_fmt_f(card.vol_vs_avg20,2)}x", inline=True)

    e.add_field(name="Why", value=card.why, inline=False)

    if card.option:
        o = card.option
        e.add_field(
            name="Option",
            value=(
                f"`{o['code']}` • **{o['side']}** • Exp **{o['expiry']}**\n"
                f"Strike **{_fmt_f(o['strike'])}** • Mid **{_fmt_f(o['mid'])}** • "
                f"Spread **{_fmt_f(o['spread_pct'])}%**\n"
                f"Vol **{o['vol']}** • OI **{o['oi']}**"
            ),
            inline=False
        )

    e.set_footer(text="Premarket Scanner • multi-signal")
    return e

def render_earnings_single_embed(row: Dict, days: int) -> discord.Embed:
    e = discord.Embed(
        title=f"{row['symbol']} • Earnings within ±{days} days",
        color=0xF1C40F
    )
    e.add_field(name="Date", value=row['date'].isoformat(), inline=True)
    if row.get("eps_est") is not None:
        e.add_field(name="EPS Est.", value=_fmt_f(row['eps_est']), inline=True)
    if row.get("reported_eps") is not None:
        e.add_field(name="Reported EPS", value=_fmt_f(row['reported_eps']), inline=True)
    if row.get("surprise") is not None:
        e.add_field(name="Surprise %", value=_fmt_f(row['surprise']), inline=True)
    e.set_footer(text="Source: yfinance (Yahoo) • cached 12h")
    return e

def render_earnings_page_embed(page: List[Dict], days: int, page_num: int, total_pages: int) -> discord.Embed:
    e = discord.Embed(
        title=f"Earnings within ±{days} days",
        description="",
        color=0xF1C40F
    )
    lines = []
    for r in page:
        lines.append(f"• **{r['symbol']}** — {r['date'].isoformat()}")
    e.description = "\n".join(lines)
    e.set_footer(text=f"Page {page_num}/{total_pages} • Source: yfinance • cached 12h")
    return e
