import os, requests
from scanner_core import run_scan

WEBHOOK = os.environ.get("DISCORD_WEBHOOK","")

def color_for(bias):
    return 0x2ecc71 if bias=="CALL" else 0xe74c3c

def chunk_embeds(embeds, size=10):
    for i in range(0, len(embeds), size):
        yield embeds[i:i+size]

def build_embeds(df, title="Premarket Ranked Scan"):
    header = {
        "title": f"📣 {title}",
        "description": f"Top {len(df)} picks • CALL=green • PUT=red",
        "color": 0x7289DA
    }
    embeds=[header]
    for _, r in df.iterrows():
        desc = (
            f"**Bias:** {r['Type']}  •  **Exp:** `{r['Target Expiration']}`\n"
            f"**Buy:** {r['Buy Range']}  •  **Target:** {r['Sell Target']}  •  **Stop:** {r['Stop Idea']}\n"
            f"**Risk:** {r['Risk']}\n"
            f"**Why:** {r['Why']}"
        )
        opt_line = (f"`{r['Option Contract']}` — strike **{r['Strike']}**, mid **${r['Opt Mid']}**, "
                    f"spread **~{r['Spread %']}%**, vol **{r['Opt Vol']}**, OI **{r['Opt OI']}**") if r["Option Contract"] else r["Opt Note"]
        embeds.append({
            "title": f"{r['Ticker']}  •  ${r['Price']}",
            "description": desc + ("\n" + opt_line if opt_line else ""),
            "color": color_for(r["Type"])
        })
    return embeds

def main():
    if not WEBHOOK:
        print("Missing DISCORD_WEBHOOK"); return
    df, meta = run_scan(top_k=10)
    if df.empty:
        requests.post(WEBHOOK, json={"content":"**📣 Premarket Scan**\n_No candidates today._"}); 
        return
    embeds = build_embeds(df)
    for batch in chunk_embeds(embeds, size=10):
        requests.post(WEBHOOK, json={"embeds": batch})

if __name__ == "__main__":
    main()

