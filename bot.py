# bot.py
import os
import asyncio
import datetime as dt
import discord
from discord import app_commands
from discord.ext import commands

from scanner import analyze_one, earnings_watch_text

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = os.getenv("DISCORD_GUILD_ID")  # optional but recommended for fast sync
DEFAULT_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")  # optional for /scan_now

if not TOKEN:
    raise RuntimeError("Missing DISCORD_BOT_TOKEN in environment.")

intents = discord.Intents.default()
intents.message_content = True  # for safety if ever needed
bot = commands.Bot(command_prefix="!", intents=intents)

tree = bot.tree

# ---------- Helpers ----------

def make_signal_embed(result: dict) -> discord.Embed:
    sym = result["symbol"]
    if not result.get("ok"):
        e = discord.Embed(
            title=f"❌ Could not analyze {sym}",
            description=result.get("error", "Unknown error"),
            color=discord.Color.red()
        )
        e.set_footer(text="Try again in a minute or use a different interval.")
        return e

    price = result["price"]
    sig = result["signal"]
    reasons = result.get("reasons", [])

    color = discord.Color.yellow()
    if sig == "CALL":
        color = discord.Color.green()
    elif sig == "PUT":
        color = discord.Color.red()

    e = discord.Embed(
        title=f"{sym} • {sig}",
        description=f"**Last Price:** ${price:,.2f}",
        color=color,
        timestamp=discord.utils.utcnow()
    )
    if reasons:
        e.add_field(name="Why", value="• " + "\n• ".join(reasons), inline=False)
    e.set_footer(text="Premarket Scanner • TA snapshot (multi-interval fallback)")
    return e


async def safe_followup(interaction: discord.Interaction, embed: discord.Embed, ephemeral: bool = False):
    """
    Sends a followup safely after we've already deferred.
    """
    try:
        await interaction.followup.send(embed=embed, ephemeral=ephemeral)
    except discord.errors.NotFound:
        # Interaction expired; try channel fallback if possible
        if interaction.channel:
            await interaction.channel.send(embed=embed)


# ---------- Events ----------

@bot.event
async def on_ready():
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            await tree.sync(guild=guild)
            print(f"[sync] Commands synced to guild {GUILD_ID}")
        else:
            await tree.sync()
            print("[sync] Global commands synced")
    except Exception as e:
        print(f"[sync error] {e}")
    print(f"Logged in as {bot.user} (id={bot.user.id})")


# ---------- Commands ----------

@tree.command(name="ping", description="Check if the bot is alive.")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=False, ephemeral=True)
    await safe_followup(interaction, discord.Embed(title="🏓 Pong", color=discord.Color.blurple()), ephemeral=True)


@tree.command(name="scan_ticker", description="Analyze one ticker on demand (e.g., NVDA, TSLA, JPM).")
@app_commands.describe(symbol="Ticker symbol, e.g., NVDA")
async def scan_ticker_cmd(interaction: discord.Interaction, symbol: str):
    await interaction.response.defer(thinking=True, ephemeral=False)
    try:
        result = analyze_one(symbol)
        embed = make_signal_embed(result)
    except Exception as e:
        embed = discord.Embed(
            title=f"❌ Could not analyze {symbol.upper()}",
            description=str(e),
            color=discord.Color.red()
        )
    await safe_followup(interaction, embed, ephemeral=False)


@tree.command(name="earnings", description="Show earnings date if within ±7 days.")
@app_commands.describe(symbol="Ticker symbol, e.g., AAPL")
async def earnings_cmd(interaction: discord.Interaction, symbol: str):
    await interaction.response.defer(thinking=True, ephemeral=False)
    try:
        msg = earnings_watch_text(symbol, days_window=7)
        embed = discord.Embed(title="Earnings Watch", description=msg, color=discord.Color.orange())
    except Exception as e:
        embed = discord.Embed(title="Earnings Watch", description=f"❌ {e}", color=discord.Color.red())
    await safe_followup(interaction, embed, ephemeral=False)


@tree.command(name="help", description="Show available commands and tips.")
async def help_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=False, ephemeral=True)
    desc = (
        "**/ping** — quick connectivity check\n"
        "**/scan_ticker SYMBOL** — analyze one ticker on demand (e.g., NVDA)\n"
        "**/earnings SYMBOL** — earnings date if within ±7 days\n"
        "**/scan_now** — run the full ranked scan (if configured)\n\n"
        "Tips:\n"
        "• Results use multiple data fallbacks (1m → 5m → 1h → 1d) to reduce empty-data errors.\n"
        "• If a symbol is an ETF or has no fundamentals, earnings may be N/A.\n"
    )
    e = discord.Embed(title="Premarket Scanner — Help", description=desc, color=discord.Color.blurple())
    await safe_followup(interaction, e, ephemeral=True)


# Optional: if you already have a ranked scanner wired up, leave this here.
@tree.command(name="scan_now", description="Run the full ranked scan and post results.")
async def scan_now_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=False)
    # Placeholder hook: integrate your existing ranked scan dispatcher here if needed.
    e = discord.Embed(
        title="Scan Now",
        description="Your on-demand ranked scan runner is wired in the GitHub workflow. "
                    "This button will be enabled when that handler is exposed here.",
        color=discord.Color.greyple()
    )
    await safe_followup(interaction, e, ephemeral=False)


# ---------- Run ----------
bot.run(TOKEN)
