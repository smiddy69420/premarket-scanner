# bot.py — robust /scan_now with fast defer and fallback
import os, asyncio, discord
from discord import app_commands
from scanner_core import run_scan

TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("GUILD_ID")  # optional: speeds up command syncing in one server

def color_for(bias): return 0x2ecc71 if bias=="CALL" else 0xe74c3c

class ScannerBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)   # instant in that guild
        else:
            await self.tree.sync()              # global sync

client = ScannerBot()

@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user} (latency {client.latency*1000:.0f} ms)")

@client.tree.command(name="ping", description="Check if the bot is alive.")
async def ping(interaction: discord.Interaction):
    try:
        await interaction.response.send_message("🏓 pong", ephemeral=True)
    except Exception as e:
        # fallback if interaction expired
        await interaction.channel.send("🏓 pong (fallback)")

@client.tree.command(name="scan_now", description="Run the options scanner and post top picks.")
async def scan_now(interaction: discord.Interaction):
    deferred = False
    try:
        # Defer immediately to keep the interaction alive
        await interaction.response.defer(thinking=True, ephemeral=False)
        deferred = True
    except discord.NotFound:
        # Interaction expired (cold start or slow). We'll fallback to channel send.
        deferred = False
    except Exception:
        deferred = False

    # Run the scan off the event loop (blocking I/O + CPU work)
    df, meta = await asyncio.to_thread(run_scan, 10)

    # Build embeds
    embeds=[]
    header = discord.Embed(
        title="📣 On-Demand Scan",
        description=f"{meta}\nTop {len(df)} picks • CALL=green • PUT=red" if not df.empty else meta,
        color=0x7289DA
    )
    embeds.append(header)

    if not df.empty:
        for _, r in df.iterrows():
            e = discord.Embed(
                title=f"{r['Ticker']}  •  ${r['Price']}",
                description=(f"**Bias:** {r['Type']}  •  **Exp:** `{r['Target Expiration']}`\n"
                             f"**Buy:** {r['Buy Range']}  •  **Target:** {r['Sell Target']}  •  **Stop:** {r['Stop Idea']}\n"
                             f"**Risk:** {r['Risk']}\n"
                             f"**Why:** {r['Why']}"),
                color=color_for(r["Type"])
            )
            if r["Option Contract"]:
                e.add_field(
                    name="Option",
                    value=(f"`{r['Option Contract']}`\n"
                           f"Strike **{r['Strike']}** • Mid **${r['Opt Mid']}** • Spread **~{r['Spread %']}%**\n"
                           f"Vol **{r['Opt Vol']}** • OI **{r['Opt OI']}**"),
                    inline=False
                )
            elif r["Opt Note"]:
                e.add_field(name="Option", value=r["Opt Note"], inline=False)
            embeds.append(e)

    # Send result — followup if we deferred, else fallback to channel
    try:
        for i in range(0, len(embeds), 10):  # Discord max 10 embeds/msg
            if deferred:
                await interaction.followup.send(embeds=embeds[i:i+10])
            else:
                await interaction.channel.send(embeds=embeds[i:i+10])
    except discord.NotFound:
        # As a last resort, try a plain message
        msg = "**📣 Scan result:** No candidates passed filters." if df.empty else "📣 Scan complete."
        await interaction.channel.send(msg)

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN env var to your Bot Token.")
    client.run(TOKEN)
