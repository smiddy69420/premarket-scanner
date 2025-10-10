# bot.py
import os
import asyncio
import datetime as dt

import discord
from discord import app_commands

import scanner  # local module

# ----------------------------
# Env & Client
# ----------------------------
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Missing DISCORD_BOT_TOKEN in environment.")

GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "0") or "0")
CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID", "0") or "0")

intents = discord.Intents.default()
intents.message_content = True  # needed for / commands help & debug text

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# ----------------------------
# Helpers
# ----------------------------
def build_signal_embed(sig: dict) -> discord.Embed:
    title, body = scanner.signal_to_embed_fields(sig)
    color = 0x2ecc71 if sig["type"] == "CALL" else 0xe74c3c
    e = discord.Embed(title=title, description=body, color=color)
    e.set_footer(text="Premarket Scanner • educational only, not financial advice")
    return e


async def run_full_scan_to_embeds() -> list[discord.Embed]:
    universe = scanner.get_universe_from_env()
    results = scanner.scan_universe_and_rank(universe, top_n=10, period="5d", interval="5m")
    embeds = []
    for sig in results:
        embeds.append(build_signal_embed(sig))
    return embeds


# ----------------------------
# Lifecycle
# ----------------------------
@client.event
async def on_ready():
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            await tree.sync(guild=guild)
            print(f"✅ Synced slash commands to guild {GUILD_ID}")
        else:
            await tree.sync()
            print("✅ Synced slash commands globally (can take up to an hour)")
    except Exception as e:
        print(f"Sync error: {e}")


# ----------------------------
# Commands
# ----------------------------
@tree.command(name="ping", description="Health check")
async def ping(interaction: discord.Interaction):
    # No heavy work; we can respond immediately without defer
    await interaction.response.send_message("🏓 Pong", ephemeral=True)


@tree.command(name="help", description="Show command list and how to read signals")
async def help_cmd(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=True)

        e = discord.Embed(
            title="📘 Premarket Scanner — Help",
            description=(
                "**Commands**\n"
                "• `/ping` — health check\n"
                "• `/scan_now` — run the premarket scan now\n"
                "• `/scan_ticker SYMBOL` — analyze one ticker\n"
                "• `/earnings [SYMBOL]` — earnings window (±7 days) for SYMBOL or your universe\n"
                "\n"
                "**Reading Signals**\n"
                "• **Type**: CALL/PUT\n"
                "• **Buy/Target/Stop**: suggested intraday plan\n"
                "• **Risk**: liquidity, spreads, event risk\n"
                "• **Why**: TA summary (trend, MACD, RSI)\n"
                "• **Options Block**: contract, mid, spread%, vol, OI\n"
            ),
            color=0x2ecc71
        )
        e.set_footer(text="Tip: try /scan_ticker NVDA")
        await interaction.followup.send(embed=e, ephemeral=True)
    except Exception as ex:
        try:
            await interaction.followup.send(f"❌ Help failed: {ex}", ephemeral=True)
        except:
            pass


@tree.command(name="scan_now", description="Run a fresh premarket scan now and post it")
async def scan_now(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=False)

        embeds = await run_full_scan_to_embeds()
        if not embeds:
            await interaction.followup.send("⚠️ No qualifying signals right now.")
            return

        # send in chunks of 10
        batch = []
        for e in embeds:
            batch.append(e)
            if len(batch) == 10:
                await interaction.followup.send(embeds=batch)
                batch = []
        if batch:
            await interaction.followup.send(embeds=batch)
    except Exception as ex:
        msg = f"❌ Scan failed: `{ex}`"
        try:
            await interaction.followup.send(msg)
        except:
            try:
                await interaction.channel.send(msg)
            except:
                pass


@tree.command(name="scan_ticker", description="Analyze a single ticker")
@app_commands.describe(symbol="Ticker symbol (e.g., NVDA, TSLA, RMCF)")
async def scan_ticker(interaction: discord.Interaction, symbol: str):
    try:
        await interaction.response.defer(ephemeral=False)

        sig = scanner.analyze_one_ticker(symbol, period="5d", interval="5m")
        e = build_signal_embed(sig)
        await interaction.followup.send(embed=e)
    except Exception as ex:
        try:
            await interaction.followup.send(f"❌ Could not analyze **{symbol.upper()}**: {ex}")
        except:
            pass


@tree.command(name="earnings", description="Show earnings within ±7 days")
@app_commands.describe(symbol="Optional ticker; if omitted use your configured universe")
async def earnings(interaction: discord.Interaction, symbol: str = ""):
    try:
        await interaction.response.defer(ephemeral=False)

        if symbol.strip():
            syms = [symbol.strip().upper()]
        else:
            syms = scanner.get_universe_from_env()

        text = scanner.earnings_watch_text(syms, days_window=7)
        await interaction.followup.send(text)
    except Exception as ex:
        try:
            await interaction.followup.send(f"❌ Earnings watch failed: {ex}")
        except:
            pass


# ----------------------------
# Entry
# ----------------------------
if __name__ == "__main__":
    # Optional: pin New York timezone so your daily workflow timestamps look right
    if "TZ" not in os.environ:
        os.environ["TZ"] = "America/New_York"
    client.run(BOT_TOKEN)
