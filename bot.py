import os
import json
import time
import logging
import asyncio
import pathlib
import argparse
import urllib.request
import discord
from discord import app_commands
from discord.ext import commands
import yfinance as yf
import datetime as dt

# ============================================================
#  ENVIRONMENT & LOGGING BOOTSTRAP
# ============================================================

CACHE_DIR = os.getenv("CACHE_DIR", "/tmp/premarket_cache")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
KEEP_ALIVE = os.getenv("KEEP_ALIVE", "false").lower() == "true"

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID")
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")
SCAN_UNIVERSE = os.getenv("SCAN_UNIVERSE", "AAPL,MSFT,NVDA,TSLA,AMZN,AMD,JPM").split(",")

if not DISCORD_BOT_TOKEN:
    raise RuntimeError("Missing DISCORD_BOT_TOKEN in environment.")

numeric_level = getattr(logging, LOG_LEVEL, logging.INFO)
logging.basicConfig(
    level=numeric_level,
    format="[%(asctime)s] [%(levelname)8s] %(name)s: %(message)s",
)
log_boot = logging.getLogger("bootstrap")

pathlib.Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
log_boot.info(f"CACHE_DIR={CACHE_DIR} | LOG_LEVEL={LOG_LEVEL} | KEEP_ALIVE={KEEP_ALIVE}")

async def _heartbeat_task():
    log = logging.getLogger("heartbeat")
    while True:
        await asyncio.sleep(60)
        log.debug("tick")

# ============================================================
#  DISCORD SETUP
# ============================================================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


@bot.event
async def on_ready():
    if DISCORD_GUILD_ID:
        guild = discord.Object(id=int(DISCORD_GUILD_ID))
        await tree.sync(guild=guild)
        logging.info(f"Slash commands synced to guild {DISCORD_GUILD_ID}")
    else:
        await tree.sync()
        logging.info("Slash commands synced globally")

    if KEEP_ALIVE:
        asyncio.create_task(_heartbeat_task())

    logging.info(f"Logged in as {bot.user} (id={bot.user.id})")

# ============================================================
#  SCANNER FUNCTIONS
# ============================================================

def analyze_ticker(symbol: str):
    """Basic analysis block using yfinance"""
    try:
        data = yf.download(symbol, period="3mo", interval="1d", progress=False)
        if data.empty:
            raise ValueError("No price data returned")

        close = data["Close"].iloc[-1]
        ema20 = data["Close"].ewm(span=20).mean().iloc[-1]
        ema50 = data["Close"].ewm(span=50).mean().iloc[-1]
        macd = (data["Close"].ewm(span=12).mean() - data["Close"].ewm(span=26).mean()).iloc[-1]
        rsi = 100 - (100 / (1 + (data["Close"].diff().clip(lower=0).rolling(14).mean() /
                                 abs(data["Close"].diff().clip(upper=0)).rolling(14).mean()).iloc[-1]))

        decision = "CALL" if close > ema20 > ema50 else "PUT" if close < ema20 < ema50 else "NEUTRAL"
        return {
            "symbol": symbol,
            "close": round(close, 2),
            "ema20": round(ema20, 2),
            "ema50": round(ema50, 2),
            "macd": round(macd, 3),
            "rsi": round(rsi, 1),
            "decision": decision,
        }
    except Exception as e:
        logging.exception(f"Analyze error for {symbol}: {e}")
        return None


def get_earnings(symbol: str):
    """Return next and previous earnings from yfinance"""
    try:
        tk = yf.Ticker(symbol)
        cal = tk.earnings_dates
        if cal is None or cal.empty:
            return None
        today = dt.datetime.utcnow().date()
        # Flatten index for ease
        cal = cal.reset_index()
        cal["Earnings Date"] = cal["Earnings Date"].dt.date
        nearest = cal.iloc[(cal["Earnings Date"] - today).abs().argsort()[:1]]
        date = nearest["Earnings Date"].values[0]
        eps = nearest["EPS Estimate"].values[0] if "EPS Estimate" in nearest else None
        return {"symbol": symbol, "date": str(date), "eps": eps}
    except Exception as e:
        logging.exception(f"Earnings fetch failed for {symbol}: {e}")
        return None


# ============================================================
#  DISCORD COMMANDS
# ============================================================

@tree.command(name="ping", description="Check bot responsiveness")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("📍 Pong", ephemeral=True)


@tree.command(name="scan_ticker", description="Analyze one ticker (e.g. NVDA, TSLA)")
@app_commands.describe(symbol="Ticker symbol to analyze (e.g. NVDA)")
async def scan_ticker(interaction: discord.Interaction, symbol: str):
    await interaction.response.defer(thinking=True)
    data = analyze_ticker(symbol.upper())
    if not data:
        await interaction.followup.send(f"❌ Could not analyze {symbol.upper()}", ephemeral=True)
        return
    msg = (
        f"**{data['symbol']} • {data['decision']}**\n"
        f"Last: ${data['close']}\nEMA20: {data['ema20']} | EMA50: {data['ema50']}\n"
        f"MACD: {data['macd']} | RSI: {data['rsi']}"
    )
    await interaction.followup.send(msg)


@tree.command(name="earnings_watch", description="Show all companies with earnings within ±N days")
@app_commands.describe(days="Number of days to search ahead/back (1–30)")
async def earnings_watch(interaction: discord.Interaction, days: int = 7):
    if days < 1 or days > 30:
        await interaction.response.send_message("Please pick between 1–30 days.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    today = dt.datetime.utcnow().date()
    results = []
    for sym in SCAN_UNIVERSE:
        e = get_earnings(sym.strip())
        if e:
            edate = dt.datetime.strptime(e["date"], "%Y-%m-%d").date()
            delta = (edate - today).days
            if abs(delta) <= days:
                results.append(f"**{sym}** → {e['date']} ({delta:+} days)")
    if not results:
        await interaction.followup.send(f"No earnings within ±{days} days.", ephemeral=True)
    else:
        await interaction.followup.send("\n".join(results))

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
    """One-shot digest (future: add top-N scans or 30d earnings summary)."""
    try:
        utc_now = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())
        msg = f"🤖 Cron heartbeat OK • {utc_now}\nNext upgrade → full top-10 scan & 30d earnings digest."
        _post_message_rest(DISCORD_CHANNEL_ID, msg)
    except Exception as e:
        logging.getLogger("cron").exception("Cron failed: %s", e)
        _post_message_rest(DISCORD_CHANNEL_ID, f"⚠️ Cron failed: {e}")

# ============================================================
#  MAIN ENTRYPOINT
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Premarket Scanner bot")
    parser.add_argument("--mode", choices=["bot", "cron"], default="bot",
                        help="Run live Discord bot (gateway) or one-shot cron poster.")
    args = parser.parse_args()

    if args.mode == "cron":
        logging.info("Running in CRON mode (no gateway).")
        if not DISCORD_CHANNEL_ID:
            raise RuntimeError("CRON mode requires DISCORD_CHANNEL_ID.")
        _cron_morning_digest()
    else:
        logging.info("Running in BOT mode (gateway).")
        bot.run(DISCORD_BOT_TOKEN)
