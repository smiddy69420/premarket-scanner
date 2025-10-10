# bot.py
import os
import sys
import asyncio
import datetime as dt
from typing import Iterable, List, Optional, Tuple, Union

import logging
import discord
from discord import app_commands

# ---------------- Logging ----------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="[{asctime}] [{levelname:^8}] {name}: {message}",
    style="{",
)
log = logging.getLogger("bot")

# ---------------- Environment ------------
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing DISCORD_BOT_TOKEN in environment.")

# Single-guild sync for speed; if unset we sync globally
GUILD_ID = os.getenv("DISCORD_GUILD_ID") or os.getenv("GUILD_ID")
GUILD = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None

CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")  # optional target channel
CACHE_DIR = os.getenv("CACHE_DIR", "/tmp/premarket_cache")
KEEP_ALIVE = os.getenv("KEEP_ALIVE", "true").lower() in ("1", "true", "yes")

# Admin whitelist to run /sync (comma-separated Discord user IDs)
ADMIN_USER_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip().isdigit()
}

# Universe sources (in order of preference)
ALL_TICKERS_ENV = os.getenv("ALL_TICKERS", "")          # giant comma-separated list (optional)
SCAN_UNIVERSE = os.getenv(
    "SCAN_UNIVERSE",
    "AAPL,MSFT,NVDA,TSLA,AMZN,AMD,JPM"
)

SYMBOLS_FILE = os.getenv("SYMBOLS_FILE", "data/symbols_robinhood.txt")  # optional flat file

# ---------------- Discord client ----------
# Minimal intents but include guilds to silence warnings and keep state sane.
intents = discord.Intents.none()
intents.guilds = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ---------------- Import scanner ----------
# We integrate with your local scanner.py but do not assume exact API.
import importlib
try:
    scanner = importlib.import_module("scanner")
except Exception:
    log.exception("Failed importing scanner.py")
    raise

# ------------- helpers -------------------
def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def _parse_csv_symbols(csv: str) -> List[str]:
    return [s.strip().upper() for s in csv.split(",") if s.strip()]

def _load_symbols_file(path: str) -> List[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            syms = [ln.strip().upper() for ln in f if ln.strip()]
            return [s for s in syms if s.isascii()]
    except FileNotFoundError:
        return []

def universe_symbols() -> List[str]:
    # 1) massive list via env
    if ALL_TICKERS_ENV.strip():
        return _parse_csv_symbols(ALL_TICKERS_ENV)
    # 2) repo file (drop in later for “all Robinhood”)
    file_syms = _load_symbols_file(SYMBOLS_FILE)
    if file_syms:
        return file_syms
    # 3) fallback to configured small universe
    return _parse_csv_symbols(SCAN_UNIVERSE)

async def _safe_send_followup(
    interaction: discord.Interaction,
    embed: Optional[discord.Embed] = None,
    content: Optional[str] = None,
    files: Optional[List[discord.File]] = None,
    ephemeral: bool = False,
):
    try:
        if embed:
            await interaction.followup.send(content=content or "", embed=embed, files=files, ephemeral=ephemeral)
        else:
            await interaction.followup.send(content or "Done.", ephemeral=ephemeral)
    except Exception:
        log.exception("Failed followup.send")

def _coerce_result(
    res: Union[discord.Embed, Tuple[discord.Embed, List[discord.File]], None]
) -> Tuple[Optional[discord.Embed], Optional[List[discord.File]]]:
    if res is None:
        return None, None
    if isinstance(res, tuple) and len(res) == 2 and isinstance(res[0], discord.Embed):
        return res[0], res[1]
    if isinstance(res, discord.Embed):
        return res, None
    return None, None

# ---------------- Lifecycle ---------------
@client.event
async def on_ready():
    log.info("CACHE_DIR=%s | LOG_LEVEL=%s | KEEP_ALIVE=%s", CACHE_DIR, LOG_LEVEL, str(KEEP_ALIVE))
    log.info("Running in BOT mode (gateway).")
    try:
        if GUILD:
            cmds = await tree.sync(guild=GUILD)
            log.info("Slash commands synced to guild %s (%d cmds)", GUILD.id, len(cmds))
        else:
            cmds = await tree.sync()
            log.info("Slash commands synced globally (%d cmds)", len(cmds))
    except Exception:
        log.exception("Initial slash sync failed")

    if KEEP_ALIVE:
        async def heartbeat():
            while True:
                log.debug("heartbeat: tick")
                await asyncio.sleep(60)
        client.loop.create_task(heartbeat())

# ---------------- Commands ----------------
@tree.command(name="ping", description="Health check.", guild=GUILD)
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("📍 Pong", ephemeral=True)

@tree.command(name="sync", description="Force re-sync slash commands (admin).", guild=GUILD)
async def sync_cmd(interaction: discord.Interaction):
    # Permission: server owner OR whitelisted admin ID
    allowed = False
    try:
        if interaction.guild and interaction.user:
            if interaction.user.id == interaction.guild.owner_id:
                allowed = True
    except Exception:
        pass
    if interaction.user and interaction.user.id in ADMIN_USER_IDS:
        allowed = True

    if not allowed:
        await interaction.response.send_message("Not allowed.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        if GUILD:
            cmds = await tree.sync(guild=GUILD)
        else:
            cmds = await tree.sync()
        names = ", ".join(sorted(c.name for c in cmds))
        await interaction.followup.send(f"Synced. Commands available: `{names}`")
        log.info("Manual sync by %s -> %s", interaction.user, names)
    except Exception:
        log.exception("Manual sync failed")
        await interaction.followup.send("Sync failed (see logs).", ephemeral=True)

@tree.command(name="scan_ticker", description="Run the multi-signal scan for a single ticker.", guild=GUILD)
@app_commands.describe(symbol="Ticker symbol, e.g. NVDA")
async def scan_ticker_cmd(interaction: discord.Interaction, symbol: str):
    sym = symbol.upper().strip()
    await interaction.response.defer()
    try:
        if hasattr(scanner, "analyze_ticker"):
            fn = scanner.analyze_ticker
            res = await fn(sym) if asyncio.iscoroutinefunction(fn) else fn(sym)
            emb, files = _coerce_result(res)
            if emb:
                await _safe_send_followup(interaction, embed=emb, files=files)
                return
        await _safe_send_followup(interaction, content=f"Scanner missing analyze_ticker() for `{sym}`.", ephemeral=True)
    except Exception:
        log.exception("scan_ticker failed for %s", sym)
        await _safe_send_followup(interaction, content="Sorry, that failed unexpectedly. See logs.", ephemeral=True)

@tree.command(name="earnings_watch", description="Show tickers with earnings within ±N days (broad universe supported).", guild=GUILD)
@app_commands.describe(days="Window in days (±N). Default 30.", limit="Max results to show (default 50).")
async def earnings_watch_cmd(interaction: discord.Interaction, days: Optional[int] = 30, limit: Optional[int] = 50):
    days = int(days or 30)
    limit = max(1, min(int(limit or 50), 200))  # keep messages sane
    await interaction.response.defer()

    try:
        # Preferred fast-path if your scanner exposes a universe helper
        if hasattr(scanner, "earnings_universe_window"):
            fn = scanner.earnings_universe_window
            res = await fn(days) if asyncio.iscoroutinefunction(fn) else fn(days)
            # accept either list[Embed] or (Embed, files)
            if isinstance(res, list):
                if not res:
                    await _safe_send_followup(interaction, content=f"No earnings within ±{days} days.")
                    return
                for emb in res[:limit]:
                    if isinstance(emb, discord.Embed):
                        await interaction.followup.send(embed=emb)
                return
            emb, files = _coerce_result(res)
            if emb:
                await _safe_send_followup(interaction, embed=emb, files=files)
                return

        # Back-compat slow-path: build the universe and ask scanner per-ticker.
        syms = universe_symbols()
        if not syms:
            await _safe_send_followup(interaction, content="Universe is empty (check SCAN_UNIVERSE / ALL_TICKERS / symbols file).", ephemeral=True)
            return

        # If single-ticker earnings helper exists, we can parallelize with a small semaphore.
        results: List[discord.Embed] = []
        sem = asyncio.Semaphore(10)

        async def check(sym: str):
            try:
                if hasattr(scanner, "earnings_for_ticker"):
                    fn = scanner.earnings_for_ticker
                    info = await fn(sym, days) if asyncio.iscoroutinefunction(fn) else fn(sym, days)
                    # earnings_for_ticker can return None or an Embed
                    if isinstance(info, discord.Embed):
                        results.append(info)
            except Exception:
                log.exception("earnings_for_ticker failed for %s", sym)

        async def worker(sym: str):
            async with sem:
                await check(sym)

        await asyncio.gather(*(worker(s) for s in syms))

        if not results:
            await _safe_send_followup(interaction, content=f"No earnings within ±{days} days.")
            return

        # Post up to 'limit' embeds (Discord rate-limits messages; we keep it reasonable)
        for emb in results[:limit]:
            await interaction.followup.send(embed=emb)

    except Exception:
        log.exception("Earnings watch failed")
        await _safe_send_followup(interaction, content="❌ Earnings scan failed (see logs).", ephemeral=True)

@tree.command(name="scan", description="Run a ranked scan for the configured universe (ad-hoc).", guild=GUILD)
@app_commands.describe(top_n="How many top picks to post (default 5, max 20).")
async def scan_cmd(interaction: discord.Interaction, top_n: Optional[int] = 5):
    await interaction.response.defer()
    syms = universe_symbols()
    if not syms:
        await _safe_send_followup(interaction, content="Universe is empty. Set SCAN_UNIVERSE or provide symbols file.", ephemeral=True)
        return

    top_n = max(1, min(int(top_n or 5), 20))
    try:
        if hasattr(scanner, "rank_universe"):
            fn = scanner.rank_universe
            result = await fn(syms, top_n=top_n) if asyncio.iscoroutinefunction(fn) else fn(syms, top_n=top_n)
            if isinstance(result, list):
                if not result:
                    await _safe_send_followup(interaction, content="No picks.")
                    return
                for emb in result[:top_n]:
                    if isinstance(emb, discord.Embed):
                        await interaction.followup.send(embed=emb)
                return
            emb, files = _coerce_result(result)
            if emb:
                await _safe_send_followup(interaction, embed=emb, files=files)
                return

        # Fallback: analyze sequentially until we have top_n embeds.
        posted = 0
        for sym in syms:
            if hasattr(scanner, "analyze_ticker"):
                fn = scanner.analyze_ticker
                res = await fn(sym) if asyncio.iscoroutinefunction(fn) else fn(sym)
                emb, files = _coerce_result(res)
                if emb:
                    await _safe_send_followup(interaction, embed=emb, files=files)
                    posted += 1
                    if posted >= top_n:
                        break
        if posted == 0:
            await _safe_send_followup(interaction, content="No results.", ephemeral=True)

    except Exception:
        log.exception("scan failed")
        await _safe_send_followup(interaction, content="Scan failed (see logs).", ephemeral=True)

# ---------------- Run ---------------------
if __name__ == "__main__":
    client.run(TOKEN)
