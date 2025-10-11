import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

import pandas as pd
import yfinance as yf

# Tech indicators
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator

# Our universe manager (loads symbols list from data/symbols_robinhood.txt, or env fallback)
from utils.universe import UniverseManager

# ------------------------------------------------------------
# Logging / basic config
# ------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)7s] %(name)s: %(message)s",
)
logger = logging.getLogger("bot")

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
GUILD_ID = os.getenv("DISCORD_GUILD_ID", "")  # optional, speeds up slash command sync
CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID", "")  # optional default posting channel
KEEP_ALIVE = os.getenv("KEEP_ALIVE", "True").lower() == "true"

if not TOKEN:
    logger.error("DISCORD_BOT_TOKEN is not set. Exiting.")
    raise SystemExit(1)

intents = discord.Intents.default()
# If you ever need member/guild cache, enable more intents here.
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

universe = UniverseManager()

# ------------------------------------------------------------
# Small helpers
# ------------------------------------------------------------
def _chunk(lst: List[str], n: int):
    """Yield successive n-sized chunks from list."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]

async def _safe_followup(interaction: discord.Interaction, content: Optional[str] = None, embed: Optional[discord.Embed] = None):
    try:
        if embed:
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(content or "\u200b")
    except discord.HTTPException:
        logger.exception("Failed to send followup message.")

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

# ------------------------------------------------------------
# Indicators / scanning logic for a single ticker
# ------------------------------------------------------------
def analyze_ticker_daily(ticker: str) -> Tuple[Optional[discord.Embed], Optional[str]]:
    """
    Returns (embed, error) for a single ticker using daily bars.
    """
    try:
        df = yf.download(ticker, period="6mo", interval="1d", progress=False, auto_adjust=False)
    except Exception as e:
        return None, f"{ticker}: download error: {e}"

    if df is None or df.empty or len(df) < 60:
        return None, f"{ticker}: insufficient data"

    close = df["Close"].copy()

    ema20 = EMAIndicator(close, window=20).ema_indicator()
    ema50 = EMAIndicator(close, window=50).ema_indicator()
    rsi14 = RSIIndicator(close, window=14).rsi()

    macd_ind = MACD(close, window_slow=26, window_fast=12, window_sign=9)
    macd_line = macd_ind.macd()
    macd_signal = macd_ind.macd_signal()
    macd_hist = macd_ind.macd_diff()

    vol = df["Volume"]
    vol_avg20 = vol.rolling(20).mean()

    last = close.iloc[-1]
    one_d = (close.iloc[-1] / close.iloc[-2] - 1.0) * 100 if len(close) >= 2 else 0.0
    five_d = (close.iloc[-1] / close.iloc[-6] - 1.0) * 100 if len(close) >= 6 else 0.0
    one_m = (close.iloc[-1] / close.iloc[-21] - 1.0) * 100 if len(close) >= 21 else 0.0

    e20 = ema20.iloc[-1]
    e50 = ema50.iloc[-1]
    rsi = rsi14.iloc[-1]
    macd_val = macd_hist.iloc[-1]
    vol_ratio = (vol.iloc[-1] / vol_avg20.iloc[-1]) if vol_avg20.iloc[-1] > 0 else 1.0

    # Simple bias rules – you can tune these later
    if last > e20 > e50 and macd_val > 0 and rsi >= 50:
        bias = "CALL"
        why = f"Close > EMA20 > EMA50; MACD Δ: {macd_val:.3f}; RSI: {rsi:.1f}; Vol/Avg20: {vol_ratio:.2f}x"
    elif last < e20 < e50 and macd_val < 0 and rsi <= 50:
        bias = "PUT"
        why = f"Close < EMA20 < EMA50; MACD Δ: {macd_val:.3f}; RSI: {rsi:.1f}; Vol/Avg20: {vol_ratio:.2f}x"
    else:
        bias = "NEUTRAL"
        why = f"Mixed: EMA20={e20:.2f}, EMA50={e50:.2f}; MACD Δ: {macd_val:.3f}; RSI: {rsi:.1f}; Vol/Avg20: {vol_ratio:.2f}x"

    # Build a tidy embed
    emb = discord.Embed(
        title=f"{ticker} • {bias}",
        color=0x2ECC71 if bias == "CALL" else (0xE74C3C if bias == "PUT" else 0x95A5A6),
        timestamp=_now_utc(),
    )
    emb.add_field(name="Last", value=f"${last:,.2f}", inline=True)
    emb.add_field(name="1D / 5D / 1M", value=f"{one_d:+.2f}% / {five_d:+.2f}% / {one_m:+.2f}%", inline=True)
    emb.add_field(name="Vol/Avg20", value=f"{vol_ratio:.2f}x", inline=True)

    emb.add_field(name="EMA20 / 50", value=f"{e20:.2f} / {e50:.2f}", inline=True)
    emb.add_field(name="RSI(14) | MACD Δ", value=f"{rsi:.1f} | {macd_val:+.3f}", inline=True)
    emb.add_field(name="Why", value=why, inline=False)

    emb.set_footer(text="Premarket Scanner • daily")
    return emb, None

# ------------------------------------------------------------
# Earnings watch helpers
# ------------------------------------------------------------
def _next_earnings_within(ticker: str, days: int) -> Optional[datetime]:
    """
    Try to detect the next earnings date within N days.
    yfinance can be noisy; we try a couple approaches.
    """
    try:
        t = yf.Ticker(ticker)
        # 1) Use get_earnings_dates if available
        try:
            df = t.get_earnings_dates(limit=8)
            if df is not None and not df.empty:
                # df index is DatetimeIndex of event dates
                now = datetime.now(timezone.utc)
                horizon = now + timedelta(days=days)
                for dt in df.index:
                    # Normalize to timezone-aware UTC
                    dtu = dt if dt.tzinfo else dt.tz_localize("UTC")
                    if now <= dtu <= horizon:
                        return dtu
        except Exception:
            pass

        # 2) Fallback: use calendar attribute (older yfinance style)
        cal = t.calendar
        if isinstance(cal, pd.DataFrame) and "Earnings Date" in cal.index:
            raw = cal.loc["Earnings Date"].values
            if raw is not None and len(raw) >= 1:
                ed = raw[0]
                if isinstance(ed, (pd.Timestamp, datetime)):
                    dtu = ed.to_pydatetime() if isinstance(ed, pd.Timestamp) else ed
                    if dtu.tzinfo is None:
                        dtu = dtu.replace(tzinfo=timezone.utc)
                    now = datetime.now(timezone.utc)
                    if now <= dtu <= now + timedelta(days=days):
                        return dtu
    except Exception:
        # swallow and treat as "unknown"
        return None
    return None

async def _earnings_scan(tickers: List[str], days: int, max_concurrency: int = 10) -> List[Tuple[str, datetime]]:
    sem = asyncio.Semaphore(max_concurrency)
    results: List[Tuple[str, datetime]] = []

    async def worker(sym: str):
        async with sem:
            loop = asyncio.get_running_loop()
            dt = await loop.run_in_executor(None, _next_earnings_within, sym, days)
            if dt:
                results.append((sym, dt))

    tasks = [asyncio.create_task(worker(t)) for t in tickers]
    await asyncio.gather(*tasks, return_exceptions=True)
    results.sort(key=lambda x: x[1])
    return results

# ------------------------------------------------------------
# Discord events & commands
# ------------------------------------------------------------
@bot.event
async def on_ready():
    logger.info("Logged in as %s (%s)", bot.user, bot.user.id)

    # Load/refresh the universe
    await universe.initialize()
    bot.loop.create_task(universe.refresh_weekly_forever())

    # Fast, deterministic slash-command sync to a single guild if provided
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            await tree.sync(guild=guild)
            logger.info("Slash commands synced to guild %s", GUILD_ID)
        else:
            await tree.sync()
            logger.info("Slash commands synced globally (can take ~1h the first time).")
    except Exception:
        logger.exception("Slash command sync failed")

    logger.info("Bot ready.")

# Simple health check
@tree.command(name="ping", description="Latency/health check")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! Latency: {bot.latency*1000:.0f} ms")

# Manual resync if you change commands
@tree.command(name="sync", description="Admin-only: force resync of slash commands")
async def sync(interaction: discord.Interaction):
    # Gate: only server owner or user with manage_guild (adjust if needed)
    if not (interaction.user == interaction.guild.owner or interaction.user.guild_permissions.manage_guild):
        await interaction.response.send_message("Not allowed.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        if GUILD_ID:
            await tree.sync(guild=discord.Object(id=int(GUILD_ID)))
        else:
            await tree.sync()
        await interaction.followup.send("Synced.")
    except Exception as e:
        await interaction.followup.send(f"Sync failed: {e}")

# Ticker scan with indicators
@tree.command(name="scan_ticker", description="Analyze a single ticker (daily).")
@app_commands.describe(ticker="Symbol, e.g., NVDA")
async def scan_ticker(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer(thinking=True)
    ticker = ticker.strip().upper()
    embed, err = analyze_ticker_daily(ticker)
    if err:
        await _safe_followup(interaction, f"{err}")
        return
    await _safe_followup(interaction, embed=embed)

# Broad upcoming earnings scan across the maintained universe
@tree.command(name="earnings_watch", description="Upcoming earnings across the broad universe.")
@app_commands.describe(days="Look-ahead window in days (default 30)", limit="How many symbols to check this run (default 300)")
async def earnings_watch(interaction: discord.Interaction, days: int = 30, limit: int = 300):
    await interaction.response.defer(thinking=True)
    days = max(1, min(120, days))
    limit = max(25, min(3000, limit))  # safety guard
    symbols = universe.get(limit=None)  # full list
    if not symbols:
        await _safe_followup(interaction, "Universe is empty.")
        return

    # Clip this run to 'limit' to control request count
    target = symbols[:limit]
    results = await _earnings_scan(target, days=days, max_concurrency=12)

    if not results:
        await _safe_followup(interaction, f"No earnings within {days} days in the first {limit} tickers.")
        return

    # Build a clean text block (paginate if long)
    lines = [f"**Upcoming earnings (≤ {days} days, first {limit} names):**"]
    for sym, dt in results[:200]:  # keep Discord message size in check
        ts = dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
        lines.append(f"- `{sym}` → {ts}")
    text = "\n".join(lines)
    await _safe_followup(interaction, text)

# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
def main():
    logger.info("Starting bot…")
    bot.run(TOKEN, log_handler=None)

if __name__ == "__main__":
    main()
