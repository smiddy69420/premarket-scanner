import os
import json
import time
import logging
import asyncio
import pathlib
import argparse
import urllib.request
import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands

# === Third-party data layer ===
# We will call into your scanner.py (robust, cached) for all heavy lifting.
import scanner as sc

# ============================================================
#  ENVIRONMENT & LOGGING BOOTSTRAP
# ============================================================

CACHE_DIR = os.getenv("CACHE_DIR", "/tmp/premarket_cache")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
KEEP_ALIVE = os.getenv("KEEP_ALIVE", "false").lower() == "true"

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID")  # optional, speeds up command sync to one guild
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")  # used by cron/rest poster

# Optional universe used only for slash autocomplete in a future wave; scanner.py builds its own universe.
SCAN_UNIVERSE = [s.strip().upper() for s in os.getenv(
    "SCAN_UNIVERSE", "AAPL,MSFT,NVDA,TSLA,AMZN,AMD,JPM"
).split(",") if s.strip()]

if not DISCORD_BOT_TOKEN:
    raise RuntimeError("Missing DISCORD_BOT_TOKEN in environment.")

pathlib.Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)

numeric_level = getattr(logging, LOG_LEVEL, logging.INFO)
logging.basicConfig(
    level=numeric_level,
    format="[%(asctime)s] [%(levelname)8s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")
log.info(f"CACHE_DIR={CACHE_DIR} | LOG_LEVEL={LOG_LEVEL} | KEEP_ALIVE={KEEP_ALIVE}")

# ============================================================
#  DISCORD SETUP
# ============================================================

intents = discord.Intents.default()
# Slash commands don’t need message_content, but leaving True is fine when privileged intent is enabled.
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


async def _heartbeat_task():
    hb = logging.getLogger("heartbeat")
    while True:
        await asyncio.sleep(60)
        hb.debug("tick")


async def _refresh_caches_loop():
    """
    Background task: refresh the full earnings cache twice a day.
    Runs in a thread so it never blocks the event loop.
    """
    rlog = logging.getLogger("refresh")
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            rlog.info("Refreshing earnings cache…")
            # Run the heavy job off the event loop
            await asyncio.to_thread(sc.refresh_all_caches)
            rlog.info("Earnings cache refresh completed.")
        except Exception as e:
            rlog.exception("Cache refresh failed: %s", e)
        # Sleep ~12 hours
        await asyncio.sleep(12 * 3600)


@bot.event
async def on_ready():
    # Fast, reliable guild sync (avoids CommandNotFound during global propagation)
    if DISCORD_GUILD_ID:
        guild = discord.Object(id=int(DISCORD_GUILD_ID))
        await tree.sync(guild=guild)
        log.info(f"Slash commands synced to guild {DISCORD_GUILD_ID}")
    else:
        await tree.sync()
        log.info("Slash commands synced globally")

    if KEEP_ALIVE:
        asyncio.create_task(_heartbeat_task())

    # Start background cache refresh loop
    asyncio.create_task(_refresh_caches_loop())

    log.info(f"Logged in as {bot.user} (id={bot.user.id})")

# ============================================================
#  SAFE HELPERS
# ============================================================

async def _safe_followup(interaction: discord.Interaction, content=None, embed=None, ephemeral=False):
    """
    Always send *something* so we never trip the 'application did not respond' banner.
    """
    try:
        if embed is not None:
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.followup.send(content or "Done.", ephemeral=ephemeral)
    except discord.HTTPException as e:
        log.exception("Followup send failed: %s", e)


def _chunk(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]


# ============================================================
#  SLASH COMMANDS (using scanner.py)
# ============================================================

@tree.command(name="ping", description="Check bot responsiveness")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("📍 Pong", ephemeral=True)


@tree.command(name="scan_ticker", description="Analyze a single ticker with TA & options snapshot")
@app_commands.describe(symbol="Ticker symbol (e.g., NVDA)")
async def scan_ticker(interaction: discord.Interaction, symbol: str):
    # Defer quickly so Discord knows we're working
    await interaction.response.defer(thinking=True)

    sym = symbol.strip().upper()
    try:
        # Run analysis in a thread (yfinance is I/O + CPU); never block the event loop
        card = await asyncio.to_thread(sc.analyze_one_ticker, sym)
        if card is None:
            await _safe_followup(interaction, f"❌ Could not analyze **{sym}** (no price data).", ephemeral=True)
            return

        embed = sc.render_ticker_embed(card)
        await _safe_followup(interaction, embed=embed)
    except Exception as e:
        log.exception("scan_ticker failed for %s: %s", sym, e)
        await _safe_followup(interaction, f"❌ Error analyzing **{sym}**.", ephemeral=True)


@tree.command(name="earnings_watch", description="List tickers with earnings within ±N days (1–30)")
@app_commands.describe(days="Number of days to search ahead/back (1–30)")
async def earnings_watch(interaction: discord.Interaction, days: int = 7):
    if not (1 <= days <= 30):
        await interaction.response.send_message("Please choose a number between 1 and 30.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)

    try:
        # Use your cached, robust universe scanner
        rows = await asyncio.to_thread(sc.earnings_universe_window, days)
        if not rows:
            await _safe_followup(interaction, f"No earnings within ±{days} days.")
            return

        # Paginate in embeds (Discord limits)
        PAGE = 25
        total_pages = (len(rows) + PAGE - 1) // PAGE
        page_no = 1
        for chunk in _chunk(rows, PAGE):
            embed = sc.render_earnings_page_embed(chunk, days, page_no, total_pages)
            await interaction.followup.send(embed=embed)
            page_no += 1
    except Exception as e:
        log.exception("earnings_watch failed: %s", e)
        await _safe_followup(interaction, f"❌ Error while scanning earnings window ±{days} days.", ephemeral=True)

# ============================================================
#  CRON/REST SUPPORT (unchanged from your version, kept for wave-2)
# ============================================================

DISCORD_API_BASE = "https://discord.com/api/v10"

def _post_message_rest(channel_id: str, content: str) -> None:
    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
    data = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bot {DISCORD_BOT_TOKEN}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        _ = resp.read()

def _cron_morning_digest() -> None:
    try:
        utc_now = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())
        msg = f"🤖 Cron heartbeat OK • {utc_now}\nNext upgrade → top-10 scan & 30-day earnings digest."
        _post_message_rest(DISCORD_CHANNEL_ID, msg)
    except Exception as e:
        logging.getLogger("cron").exception("Cron failed: %s", e)
        _post_message_rest(DISCORD_CHANNEL_ID, f"⚠️ Cron failed: {e}")

# ============================================================
#  MAIN ENTRYPOINT
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Premarket Scanner bot")
    parser.add_argument(
        "--mode", choices=["bot", "cron"], default="bot",
        help="Run Discord gateway (bot) or one-shot cron poster."
    )
    args = parser.parse_args()

    if args.mode == "cron":
        log.info("Running in CRON mode (no gateway).")
        if not DISCORD_CHANNEL_ID:
            raise RuntimeError("CRON mode requires DISCORD_CHANNEL_ID.")
        _cron_morning_digest()
    else:
        log.info("Running in BOT mode (gateway).")
        bot.run(DISCORD_BOT_TOKEN)
