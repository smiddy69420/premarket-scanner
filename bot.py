# bot.py
import os
import traceback
import datetime as dt
import discord
from discord import app_commands

import scanner  # our analysis helpers

# -------- ENV (GUILD_ID strongly recommended so commands sync instantly)
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = os.getenv("DISCORD_GUILD_ID")  # 18-digit server ID, optional but recommended
CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")  # optional
UNIVERSE_ENV = os.getenv("SCAN_UNIVERSE", "")  # comma-separated tickers, optional

if not TOKEN:
    raise RuntimeError("Missing DISCORD_BOT_TOKEN in environment.")

GUILD = discord.Object(int(GUILD_ID)) if GUILD_ID and GUILD_ID.isdigit() else None

# Default “universe” if SCAN_UNIVERSE isn’t set
DEFAULT_UNIVERSE = [
    "AAPL","MSFT","NVDA","TSLA","AMZN","META","GOOGL",
    "AMD","JPM","NFLX","AVGO","KO","PEP","XOM","CVX","WMT","DIS","BA","INTC","CSCO"
]
UNIVERSE = [x.strip().upper() for x in UNIVERSE_ENV.split(",") if x.strip()] or DEFAULT_UNIVERSE

# -------- Discord client (slash-only — no message content intent required)
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


def scope_kwargs():
    """If GUILD is provided, keep commands guild-scoped for instant updates."""
    return dict(guild=GUILD) if GUILD else {}


async def safe_respond(interaction: discord.Interaction, *, content=None, embed=None, ephemeral=False):
    """Never throw 'Unknown interaction'. Uses response if unused, otherwise followup."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral)
    except discord.NotFound:
        # User navigated away before reply; ignore gracefully
        pass


def color_for_bias(bias: str) -> discord.Color:
    return discord.Color.green() if bias == "CALL" else (discord.Color.red() if bias == "PUT" else discord.Color.gold())


@client.event
async def on_ready():
    try:
        # Copy guild commands to global when no GUILD is provided (slower to propagate),
        # but prefer guild-scoped if GUILD is set (instant).
        if GUILD:
            await tree.sync(guild=GUILD)
        else:
            await tree.sync()
        print(f"✅ {client.user} ready • commands synced at {dt.datetime.now().isoformat()}")
    except Exception:
        print("Command sync failed:\n", traceback.format_exc())


@client.event
async def on_guild_available(guild: discord.Guild):
    """Resync on cold starts/reconnects so changed command schemas appear immediately."""
    try:
        if GUILD and guild.id == int(GUILD.id):
            await tree.sync(guild=GUILD)
            print(f"🔁 Resynced commands for guild {guild.id}")
    except Exception:
        print("Resync failed:\n", traceback.format_exc())


# ---------------- Slash Commands ----------------

@tree.command(name="ping", description="Check bot status", **scope_kwargs())
async def ping_cmd(interaction: discord.Interaction):
    await safe_respond(interaction, content="📍 Pong", ephemeral=True)


@tree.command(name="scan_ticker", description="Analyze one ticker (signals, stats, sentiment)", **scope_kwargs())
@app_commands.describe(ticker="Stock ticker, e.g., NVDA")
async def scan_ticker_cmd(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer(thinking=True)
    t = (ticker or "").strip().upper()

    try:
        data = scanner.analyze_one_ticker(t)
        # Build the card
        desc = []
        desc.append(f"**Last:** ${data['last_price']:.2f}")
        desc.append(f"**1D / 5D / 1M:** {data['change_1d']:.2f}% / {data['change_5d']:.2f}% / {data['change_1m']:.2f}%")
        desc.append(f"**52W Range:** ${data['low_52']:.2f} – ${data['high_52']:.2f}")
        desc.append(f"**RSI:** {data['rsi'] if data['rsi'] is not None else '—'} | **MACD Δ:** {data['macd'] if data['macd'] is not None else '—'}")
        desc.append(f"**EMA20/50:** {data['ema20'] if data['ema20'] is not None else '—'} / {data['ema50'] if data['ema50'] is not None else '—'}")
        if data['volume_ratio'] is not None:
            desc.append(f"**Vol/Avg20:** {data['volume_ratio']}x")
        desc.append(f"**News Sentiment:** {data['sentiment']:+.2f}")
        why = "\n".join(desc)

        emb = discord.Embed(
            title=f"{data['ticker']} • {data['rec']}",
            description=why,
            color=color_for_bias(data['rec'])
        )
        await interaction.followup.send(embed=emb)
    except ValueError as ve:
        await interaction.followup.send(f"❌ Could not analyze **{t}**: {ve}", ephemeral=True)
    except Exception:
        print("scan_ticker crash:\n", traceback.format_exc())
        await interaction.followup.send(f"❌ Could not analyze **{t}** due to an internal error.", ephemeral=True)


@tree.command(name="earnings_watch", description="Show earnings within ±days (default 7). Leave ticker blank to scan universe.", **scope_kwargs())
@app_commands.describe(ticker="Optional single ticker (e.g., JPM)", days="Window size in days (1–30). Default 7.")
async def earnings_watch_cmd(interaction: discord.Interaction, ticker: str | None = None, days: int = 7):
    await interaction.response.defer(thinking=True)

    try:
        window = max(1, min(30, int(days)))
    except Exception:
        window = 7

    try:
        tickers = [ticker.strip().upper()] if ticker else UNIVERSE
        matches = scanner.earnings_within_window(tickers, days=window)
        if not matches:
            scope = (ticker or "current universe").upper() if ticker else "current universe"
            await interaction.followup.send(f"No earnings within ±{window} days for **{scope}**.")
            return

        lines = [f"**{t}** → {d.strftime('%Y-%m-%d')}" for t, d in matches]
        emb = discord.Embed(
            title=f"Earnings within ±{window} days",
            description="\n".join(lines),
            color=discord.Color.blurple()
        )
        await interaction.followup.send(embed=emb)
    except Exception:
        print("earnings_watch crash:\n", traceback.format_exc())
        await interaction.followup.send("❌ Earnings watch failed due to an internal error.", ephemeral=True)


@tree.command(name="help", description="Show commands and how to read signals", **scope_kwargs())
async def help_cmd(interaction: discord.Interaction):
    text = (
        "**Commands**\n"
        "• `/scan_ticker SYMBOL` — analyze one ticker (signals/stats/sentiment)\n"
        "• `/earnings_watch [ticker] [days]` — earnings within ±days (default 7)\n\n"
        "**How to read a signal**\n"
        "• **Bias:** CALL (green) or PUT (red) from trend + momentum\n"
        "• **Buy Range:** watch pullbacks vs EMA20/EMA50\n"
        "• **Target/Stop:** recent swing levels\n"
        "• **Risk:** liquidity/volatility heuristics\n"
    )
    emb = discord.Embed(title="Scanner Help", description=text, color=discord.Color.dark_grey())
    await safe_respond(interaction, embed=emb, ephemeral=True)


client.run(TOKEN)
