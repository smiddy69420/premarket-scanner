# bot.py
import os
import asyncio
import math
import datetime as dt
from typing import List, Tuple, Optional

import discord
from discord import app_commands, Embed
from discord.ext import commands

import pandas as pd
import yfinance as yf

from utils.universe import UniverseManager

# ---------- intents & bot ----------
intents = discord.Intents.default()
intents.guilds = True
# message_content not required for slash commands
bot = commands.Bot(command_prefix="!", intents=intents)

UNIVERSE = UniverseManager()

ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")
ADMIN_ID_INT = int(ADMIN_USER_ID) if ADMIN_USER_ID and ADMIN_USER_ID.isdigit() else None

# ---------- helpers ----------
def fmt_money(x: float) -> str:
    return f"${x:,.2f}"

def pct(a: float) -> str:
    s = f"{a:+.2f}%"
    return s

def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = (delta.clip(lower=0)).rolling(window=period).mean()
    loss = (-delta.clip(upper=0)).rolling(window=period).mean()
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
            # dataframe often has index named "Earnings Date"
            col = "Earnings Date"
            if col in df.columns:
                # sometimes 'Earnings Date' is a column
                s = pd.to_datetime(df[col])
                # choose the next future date, else the most recent past
                now = pd.Timestamp.utcnow().tz_localize(None)
                future = s[s >= now]
                return future.min() if not future.empty else s.max()
            else:
                # sometimes the index is the date
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
    p1m = perf(21)  # trading days ~21

    low52 = float(df["Low"].rolling(252, min_periods=1).min().iloc[-1])
    high52 = float(df["High"].rolling(252, min_periods=1).max().iloc[-1])

    bias = "CALL" if close > ema20 > ema50 else "PUT"
    why_bits = []
    if close > ema20: why_bits.append("Close > EMA20")
    if ema20 > ema50: why_bits.append("EMA20 > EMA50")
    if close < ema20: why_bits.append("Close < EMA20")
    if ema20 < ema50: why_bits.append("EMA20 < EMA50")
    why_bits.append(f"RSI: {rsi14:.1f}")

    e = Embed(title=f"{symbol} • {bias}", color=0x00D084 if bias == "CALL" else 0xE33E3E)
    e.add_field(name="Last", value=fmt_money(close), inline=True)
    e.add_field(name="1D / 5D / 1M", value=f"{pct(p1d)} / {pct(p5d)} / {pct(p1m)}", inline=True)
    e.add_field(name="52W Range", value=f"{fmt_money(low52)} – {fmt_money(high52)}", inline=True)
    e.add_field(name="EMA20/50", value=f"{ema20:.2f} / {ema50:.2f}", inline=True)
    e.add_field(name="RSI(14)", value=f"{rsi14:.1f}", inline=True)
    e.add_field(name="Why", value="; ".join(why_bits), inline=False)
    e.set_footer(text="Premarket Scanner • multi-signal")
    return e

# ---------- background: symbols weekly ----------
async def _refresh_symbols_weekly():
    """
    1) ensure we have data/symbols_robinhood.txt on boot.
    2) keep it fresh weekly without blocking the bot.
    """
    loop = asyncio.get_event_loop()
    # one-time generate if missing
    await loop.run_in_executor(None, UNIVERSE.ensure_file_exists)

    # do the weekly refresher (blocking loop moved to a worker thread)
    def _run():
        UNIVERSE.weekly_refresh_forever()
    await loop.run_in_executor(None, _run)

# ---------- lifecycle ----------
@bot.event
async def on_ready():
    # sync slash commands globally (or change to guild-only if you prefer)
    try:
        await bot.tree.sync()
        print("[INFO] Slash commands synced.")
    except Exception as e:
        print(f"[WARN] Could not sync commands: {e}")

    # start the background symbols refresher
    bot.loop.create_task(_refresh_symbols_weekly())

    print(f"[INFO] Logged in as {bot.user} (id={bot.user.id})")

# ---------- commands ----------
@bot.tree.command(name="ping", description="Health check")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("🏓 Pong", ephemeral=True)

# admin-only sync (optional)
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
    embed = build_scan_embed(symbol, df)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="scan", description="Broad scan across the current universe")
@app_commands.describe(limit="Max number of tickers to scan (to avoid timeouts)")
async def scan(interaction: discord.Interaction, limit: int = 50):
    await interaction.response.defer(thinking=True)

    universe = UNIVERSE.get_universe()
    symbols = universe[: max(1, min(limit, len(universe)))]

    async def scan_one(sym: str) -> Tuple[str, Optional[Embed]]:
        df = await download_price_history(sym, period="6mo")
        if df is None or df.empty:
            return sym, None
        return sym, build_scan_embed(sym, df)

    # small concurrency to keep Yahoo happy
    sem = asyncio.Semaphore(8)
    async def bound(sym: str):
        async with sem:
            return await scan_one(sym)

    tasks = [bound(s) for s in symbols]
    results: List[Tuple[str, Optional[Embed]]] = await asyncio.gather(*tasks)

    sent = 0
    for sym, emb in results:
        if emb:
            await interaction.followup.send(embed=emb)
            sent += 1

    if sent == 0:
        await interaction.followup.send("No picks found (or no data).")

@bot.tree.command(name="earnings_watch", description="Find tickers with earnings within N days")
@app_commands.describe(days="Days ahead (default 30)", limit="Max tickers to check")
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

    hits = []
    for chunk_start in range(0, len(symbols), 50):
        chunk = symbols[chunk_start:chunk_start+50]
        parts = await asyncio.gather(*[bound(s) for s in chunk])
        for item in parts:
            if item:
                hits.append(item)

    if not hits:
        await interaction.followup.send(f"No earnings within ±{days} days.")
        return

    # sort by date soonest first
    hits.sort(key=lambda x: x[1])
    lines = []
    for sym, d in hits[:100]:
        dd = d.strftime("%Y-%m-%d")
        lines.append(f"• **{sym}** — {dd}")
    text = "\n".join(lines)
    e = Embed(title=f"Earnings within ±{days} days", description=text, color=0x2F81F7)
    e.set_footer(text=f"Universe size scanned: {len(symbols)}  •  Source: Yahoo Finance via yfinance")
    await interaction.followup.send(embed=e)

# ---------- run ----------
def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Missing DISCORD_TOKEN env variable.")
    bot.run(token)

if __name__ == "__main__":
    main()
