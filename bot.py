# bot.py — Discord slash-command wrapper for the scanner
# Works on Python 3.12+. Requires: discord.py, yfinance, pandas, numpy, ta, vaderSentiment (if you use sentiment)

import os
import asyncio
import datetime as dt
import traceback
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands

# ---- TIME / ENV ----
UTC = dt.timezone.utc
DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
GUILD_ID_ENV = os.environ.get("DISCORD_GUILD_ID", "").strip()  # optional; speeds up command registration

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_BOT_TOKEN in environment.")

# ---- SCANNER IMPORT ----
# We don't assume exact function names; we'll probe for several.
try:
    import scanner
except Exception as e:
    raise RuntimeError(f"Failed to import scanner.py: {e}")

def _now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)

# -------- Helpers --------

async def send_safely(
    interaction: discord.Interaction,
    *,
    content: Optional[str] = None,
    embed: Optional[discord.Embed] = None,
    ephemeral: bool = False,
) -> None:
    """Send once; if we already deferred or responded, use followup."""
    if not interaction.response.is_done():
        await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral)
    else:
        await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)

def safe_getattr(obj, names: List[str]):
    """Return first attribute that exists from names list, else None."""
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    return None

def format_error(title: str, err: Exception) -> str:
    return f"❌ {title}: {str(err)}"

# ---- Expected scanner call wrappers (defensive) ----

async def call_ranked_scan(tickers: Optional[str] = None) -> str:
    """
    Asks scanner for a full ranked scan (top 10) and returns a pre-formatted discord-friendly string.
    Tries multiple scanner function names so you don't have to keep renaming.
    """
    fn = safe_getattr(scanner, [
        "ranked_scan_text",      # returns ready-to-post text
        "run_ranked_scan_text",
        "scan_now_text",
        "ranked_scan",           # returns list/dicts -> we format lightly
        "run_ranked_scan",
        "scan_market",
        "scan_now"
    ])
    if fn is None:
        raise RuntimeError("scanner.py is missing a ranked scan function (e.g., ranked_scan_text or run_ranked_scan).")

    # If function returns ready text, just return it; otherwise do a simple formatter.
    result = await _maybe_await(fn(tickers)) if asyncio.iscoroutinefunction(fn) else fn(tickers)
    if isinstance(result, str):
        return result

    # Otherwise, assume it's a list of dicts like [{'Ticker': 'AAPL', 'Type': 'CALL', 'Price': ...}, ...]
    if not isinstance(result, list):
        raise RuntimeError("Scanner ranked function returned unexpected type. Expected str or list[dict].")
    if len(result) == 0:
        return "No high-conviction signals right now."

    lines = ["**📊 Premarket Ranked Scan (Top 10)**"]
    for row in result[:10]:
        t = row.get("Ticker", "?")
        p = row.get("Price", "?")
        bias = row.get("Type", "?")
        exp = row.get("Target Expiration", row.get("Expiration", row.get("Exp", "?")))
        br = row.get("Buy Range", "?")
        tgt = row.get("Sell Target", row.get("Target", "?"))
        stp = row.get("Stop Idea", row.get("Stop", "?"))
        why = row.get("Why", row.get("Reason", "?"))
        oc = row.get("Option Contract", row.get("Contract", "?"))
        strike = row.get("Strike", "?")
        mid = row.get("Opt Mid", row.get("Mid", "?"))
        spr = row.get("Spread %", row.get("Spread", "?"))
        vol = row.get("Opt Vol", row.get("Vol", "?"))
        oi = row.get("Opt OI", row.get("OI", "?"))

        lines.append(
            f"• **{t}** @ ${p} → **{bias}** | Exp **{exp}**\n"
            f"  **Buy** {br} • **Target** {tgt} • **Stop** {stp}\n"
            f"  **Option** {oc} | Strike **{strike}** | Mid **{mid}** | Spread **{spr}** | Vol **{vol}** | OI **{oi}**\n"
            f"  *Why:* {why}"
        )
    return "\n".join(lines)

async def call_scan_ticker(symbol: str) -> str:
    fn = safe_getattr(scanner, [
        "scan_ticker_text",
        "analyze_ticker_text",
        "analyze_single_ticker_text",
        "scan_ticker",
        "analyze_ticker",
        "analyze_single_ticker",
    ])
    if fn is None:
        raise RuntimeError("scanner.py is missing a single-ticker function (e.g., analyze_ticker_text).")

    result = await _maybe_await(fn(symbol)) if asyncio.iscoroutinefunction(fn) else fn(symbol)
    if isinstance(result, str):
        return result
    # If dict -> pretty print
    if isinstance(result, dict):
        t = result.get("Ticker", symbol.upper())
        p = result.get("Price", "?")
        bias = result.get("Type", "?")
        br = result.get("Buy Range", "?")
        tgt = result.get("Target", result.get("Sell Target", "?"))
        stp = result.get("Stop", result.get("Stop Idea", "?"))
        why = result.get("Why", result.get("Reason", "?"))

        oc = result.get("Option Contract", result.get("Contract", "?"))
        exp = result.get("Target Expiration", result.get("Expiration", result.get("Exp", "?")))
        strike = result.get("Strike", "?")
        mid = result.get("Opt Mid", result.get("Mid", "?"))
        spr = result.get("Spread %", result.get("Spread", "?"))
        vol = result.get("Opt Vol", result.get("Vol", "?"))
        oi = result.get("Opt OI", result.get("OI", "?"))

        return (
            f"**{t}** • **${p}**\n"
            f"**Bias:** {bias} • **Buy:** {br} • **Target:** {tgt} • **Stop:** {stp}\n"
            f"**Why:** {why}\n\n"
            f"**Option**\n"
            f"{oc} • **Exp** {exp}\n"
            f"Strike **{strike}** • Mid **{mid}** • Spread **{spr}**\n"
            f"Vol **{vol}** • OI **{oi}**"
        )
    raise RuntimeError("Scanner single-ticker function returned unexpected type. Expected str or dict.")

async def call_earnings_watch(symbols: Optional[str] = None, window_days: int = 7) -> str:
    fn = safe_getattr(scanner, [
        "earnings_watch_text",
        "earnings_in_window_text",
        "earnings_watch",
        "earnings_in_window",
        "get_earnings_window",
    ])
    if fn is None:
        raise RuntimeError("scanner.py is missing an earnings function (e.g., earnings_watch_text).")
    result = await _maybe_await(fn(symbols, window_days)) if asyncio.iscoroutinefunction(fn) else fn(symbols, window_days)
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        if not result:
            return f"No earnings within ±{window_days} days for the current universe."
        lines = [f"**📅 Earnings (±{window_days}d)**"]
        for r in result:
            sym = r.get("Ticker") or r.get("Symbol") or "?"
            date = r.get("Earnings Date") or r.get("Date") or "?"
            note = r.get("Note", "")
            lines.append(f"• **{sym}** — {date} {note}")
        return "\n".join(lines)
    return f"No earnings within ±{window_days} days for the current universe."

async def call_news_sentiment(symbol: str) -> str:
    fn = safe_getattr(scanner, [
        "news_sentiment_text",
        "sentiment_snapshot_text",
        "news_sentiment",
        "sentiment_snapshot",
    ])
    if fn is None:
        raise RuntimeError("scanner.py is missing a news/sentiment function (e.g., news_sentiment_text).")
    result = await _maybe_await(fn(symbol)) if asyncio.iscoroutinefunction(fn) else fn(symbol)
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        score = result.get("Score", "?")
        sample = result.get("Sample", "")
        return f"**📰 {symbol.upper()} — Sentiment:** {score}\n{sample}"
    return f"Couldn't compute sentiment for {symbol.upper()}."

async def call_signal_history(symbol: str, n: int = 3) -> str:
    fn = safe_getattr(scanner, [
        "signal_history_text",
        "get_signal_history_text",
        "signal_history",
        "get_signal_history",
    ])
    if fn is None:
        raise RuntimeError("scanner.py is missing a signal history function.")
    result = await _maybe_await(fn(symbol, n)) if asyncio.iscoroutinefunction(fn) else fn(symbol, n)
    if isinstance(result, str):
        return result
    if isinstance(result, list) and result:
        lines = [f"**🧭 {symbol.upper()} — Last {min(n, len(result))} signals**"]
        for r in result[:n]:
            ts = r.get("Time", r.get("Timestamp", "?"))
            bias = r.get("Type", r.get("Bias", "?"))
            px = r.get("Price", "?")
            outcome = r.get("Outcome", "")
            lines.append(f"• {ts} — {bias} @ {px} {outcome}")
        return "\n".join(lines)
    return f"No saved signals yet for {symbol.upper()}."

async def _maybe_await(val):
    if asyncio.iscoroutine(val):
        return await val
    return val

# -------- Discord Client / Tree --------

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# If you set DISCORD_GUILD_ID, register commands only to that server (much faster).
GUILD_OBJ = None
if GUILD_ID_ENV.isdigit():
    GUILD_OBJ = discord.Object(id=int(GUILD_ID_ENV))

@client.event
async def on_ready():
    try:
        if GUILD_OBJ is not None:
            await tree.sync(guild=GUILD_OBJ)
        else:
            await tree.sync()
        print(f"[{_now_utc().isoformat()}] Logged in as {client.user} (commands synced)")
    except Exception:
        traceback.print_exc()

# -------- Commands --------

@tree.command(name="ping", description="Health check")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=False, ephemeral=True)
    await interaction.followup.send("pong", ephemeral=True)

@tree.command(name="scan_now", description="Run full ranked scan (top 10). Optionally pass tickers like: AAPL,MSFT,NVDA")
@app_commands.describe(tickers="Comma-separated list (optional). If empty, uses your default universe.")
async def scan_now_cmd(interaction: discord.Interaction, tickers: Optional[str] = None):
    await interaction.response.defer(thinking=True, ephemeral=False)
    try:
        text = await call_ranked_scan(tickers)
        await interaction.followup.send(text, ephemeral=False)
    except Exception as e:
        await interaction.followup.send(format_error("Scan failed", e), ephemeral=False)

@tree.command(name="scan_ticker", description="Analyze a single ticker on demand (e.g., NVDA)")
@app_commands.describe(symbol="Ticker symbol (e.g., NVDA)")
async def scan_ticker_cmd(interaction: discord.Interaction, symbol: str):
    await interaction.response.defer(thinking=True, ephemeral=False)
    try:
        text = await call_scan_ticker(symbol)
        await interaction.followup.send(text, ephemeral=False)
    except Exception as e:
        await interaction.followup.send(format_error(f"Could not analyze {symbol.upper()}", e), ephemeral=False)

@tree.command(name="earnings_watch", description="Show earnings within ±7 days. Accepts optional list or single symbol.")
@app_commands.describe(symbols="Optional: single ticker or comma-separated list. If empty, use default universe.")
async def earnings_watch_cmd(interaction: discord.Interaction, symbols: Optional[str] = None):
    await interaction.response.defer(thinking=True, ephemeral=False)
    try:
        text = await call_earnings_watch(symbols, window_days=7)
        await interaction.followup.send(text, ephemeral=False)
    except Exception as e:
        await interaction.followup.send(format_error("Earnings watch failed", e), ephemeral=False)

@tree.command(name="news_sentiment", description="Headline sentiment snapshot for a ticker")
@app_commands.describe(symbol="Ticker symbol (e.g., NVDA)")
async def news_sentiment_cmd(interaction: discord.Interaction, symbol: str):
    await interaction.response.defer(thinking=True, ephemeral=False)
    try:
        text = await call_news_sentiment(symbol)
        await interaction.followup.send(text, ephemeral=False)
    except Exception as e:
        await interaction.followup.send(format_error("Sentiment failed", e), ephemeral=False)

@tree.command(name="signal_history", description="Last 3 signals we posted for a symbol")
@app_commands.describe(symbol="Ticker symbol (e.g., NVDA)", count="How many (default 3, max 10)")
async def signal_history_cmd(interaction: discord.Interaction, symbol: str, count: Optional[int] = 3):
    await interaction.response.defer(thinking=True, ephemeral=False)
    try:
        c = max(1, min(10, count or 3))
        text = await call_signal_history(symbol, c)
        await interaction.followup.send(text, ephemeral=False)
    except Exception as e:
        await interaction.followup.send(format_error("Signal history failed", e), ephemeral=False)

@tree.command(name="help", description="Commands & how to read signals")
async def help_cmd(interaction: discord.Interaction):
    # Always defer first; then follow up (prevents Unknown interaction)
    await interaction.response.defer(thinking=False, ephemeral=True)
    txt = (
        "🤖 **Scanner Help**\n\n"
        "**Commands**\n"
        "• `/scan_now` *[tickers]* — run full ranked scan (top 10)\n"
        "• `/scan_ticker SYMBOL` — analyze one ticker on demand\n"
        "• `/earnings_watch` *[symbols]* — earnings within ±7 days\n"
        "• `/news_sentiment SYMBOL` — headline sentiment snapshot\n"
        "• `/signal_history SYMBOL` — last 3 signals we posted\n\n"
        "**How to read a signal**\n"
        "• **Bias**: CALL (green) or PUT (red)\n"
        "• **Buy Range**: zone to enter\n"
        "• **Target/Stop**: exit planning\n"
        "• **Option**: ~7–21 DTE, ATM, mid & spread %\n"
        "• **Risk**: from liquidity/volatility\n"
    )
    await interaction.followup.send(txt, ephemeral=True)

# ---- Run ----
def _guild_kw():
    return {"guild": GUILD_OBJ} if GUILD_OBJ else {}

# If you want commands to register only to your server faster, re-add them with guild=GUILD_OBJ:
# (We already sync in on_ready; this is optional sugar while developing.)
# tree.copy_global_to(guild=GUILD_OBJ); await tree.sync(guild=GUILD_OBJ)

client.run(DISCORD_TOKEN)
