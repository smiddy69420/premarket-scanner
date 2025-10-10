# bot.py — unified Discord bot (Wave 2 ready)
import os
import json
import time
import math
import logging
import asyncio
import pathlib
import argparse
import urllib.request
from typing import List

import discord
from discord import app_commands
from discord.ext import commands

import datetime as dt
import yfinance as yf

# Local modules (already in your repo)
import scanner as sc           # earnings + single-ticker rich card + embeds
from scanner_core import run_scan  # ranked multi-ticker scan (DataFrame)

# ============================================================
#  ENVIRONMENT & LOGGING BOOTSTRAP
# ============================================================

CACHE_DIR        = os.getenv("CACHE_DIR", "/tmp/premarket_cache")
LOG_LEVEL        = os.getenv("LOG_LEVEL", "INFO").upper()
KEEP_ALIVE       = os.getenv("KEEP_ALIVE", "false").lower() == "true"
TZ               = os.getenv("TZ", "America/New_York")

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_GUILD_ID  = os.getenv("DISCORD_GUILD_ID")
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")

# Comma-separated default universe for quick ops
SCAN_UNIVERSE = [s.strip().upper() for s in os.getenv(
    "SCAN_UNIVERSE", "AAPL,MSFT,NVDA,TSLA,AMZN,AMD,JPM"
).split(",") if s.strip()]

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

# quiet optional warnings (voice not used)
import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="discord")

# ============================================================
#  DISCORD SETUP
# ============================================================

intents = discord.Intents.default()
intents.message_content = True  # needed for completeness; slash commands are fine regardless
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

async def _heartbeat_task():
    hlog = logging.getLogger("heartbeat")
    while True:
        await asyncio.sleep(60)
        hlog.debug("tick")

async def _refresh_earnings_cache_task():
    rlog = logging.getLogger("refresh")
    while True:
        try:
            rlog.info("Refreshing earnings cache…")
            sc.refresh_all_caches()  # 12h refresh of the entire universe
            rlog.info("Earnings cache refresh complete.")
        except Exception as e:
            rlog.exception("Refresh failed: %s", e)
        # run every 12h
        await asyncio.sleep(12 * 3600)

@bot.event
async def on_ready():
    # Sync slash commands to a single guild (fast) if provided; otherwise global (slower)
    if DISCORD_GUILD_ID:
        guild = discord.Object(id=int(DISCORD_GUILD_ID))
        await tree.sync(guild=guild)
        log.info(f"Slash commands synced to guild {DISCORD_GUILD_ID}")
    else:
        await tree.sync()
        log.info("Slash commands synced globally")

    if KEEP_ALIVE:
        asyncio.create_task(_heartbeat_task())
    # background cache refresher
    asyncio.create_task(_refresh_earnings_cache_task())

    log.info(f"Logged in as {bot.user} (id={bot.user.id})")

# ============================================================
#  HELPERS
# ============================================================

def _now_utc_date() -> dt.date:
    return dt.datetime.utcnow().date()

def _chunk(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i+size]

def _post_message_rest(channel_id: str, content: str) -> None:
    """Used for cron/diagnostics without gateway context."""
    if not channel_id:
        return
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    data = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bot {DISCORD_BOT_TOKEN}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        _ = resp.read()

# ---- robust, local analyzer for quick single-ticker calc (used as fallback) ----
def analyze_ticker(symbol: str):
    """Resilient one-pager using only yfinance; returns dict or None."""
    import numpy as np
    try:
        df = yf.download(symbol, period="3mo", interval="1d", progress=False, threads=False, auto_adjust=False)
        if df is None or df.empty or "Close" not in df.columns:
            raise ValueError("No valid data returned for ticker")

        df = df.dropna(subset=["Close"]).copy()
        if len(df) < 55:  # ensure enough lookback for EMA50
            raise ValueError("Insufficient history for indicators")

        close = float(df["Close"].iloc[-1])
        ema20 = float(df["Close"].ewm(span=20, min_periods=20).mean().iloc[-1])
        ema50 = float(df["Close"].ewm(span=50, min_periods=50).mean().iloc[-1])

        macd_series = (df["Close"].ewm(span=12, min_periods=12).mean()
                       - df["Close"].ewm(span=26, min_periods=26).mean())
        macd = float(macd_series.iloc[-1])

        delta = df["Close"].diff()
        gain = delta.clip(lower=0).rolling(window=14).mean()
        loss = -delta.clip(upper=0).rolling(window=14).mean()
        rs = gain / (loss + 1e-9)
        rsi = float(100 - (100 / (1 + rs.iloc[-1])))

        if any(map(lambda x: x is None or math.isnan(x), [close, ema20, ema50, macd, rsi])):
            raise ValueError("NaN encountered in indicator calculation")

        decision = "CALL" if close > ema20 > ema50 else "PUT" if close < ema20 < ema50 else "NEUTRAL"
        return {
            "symbol": symbol.upper(),
            "close": round(close, 2),
            "ema20": round(ema20, 2),
            "ema50": round(ema50, 2),
            "macd": round(macd, 3),
            "rsi": round(rsi, 1),
            "decision": decision,
        }
    except Exception as e:
        logging.getLogger("analyze").exception("Analyze error for %s: %s", symbol, e)
        return None

# ============================================================
#  SLASH COMMANDS
# ============================================================

@tree.command(name="ping", description="Check bot responsiveness")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("📍 Pong", ephemeral=True)

@tree.command(name="scan_ticker", description="Analyze one ticker with indicators & ATM option")
@app_commands.describe(symbol="Ticker symbol to analyze (e.g., NVDA)")
async def scan_ticker_cmd(interaction: discord.Interaction, symbol: str):
    await interaction.response.defer(thinking=True)

    sym = symbol.strip().upper()
    # Prefer rich card from scanner.py; fall back to local calc if needed
    try:
        card = sc.analyze_one_ticker(sym)
        if card:
            embed = sc.render_ticker_embed(card)
            await interaction.followup.send(embed=embed)
            return
    except Exception as e:
        logging.getLogger("scan_ticker").exception("scanner.analyze_one_ticker failed: %s", e)

    basic = analyze_ticker(sym)
    if not basic:
        await interaction.followup.send(f"❌ Could not analyze **{sym}** (no data or indicator error).", ephemeral=True)
        return

    # simple embed
    color = 0x2ECC71 if basic["decision"] == "CALL" else (0xE74C3C if basic["decision"] == "PUT" else 0x999999)
    e = discord.Embed(title=f"{basic['symbol']} • {basic['decision']}", color=color)
    e.add_field(name="Last", value=f"${basic['close']}", inline=True)
    e.add_field(name="EMA20 / EMA50", value=f"{basic['ema20']} / {basic['ema50']}", inline=True)
    e.add_field(name="MACD / RSI", value=f"{basic['macd']} / {basic['rsi']}", inline=True)
    e.set_footer(text="Premarket Scanner • yfinance fallback")
    await interaction.followup.send(embed=e)

@tree.command(name="earnings_watch", description="Show companies with earnings within ±N days")
@app_commands.describe(
    days="Days ahead/back to search (1–30).",
    all="If true, search full cached universe; otherwise SCAN_UNIVERSE only."
)
async def earnings_watch_cmd(interaction: discord.Interaction, days: int = 7, all: bool = False):
    if days < 1 or days > 30:
        await interaction.response.send_message("Please choose a window between **1–30** days.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)

    today = _now_utc_date()
    rows: List[dict] = []

    try:
        if all:
            # Use cached full universe from scanner.py
            rows = sc.earnings_universe_window(days)
        else:
            # Quick path: only your SCAN_UNIVERSE env list
            for sym in SCAN_UNIVERSE:
                info = sc.earnings_for_ticker(sym, days)
                if info:
                    rows.append({"symbol": info["symbol"], "date": info["date"]})
    except Exception as e:
        logging.getLogger("earnings").exception("Earnings scan failed: %s", e)
        await interaction.followup.send("❌ Earnings scan failed (see logs).", ephemeral=True)
        return

    if not rows:
        await interaction.followup.send(f"No earnings within ±{days} days.", ephemeral=True)
        return

    # Sort & paginate in embeds
    rows.sort(key=lambda r: (r["date"], r["symbol"]))
    PAGE = 25
    pages = [rows[i:i+PAGE] for i in range(0, len(rows), PAGE)]
    embeds = []
    for i, page in enumerate(pages, start=1):
        embeds.append(sc.render_earnings_page_embed(page, days, i, len(pages)))

    # Send sequentially (Discord max 10 embeds per message)
    for batch in _chunk(embeds, 10):
        await interaction.followup.send(embeds=batch)

@tree.command(name="scan_top", description="Run ranked scan and show top N picks (like the webhook)")
@app_commands.describe(top="How many to show (1–10)")
async def scan_top_cmd(interaction: discord.Interaction, top: int = 5):
    if top < 1: top = 1
    if top > 10: top = 10

    await interaction.response.defer(thinking=True)
    try:
        df, meta = run_scan(top_k=top)
        if df is None or df.empty:
            await interaction.followup.send("No candidates found.", ephemeral=True)
            return

        # Build compact embeds (re-implement here to avoid webhook imports)
        header = discord.Embed(
            title="📣 Premarket Ranked Scan",
            description=f"Top {len(df)} picks • CALL=green • PUT=red\n{meta}",
            color=0x7289DA
        )
        out = [header]

        def _color_for(bias: str):
            return 0x2ECC71 if str(bias).upper() == "CALL" else 0xE74C3C

        for _, r in df.iterrows():
            desc = (
                f"**Bias:** {r['Type']}  •  **Exp:** `{r['Target Expiration']}`\n"
                f"**Buy:** {r['Buy Range']}  •  **Target:** {r['Sell Target']}  •  **Stop:** {r['Stop Idea']}\n"
                f"**Risk:** {r['Risk']}\n"
                f"**Why:** {r['Why']}"
            )
            opt_line = str(r.get("Opt Note", "")) or ""
            if r.get("Option Contract"):
                opt_line = (f"`{r['Option Contract']}` — strike **{r['Strike']}**, "
                            f"mid **${r['Opt Mid']}**, spread **~{r['Spread %']}%**, "
                            f"vol **{r['Opt Vol']}**, OI **{r['Opt OI']}**")

            emb = discord.Embed(
                title=f"{r['Ticker']}  •  ${r['Price']}",
                description=desc + ("\n" + opt_line if opt_line else ""),
                color=_color_for(r["Type"])
            )
            out.append(emb)

        # send in batches (<=10 embeds per message)
        for batch in _chunk(out, 10):
            await interaction.followup.send(embeds=batch)

    except Exception as e:
        logging.getLogger("scan_top").exception("scan_top failed: %s", e)
        await interaction.followup.send("❌ Scan failed unexpectedly. Check logs.", ephemeral=True)

# ============================================================
#  CRON / ONE-SHOT MODE
# ============================================================

def _cron_morning_digest() -> None:
    """One-shot heartbeat (kept minimal; you also have webhook_runner.py for full digest)."""
    try:
        utc_now = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())
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
