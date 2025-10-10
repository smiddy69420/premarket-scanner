# scanner.py — resilient analysis & earnings cache
import os, json, math, time, datetime as dt, logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD

log = logging.getLogger("scanner")

CACHE_DIR = os.environ.get("CACHE_DIR", "/tmp/premarket_cache")
os.makedirs(CACHE_DIR, exist_ok=True)
UNIVERSE_CACHE = os.path.join(CACHE_DIR, "universe.csv")
EARNINGS_CACHE = os.path.join(CACHE_DIR, "earnings_cache.json")

# ---------- Utilities ----------
def _now_utc_date() -> dt.date:
    return dt.datetime.utcnow().date()

def chunk(items, size):
    for i in range(0, len(items), size):
        yield items[i:i+size]

# ---------- Universe ----------
def ensure_universe() -> List[str]:
    # Load cached list first
    if os.path.exists(UNIVERSE_CACHE):
        try:
            df = pd.read_csv(UNIVERSE_CACHE)
            syms = [s for s in df["symbol"].astype(str).str.upper().tolist() if s.isalnum()]
            if syms:
                return sorted(set(syms))
        except Exception:
            pass

    symbols = set()
    # yfinance helper lists (broadest non-paid approach)
    try: symbols.update([s.upper() for s in yf.tickers_nasdaq()])
    except Exception: pass
    try: symbols.update([s.upper() for s in yf.tickers_sp500()])
    except Exception: pass
    try: symbols.update([s.upper() for s in yf.tickers_dow()])
    except Exception: pass

    # Seed with SCAN_UNIVERSE env if present
    env_list = [s.strip().upper() for s in os.getenv("SCAN_UNIVERSE","").split(",") if s.strip()]
    symbols.update(env_list)

    if not symbols:
        symbols = {"AAPL","MSFT","NVDA","TSLA","AMZN","GOOGL","META","JPM"}  # safety fallback

    df = pd.DataFrame({"symbol": sorted(symbols)})
    df.to_csv(UNIVERSE_CACHE, index=False)
    return df["symbol"].tolist()

# ---------- Pricing & Indicators ----------
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
    bias: str
    why: str
    option: Optional[Dict]

def _pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    try:
        return (a - b) / b * 100.0 if (a is not None and b and b != 0) else None
    except Exception:
        return None

def _history(symbol: str) -> pd.DataFrame:
    """Robust history loader with fallbacks and normalization."""
    combos = [("6mo","1d"), ("3mo","1d"), ("60d","1h"), ("30d","30m"), ("15d","15m")]
    for period, interval in combos:
        try:
            df = yf.download(symbol, period=period, interval=interval,
                             auto_adjust=False, progress=False, threads=False)
            if not isinstance(df, pd.DataFrame) or df.empty:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [lvl0 for (lvl0, _) in df.columns]
            # guarantee the columns we use exist
            need = ["Open","High","Low","Close"]
            if not all(c in df.columns for c in need):
                continue
            if "Volume" not in df.columns:
                df["Volume"] = np.nan
            out = df[["Open","High","Low","Close","Volume"]].dropna(how="all")
            if not out.empty:
                return out
        except Exception:
            continue
    return pd.DataFrame()

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
    rsi   = RSIIndicator(close=close, window=14, fillna=False).rsi().iloc[-1]
    macd  = MACD(close=close, window_slow=26, window_fast=12, window_sign=9).macd_diff().iloc[-1]
    return dict(ema20=float(ema20), ema50=float(ema50), rsi=float(rsi), macd_diff=float(macd))

def _volume_vs_avg20(hist: pd.DataFrame) -> Optional[float]:
    try:
        v = float(hist["Volume"].iloc[-1])
        v20 = float(hist["Volume"].rolling(20).mean().iloc[-1])
        return v / v20 if (v20 and not math.isnan(v20)) else None
    except Exception:
        return None

def _bias(last, ema20, ema50, macd_diff) -> str:
    score = 0
    try:
        if last is not None and ema20 is not None and ema50 is not None:
            if last > ema20 > ema50: score += 1
            if last < ema20 < ema50: score -= 1
        if macd_diff is not None:
            score += (1 if macd_diff > 0 else (-1 if macd_diff < 0 else 0))
    except Exception:
        pass
    return "CALL" if score >= 1 else ("PUT" if score <= -1 else "NEUTRAL")

def _history_52w(symbol: str) -> pd.DataFrame:
    try:
        df = yf.download(symbol, period="1y", interval="1d", auto_adjust=False, progress=False, threads=False)
        if isinstance(df, pd.DataFrame) and not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0] for c in df.columns]
            return df.dropna(how="all")
    except Exception:
        pass
    return pd.DataFrame()

def analyze_one_ticker(symbol: str) -> Optional[TickerCard]:
    hist_d = _history(symbol)
    if hist_d.empty:
        log.warning("No history for %s", symbol)
        return None

    last = _safe_last(hist_d)
    d1 = _pct(last, float(hist_d["Close"].iloc[-2])) if len(hist_d) >= 2 else None
    d5 = _pct(last, float(hist_d["Close"].iloc[-6])) if len(hist_d) >= 6 else None
    d21 = _pct(last, float(hist_d["Close"].iloc[-21])) if len(hist_d) >= 21 else None

    ind = _compute_indicators(hist_d)
    volx = _volume_vs_avg20(hist_d)

    hist_52 = _history_52w(symbol)
    r52 = None
    if not hist_52.empty:
        r52 = (float(hist_52["Low"].min()), float(hist_52["High"].max()))

    def _gt(a, b):
        try:
            return (a is not None) and (b is not None) and (a > b)
        except Exception:
            return False

    why = (
        f"Close {'>' if _gt(last, ind['ema20']) else '<'} EMA20; "
        f"EMA20 {'>' if _gt(ind['ema20'], ind['ema50']) else '<'} EMA50; "
        f"MACD Δ: {None if ind['macd_diff'] is None else round(ind['macd_diff'], 3)}; "
        f"RSI: {None if ind['rsi'] is None else round(ind['rsi'], 1)}"
    )
    option = None  # keep for future; yfinance option chain stays flaky at times

    return TickerCard(
        symbol=symbol, last=last, d1=d1, d5=d5, d21=d21,
        range52=r52, ema20=ind["ema20"], ema50=ind["ema50"],
        rsi14=ind["rsi"], macd_diff=ind["macd_diff"], vol_vs_avg20=volx,
        bias=_bias(last, ind["ema20"], ind["ema50"], ind["macd_diff"]),
        why=why, option=option
    )

# ---------- Earnings ----------
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
    try:
        if df is None or df.empty:
            return None
        fut = [d.to_pydatetime().date() for d in df.index if d.to_pydatetime().date() >= _now_utc_date()]
        return min(fut) if fut else None
    except Exception:
        return None

def _earnings_fetch_one(symbol: str) -> Optional[str]:
    try:
        df = yf.Ticker(symbol).get_earnings_dates(limit=8)
        nxt = _next_earnings_from_df(df)
        return nxt.isoformat() if nxt else None
    except Exception:
        return None

def refresh_all_caches():
    universe = ensure_universe()
    cache = _load_earnings_cache()
    for i, sym in enumerate(universe, start=1):
        cache[sym] = {"date": _earnings_fetch_one(sym), "ts": time.time()}
        if i % 50 == 0:
            _save_earnings_cache(cache)
        time.sleep(0.03)
    _save_earnings_cache(cache)

def earnings_universe_window(days: int) -> List[Dict]:
    universe = ensure_universe()
    cache = _load_earnings_cache()
    stale_cut = time.time() - 24 * 3600
    to_fetch = [s for s in universe if (s not in cache) or (cache[s].get("ts", 0) < stale_cut)]

    # modest batch to keep interactions snappy; background task will fill the rest
    for sym in to_fetch[:500]:
        cache[sym] = {"date": _earnings_fetch_one(sym), "ts": time.time()}
        time.sleep(0.03)
    _save_earnings_cache(cache)

    out = []
    today = _now_utc_date()
    for sym in universe:
        d = cache.get(sym, {}).get("date")
        if isinstance(d, str):
            try: d = dt.datetime.strptime(d, "%Y-%m-%d").date()
            except Exception: d = None
        if isinstance(d, dt.date):
            if abs((d - today).days) <= days:
                out.append({"symbol": sym, "date": d})
    out.sort(key=lambda x: (x["date"], x["symbol"]))
    return out

# ---------- Discord Embeds ----------
import discord

def _fmt_pct(x: Optional[float]) -> str:
    return "—" if x is None else f"{x:+.2f}%"

def _fmt_f(v: Optional[float], prec=2) -> str:
    try:
        if v is None or (isinstance(v, float) and math.isnan(v)): return "—"
        return f"{v:.{prec}f}"
    except Exception:
        return "—"

def render_ticker_embed(card: TickerCard) -> discord.Embed:
    c = 0x2ECC71 if card.bias == "CALL" else (0xE74C3C if card.bias == "PUT" else 0x999999)
    e = discord.Embed(title=f"{card.symbol} • {card.bias}", color=c)
    e.add_field(name="Last", value=f"${_fmt_f(card.last)}", inline=True)
    e.add_field(name="1D / 5D / 1M", value=f"{_fmt_pct(card.d1)} / {_fmt_pct(card.d5)} / {_fmt_pct(card.d21)}", inline=True)
    r52 = "—" if not card.range52 else f"${_fmt_f(card.range52[0])} – ${_fmt_f(card.range52[1])}"
    e.add_field(name="52W Range", value=r52, inline=True)
    e.add_field(name="EMA20/50", value=f"{_fmt_f(card.ema20)} / {_fmt_f(card.ema50)}", inline=True)
    e.add_field(name="RSI14 | MACD Δ", value=f"{_fmt_f(card.rsi14,1)} | {_fmt_f(card.macd_diff,3)}", inline=True)
    e.add_field(name="Vol/Avg20", value=f"{_fmt_f(card.vol_vs_avg20,2)}x", inline=True)
    e.add_field(name="Why", value=card.why, inline=False)
    e.set_footer(text="Premarket Scanner • multi-signal")
    return e

def render_earnings_page_embed(page: List[Dict], days: int, page_num: int, total_pages: int) -> discord.Embed:
    e = discord.Embed(title=f"Earnings within ±{days} days", color=0xF1C40F)
    e.description = "\n".join(f"• **{r['symbol']}** — {r['date'].isoformat()}" for r in page)
    e.set_footer(text=f"Page {page_num}/{total_pages} • Source: yfinance • cached 12h")
    return e
