# bot.py — Premarket Scanner (stable, async-safe, guild-sync forced)
import os
import json
import time
import logging
import asyncio
import pathlib
import argparse
import traceback
import urllib.request
import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands, tasks

import scanner as sc  # our local yfinance-only engine

# ============================================================
#  ENVIRONMENT & LOGGING
# ============================================================

CACHE_DIR         = os.getenv("CACHE_DIR", "/tmp/premarket_cache")
LOG_LEVEL         = os.getenv("LOG_LEVEL", "INFO").upper()
KEEP_ALIVE        = os.getenv("KEEP_ALIVE", "false").lower() == "true"
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_GUILD_ID  = os.getenv("DISCORD_GUILD_ID")          # strongly recommended (fast dev sync)
DISCORD_CHANNEL_ID= os.getenv("DISCORD_CHANNEL_ID")        # for cron helper

SCAN_UNIVERSE = [s.strip().upper() for s in os.getenv(
    "SCAN_UNIVERSE", "AAPL,MSFT,NVDA,TSLA,AMZN,AMD,JPM"
).split(",") if s.strip()]

if not DISCORD_BOT_TOKEN:
    raise RuntimeError("Missing DISCORD_BOT_TOKEN in environment.")

pathlib.Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)

numeric_level = getattr(logging, LOG_LEVEL, logging.INFO)
logging.basicConfig(level=numeric_level, format="[%(asctime)s] [%(levelname)8s] %(name)s: %(message)s")
log = logging.getLogger("bot")
log.info(f"CACHE_DIR={CACHE_DIR} | LOG_LEVEL={LOG_LEVEL} | KEEP_ALIVE={KEEP_ALIVE}")

# A simple version tag to force-rotate command schemas when we change options
COMMAND_VERSION = "v7"

# ============================================================
#  DISCORD CLIENT
# ============================================================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# Concurrency & timeouts for yfinance work
IO_SEMAPHORE = asyncio.Semaphore(4)
SCAN_TIMEOUT = 25
PAGE_SIZE    = 20

async def run_io(func, *args, timeout=SCAN_TIMEOUT, **kwargs):
    async with IO_SEMAPHORE:
        return await asyncio.wait_for(asyncio.to_thread(func, *args, **kwargs), timeout=timeout)

async def _heartbeat_task():
    while True:
        await asyncio.sleep(60)
        logging.getLogger("heartbeat").debug("tick")

# ------------------------------------------------------------
# Force re-sync helper: clear any stale commands and re-add ours
# ------------------------------------------------------------
async def force_sync_commands():
    target_guild = None
    if DISCORD_GUILD_ID:
        target_guild = discord.Object(id=int(DISCORD_GUILD_ID))

    try:
        # Wipe old schemas *for this guild only* (avoids global cache delay)
        if target_guild:
            tree.clear_commands(guild=target_guild)
        else:
            tree.clear_commands(guild=None)

        # Re-register commands (optionally guild-scoped decorator)
        # Using decorator below too, but explicit add helps if tree was cleared.
        if target_guild:
            tree.add_command(ping_cmd, guild=target_guild)
            tree.add_command(scan_ticker_cmd, guild=target_guild)
            tree.add_command(earnings_watch_cmd, guild=target_guild)

        # Sync
        if target_guild:
            synced = await tree.sync(guild=target_guild)
            log.info(f"Slash commands synced to guild {DISCORD_GUILD_ID} ({len(synced)} cmds, {COMMAND_VERSION})")
        else:
            synced = await tree.sync()
            log.info(f"Slash commands synced globally ({len(synced)} cmds, {COMMAND_VERSION})")

    except Exception:
        log.exception("Slash command sync failed")

@bot.event
async def on_ready():
    await force_sync_commands()

    if KEEP_ALIVE:
        asyncio.create_task(_heartbeat_task())

    if not earnings_refresh.is_running():
        earnings_refresh.start()

    log.info(f"Logged in as {bot.user} (id={bot.user.id})")

# ============================================================
#  BACKGROUND TASKS (EARNINGS CACHE)
# ============================================================

@tasks.loop(hours=12, reconnect=True)
async def earnings_refresh():
    rlog = logging.getLogger("refresh")
    try:
        rlog.info("Refreshing earnings cache…")
        await run_io(sc.refresh_all_caches)
        rlog.info("Earnings cache refresh complete.")
    except Exception:
        rlog.exception("Earnings cache refresh failed")

# ============================================================
#  COMMANDS  (guild-scoped to avoid global cache lag)
# ============================================================

_guilds = []
if DISCORD_GUILD_ID:
    _guilds = [discord.Object(id=int(DISCORD_GUILD_ID))]

@app_commands.guilds(*_guilds)
@tree.command(name="ping", description=f"Check bot responsiveness • {COMMAND_VERSION}")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("📍 Pong", ephemeral=True)

@app_commands.guilds(*_guilds)
@tree.command(name="scan_ticker", description=f"Analyze one ticker (e.g. NVDA, TSLA) • {COMMAND_VERSION}")
@app_commands.describe(symbol="Ticker symbol (e.g. NVDA)")
async def scan_ticker_cmd(interaction: discord.Interaction, symbol: str):
    symbol = symbol.strip().upper()
    slog = logging.getLogger("cmd.scan_ticker")
    try:
        await interaction.response.defer(thinking=True)
        card = await run_io(sc.analyze_one_ticker, symbol)
        if not card:
            await interaction.followup.send(f"❌ Could not analyze **{symbol}** (no data).", ephemeral=True)
            return
        embed = sc.render_ticker_embed(card)
        await interaction.followup.send(embed=embed)
    except asyncio.TimeoutError:
        slog.error("timeout for %s", symbol)
        await interaction.followup.send("⏱️ Scan timed out. Try again in a moment.", ephemeral=True)
    except Exception:
        slog.exception("failed for %s", symbol)
        await interaction.followup.send("❌ Sorry, that failed unexpectedly. Check logs.", ephemeral=True)

@app_commands.guilds(*_guilds)
@tree.command(name="earnings_watch", description=f"List earnings within ±N days (1–30) • {COMMAND_VERSION}")
@app_commands.describe(days="Number of days to search ahead/back (1–30)")
async def earnings_watch_cmd(interaction: discord.Interaction, days: int = 7):
    elog = logging.getLogger("cmd.earnings_watch")
    try:
        if days < 1 or days > 30:
            await interaction.response.send_message("Please pick **1–30** days.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        rows = await run_io(sc.earnings_universe_window, days)  # <-- single API we rely on
        total = len(rows)

        if total == 0:
            await interaction.followup.send(f"No earnings within ±{days} days.", ephemeral=True)
            return

        if total <= PAGE_SIZE:
            embed = sc.render_earnings_page_embed(rows, days, 1, 1)
            await interaction.followup.send(embed=embed)
            return

        pages = [rows[i:i+PAGE_SIZE] for i in range(0, total, PAGE_SIZE)]
        for idx, page in enumerate(pages, start=1):
            embed = sc.render_earnings_page_embed(page, days, idx, len(pages))
            await interaction.followup.send(embed=embed)

        elog.debug("earnings pages sent: %s rows, %s pages", total, len(pages))

    except asyncio.TimeoutError:
        elog.error("timeout (days=%s)", days)
        await interaction.followup.send("⏱️ Earnings scan timed out. Try again in a moment.", ephemeral=True)
    except Exception:
        elog.exception("earnings scan failed")
        await interaction.followup.send("❌ Earnings scan failed (see logs).", ephemeral=True)

# ============================================================
#  REST POSTER + CRON SUPPORT
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
        utc_now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        msg = f"🤖 Cron heartbeat OK • {utc_now}\nNext upgrade → full top-10 scan & 30d earnings digest."
        _post_message_rest(DISCORD_CHANNEL_ID, msg)
    except Exception as e:
        logging.getLogger("cron").exception("Cron failed: %s", e)
        _post_message_rest(DISCORD_CHANNEL_ID, f"⚠️ Cron failed: {e}")

# ============================================================
#  MAIN
# ============================================================

if __name__ == "__main__":
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
