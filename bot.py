# bot.py
import os
import discord
from discord import app_commands
from discord.ext import commands

from scanner import analyze_one_ticker, earnings_watch_text  # keep legacy names to match commands

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = os.getenv("DISCORD_GUILD_ID")  # optional but recommended

if not TOKEN:
    raise RuntimeError("Missing DISCORD_BOT_TOKEN in environment.")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# ---------- helpers ----------

def make_signal_embed(result: dict) -> discord.Embed:
    sym = result.get("symbol", "Ticker")
    if not result.get("ok"):
        e = discord.Embed(
            title=f"❌ Could not analyze {sym}",
            description=result.get("error", "Unknown error"),
            color=discord.Color.red(),
        )
        e.set_footer(text="Premarket Scanner")
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
        timestamp=discord.utils.utcnow(),
    )
    if reasons:
        e.add_field(name="Why", value="• " + "\n• ".join(reasons), inline=False)
    e.set_footer(text="Premarket Scanner • multi-interval fallback")
    return e


async def safe_followup(interaction: discord.Interaction, embed: discord.Embed, ephemeral: bool = False):
    try:
        await interaction.followup.send(embed=embed, ephemeral=ephemeral)
    except discord.errors.NotFound:
        if interaction.channel:
            await interaction.channel.send(embed=embed)


# ---------- lifecycle ----------

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


# ---------- commands ----------

@tree.command(name="ping", description="Check if the bot is alive.")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=False, ephemeral=True)
    await safe_followup(interaction, discord.Embed(title="🏓 Pong", color=discord.Color.blurple()), ephemeral=True)


@tree.command(name="scan_ticker", description="Analyze one ticker on demand (e.g., NVDA, TSLA, JPM).")
@app_commands.describe(symbol="Ticker symbol, e.g., NVDA")
async def scan_ticker_cmd(interaction: discord.Interaction, symbol: str):
    await interaction.response.defer(thinking=True, ephemeral=False)
    try:
        result = analyze_one_ticker(symbol)
        embed = make_signal_embed(result)
    except Exception as e:
        embed = discord.Embed(
            title=f"❌ Could not analyze {symbol.upper()}",
            description=str(e),
            color=discord.Color.red(),
        )
    await safe_followup(interaction, embed, ephemeral=False)


# Support BOTH names so your existing button/usage still works
@tree.command(name="earnings_watch", description="Show earnings date if within ±7 days.")
@app_commands.describe(symbol="Ticker symbol, e.g., AAPL")
async def earnings_watch_cmd(interaction: discord.Interaction, symbol: str):
    await interaction.response.defer(thinking=True, ephemeral=False)
    try:
        msg = earnings_watch_text(symbol, days_window=7)
        embed = discord.Embed(title="Earnings Watch", description=msg, color=discord.Color.orange())
    except Exception as e:
        embed = discord.Embed(title="Earnings Watch", description=f"❌ {e}", color=discord.Color.red())
    await safe_followup(interaction, embed, ephemeral=False)


# Optional alias (/earnings also works)
@tree.command(name="earnings", description="Show earnings date if within ±7 days.")
@app_commands.describe(symbol="Ticker symbol, e.g., AAPL")
async def earnings_cmd(interaction: discord.Interaction, symbol: str):
    await earnings_watch_cmd.callback(interaction, symbol)  # reuse same handler


@tree.command(name="help", description="Show available commands and tips.")
async def help_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=False, ephemeral=True)
    desc = (
        "**/ping** — connectivity\n"
        "**/scan_ticker SYMBOL** — one-ticker TA snapshot\n"
        "**/earnings_watch SYMBOL** — earnings within ±7 days (also `/earnings`)\n"
    )
    e = discord.Embed(title="Premarket Scanner — Help", description=desc, color=discord.Color.blurple())
    await safe_followup(interaction, e, ephemeral=True)


bot.run(TOKEN)
