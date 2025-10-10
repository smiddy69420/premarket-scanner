import os
import asyncio
import datetime as dt
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks

import scanner  # our local module

# --- Environment & logging bootstrap (ONE TIME DROP-IN) ---
import os, logging, pathlib, asyncio, json, time
from typing import Optional

# Read env
CACHE_DIR = os.getenv("CACHE_DIR", "/tmp/premarket_cache")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
KEEP_ALIVE = os.getenv("KEEP_ALIVE", "false").lower() == "true"

# Required existing env (you already have these in Render)
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID")
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")

if not DISCORD_BOT_TOKEN:
    # Keep this explicit so Render errors are obvious
    raise RuntimeError("Missing DISCORD_BOT_TOKEN in environment.")

# Logging early
numeric_level = getattr(logging, LOG_LEVEL, logging.INFO)
logging.basicConfig(
    level=numeric_level,
    format="[%(asctime)s] [%(levelname)8s] %(name)s: %(message)s",
)
log_boot = logging.getLogger("bootstrap")

# Ensure cache dir exists
pathlib.Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
log_boot.info(f"CACHE_DIR={CACHE_DIR} | LOG_LEVEL={LOG_LEVEL} | KEEP_ALIVE={KEEP_ALIVE}")

# Lightweight heartbeat (only if KEEP_ALIVE=true)
async def _heartbeat_task():
    log = logging.getLogger("heartbeat")
    while True:
        await asyncio.sleep(60)  # once per minute
        log.debug("tick")


TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing DISCORD_BOT_TOKEN in environment.")

GUILD_ID = os.getenv("DISCORD_GUILD_ID")  # optional but recommended

# Intents: we don't need message content for slash commands
intents = discord.Intents.default()
intents.guilds = True

class PremarketBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.synced = False

    async def setup_hook(self):
        # If a guild id is provided, register commands guild-scoped for instant availability.
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        # Make sure commands are synced (belt & suspenders).
        try:
            if GUILD_ID:
                await self.tree.sync(guild=discord.Object(id=int(GUILD_ID)))
            else:
                await self.tree.sync()
        except Exception as e:
            print(f"[WARN] Command sync failed: {e}")

        print(f"[INFO] Logged in as {self.user} (id={self.user.id})")
        # Warm caches in the background (does not block bot)
        background_cache_refresher.start()


client = PremarketBot()
tree = client.tree

# -----------------------
# Slash Commands
# -----------------------

@tree.command(name="ping", description="Health check")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=False, ephemeral=True)
    await interaction.followup.send("📍 Pong", ephemeral=True)


@tree.command(name="help", description="Show commands and how to read outputs")
async def help_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    embed = discord.Embed(
        title="🤖 Scanner Help",
        color=0x5865F2,
        description=(
            "Commands:\n"
            "• `/scan_ticker SYMBOL` — analyze one ticker (trend/momentum/RSI/MACD, ranges, vol vs avg, ATM option)\n"
            "• `/earnings_watch days:<1–30> [ticker:<symbol>]` — upcoming earnings. "
            "If `ticker` omitted, scans the whole market and paginates results.\n\n"
            "**How to read a /scan_ticker card**\n"
            "• **Bias** CALL/PUT from multi-signal blend\n"
            "• **Last / 1D/5D/1M** price & changes\n"
            "• **EMA20/EMA50, RSI14, MACD Δ**, **Vol/Avg20**\n"
            "• **Option**: ~7–21 DTE, ATM contract with mid & spread\n"
        ),
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="scan_ticker", description="Analyze one ticker on demand, e.g. NVDA")
@app_commands.describe(symbol="Ticker symbol, e.g. NVDA")
async def scan_ticker_cmd(interaction: discord.Interaction, symbol: str):
    symbol = symbol.upper().strip()
    await interaction.response.defer(thinking=True)

    try:
        card = await asyncio.to_thread(scanner.analyze_one_ticker, symbol)
        if not card:
            raise RuntimeError("No price data returned.")

        await interaction.followup.send(embed=scanner.render_ticker_embed(card))
    except Exception as e:
        await interaction.followup.send(f"❌ Could not analyze **{symbol}**: {e}")


@tree.command(name="earnings_watch", description="Earnings in the next N days (1–30). Optional ticker filter.")
@app_commands.describe(days="Days ahead (1–30)", ticker="Optional ticker to check a single name")
async def earnings_watch_cmd(interaction: discord.Interaction, days: app_commands.Range[int, 1, 30], ticker: Optional[str] = None):
    await interaction.response.defer(thinking=True)

    try:
        if ticker:
            ticker = ticker.upper().strip()
            result = await asyncio.to_thread(scanner.earnings_for_ticker, ticker, days)
            if result:
                embed = scanner.render_earnings_single_embed(result, days)
                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send(f"No earnings within ±{days} days for **{ticker}**.")
            return

        # Full universe scan (cached + background warmed)
        rows = await asyncio.to_thread(scanner.earnings_universe_window, days)
        if not rows:
            await interaction.followup.send(f"No earnings within ±{days} days for the current universe.")
            return

        # Paginate 25 per embed
        pages = scanner.chunk(rows, 25)
        for i, page in enumerate(pages, start=1):
            embed = scanner.render_earnings_page_embed(page, days, i, len(pages))
            await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"❌ Earnings watch failed: {e}")


# -----------------------
# Background cache refresh
# -----------------------

@tasks.loop(hours=12)
async def background_cache_refresher():
    try:
        await asyncio.to_thread(scanner.refresh_all_caches)
    except Exception as e:
        print(f"[WARN] background_cache_refresher error: {e}")


if __name__ == "__main__":
    client.run(TOKEN)
