# bot.py
import os
import traceback
import datetime as dt
import discord
from discord import app_commands
import scanner  # local module

# ---- ENV ----
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = os.getenv("DISCORD_GUILD_ID")           # optional, 18-digit server ID
CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")       # optional, 18-digit channel ID
UNIVERSE_ENV = os.getenv("SCAN_UNIVERSE", "")      # optional, comma list

if not TOKEN:
    raise RuntimeError("Missing DISCORD_BOT_TOKEN in environment.")

GUILD = discord.Object(int(GUILD_ID)) if (GUILD_ID and GUILD_ID.isdigit()) else None

DEFAULT_UNIVERSE = [
    "AAPL","MSFT","NVDA","TSLA","AMZN","META","GOOGL","AMD","JPM","NFLX",
    "AVGO","KO","PEP","XOM","CVX","WMT","DIS","BA","INTC","CSCO"
]
UNIVERSE = [x.strip().upper() for x in UNIVERSE_ENV.split(",") if x.strip()] or DEFAULT_UNIVERSE

# ---- DISCORD CLIENT ----
intents = discord.Intents.default()  # slash-only
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


async def safe_respond(interaction: discord.Interaction, *, content=None, embed=None, ephemeral=False):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral)
    except discord.NotFound:
        pass


def color_for_bias(bias: str) -> discord.Color:
    return discord.Color.green() if bias == "CALL" else (discord.Color.red() if bias == "PUT" else discord.Color.gold())


@client.event
async def on_ready():
    try:
        if GUILD:
            tree.copy_global_to(guild=GUILD)
            sg = await tree.sync(guild=GUILD)
            print(f"🔁 Synced {len(sg)} guild commands to {GUILD.id}")
        sg2 = await tree.sync()
        print(f"✅ Synced {len(sg2)} global commands • {dt.datetime.now().isoformat()}")
        print("Loaded:", [c.name for c in tree.get_commands()])
    except Exception:
        print("❌ Command sync failed:\n", traceback.format_exc())


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print("Slash command error:", repr(error), "\n", traceback.format_exc())
    await safe_respond(interaction, content="⚠️ Something went wrong while processing that command.", ephemeral=True)

# ---- COMMANDS ----

@tree.command(name="ping", description="Check bot status")
async def ping_cmd(interaction: discord.Interaction):
    await safe_respond(interaction, content="📍 Pong", ephemeral=True)


@tree.command(name="scan_ticker", description="Analyze one ticker (signals, stats, sentiment)")
@app_commands.describe(ticker="Stock ticker, e.g., NVDA")
async def scan_ticker_cmd(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer(thinking=True)
    t = (ticker or "").strip().upper()
    try:
        data = scanner.analyze_one_ticker(t)

        lines = []
        lines.append(f"**Last:** ${data['last_price']:.2f}")
        lines.append(f"**1D / 5D / 1M:** {data['change_1d']:.2f}% / {data['change_5d']:.2f}% / {data['change_1m']:.2f}%")
        lines.append(f"**52W Range:** ${data['low_52']:.2f} – ${data['high_52']:.2f}")
        lines.append(f"**EMA20/50:** {data['ema20'] if data['ema20'] is not None else '—'} / {data['ema50'] if data['ema50'] is not None else '—'}")
        lines.append(f"**RSI:** {data['rsi'] if data['rsi'] is not None else '—'} | **MACD Δ:** {data['macd'] if data['macd'] is not None else '—'}")
        if data['volume_ratio'] is not None:
            lines.append(f"**Vol/Avg20:** {data['volume_ratio']}x")
        lines.append(f"**News Sentiment:** {data['sentiment']:+.2f}")
        if data.get('earnings_date'):
            lines.append(f"**Nearest Earnings:** {data['earnings_date']}")
        emb = discord.Embed(title=f"{data['ticker']} • {data['rec']}", description="\n".join(lines), color=color_for_bias(data['rec']))
        await interaction.followup.send(embed=emb)
    except ValueError as ve:
        await interaction.followup.send(f"❌ Could not analyze **{t}**: {ve}", ephemeral=True)
    except Exception:
        print("scan_ticker crash:\n", traceback.format_exc())
        await interaction.followup.send(f"❌ Could not analyze **{t}** due to an internal error.", ephemeral=True)


@tree.command(
    name="earnings_watch",
    description="Show earnings within ±days (default 7). Leave ticker blank to scan the universe."
)
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
        out = [f"**{t}** → {d.strftime('%Y-%m-%d')}" for t, d in matches]
        emb = discord.Embed(title=f"Earnings within ±{window} days", description="\n".join(out), color=discord.Color.blurple())
        await interaction.followup.send(embed=emb)
    except Exception:
        print("earnings_watch crash:\n", traceback.format_exc())
        await interaction.followup.send("❌ Earnings watch failed due to an internal error.", ephemeral=True)


@tree.command(name="help", description="Show commands and how to read signals")
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
