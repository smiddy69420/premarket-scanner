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

import scanner as sc  # local module

# ---------------- Env & logging ----------------
CACHE_DIR = os.getenv("CACHE_DIR", "/tmp/premarket_cache")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
KEEP_ALIVE = os.getenv("KEEP_ALIVE", "false").lower() == "true"

DISCORD_BOT_TOKEN   = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_GUILD_ID    = os.getenv("DISCORD_GUILD_ID")  # optional (guild-scoped sync)
DISCORD_CHANNEL_ID  = os.getenv("DISCORD_CHANNEL_ID")  # used by cron

if not DISCORD_BOT_TOKEN:
    raise RuntimeError("Missing DISCORD_BOT_TOKEN in environment.")

numeric_level = getattr(logging, LOG_LEVEL, logging.INFO)
logging.basicConfig(level=numeric_level,
                    format="[%(asctime)s] [%(levelname)8s] %(name)s: %(message)s")
log = logging.getLogger("bot")

pathlib.Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
log.info(f"CACHE_DIR={CACHE_DIR} | LOG_LEVEL={LOG_LEVEL} | KEEP_ALIVE={KEEP_ALIVE}")

# Bump this whenever command signatures/descriptions change
COMMAND_VERSION = "v10"

# ---------------- Small helpers ----------------
async def run_io(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)

async def _heartbeat_task():
    hb = logging.getLogger("heartbeat")
    while True:
        await asyncio.sleep(60)
        hb.debug("tick")

# ---------------- Discord bootstrap ----------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

_guilds = []
if DISCORD_GUILD_ID:
    try:
        _guilds = [discord.Object(id=int(DISCORD_GUILD_ID))]
    except Exception:
        log.warning("DISCORD_GUILD_ID is not a valid int; falling back to global sync.")

async def _background_refresh_earnings():
    rlog = logging.getLogger("refresh")
    try:
        rlog.info("Refreshing earnings cache…")
        await run_io(sc.refresh_all_caches)
        rlog.info("Earnings cache refresh complete.")
    except Exception:
        rlog.exception("Earnings cache refresh failed")

async def force_sync_commands(source: str = "auto"):
    """Hard resync AND print the command list so we can see what Discord has."""
    # Doing two passes helps bust stale schema on Discord’s side.
    if _guilds:
        g = _guilds[0]
        cmds1 = await tree.sync(guild=g)
        cmds2 = await tree.sync(guild=g)
        names = ", ".join(sorted(c.name for c in cmds2))
        log.info(f"Slash commands synced to guild {g.id} ({len(cmds2)} cmds, {COMMAND_VERSION}) via {source}: {names}")
    else:
        cmds1 = await tree.sync()
        cmds2 = await tree.sync()
        names = ", ".join(sorted(c.name for c in cmds2))
        log.info(f"Slash commands synced globally ({len(cmds2)} cmds, {COMMAND_VERSION}) via {source}: {names}")

@bot.event
async def on_ready():
    await force_sync_commands(source="on_ready")
    if KEEP_ALIVE:
        asyncio.create_task(_heartbeat_task())
    asyncio.create_task(_background_refresh_earnings())
    log.info(f"Logged in as {bot.user} (id={bot.user.id})")

# ---------------- REST poster for cron ----------------
DISCORD_API_BASE = "https://discord.com/api/v10"

def _post_message_rest(channel_id: str, content: str) -> None:
    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
    req = urllib.request.Request(
        url,
        data=json.dumps({"content": content}).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30):
        pass

def _cron_morning_digest() -> None:
    try:
        utc_now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        _post_message_rest(DISCORD_CHANNEL_ID, f"🤖 Cron heartbeat OK • {utc_now}")
    except Exception as e:
        logging.getLogger("cron").exception("Cron failed: %s", e)
        _post_message_rest(DISCORD_CHANNEL_ID, f"⚠️ Cron failed: {e}")

# ---------------- Slash commands ----------------
@tree.command(name="ping", description=f"Check bot responsiveness • {COMMAND_VERSION}")
@app_commands.guilds(*_guilds)
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("📍 Pong", ephemeral=True)

# admin-only sync (so you can force it from Discord if a command is missing)
@tree.command(name="sync", description=f"(Admin) Force resync of slash commands • {COMMAND_VERSION}")
@app_commands.guilds(*_guilds)
async def sync_cmd(interaction: discord.Interaction):
    # Require the user to have Administrator in the guild
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You need **Administrator** to run `/sync`.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True, ephemeral=True)
    await force_sync_commands(source=f"manual by {interaction.user}")
    await interaction.followup.send("✅ Synced. If a command just changed, it should appear now.", ephemeral=True)

# ---- /scan_ticker ----
@app_commands.guilds(*_guilds)
@tree.command(name="scan_ticker", description=f"Analyze one ticker (e.g. NVDA) • {COMMAND_VERSION}")
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

# ---- /scan (alias) ----
@app_commands.guilds(*_guilds)
@tree.command(name="scan", description=f"Analyze one ticker (alias of /scan_ticker) • {COMMAND_VERSION}")
@app_commands.describe(ticker="Ticker symbol (e.g. NVDA)")
async def scan_alias_cmd(interaction: discord.Interaction, ticker: str):
    await scan_ticker_cmd.callback(interaction, ticker)

# ---- /earnings_watch (broad universe) ----
@app_commands.guilds(*_guilds)
@tree.command(name="earnings_watch",
              description=f"Show all tickers with earnings within ±N days (broad universe) • {COMMAND_VERSION}")
@app_commands.describe(days="Number of days ahead/back to search (1–30)")
async def earnings_watch_cmd(interaction: discord.Interaction, days: int = 7):
    elog = logging.getLogger("cmd.earnings")
    if days < 1 or days > 30:
        await interaction.response.send_message("Please pick **1–30** days.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    try:
        rows = await run_io(sc.earnings_universe_window, days)
        if not rows:
            await interaction.followup.send(f"No earnings within ±{days} days.", ephemeral=True)
            return

        # paginate results nicely
        page_size = 20
        total = len(rows)
        pages = [(i, rows[i:i + page_size]) for i in range(0, total, page_size)]
        embeds = []
        page_num = 1
        total_pages = len(pages)
        for _, chunk in pages:
            embeds.append(sc.render_earnings_page_embed(chunk, days, page_num, total_pages))
            page_num += 1

        while embeds:
            batch = embeds[:10]
            embeds = embeds[10:]
            await interaction.followup.send(embeds=batch)

    except Exception:
        elog.exception("Earnings scan failed")
        await interaction.followup.send("❌ Earnings scan failed (see logs).", ephemeral=True)

# ---------------- main ----------------
def main():
    parser = argparse.ArgumentParser(description="Premarket Scanner bot")
    parser.add_argument("--mode", choices=["bot", "cron"], default="bot")
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
