import discord
from discord import app_commands
from discord.ext import commands
import os
import scanner

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = int(os.getenv("DISCORD_GUILD_ID"))
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))

if not TOKEN:
    raise RuntimeError("Missing DISCORD_BOT_TOKEN in environment.")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

@bot.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    await tree.sync(guild=guild)
    print(f"✅ Logged in as {bot.user} | Commands synced to guild {GUILD_ID}")

@tree.command(name="ping", description="Check if the bot is alive", guild=discord.Object(id=GUILD_ID))
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("📍 Pong", ephemeral=True)

@tree.command(name="scan_ticker", description="Analyze a single ticker for buy/sell signals", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(ticker="Enter a stock ticker (e.g. NVDA)")
async def scan_ticker(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer()
    data = scanner.analyze_one_ticker(ticker)
    if not data:
        await interaction.followup.send(f"❌ Could not analyze **{ticker.upper()}**.")
        return

    embed = discord.Embed(
        title=f"{data['ticker']} • {data['rec']}",
        description=f"**Last Price:** ${data['last_price']:.2f}\n"
                    f"**1D:** {data['change_1d']:.2f}% | **5D:** {data['change_5d']:.2f}% | **1M:** {data['change_1m']:.2f}%\n"
                    f"**52W Range:** ${data['low_52']:.2f} – ${data['high_52']:.2f}\n"
                    f"**Vol vs Avg:** {data['volume_ratio']:.2f}x\n"
                    f"**RSI:** {data['rsi']}\n**MACD:** {data['macd']}\n"
                    f"**Sentiment:** {data['sentiment']:.2f}",
        color=discord.Color.green() if data["rec"] == "CALL" else discord.Color.red() if data["rec"] == "PUT" else discord.Color.gold()
    )
    await interaction.followup.send(embed=embed)

@tree.command(name="earnings_watch", description="Show tickers with earnings within ±7 days", guild=discord.Object(id=GUILD_ID))
async def earnings_watch(interaction: discord.Interaction):
    await interaction.response.defer()
    text = scanner.earnings_watch_text()
    await interaction.followup.send(f"**Upcoming Earnings:**\n{text}")

bot.run(TOKEN)
