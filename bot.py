import os
import traceback

import discord
from discord import app_commands
from discord.ext import commands

import scanner

# ------------ Config via env (guild/channel optional)
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = os.getenv("DISCORD_GUILD_ID")  # optional
CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")  # optional
UNIVERSE_ENV = os.getenv("SCAN_UNIVERSE", "")  # optional: comma-separated tickers

if not TOKEN:
    raise RuntimeError("Missing DISCORD_BOT_TOKEN in environment.")

GUILD_OBJ = discord.Object(id=int(GUILD_ID)) if GUILD_ID and GUILD_ID.isdigit() else None

DEFAULT_UNIVERSE = [
    # solid, liquid names; you can change in env SCAN_UNIVERSE
    "AAPL","MSFT","NVDA","TSLA","AMZN","META","GOOGL","AMD","JPM","NFLX",
    "AVGO","KO","PEP","XOM","CVX","WMT","DIS","BA","INTC","CSCO"
]
UNIVERSE = [x.strip().upper() for x in UNIVERSE_ENV.split(",") if x.strip()] or DEFAULT_UNIVERSE

# ------------ Discord setup
intents = discord.Intents.default()
intents.message_content = False  # not needed for slash commands
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ------------ Helpers

async def safe_followup(interaction: discord.Interaction, content=None, embed=None, ephemeral=False):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral)
    except discord.errors.NotFound:
        # interaction expired; nothing to do
        pass

def color_for(rec: str) -> discord.Color:
    if rec == "CALL":
        return discord.Color.green()
    if rec == "PUT":
        return discord.Color.red()
    return discord.Color.gold()

def ensure_sync_scope():
    # sync local to guild for instant availability if GUILD_ID set,
    # else global (may take longer first time).
    if GUILD_OBJ:
        return dict(guild=GUILD_OBJ)
    return {}

# ------------ Events

@bot.event
async def on_ready():
    try:
        synced = await tree.sync(**ensure_sync_scope())
        print(f"✅ Logged in as {bot.user} | {len(synced)} commands synced")
    except Exception:
        print("Command sync failed:\n", traceback.format_exc())

# ------------ Commands

@tree.command(name="ping", description="Check bot status", **ensure_sync_scope())
async def ping_cmd(interaction: discord.Interaction):
    await safe_followup(interaction, "📍 Pong", ephemeral=True)

@tree.command(name="scan_ticker", description="Analyze one ticker (signals, stats, sentiment)", **ensure_sync_scope())
@app_commands.describe(ticker="Stock ticker, e.g. NVDA")
async def scan_ticker_cmd(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer(thinking=True)
    try:
        data = scanner.analyze_one_ticker(ticker)
        if not data:
            await interaction.followup.send(f"❌ Could not analyze **{ticker.upper()}**: no price data.", ephemeral=True)
            return

        desc_lines = [
            f"**Last:** ${data['last_price']:.2f}",
            f"**1D / 5D / 1M:** {data['change_1d']:.2f}% / {data['change_5d']:.2f}% / {data['change_1m']:.2f}%",
            f"**52W Range:** ${data['low_52']:.2f} – ${data['high_52']:.2f}",
            f"**RSI:** {data['rsi'] if data['rsi'] is not None else '—'} | **MACD Δ:** {data['macd'] if data['macd'] is not None else '—'}",
            f"**EMA20/50:** {data['ema20'] if data['ema20'] else '—'} / {data['ema50'] if data['ema50'] else '—'}",
            f"**Vol/Avg20:** {data['volume_ratio']}x" if data['volume_ratio'] is not None else "",
            f"**News Sentiment:** {data['sentiment']:+.2f}",
        ]
        desc = "\n".join([s for s in desc_lines if s])

        e = discord.Embed(
            title=f"{data['ticker']} • {data['rec']}",
            description=desc,
            color=color_for(data["rec"]),
        )
        await interaction.followup.send(embed=e)
    except Exception as e:
        print("scan_ticker error:\n", traceback.format_exc())
        await interaction.followup.send(f"❌ Could not analyze **{ticker.upper()}**: {e}", ephemeral=True)

@tree.command(name="earnings_watch", description="Earnings within ±7 days (or check one ticker)", **ensure_sync_scope())
@app_commands.describe(ticker="Optional single ticker to check", days="Window in days (default 7)")
async def earnings_watch_cmd(interaction: discord.Interaction, ticker: str = "", days: int = 7):
    await interaction.response.defer(thinking=True)
    try:
        days = max(1, min(30, int(days)))
    except Exception:
        days = 7

    try:
        tickers = [ticker] if ticker else UNIVERSE
        matches = scanner.earnings_within_window(tickers, days=days)
        if not matches:
            scope = ticker.upper() if ticker else "current universe"
            await interaction.followup.send(f"No earnings within ±{days} days for **{scope}**.", ephemeral=False)
            return

        lines = [f"**{t}** → {d.isoformat()}" for t, d in matches]
        e = discord.Embed(
            title=f"Earnings within ±{days} days",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.followup.send(embed=e)
    except Exception as e:
        print("earnings_watch error:\n", traceback.format_exc())
        await interaction.followup.send(f"❌ Earnings watch failed: {e}", ephemeral=True)

@tree.command(name="help", description="Show commands and how to read signals", **ensure_sync_scope())
async def help_cmd(interaction: discord.Interaction):
    e = discord.Embed(
        title="Scanner Help",
        description=(
            "Commands\n"
            "• `/scan_ticker SYMBOL` — analyze one ticker\n"
            "• `/earnings_watch [ticker] [days]` — earnings within ±days (default 7)\n\n"
            "How to read a signal\n"
            "• **Bias:** CALL (green) or PUT (red) from trend+momentum\n"
            "• **Buy Range:** use EMA20/50 and pullbacks\n"
            "• **Target/Stop:** use recent swing high/low\n"
            "• **Risk:** liquidity/volatility heuristics\n"
        ),
        color=discord.Color.dark_grey(),
    )
    await safe_followup(interaction, embed=e, ephemeral=True)

bot.run(TOKEN)
