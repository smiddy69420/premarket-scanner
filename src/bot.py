# src/bot.py
import os
import sys
import asyncio
import datetime as dt
from typing import List, Tuple, Optional

import discord
from discord import app_commands, Embed
from discord.ext import commands

import pandas as pd
import yfinance as yf

# Ensure project root is importable (safety if run from elsewhere)
from pathlib import Path
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from utils.universe import UniverseManager

# ---------- intents & bot ----------
intents = discord.Intents.default()
intents.guilds = True  # Slash commands don’t need message content intent
bot = commands.Bot(command_prefix="!", intents=intents)

UNIVERSE = UniverseManager()

ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")
ADMIN_ID_INT = int(ADMIN_USER_ID) if ADMIN_USER_ID and ADMIN_USER_ID.isdigit() else None

# ---------- helpers ----------
def fmt_money(x: float) -> str:
    return f"${x:,.2f}"

def pct(x: float) -> str:
    return f"{x:+.2f}%"

def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = (delta.clip(lower=0)).rolling(window=period, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).rolling(window=period, min_periods=period).mean()
    rs = gain / loss.replace(0, 1e-9)
    return 100 - (100 / (1 + rs))

async def download_price_history(symbol: str, period: str = "6mo") -> Optional[pd.DataFrame]:
    loop = asyncio.get_event_loop()
    def _job():
        df = yf.download(symbol, period=period, interval="1d", auto_adjust=False, progress=False)
        if isinstance(df, pd.DataFrame) and not df.empty:
            return df
        return None
    return await loop.run_in_executor(None, _job)

async def get_next_earnings_date(symbol: str, limit: int = 12) -> Optional[pd.Timestamp]:
    loop = asyncio.get_event_loop()
    def _job():
        try:
            t = yf.Ticker(symbol)
            df = t.get_earnings_dates(limit=limit)
            if df is None or df.empty:
                return None
            # Support both formats (date as column or index)
            if "Earnings Date" in df.columns:
                s = pd.to_datetime(df["Earnings Date"])
            else:
                s = pd.to_datetime(df.index)
            now = pd.Timestamp.utcnow().tz_localize(None)
            future = s[s >= now]
            return future.min() if not future.empty else s.max()
        except Exception:
            return None
    return await loop.run_in_executor(None, _job)

def build_scan_embed(symbol: str, df: pd.DataFrame) -> Embed:
    close = float(df["Close"].iloc[-1])
    ema20 = float(ema(df["Close"], 20).iloc[-1])
    ema50 = float(ema(df["Close"], 50).iloc[-1])
    rsi14 = float(rsi(df["Close"], 14).iloc[-1])

    def perf(days: int) -> float:
        if len(df) < days + 1:
            return 0.0
        return (df["Close"].iloc[-1] / df["Close"].iloc[-(days+1)] - 1) * 100

    p1d = perf(1)
    p5d = perf(5)
    p1m = perf(21)  # ~1 trading month

    low52 = float(df["Low"].rolling(252, min_periods=1).min().iloc[-1])
    high52 = float(df["High"].rolling(252, min_periods=1).max().iloc[-1])

    bias = "CALL" if close > ema20 > ema50 else "PUT"
    why = []
    if close > ema20: why.append("Close > EMA20")
    if ema20 > ema50: why.append("EMA20 > EMA50")
    if close < ema20: why.append("Close < EMA20")
    if ema20 < ema50: why.append("EMA20 < EMA50")
    why.append(f"RSI: {rsi14:.1f}")

    e = Embed(title=f"{symbol} • {bias}", color=0x00D084 if bias == "CALL" else 0xE33E3E)
    e.add_field(name="Last", value=fmt_money(close), inline=True)
    e.add_field(name="1D / 5D / 1M", value=f"{pct(p1d)} / {pct(p5d)} / {pct(p1m)}", inline=True)
    e.add_field(name="52W Range", value=f"{fmt_money(low52)} – {fmt_money(high52)}", inline=True)
    e.add_field(name="EMA20/50", value=f"{ema20:.2f} / {ema50:.2f}", inline=True)
    e.add_field(name="RSI(14)", value=f"{rsi14:.1f}", inline=True)
    e.add_field(name="Why", value="; ".join(why), inline=False)
    e.set_footer(text="Premarket Scanner • multi-signal")
    return e

# ---------- background: symbols weekly ----------
async def _refresh_symbols_weekly():
    """
    1) Generate symbols file if missing.
    2) Keep it fresh weekly without blocking the bot.
    """
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, UNIVERSE.ensure_file_exists)

    # Run the blocking refresher in a worker thread via executor
    def _run():
        UNIVERSE.weekly_refresh_forever()
    await loop.run_in_executor(None, _run)

# ---------- lifecycle ----------
@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
        print("[INFO] Slash commands synced.")
    except Exception as e:
        print(f"[WARN] Could not sync commands: {e}")

    bot.loop.create_task(_refresh_symbols_weekly())
    print(f"[INFO] Logged in as {bot.user} (id={bot.user.id})")

# ---------- commands ----------
@bot.tree.command(name="ping", description="Health check")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("🏓 Pong", ephemeral=True)

@bot.tree.command(name="sync", description="(Admin) Force resync slash commands")
async def sync_cmd(interaction: discord.Interaction):
    if ADMIN_ID_INT is None or interaction.user.id != ADMIN_ID_INT:
        await interaction.response.send_message("Not allowed.", ephemeral=True)
        return
    await bot.tree.sync()
    await interaction.response.send_message("✅ Synced.", ephemeral=True)

@bot.tree.command(name="scan_ticker", description="Scan a single ticker with technicals")
@app_commands.describe(symbol="Stock symbol like AAPL")
async def scan_ticker(interaction: discord.Interaction, symbol: str):
    symbol = symbol.upper().strip()
    await interaction.response.defer(thinking=True)
    df = await download_price_history(symbol, period="6mo")
    if df is None or df.empty:
        await interaction.followup.send(f"❌ No data for {symbol}")
        return
    await interaction.followup.send(embed=build_scan_embed(symbol, df))

@bot.tree.command(name="scan", description="Broad scan across the current universe")
@app_commands.describe(limit="Max number of tickers to scan (default 50)")
async def scan(interaction: discord.Interaction, limit: int = 50):
    await interaction.response.defer(thinking=True)

    universe = UNIVERSE.get_universe()
    symbols = universe[: max(1, min(limit, len(universe)))]

    async def scan_one(sym: str) -> Tuple[str, Optional[Embed]]:
        df = await download_price_history(sym, period="6mo")
        if df is None or df.empty:
            return sym, None
        return sym, build_scan_embed(sym, df)

    sem = asyncio.Semaphore(8)
    async def bound(sym: str):
        async with sem:
            return await scan_one(sym)

    results = await asyncio.gather(*[bound(s) for s in symbols])

    sent = 0
    for sym, emb in results:
        if emb:
            await interaction.followup.send(embed=emb)
            sent += 1
    if sent == 0:
        await interaction.followup.send("No picks found (or no data).")

@bot.tree.command(name="earnings_watch", description="Find tickers with earnings within N days")
@app_commands.describe(days="Days ahead (default 30)", limit="Max tickers to check (default 300)")
async def earnings_watch(interaction: discord.Interaction, days: int = 30, limit: int = 300):
    await interaction.response.defer(thinking=True)

    universe = UNIVERSE.get_universe()
    symbols = universe[: max(1, min(limit, len(universe)))]
    window = pd.Timedelta(days=abs(days))
    now = pd.Timestamp.utcnow().tz_localize(None)

    async def check(sym: str) -> Optional[Tuple[str, pd.Timestamp]]:
        d = await get_next_earnings_date(sym, limit=12)
        if d is None:
            return None
        if abs((d - now)) <= window:
            return (sym, d)
        return None

    sem = asyncio.Semaphore(8)
    async def bound(sym: str):
        async with sem:
            return await check(sym)

    hits: List[Tuple[str, pd.Timestamp]] = []
    for i in range(0, len(symbols), 50):
        chunk = symbols[i:i+50]
        parts = await asyncio.gather(*[bound(s) for s in chunk])
        for it in parts:
            if it:
                hits.append(it)

    if not hits:
        await interaction.followup.send(f"No earnings within ±{days} days.")
        return

    hits.sort(key=lambda x: x[1])
    lines = [f"• **{sym}** — {d.strftime('%Y-%m-%d')}" for sym, d in hits[:100]]
    e = Embed(
        title=f"Earnings within ±{days} days",
        description="\n".join(lines),
        color=0x2F81F7,
    )
    e.set_footer(text=f"Universe size scanned: {len(symbols)} • Source: Yahoo Finance")
    await interaction.followup.send(embed=e)

# --- admin helpers ---
@bot.tree.command(name="universe_stats", description="(Admin) Show current universe source and size")
async def universe_stats(interaction: discord.Interaction):
    if ADMIN_ID_INT is None or interaction.user.id != ADMIN_ID_INT:
        await interaction.response.send_message("Not allowed.", ephemeral=True)
        return
    src = os.getenv("ALL_TICKERS") and "ALL_TICKERS env" or \
          (os.getenv("SYMBOLS_FILE", "data/symbols_robinhood.txt"))
    uni = UNIVERSE.get_universe()
    await interaction.response.send_message(
        f"Source: **{src}**\nSymbols loaded: **{len(uni)}**", ephemeral=True
    )

@bot.tree.command(name="refresh_universe", description="(Admin) Regenerate symbols file now")
async def refresh_universe(interaction: discord.Interaction):
    if ADMIN_ID_INT is None or interaction.user.id != ADMIN_ID_INT:
        await interaction.response.send_message("Not allowed.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    ok = await asyncio.get_event_loop().run_in_executor(None, UNIVERSE.refresh_once)
    if ok:
        await interaction.followup.send("✅ Symbols refreshed.", ephemeral=True)
    else:
        await interaction.followup.send("❌ Refresh failed. Check logs.", ephemeral=True)

# ---------- run ----------
def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Missing DISCORD_TOKEN env variable.")
    bot.run(token)

if __name__ == "__main__":
    main()
