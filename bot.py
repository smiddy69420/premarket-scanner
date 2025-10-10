# bot.py
import os, asyncio, re
from typing import Optional, List
import discord
from discord import app_commands, Colour, Embed

import pandas as pd
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
import yfinance as yf

from scanner import analyze_symbol, scan_many, earnings_date, NY

TOKEN = os.getenv("DISCORD_BOT_TOKEN")  # your bot token
GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0"))  # optional lock
SCANS_CHANNEL_ID = int(os.getenv("DISCORD_SCANS_CHANNEL_ID", "0"))  # optional

intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# ---------- helpers

def parse_ticker_list(s: Optional[str]) -> List[str]:
    if not s: return []
    tickers = [t.strip().upper() for t in re.split(r"[,\s]+", s) if t.strip()]
    # sanity filter
    return [t for t in tickers if re.fullmatch(r"[A-Z.\-]{1,12}", t)]

def format_money(x: Optional[float]) -> str:
    return f"${x:.2f}" if isinstance(x, (int,float)) and pd.notna(x) else "—"

def format_pct(x: Optional[float]) -> str:
    return f"{x*100:.1f}%" if isinstance(x,(int,float)) and pd.notna(x) else "—"

# ---------- on_ready

@bot.event
async def on_ready():
    try:
        await tree.sync(guild=discord.Object(id=GUILD_ID)) if GUILD_ID else await tree.sync()
        print("Slash commands synced.")
    except Exception as e:
        print("Sync error:", e)
    print(f"Logged in as {bot.user}")

# ---------- basic commands

@tree.command(name="ping", description="Bot health")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong")

@tree.command(name="help", description="Commands & how to read signals")
async def help_cmd(interaction: discord.Interaction):
    txt = (
        "**Commands**\n"
        "• `/scan_now` *[tickers]* — run ranked scan (top 10)\n"
        "• `/scan_ticker SYMBOL` — analyze a single ticker\n"
        "• `/earnings_watch` *[symbol or tickers]* — earnings ±7d\n"
        "• `/news_sentiment SYMBOL` — quick news sentiment\n"
        "• `/signal_history SYMBOL` — last 3 signals (if enabled)\n\n"
        "**How to read**\n"
        "• **Bias**: CALL (green) or PUT (red)\n"
        "• **Buy Range**: zone to enter\n"
        "• **Target/Stop**: exit planning\n"
        "• **Option**: ~7–21 DTE, ATM, mid & spread\n"
        "• **Risk**: from liquidity/volatility\n"
    )
    await interaction.response.send_message(txt, ephemeral=True)

# ---------- /scan_ticker

@tree.command(name="scan_ticker", description="Analyze one ticker on demand")
@app_commands.describe(symbol="Ticker, e.g., NVDA")
async def scan_ticker(interaction: discord.Interaction, symbol: str):
    await interaction.response.defer(thinking=True)
    sym = symbol.upper().strip()
    try:
        ta, opt = await asyncio.to_thread(analyze_symbol, sym)
        color = Colour.green() if ta.bias == "CALL" else Colour.red() if ta.bias == "PUT" else Colour.gray()
        emb = Embed(title=f"{ta.symbol} • ${ta.price:.2f}", colour=color)
        emb.add_field(name="Bias", value=ta.bias, inline=True)
        emb.add_field(name="Buy", value=f"{format_money(ta.buy_low)}–{format_money(ta.buy_high)}", inline=True)
        emb.add_field(name="Target/Stop", value=f"{format_money(ta.target)} / {format_money(ta.stop)}", inline=True)
        emb.add_field(name="Why", value=f"{ta.reasons}", inline=False)

        if opt and opt.contract:
            emb.add_field(
                name="Option",
                value=(
                    f"`{opt.contract}` • Exp **{opt.exp}**\n"
                    f"Strike **{opt.strike}** • Mid **{format_money(opt.mid)}** • Spread **~{format_pct(opt.spread_pct)}**\n"
                    f"Vol **{opt.vol}** • OI **{opt.oi}**"
                ),
                inline=False
            )
        else:
            emb.add_field(name="Option", value=opt.note if opt and opt.note else "No liquid option found", inline=False)

        await interaction.followup.send(embed=emb)
    except Exception as e:
        await interaction.followup.send(f"❌ Could not analyze **{sym}**: {e}")

# ---------- /scan_now

@tree.command(name="scan_now", description="Run ranked scan (top 10) for any comma-separated tickers")
@app_commands.describe(tickers="Optional: e.g., AAPL,MSFT,RMCF", top_n="Number of picks to post (default 10)")
async def scan_now(interaction: discord.Interaction, tickers: Optional[str] = None, top_n: Optional[int] = 10):
    await interaction.response.defer(thinking=True)
    syms = parse_ticker_list(tickers)
    if not syms:
        # default list: S&P 100 (embedded minimal set if file missing)
        try:
            with open("data/sp100.txt","r") as f:
                syms = [x.strip().upper() for x in f if x.strip()]
        except Exception:
            syms = ["AAPL","MSFT","NVDA","AMZN","META","TSLA","GOOGL","GOOG","NFLX","AMD","INTC","BA","WMT","KO","PEP","ORCL","CRM"]

    rows = await asyncio.to_thread(scan_many, syms, int(top_n or 10))
    if not rows:
        await interaction.followup.send("No results.")
        return

    # Build a single embed with the top picks first
    emb = Embed(title="Premarket Ranked Scan", colour=Colour.blurple())
    for r in rows:
        if "error" in r:
            emb.add_field(name=f"{r['symbol']}", value=f"❌ {r['error']}", inline=False)
            continue
        opt = r["opt"]
        value = (
            f"**{r['bias']}** • ${r['price']:.2f} • Risk **{r['risk']}**\n"
            f"Buy **{r['buy_low']:.2f}–{r['buy_high']:.2f}** • Target **{r['target']:.2f}** • Stop **{r['stop']:.2f}**\n"
            f"{r['reasons']}\n"
        )
        if opt and opt.contract:
            value += (
                f"`{opt.contract}` • Exp **{opt.exp}** • Strike **{opt.strike}** • "
                f"Mid **{format_money(opt.mid)}** • Spread **~{format_pct(opt.spread_pct)}** • "
                f"Vol **{opt.vol}** • OI **{opt.oi}**"
            )
        else:
            value += f"_Option:_ {opt.note if opt and opt.note else 'No liquid option found'}"
        emb.add_field(name=r["symbol"], value=value, inline=False)

    await interaction.followup.send(embed=emb)

# ---------- /earnings_watch

@tree.command(name="earnings_watch", description="Earnings within ±7 days (any symbol or list)")
@app_commands.describe(symbol="Optional: single symbol", tickers="Optional: comma-separated list")
async def earnings_watch(interaction: discord.Interaction, symbol: Optional[str] = None, tickers: Optional[str] = None):
    await interaction.response.defer(thinking=True)
    syms = parse_ticker_list(tickers)
    if symbol:
        syms = [symbol.upper()]
    if not syms:
        try:
            with open("data/sp100.txt","r") as f:
                syms = [x.strip().upper() for x in f if x.strip()]
        except Exception:
            syms = ["AAPL","MSFT","NVDA","AMZN","META","TSLA","GOOGL","GOOG","NFLX","AMD","INTC","BA","WMT","KO","PEP","ORCL","CRM"]

    out = []
    now = pd.Timestamp.now(tz=NY).normalize()
    for s in syms:
        d = await asyncio.to_thread(earnings_date, s)
        if d and abs((pd.Timestamp(d, tz=NY).normalize() - now).days) <= 7:
            out.append((s, d))
    if not out:
        scope = f"(checked {symbol.upper()})" if symbol else ""
        await interaction.followup.send(f"No earnings within **±7 days** {scope or 'for current scope'}.")
        return

    out.sort(key=lambda x: abs((x[1] - now.to_pydatetime()).days))
    emb = Embed(title="Earnings Watch (±7d)", colour=Colour.orange())
    for s,d in out:
        delta = (pd.Timestamp(d, tz=NY).normalize() - now).days
        rel = "today" if delta==0 else (f"in {delta}d" if delta>0 else f"{abs(delta)}d ago")
        emb.add_field(name=s, value=f"**{pd.Timestamp(d).strftime('%a, %b %d')}** ({rel})", inline=False)
    await interaction.followup.send(embed=emb)

# ---------- /news_sentiment

@tree.command(name="news_sentiment", description="Headline sentiment snapshot (VADER)")
@app_commands.describe(symbol="Ticker, e.g., RMCF")
async def news_sentiment(interaction: discord.Interaction, symbol: str):
    await interaction.response.defer(thinking=True)
    sym = symbol.upper()
    try:
        t = yf.Ticker(sym)
        news = t.news or []
        if not news:
            await interaction.followup.send(f"No recent headlines for **{sym}**.")
            return
        analyzer = SentimentIntensityAnalyzer()
        rows = []
        for n in news[:12]:
            title = n.get("title","")
            score = analyzer.polarity_scores(title)["compound"]
            rows.append((title, score))
        avg = sum(s for _,s in rows)/len(rows)
        color = Colour.green() if avg>0.05 else Colour.red() if avg<-0.05 else Colour.gray()
        emb = Embed(title=f"{sym} — News Sentiment", colour=color, description=f"**Avg**: {avg:+.3f} (from {len(rows)} headlines)")
        for title,score in rows[:6]:
            emb.add_field(name=f"{score:+.3f}", value=title[:256], inline=False)
        await interaction.followup.send(embed=emb)
    except Exception as e:
        await interaction.followup.send(f"❌ sentiment error for **{sym}**: {e}")

# (optional) /signal_history could be added later to read from SQLite if you enable logging.

bot.run(TOKEN)
