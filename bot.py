# bot.py — Discord slash command /scan_now using scanner_core
import os, discord
from discord import app_commands
from scanner_core import run_scan

TOKEN = os.environ.get("DISCORD_TOKEN")  # set this when running locally

def color_for(bias):
    return 0x2ecc71 if bias=="CALL" else 0xe74c3c

class ScannerBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()  # register global commands

client = ScannerBot()

@client.tree.command(name="scan_now", description="Run the options scanner and post the top picks.")
async def scan_now(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=False)
    df, meta = run_scan(top_k=10)
    if df.empty:
        await interaction.followup.send("**📣 Scan result:** No candidates passed filters.")
        return

    embeds=[]
    header = discord.Embed(title="📣 On-Demand Scan",
                           description=f"{meta}\nTop {len(df)} picks • CALL=green • PUT=red",
                           color=0x7289DA)
    embeds.append(header)

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
            e.add_field(name="Option", value=(f"`{r['Option Contract']}`\n"
                      f"Strike **{r['Strike']}** • Mid **${r['Opt Mid']}** • Spread **~{r['Spread %']}%**\n"
                      f"Vol **{r['Opt Vol']}** • OI **{r['Opt OI']}**"), inline=False)
        elif r["Opt Note"]:
            e.add_field(name="Option", value=r["Opt Note"], inline=False)
        embeds.append(e)

    for i in range(0, len(embeds), 10):   # Discord max 10 embeds/msg
        await interaction.followup.send(embeds=embeds[i:i+10])

if __name__ == "__main__":
    if not TOKEN:
        print("Set DISCORD_TOKEN env var to your Bot Token.")
    else:
        client.run(TOKEN)
