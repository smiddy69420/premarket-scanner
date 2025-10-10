# bot.py
import os
import sys
import asyncio
import datetime as dt
import logging
from typing import Iterable, List, Optional, Tuple, Union

import discord
from discord import app_commands

# ---- Logging ---------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="[{asctime}] [{levelname:^8}] {name}: {message}",
    style="{",
)
log = logging.getLogger("bot")

# ---- Environment -----------------------------------------------------------
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing DISCORD_BOT_TOKEN in environment.")

GUILD_ID = os.getenv("DISCORD_GUILD_ID") or os.getenv("GUILD_ID")
GUILD = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None

CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")
SCAN_UNIVERSE = os.getenv(
    "SCAN_UNIVERSE",
    "AAPL,MSFT,NVDA,TSLA,AMZN,AMD,JPM"
)
KEEP_ALIVE = os.getenv("KEEP_ALIVE", "true").lower() in ("1", "true", "yes")
CACHE_DIR = os.getenv("CACHE_DIR", "/tmp/premarket_cache")

# ---- Intents / Client ------------------------------------------------------
intents = discord.Intents.none()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ---- scanner module (local code) ------------------------------------------
# We don’t assume exact function names; code is defensive.
import importlib

try:
    scanner = importlib.import_module("scanner")
except Exception as e:
    log.exception("Failed to import scanner.py: %s", e)
    raise

def _parse_universe(env_val: str) -> List[str]:
    return [s.strip().upper() for s in env_val.split(",") if s.strip()]

# --- helpers to present results --------------------------------------------
async def _safe_followup_embed(
    interaction: discord.Interaction,
    embed: Optional[discord.Embed],
    files: Optional[List[discord.File]] = None,
) -> None:
    try:
        if embed is None:
            await interaction.followup.send("No data.", ephemeral=True)
            return
        if files:
            await interaction.followup.send(embed=embed, files=files)
        else:
            await interaction.followup.send(embed=embed)
    except Exception:
        log.exception("Failed sending embed")

# Some scanners return just an Embed; some return (Embed, [Files]).
def _coerce_result(
    res: Union[discord.Embed, Tuple[discord.Embed, List[discord.File]], None]
) -> Tuple[Optional[discord.Embed], Optional[List[discord.File]]]:
    if res is None:
        return None, None
    if isinstance(res, tuple) and len(res) == 2:
        return res[0], res[1]
    if isinstance(res, discord.Embed):
        return res, None
    return None, None

# ---- Lifecyle --------------------------------------------------------------
@client.event
async def on_ready():
    log.info("Running in BOT mode (gateway).")
    try:
        if GUILD:
            await tree.sync(guild=GUILD)
            log.info(
                "Slash commands synced to guild %s",
                GUILD.id,
            )
        else:
            await tree.sync()
            log.info("Slash commands synced globally")
    except Exception:
        log.exception("Initial sync failed")

    # optional heartbeat just to see liveness
    if KEEP_ALIVE:
        async def _heartbeat():
            while True:
                log.debug("heartbeat: tick")
                await asyncio.sleep(60)
        client.loop.create_task(_heartbeat())

# ---- Commands --------------------------------------------------------------
@tree.command(name="ping", description="Health check.", guild=GUILD)
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("📍 Pong", ephemeral=True)

@tree.command(name="sync", description="Force re-sync slash commands (admin).", guild=GUILD)
async def sync_cmd(interaction: discord.Interaction):
    # Basic guard: restrict to server owner if available
    try:
        if interaction.user and interaction.guild and interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message("Not allowed.", ephemeral=True)
            return
    except Exception:
        pass

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        if GUILD:
            cmds = await tree.sync(guild=GUILD)
            names = ", ".join(sorted(c.name for c in cmds))
            await interaction.followup.send(f"Synced. If a command just changed, it should appear now.\n`{names}`")
            log.info("Slash commands synced to guild %s via manual by %s: %s",
                     GUILD.id, interaction.user, names)
        else:
            cmds = await tree.sync()
            names = ", ".join(sorted(c.name for c in cmds))
            await interaction.followup.send(f"Synced globally.\n`{names}`")
    except Exception:
        log.exception("Manual sync failed")
        await interaction.followup.send("Sync failed. Check logs.", ephemeral=True)

@tree.command(name="scan_ticker", description="Run the multi-signal scan for a single ticker.", guild=GUILD)
@app_commands.describe(symbol="Ticker symbol, e.g. NVDA")
async def scan_ticker_cmd(interaction: discord.Interaction, symbol: str):
    sym = symbol.upper().strip()
    await interaction.response.defer()  # public by default
    try:
        if hasattr(scanner, "analyze_ticker"):
            res = await scanner.analyze_ticker(sym) if asyncio.iscoroutinefunction(scanner.analyze_ticker) else scanner.analyze_ticker(sym)
            embed, files = _coerce_result(res)
            await _safe_followup_embed(interaction, embed, files)
        else:
            await interaction.followup.send(f"Scanner missing analyze_ticker() for `{sym}`.", ephemeral=True)
    except Exception:
        log.exception("scan_ticker failed for %s", sym)
        await interaction.followup.send("Sorry, that failed unexpectedly. Check logs.", ephemeral=True)

@tree.command(name="earnings_watch", description="Show tickers with earnings within ±N days across the broad universe.", guild=GUILD)
@app_commands.describe(days="Window in days (±N). Default 30.")
async def earnings_watch_cmd(interaction: discord.Interaction, days: Optional[int] = 30):
    days = int(days or 30)
    await interaction.response.defer()
    try:
        # Preferred new API
        if hasattr(scanner, "earnings_universe_window"):
            info = await scanner.earnings_universe_window(days) if asyncio.iscoroutinefunction(scanner.earnings_universe_window) else scanner.earnings_universe_window(days)
            # Expect either (embed, files) or list of embeds
            if isinstance(info, list):
                if not info:
                    await interaction.followup.send(f"No earnings within ±{days} days.")
                    return
                for emb in info[:10]:
                    await interaction.followup.send(embed=emb)
                return
            embed, files = _coerce_result(info)
            if embed:
                await _safe_followup_embed(interaction, embed, files)
                return

        # Back-compat: if a simple helper exists
        if hasattr(scanner, "earnings_watch_simple"):
            emb = scanner.earnings_watch_simple(days)
            await _safe_followup_embed(interaction, emb, None)
            return

        await interaction.followup.send(f"No earnings within ±{days} days.", suppress_embeds=True)
    except Exception:
        log.exception("Earnings watch failed")
        await interaction.followup.send("❌ Earnings scan failed (see logs).", ephemeral=True)

@tree.command(name="scan", description="Run a ranked scan for the configured universe (ad-hoc).", guild=GUILD)
@app_commands.describe(top_n="How many top picks to post (default 5).")
async def scan_cmd(interaction: discord.Interaction, top_n: Optional[int] = 5):
    await interaction.response.defer()
    universe = _parse_universe(SCAN_UNIVERSE)
    if not universe:
        await interaction.followup.send("Universe is empty. Set SCAN_UNIVERSE env var.", ephemeral=True)
        return

    top_n = max(1, min(int(top_n or 5), 20))

    try:
        # Preferred: if your scanner exposes a ranked universe function
        if hasattr(scanner, "rank_universe"):
            result = await scanner.rank_universe(universe, top_n=top_n) \
                     if asyncio.iscoroutinefunction(scanner.rank_universe) \
                     else scanner.rank_universe(universe, top_n=top_n)
            # Accept either a single embed, a tuple, or a list of embeds
            if isinstance(result, list):
                if not result:
                    await interaction.followup.send("No picks.")
                    return
                for emb in result[:top_n]:
                    if isinstance(emb, discord.Embed):
                        await interaction.followup.send(embed=emb)
                return
            emb, files = _coerce_result(result)
            if emb:
                await _safe_followup_embed(interaction, emb, files)
                return

        # Fallback: no ranking helper – analyze each ticker and post sequentially.
        posted = 0
        for sym in universe:
            if hasattr(scanner, "analyze_ticker"):
                res = await scanner.analyze_ticker(sym) if asyncio.iscoroutinefunction(scanner.analyze_ticker) else scanner.analyze_ticker(sym)
                emb, files = _coerce_result(res)
                if emb:
                    await _safe_followup_embed(interaction, emb, files)
                    posted += 1
                    if posted >= top_n:
                        break
        if posted == 0:
            await interaction.followup.send("No results.", ephemeral=True)
    except Exception:
        log.exception("scan (ranked) failed")
        await interaction.followup.send("Sorry, scan failed. Check logs.", ephemeral=True)

# ---- Run -------------------------------------------------------------------
def _today_utc_date() -> dt.date:
    # Avoid deprecated utcnow in 3.12+
    return dt.datetime.now(dt.timezone.utc).date()

if __name__ == "__main__":
    log.info(
        "CACHE_DIR=%s | LOG_LEVEL=%s | KEEP_ALIVE=%s",
        CACHE_DIR, LOG_LEVEL, str(KEEP_ALIVE)
    )
    # If you ever want a cron/webhook mode from this same file, you could parse args here.
    client.run(TOKEN)
