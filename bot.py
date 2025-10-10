import os, asyncio, math, traceback
import datetime as dt

import discord
from discord import app_commands

import yfinance as yf
import pandas as pd
import numpy as np

from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# Local modules
from scanner_core import run_scan            # your existing multi-ticker scan (already in repo)
import history                                # our tiny SQLite helper

TOKEN    = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("GUILD_ID")  # optional for instant slash sync (your server's 18-digit ID)

# Universe for /earnings_watch (±7 days)
DEFAULT_UNIVERSE = [
    "SPY","QQQ","AAPL","MSFT","NVDA","TSLA","AMZN","META","GOOGL","NFLX","AMD",
    "BA","COST","WMT","ORCL","KO","PEP","CRM","SHOP","MS","JPM","PYPL","DIS","INTC","UNH","T"
]

# ---------- helpers ----------
def color_for(bias: str) -> int:
    return 0x2ecc71 if str(bias).upper() == "CALL" else 0xe74c3c

def fmt_usd(x) -> str:
    try:
        return f"${float(x):,.2f}"
    except:
        return str(x)

def nearest_strike(series, spot):
    diffs = (series.astype(float) - float(spot)).abs()
    return int(diffs.sort_values().index[0])

async def to_thread(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)

# ---------- options picker ----------
def pick_option(symbol: str, spot: float, bias: str):
    """
    Choose an expiry ~7–21 DTE if available, ATM strike, compute mid & spread.
    """
    try:
        t = yf.Ticker(symbol)
        expiries = t.options or []
        if not expiries:
            return {"note": "No options listed."}

        today = dt.datetime.utcnow().date()

        def dte(e):
            ed = dt.datetime.strptime(e, "%Y-%m-%d").date()
            return (ed - today).days

        # Prefer around 10 DTE; if none, pick nearest
        sorted_exps = sorted(expiries, key=lambda e: abs(max(dte(e), 0) - 10))
        expiry = sorted_exps[0]

        chain = t.option_chain(expiry)
        tbl = chain.calls if bias.upper() == "CALL" else chain.puts
        if tbl.empty:
            return {"note": f"No {bias.lower()} chain for {expiry}."}

        idx = nearest_strike(tbl["strike"], spot)
        row = tbl.loc[idx]

        bid = float(row.get("bid", np.nan) or 0)
        ask = float(row.get("ask", np.nan) or 0)
        last = float(row.get("lastPrice", np.nan) or 0)
        mid = (bid + ask) / 2 if (bid and ask) else (last or None)
        spread_pct = round(((ask - bid) / mid * 100), 1) if (mid and bid and ask and mid > 0) else None

        return {
            "expiry": expiry,
            "contract": row.get("contractSymbol", ""),
            "strike": float(row.get("strike", np.nan) or 0),
            "mid": round(mid, 2) if mid else None,
            "spread_pct": spread_pct,
            "vol": int(row.get("volume", 0) or 0),
            "oi": int(row.get("openInterest", 0) or 0)
        }
    except Exception as e:
        return {"note": f"Option lookup error: {e}"}

# ---------- news & sentiment ----------
_SID = SentimentIntensityAnalyzer()

def news_sentiment(symbol: str, limit=5):
    """
    Pull recent Yahoo Finance news and score titles with VADER.
    """
    t = yf.Ticker(symbol)
    try:
        items = t.get_news() or []
    except Exception:
        items = []
    items = items[:limit]
    scored = []
    for n in items:
        title = n.get("title") or ""
        ts = n.get("providerPublishTime")
        when = dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "n/a"
        score = _SID.polarity_scores(title)["compound"]
        scored.append((title, when, score))
    if not scored:
        return {"avg": None, "items": []}
    avg = round(sum(s for _,_,s in scored)/len(scored), 3)
    return {"avg": avg, "items": scored}

# ---------- earnings watch (±7 days) ----------
def earnings_within_7d(symbol: str):
    """
    Returns list of (date, days_from_now) within +/- 7 days, else [].
    """
    try:
        t = yf.Ticker(symbol)
        df = t.get_earnings_dates(limit=6)
        if df is None or df.empty:
            return []
        dates = list(df.index.to_pydatetime())
        now = dt.datetime.utcnow()
        hits = []
        for d in dates:
            diff = (d - now).days
            if abs(diff) <= 7:
                hits.append((d, diff))
        return hits
    except Exception:
        return []

# ---------- single-ticker analysis (fixed: always 1-D) ----------
def analyze_symbol(symbol: str):
    """
    Returns dict with price, bias, buy_range, target, stop, indicators, and an option suggestion.
    Uses ~5d/5m data. Robust to MultiIndex/2D columns from yfinance.
    """
    try:
        d = yf.download(symbol, period="5d", interval="5m", auto_adjust=True, progress=False)
        if d is None or len(d) == 0:
            return {"ok": False, "error": "No data."}

        # --- normalize columns to 1-D Series ---
        def to_1d_series(obj, name: str):
            """
            Ensures a pandas Series (1-D). Handles DataFrame (n,1), MultiIndex, or numpy arrays.
            """
            if obj is None:
                raise ValueError(f"{name} missing")
            if isinstance(obj, pd.DataFrame):
                # if single column DF, take the first column
                if obj.shape[1] >= 1:
                    ser = obj.iloc[:, 0]
                else:
                    raise ValueError(f"{name} DataFrame has no columns")
            elif isinstance(obj, pd.Series):
                ser = obj
            else:
                # numpy array or other: flatten into Series, try to use d.index for alignment
                arr = np.asarray(obj).reshape(-1)
                ser = pd.Series(arr, index=d.index[:len(arr)], name=name)
            return pd.to_numeric(ser, errors="coerce")

        # Support both normal and MultiIndex (('NVDA','Close')) shapes
        if isinstance(d.columns, pd.MultiIndex):
            close_raw  = d.xs("Close",  axis=1, level=-1, drop_level=False)
            volume_raw = d.xs("Volume", axis=1, level=-1, drop_level=False)
        else:
            close_raw  = d.get("Close")
            volume_raw = d.get("Volume")

        close  = to_1d_series(close_raw,  "Close")
        volume = to_1d_series(volume_raw, "Volume")

        # Drop NaNs
        mask = close.notna()
        close = close[mask]
        volume = volume[mask]
        if len(close) < 60:
            return {"ok": False, "error": "Not enough recent data for indicators."}

        last_close = float(close.iloc[-1])
        avg_vol20  = float(volume.tail(20).mean()) if volume.notna().any() else np.nan

        ema20 = EMAIndicator(close, window=20).ema_indicator()
        ema50 = EMAIndicator(close, window=50).ema_indicator()
        rsi   = RSIIndicator(close, window=14).rsi()
        macd_obj = MACD(close)
        macd      = macd_obj.macd()
        macd_sig  = macd_obj.macd_signal()
        macd_diff = macd - macd_sig

        uptrend   = (last_close > float(ema20.iloc[-1]) > float(ema50.iloc[-1])) and float(macd_diff.iloc[-1]) > 0
        downtrend = (last_close < float(ema20.iloc[-1]) < float(ema50.iloc[-1])) and float(macd_diff.iloc[-1]) < 0

        if uptrend:
            bias = "CALL"
        elif downtrend:
            bias = "PUT"
        else:
            bias = "CALL" if (float(macd_diff.iloc[-1]) > 0 and float(rsi.iloc[-1]) >= 50) else "PUT"

        # Simple trade plan (~0.20% bands)
        pct = 0.002
        if bias == "CALL":
            buy_low, buy_high = last_close * (1 - pct), last_close * (1 - pct/2)
            target = last_close * (1 + 0.003)
            stop   = last_close * (1 - 0.003)
        else:
            buy_low, buy_high = last_close * (1 + pct/2), last_close * (1 + pct)
            target = last_close * (1 - 0.003)
            stop   = last_close * (1 + 0.003)

        reasons = []
        reasons.append(f"Trend: {'Up' if uptrend else ('Down' if downtrend else 'Mixed')} (Close {fmt_usd(last_close)} vs EMA20/EMA50)")
        reasons.append(f"MACD diff: {float(macd_diff.iloc[-1]):.3f} | RSI: {float(rsi.iloc[-1]):.1f}")
        if not np.isnan(avg_vol20) and float(volume.iloc[-1]) > avg_vol20:
            reasons.append("Moderate volume")

        opt = pick_option(symbol, last_close, bias)

        return {
            "ok": True,
            "symbol": symbol.upper(),
            "price": float(last_close),
            "bias": bias,
            "buy_range": f"{fmt_usd(buy_low)}–{fmt_usd(buy_high)}",
            "target": fmt_usd(target),
            "stop": fmt_usd(stop),
            "why": "; ".join(reasons),
            "option": opt
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ---------- Discord client ----------
class ScannerBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # create DB table
        history.init_db()
        # fast guild sync if provided
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

client = ScannerBot()

@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user} (latency {client.latency*1000:.0f} ms)")

# ---- health ----
@client.tree.command(name="ping", description="Check if the bot is alive.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("🏓 pong", ephemeral=True)

# ---- HELP ----
@client.tree.command(name="help", description="How to use the scanner & read signals.")
async def help_cmd(interaction: discord.Interaction):
    e = discord.Embed(
        title="🤖 Scanner Help",
        description=(
            "**Commands**\n"
            "• `/scan_now` – run full ranked scan (top 10)\n"
            "• `/scan_ticker SYMBOL` – analyze one ticker (e.g., NVDA)\n"
            "• `/earnings_watch` – earnings within ±7 days (watch IV)\n"
            "• `/news_sentiment SYMBOL` – headline sentiment snapshot\n"
            "• `/signal_history SYMBOL` – last 3 signals we posted\n\n"
            "**How to read a signal**\n"
            "• **Bias**: CALL (green) or PUT (red) from trend+momentum\n"
            "• **Buy Range**: zone to enter\n"
            "• **Target/Stop**: exit planning\n"
            "• **Option**: ~7–21 DTE, ATM, mid & spread %\n"
            "• **Risk**: from scanner heuristics (liquidity/volatility)\n"
        ),
        color=0x5865F2
    )
    await interaction.response.send_message(embed=e, ephemeral=True)

# ---- SCAN NOW (uses your scanner_core.run_scan) ----
@client.tree.command(name="scan_now", description="Run the options scanner and post top picks.")
async def scan_now(interaction: discord.Interaction):
    deferred = False
    try:
        await interaction.response.defer(thinking=True, ephemeral=False)
        deferred = True
    except Exception:
        pass

    # Run your core scan in a worker thread
    df, meta = await to_thread(run_scan, 10)

    embeds = []
    header = discord.Embed(
        title="📣 On-Demand Scan",
        description=f"{meta}\nTop {len(df)} picks • CALL=green • PUT=red" if not df.empty else meta,
        color=0x7289DA
    )
    embeds.append(header)

    # Post picks + log history
    if not df.empty:
        for _, r in df.iterrows():
            e = discord.Embed(
                title=f"{r['Ticker']}  •  {fmt_usd(r['Price'])}",
                description=(f"**Bias:** {r['Type']}  •  **Exp:** `{r['Target Expiration']}`\n"
                             f"**Buy:** {r['Buy Range']}  •  **Target:** {r['Sell Target']}  •  **Stop:** {r['Stop Idea']}\n"
                             f"**Risk:** {r['Risk']}\n"
                             f"**Why:** {r['Why']}"),
                color=color_for(r["Type"])
            )
            if r.get("Option Contract"):
                e.add_field(
                    name="Option",
                    value=(f"`{r['Option Contract']}`\n"
                           f"Strike **{r['Strike']}** • Mid **{fmt_usd(r['Opt Mid'])}** • Spread **~{r['Spread %']}%**\n"
                           f"Vol **{r['Opt Vol']}** • OI **{r['Opt OI']}**"),
                    inline=False
                )
            elif r.get("Opt Note"):
                e.add_field(name="Option", value=str(r["Opt Note"]), inline=False)
            embeds.append(e)

            # log to history
            try:
                history.log_signal(
                    source="scan_now",
                    ticker=str(r["Ticker"]),
                    bias=str(r["Type"]),
                    price=float(r["Price"]),
                    exp=str(r["Target Expiration"]),
                    target=str(r["Sell Target"]),
                    stop=str(r["Stop Idea"]),
                    why=str(r["Why"])
                )
            except Exception:
                traceback.print_exc()

    # send
    try:
        for i in range(0, len(embeds), 10):
            if deferred:
                await interaction.followup.send(embeds=embeds[i:i+10])
            else:
                await interaction.channel.send(embeds=embeds[i:i+10])
    except Exception:
        await interaction.channel.send("Scan posted.")

# ---- SCAN TICKER ----
@client.tree.command(name="scan_ticker", description="Analyze a single ticker (e.g., NVDA)")
@app_commands.describe(symbol="Ticker symbol, e.g., NVDA")
async def scan_ticker(interaction: discord.Interaction, symbol: str):
    try:
        await interaction.response.defer(thinking=True)
    except:
        pass

    res = await to_thread(analyze_symbol, symbol.upper())
    if not res.get("ok"):
        await interaction.followup.send(f"❌ Could not analyze **{symbol}**: {res.get('error','unknown error')}")
        return

    e = discord.Embed(
        title=f"{res['symbol']}  •  {fmt_usd(res['price'])}",
        description=(f"**Bias:** {res['bias']}  •  **Buy:** {res['buy_range']}  •  "
                     f"**Target:** {res['target']}  •  **Stop:** {res['stop']}\n"
                     f"**Why:** {res['why']}"),
        color=color_for(res['bias'])
    )
    opt = res.get("option", {}) or {}
    if opt.get("contract"):
        e.add_field(
            name="Option",
            value=(f"`{opt['contract']}`  |  Exp `{opt['expiry']}`\n"
                   f"Strike **{opt['strike']}** • Mid **{fmt_usd(opt['mid'])}** • Spread **~{opt.get('spread_pct','?')}%**\n"
                   f"Vol **{opt.get('vol',0)}** • OI **{opt.get('oi',0)}**"),
            inline=False
        )
    elif opt.get("note"):
        e.add_field(name="Option", value=opt["note"], inline=False)

    await interaction.followup.send(embed=e)

    # log to history
    try:
        history.log_signal(
            source="scan_ticker",
            ticker=res["symbol"],
            bias=res["bias"],
            price=res["price"],
            exp=str(opt.get("expiry","")),
            target=res["target"],
            stop=res["stop"],
            why=res["why"]
        )
    except Exception:
        traceback.print_exc()

# ---- EARNINGS WATCH (±7 days) ----
@client.tree.command(name="earnings_watch", description="Show universe with earnings within ±7 days")
async def earnings_watch(interaction: discord.Interaction):
    try:
        await interaction.response.defer(thinking=True)
    except:
        pass

    rows = []
    for tkr in DEFAULT_UNIVERSE:
        hits = await to_thread(earnings_within_7d, tkr)
        for d, diff in hits:
            when = d.strftime("%Y-%m-%d")
            rows.append((tkr, when, diff))
    if not rows:
        await interaction.followup.send("No earnings within ±7 days for the current universe.")
        return

    rows.sort(key=lambda x: abs(x[2]))  # closest first
    desc = []
    for tkr, when, diff in rows[:20]:
        tag = "🟢 in" if diff >= 0 else "🔴 was"
        desc.append(f"• **{tkr}** — {when} ({tag} {abs(diff)}d)")
    e = discord.Embed(
        title="🗓️ Earnings Watch (±7 days)",
        description="\n".join(desc),
        color=0xFFA500
    )
    await interaction.followup.send(embed=e)

# ---- NEWS SENTIMENT ----
@client.tree.command(name="news_sentiment", description="Headline sentiment snapshot (last ~5)")
@app_commands.describe(symbol="Ticker symbol, e.g., TSLA")
async def news_sentiment_cmd(interaction: discord.Interaction, symbol: str):
    try:
        await interaction.response.defer(thinking=True)
    except:
        pass

    out = await to_thread(news_sentiment, symbol.upper())
    items = out.get("items", [])
    avg = out.get("avg")
    head = f"Avg sentiment: {avg:+.3f}" if avg is not None else "No headlines found."
    e = discord.Embed(
        title=f"📰 {symbol.upper()} — News Sentiment",
        description=head,
        color=0x00B2FF
    )
    for title, when, score in items:
        e.add_field(name=f"{when}  |  {score:+.2f}", value=title[:256], inline=False)

    await interaction.followup.send(embed=e)

# ---- SIGNAL HISTORY ----
@client.tree.command(name="signal_history", description="Show last 3 signals we posted for this ticker")
@app_commands.describe(symbol="Ticker symbol, e.g., AAPL")
async def signal_history_cmd(interaction: discord.Interaction, symbol: str):
    rows = history.recent_for_ticker(symbol.upper(), limit=3)
    if not rows:
        await interaction.response.send_message(f"No history found for **{symbol.upper()}** yet.", ephemeral=True)
        return
    lines = []
    for ts, source, tkr, bias, price, exp, target, stop, why in rows:
        when = dt.datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M UTC")
        lines.append(
            f"• **{when}** [{source}] — **{tkr}** {bias} @ {fmt_usd(price)} | Exp `{exp}` | Target {target} | Stop {stop}\n"
            f"  ↳ {why[:180]}{'…' if len(why)>180 else ''}"
        )
    e = discord.Embed(
        title=f"🗂️ {symbol.upper()} — Last {len(rows)} signals",
        description="\n\n".join(lines),
        color=0x95A5A6
    )
    await interaction.response.send_message(embed=e, ephemeral=True)

# ---- run ----
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN env var to your Bot Token.")
    # Optional: print to confirm guild targeting
    if GUILD_ID:
        print(f"🔧 GUILD_ID set: {GUILD_ID} (commands sync instantly to that server)")
    history.init_db()
    client.run(TOKEN)
