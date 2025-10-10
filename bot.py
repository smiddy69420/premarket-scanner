# bot.py — production glue for scanner.py (one-shot)
import os, json, time, logging, asyncio, argparse, pathlib, urllib.request, datetime as dt
import discord
from discord import app_commands
from discord.ext import commands

# ---- Env / logging ----
CACHE_DIR = os.getenv("CACHE_DIR", "/tmp/premarket_cache")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
KEEP_ALIVE = os.getenv("KEEP_ALIVE", "false").lower() == "true"

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_GUILD_ID  = os.getenv("DISCORD_GUILD_ID")
DISCORD_CHANNEL_ID= os.getenv("DISCORD_CHANNEL_ID")
SCAN_UNIVERSE     = [s.strip().upper() for s in os.getenv(
    "SCAN_UNIVERSE", "AAPL,MSFT,NVDA,TSLA,AMZN,AMD,JPM"
).split(",") if s.strip()]

if not DISCORD_BOT_TOKEN:
    raise RuntimeError("Missing DISCORD_BOT_TOKEN in environment.")

pathlib.Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
numeric_level = getattr(logging, LOG_LEVEL, logging.INFO)
logging.basicConfig(level=numeric_level, format="[%(asctime)s] [%(levelname)8s] %(name)s: %(message)s")
log = logging.getLogger("bot")
log.info(f"CACHE_DIR={CACHE_DIR} | LOG_LEVEL={LOG_LEVEL} | KEEP_ALIVE={KEEP_ALIVE}")

# ---- Import robust scanner helpers (already in your repo) ----
from scanner import (
    analyze_one_ticker,
    render_ticker_embed,
    earnings_universe_window,
    render_earnings_page_embed,
    refresh_all_caches,
)

# ---- Discord setup ----
intents = discord.Intents.default()
intents.message_content = True  # fine for text-only; voice not required
bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---- Global error handler for ALL slash commands ----
@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    logging.exception("Slash command error", exc_info=error)
    try:
        if interaction.response.is_done():
            await interaction.followup.send("❌ Sorry, that failed unexpectedly. Check logs.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Sorry, that failed unexpectedly. Check logs.", ephemeral=True)
    except Exception:
        pass

# ---- Background tasks ----
async def heartbeat():
    hlog = logging.getLogger("heartbeat")
    while True:
        await asyncio.sleep(60)
        hlog.debug("tick")

async def refresh_caches_periodically():
    rlog = logging.getLogger("refresh")
    # first warmup shortly after startup, then every 12h
    await asyncio.sleep(5)
    while True:
        try:
            rlog.info("Refreshing earnings cache…")
            await asyncio.to_thread(refresh_all_caches)
            rlog.info("Earnings cache refresh complete.")
        except Exception as e:
            rlog.exception("Refresh failed: %s", e)
        await asyncio.sleep(12 * 3600)

@bot.event
async def on_ready():
    if DISCORD_GUILD_ID:
        await tree.sync(guild=discord.Object(id=int(DISCORD_GUILD_ID)))
        log.info(f"Slash commands synced to guild {DISCORD_GUILD_ID}")
    else:
        await tree.sync()
        log.info("Slash commands synced globally")
    if KEEP_ALIVE:
        asyncio.create_task(heartbeat())
    asyncio.create_task(refresh_caches_periodically())
    log.info(f"Logged in as {bot.user} (id={bot.user.id})")

# ---- Commands ----
@tree.command(name="ping", description="Check bot responsiveness")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("📍 Pong", ephemeral=True)

@tree.command(name="scan_ticker", description="Analyze one ticker (e.g., NVDA, TSLA)")
@app_commands.describe(symbol="Ticker symbol to analyze (e.g., NVDA)")
async def scan_ticker(interaction: discord.Interaction, symbol: str):
    # Always defer first to avoid 'application did not respond'
    await interaction.response.defer(thinking=True)
    sym = symbol.strip().upper()
    try:
        card = await asyncio.to_thread(analyze_one_ticker, sym)
        if not card:
            await interaction.followup.send(f"❌ Could not analyze **{sym}** (no data).", ephemeral=True)
            return
        embed = render_ticker_embed(card)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        logging.exception("scan_ticker failed for %s", sym)
        await interaction.followup.send(f"❌ Internal error for **{sym}**: {e}", ephemeral=True)

@tree.command(name="earnings_watch", description="Show companies with earnings within ±N days")
@app_commands.describe(days="Number of days to search ahead/back (1–30)")
async def earnings_watch(interaction: discord.Interaction, days: int = 7):
    if days < 1 or days > 30:
        await interaction.response.send_message("Please choose **1–30** days.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    try:
        # Pull from cached, broad universe; scanner will lazily extend cache as needed
        rows = await asyncio.to_thread(earnings_universe_window, days)
        if not rows:
            await interaction.followup.send(f"No earnings within ±{days} days.", ephemeral=True)
            return
        # paginate: ~25 per page
        page_size = 25
        total = (len(rows) + page_size - 1) // page_size
        for i in range(total):
            chunk = rows[i*page_size:(i+1)*page_size]
            embed = render_earnings_page_embed(chunk, days, i+1, total)
            await interaction.followup.send(embed=embed)
    except Exception as e:
        logging.exception("earnings_watch failed")
        await interaction.followup.send(f"❌ Earnings lookup failed: {e}", ephemeral=True)

# ---- Minimal REST poster (for future cron worker you already scaffolded) ----
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
        msg = f"🤖 Cron heartbeat OK • {utc_now}\n(Next: top-10 ranked scan & 30d earnings digest.)"
        _post_message_rest(DISCORD_CHANNEL_ID, msg)
    except Exception as e:
        logging.getLogger("cron").exception("Cron failed: %s", e)
        try:
            _post_message_rest(DISCORD_CHANNEL_ID, f"⚠️ Cron failed: {e}")
        except Exception:
            pass

# ---- Entrypoint ----
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
