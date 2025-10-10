# generate_symbols_file.py
import argparse
import os
import sys
import time
from typing import Dict, Iterable, List, Optional, Set
import requests
from pathlib import Path

ROBINHOOD_INSTRUMENTS = "https://api.robinhood.com/instruments/"

# U.S. lit exchanges (exclude OTC/grey)
ALLOWED_US_MICS: Set[str] = {
    "XNAS",  # NASDAQ
    "XNGS",  # NASDAQ Global Select
    "XNYS",  # NYSE
    "ARCX",  # NYSE Arca
    "XASE",  # NYSE American (AMEX)
    "BATS",  # Cboe BZX
}

def fetch_instruments(session: requests.Session) -> Iterable[Dict]:
    url = ROBINHOOD_INSTRUMENTS
    params = {"active_instruments_only": "true", "tradable": "true"}
    while url:
        r = session.get(url, params=params if url == ROBINHOOD_INSTRUMENTS else None, timeout=30)
        r.raise_for_status()
        data = r.json()
        for inst in data.get("results", []):
            yield inst
        url = data.get("next")
        time.sleep(0.05)  # polite paging

def instrument_is_us_common(inst: Dict, allowed_mics: Set[str]) -> bool:
    if inst.get("state") != "active":
        return False
    if not inst.get("tradeable", False):
        return False
    if inst.get("type") != "stock":  # exclude etp/adr/warrant/unit/etc.
        return False

    market = inst.get("market")
    mic = None
    if isinstance(market, dict):
        mic = market.get("mic")
    if mic and allowed_mics and mic not in allowed_mics:
        return False

    sym = (inst.get("symbol") or "").strip().upper()
    if not sym or not sym.isascii():
        return False
    if any(x in sym for x in ["^", " ", "/"]):
        return False
    if sym.endswith(("~", ".WI")):
        return False
    return True

def write_symbols(symbols: List[str], out_path: str) -> None:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    uniq = sorted(set(symbols))
    path.write_text("\n".join(uniq) + "\n", encoding="utf-8")
    print(f"✅ Wrote {len(uniq)} symbols -> {path}")

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Generate U.S. common stock symbols from Robinhood.")
    p.add_argument("--out", default="data/symbols_robinhood.txt",
                   help="Output file (default: data/symbols_robinhood.txt)")
    p.add_argument("--include-otc", action="store_true",
                   help="If set, skip MIC filtering (OTC may appear).")
    args = p.parse_args(argv)

    allowed_mics = set() if args.include_otc else ALLOWED_US_MICS

    sess = requests.Session()
    syms: List[str] = []
    total = 0
    print("🔎 Fetching instruments from Robinhood…")
    for inst in fetch_instruments(sess):
        total += 1
        if instrument_is_us_common(inst, allowed_mics):
            syms.append(inst["symbol"].strip().upper())
    print(f"Found {len(syms)} candidate symbols out of {total} instruments.")
    write_symbols(syms, args.out)
    return 0

if __name__ == "__main__":
    sys.exit(main())
