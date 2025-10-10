# bot.py — Premarket Scanner (discord.py 2.x)
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

# Hard dependency: our local modules
import scanner as sc  # uses yfinance under the hood

# ============================================================
#  ENVIRONMENT & LOGGING
# ============================================================

CACHE_DIR = os.getenv("CACHE_DIR", "/tmp/premarket_cache")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
KEEP_ALIVE = os.getenv("KEEP_ALIVE", "false").lower() == "true"

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID")  # optional: guild-scoped sync if set
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")  # used by cron poster

# Optional: narrow universe for some features; NOT used by /earnings_watch anymore
SCAN_UNIVERSE = os.getenv(
    "SCAN_UNIVERSE",
    "AAPL,MSFT,NVDA,TSLA,AMZN,AMD,JPM"
).split(",")

if not DISCORD_BOT_TOKEN:
    raise RuntimeError("Missing DISCORD_BOT_TOKEN in environment.")

numeric_level = getattr(logging, LOG_LEVEL, logging.INFO)
logging.basicConfig(
    level=numeric_level,
    format="[%(asctime)s] [%(levelname)8s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")

pathlib.Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
log.info(f"CACHE_DIR={CACHE_DIR} | LOG_LEVEL={LOG_LEVEL} | KEEP_ALIVE={KEEP_ALIVE}")

# Bump this any time command signatures/descriptions change
COMMAND_VERSION = "v9"

# ============================================================
#  UTIL — run blocking IO in a friendly thread
# ============================================================

async def run_io(fn, *args, **kwargs):
    """Run a blocking function in a thread (yfinance, filesystem, etc.)."""
    return await asyncio.to_thread(fn, *args, **kwargs)

async def _heartbeat_task():
    hb = logging.getLogger("heartbeat")
    while True:
        await asyncio.sleep(60)
        hb.debug("tick")

# ============================================================
#  DISCORD BOOTSTRAP
# ============================================================

intents = discord.Intents.default()
intents.message_content = True  # you enabled Message Content intent in the app
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# helper: guild scoping (faster command propagation during development)
_guilds = []
if DISCORD_GUILD_ID:
    try:
        _guilds = [discord.Object(id=int(DISCORD_GUILD_ID))]
    except Exception:
        pass

async def force_sync_commands():
    """Clear & resync slash commands to eliminate signature cache mismatch."""
    if _guilds:
        guild = _guilds[0]
        # purge then re-sync
        await tree.sync(guild=guild)
        await tree.sync(guild=guild)
        synced = await tree.sync(guild=guild)
        log.info(f"Slash commands synced to guild {guild.id} ({len(synced)} cmds, {COMMAND_VERSION})")
    else:
        synced = await tree.sync()
        log.info(f"Slash commands synced globally ({len(synced)} cmds, {COMMAND_VERSION})")

@bot.event
async def on_ready():
    await force_sync_commands()
    if KEEP_ALIVE:
        asyncio.create_task(_heartbeat_task())
    # kick off an earnings-cache refresh in background
    asyncio.create_task(_background_refresh_earnings())
    log.info(f"Logged in as {bot.user} (id={bot.user.id})")

async def _background_refresh_earnings():
    rlog = logging.getLogger("refresh")
    try:
        rlog.info("Refreshing earnings cache…")
        await run_io(sc.refresh_all_caches)  # scanner.py provided job
        rlog.info("Earnings cache refresh complete.")
    except Exception:
        rlog.exception("Earnings cache refresh failed")

# ============================================================
#  CORE HELPERS (REST poster used by CRON mode)
# ============================================================

DISCORD_API_BASE = "https://discord.com/api/v10"

def _post_message_rest(channel_id: str, content: str) -> None:
    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
    data = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bot {DISCORD_BOT_TOKEN}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30):
        pass

def _cron_morning_digest() -> None:
    """Simple one-shot message to prove cron pathway; expand as you like."""
    try:
        utc_now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        msg = f"🤖 Cron heartbeat OK • {utc_now}\nNext upgrade → full top-10 scan & 30d earnings digest."
        _post_message_rest(DISCORD_CHANNEL_ID, msg)
    except Exception as e:
        logging.getLogger("cron").exception("Cron failed: %s", e)
        _post_message_rest(DISCORD_CHANNEL_ID, f"⚠️ Cron failed: {e}")

# ============================================================
#  SLASH COMMANDS
# ============================================================

@tree.command(name="ping", description=f"Check bot responsiveness • {COMMAND_VERSION}")
@app_commands.guilds(*_guilds)
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("📍 Pong", ephemeral=True)

# --- Scan a single ticker (restore option name to 'ticker') ---
@app_commands.guilds(*_guilds)
@tree.command(name="scan_ticker", description=f"Analyze one ticker (e.g. NVDA, TSLA) • {COMMAND_VERSION}")
@app_commands.describe(ticker="Ticker symbol (e.g. NVDA)")
async def scan_ticker_cmd(interaction: discord.Interaction, ticker: str):
    symbol = (ticker or "").strip().upper()
    slog = logging.getLogger("cmd.scan_ticker")
    try:
        if not symbol:
            await interaction.response.send_message("Please provide a ticker, e.g. `NVDA`.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        card = await run_io(sc.analyze_one_ticker, symbol)
        if not card:
            await interaction.followup.send(f"❌ Could not analyze **{symbol}** (no data).", ephemeral=True)
            return
        embed = sc.render_ticker_embed(card)
        await interaction.followup.send(embed=embed)
    except asyncio.TimeoutError:
        slog.error("timeout for %s", symbol)
        await interaction.followup.send("⏱️ Scan timed out. Try again shortly.", ephemeral=True)
    except Exception:
        slog.exception("failed for %s", symbol)
        await interaction.followup.send("❌ Sorry, that failed unexpectedly. Check logs.", ephemeral=True)

# Clean alias
@app_commands.guilds(*_guilds)
@tree.command(name="scan", description=f"Analyze one ticker (alias of /scan_ticker) • {COMMAND_VERSION}")
@app_commands.describe(ticker="Ticker symbol (e.g. NVDA)")
async def scan_alias_cmd(interaction: discord.Interaction, ticker: str):
    await scan_ticker_cmd.callback(interaction, ticker)

# --- Earnings watch (BROAD UNIVERSE via scanner.ensure_universe/earnings_universe_window) ---
@app_commands.guilds(*_guilds)
@tree.command(name="earnings_watch", description=f"Show all tickers with earnings within ±N days (broad universe) • {COMMAND_VERSION}")
@app_commands.describe(days="Number of days ahead/back to search (1–30)")
async def earnings_watch_cmd(interaction: discord.Interaction, days: int = 7):
    elog = logging.getLogger("cmd.earnings")
    if days < 1 or days > 30:
        await interaction.response.send_message("Please pick **1–30** days.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)

    try:
        # Uses the scanner’s combined NASDAQ+S&P500+DOW universe and cached Yahoo fetch
        rows = await run_io(sc.earnings_universe_window, days)
        if not rows:
            await interaction.followup.send(f"No earnings within ±{days} days.", ephemeral=True)
            return

        # Paginate to avoid long messages. 20 items per page looks good in Discord.
        page_size = 20
        total = len(rows)
        pages = [(i, rows[i:i + page_size]) for i in range(0, total, page_size)]
        embeds = []
        page_num = 1
        total_pages = len(pages)
        for _, chunk in pages:
            embeds.append(sc.render_earnings_page_embed(chunk, days, page_num, total_pages))
            page_num += 1

        # Send as multiple messages if needed (Discord caps 10 embeds per send)
        while embeds:
            batch = embeds[:10]
            embeds = embeds[10:]
            await interaction.followup.send(embeds=batch)

    except Exception:
        elog.exception("Earnings scan failed")
        await interaction.followup.send("❌ Earnings scan failed (see logs).", ephemeral=True)

# ============================================================
#  MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Premarket Scanner bot")
    parser.add_argument("--mode", choices=["bot", "cron"], default="bot",
                        help="Run live Discord bot (gateway) or one-shot cron poster.")
    args = parser.parse_args()

    if args.mode == "cron":
        log.info("Running in CRON mode (no gateway).")
        if not DISCORD_CHANNEL_ID:
            raise RuntimeError("CRON mode requires DISCORD_CHANNEL_ID.")
        _cron_morning_digest()
    else:
        log.info("Running in BOT mode (gateway).")
        bot.run(DISCORD_BOT_TOKEN)

if __name__ == "__main__":
    main()
