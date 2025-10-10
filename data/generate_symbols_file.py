# generate_symbols_file.py
import os
import json
import requests

# Folder structure to mirror your repo setup
os.makedirs("data", exist_ok=True)
outfile = "data/symbols_robinhood.txt"

print("🔍 Fetching ticker data from Robinhood API...")

url = "https://api.robinhood.com/instruments/"
params = {"active_instruments_only": "true", "tradable": "true"}
symbols = []

while url:
    resp = requests.get(url, params=params)
    if resp.status_code != 200:
        print(f"❌ Failed: {resp.status_code}")
        break
    data = resp.json()
    for instrument in data.get("results", []):
        symbol = instrument.get("symbol")
        if symbol and symbol.isascii():
            symbols.append(symbol.strip().upper())
    url = data.get("next")

symbols = sorted(set(symbols))
with open(outfile, "w", encoding="utf-8") as f:
    for s in symbols:
        f.write(f"{s}\n")

print(f"✅ Wrote {len(symbols)} symbols to {outfile}")
