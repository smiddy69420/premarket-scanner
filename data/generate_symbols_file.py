# generate_symbols_file.py
import argparse
import os
import sys
import time
from typing import Dict, Iterable, List, Optional, Set
import requests

ROBINHOOD_INSTRUMENTS = "https://api.robinhood.com/instruments/"

# U.S. lit exchanges (exclude OTC)
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
    params = {
        "active_instruments_only": "true",
        "tradable": "true",
        # server paginates; leave page size default for safety
    }
    while url:
        r = session.get(url, params=params if url == ROBINHOOD_INSTRUMENTS else None, timeout=30)
        r.raise_for_status()
        data = r.json()
        results = data.get("results", [])
        for inst in results:
            yield inst
        url = data.get("next")
        # be polite
        time.sleep(0.05)

def instrument_is_us_common(inst: Dict, allowed_mics: Set[str]) -> bool:
    # Must be active & tradable
    if inst.get("state") != "active":
        return False
    if not inst.get("tradeable", False):
        return False

    # Must be a common stock (RH 'type' field)
    # Known non-common values: 'etp', 'warrant', 'unit', 'adr', 'preferred', 'right'
    if inst.get("type") != "stock":
        return False

    # Market MIC must be one of the allowed U.S. lit exchanges
    market = inst.get("market")
    mic = None
    if isinstance(market, dict):
        mic = market.get("mic")
    # Some API pages return a URL string for 'market'; we can’t deref here, so keep it only if unknown.
    if not mic and isinstance(market, str):
        # if we only get a URL, we accept it (RH is inconsistent). Most U.S. commons still pass type=='stock'
        pass
    elif mic and mic not in allowed_mics:
        return False

    # Basic symbol checks
    sym = inst.get("symbol", "")
    if not sym or not sym.isascii():
        return False
    s = sym.strip().upper()

    # Filter out obvious non-common patterns:
    # (We keep class shares like BRK.B, BF.B.)
    # Skip temporary/test-like tickers
    if any(x in s for x in ["^", " ", "/"]):
        return False
    # Avoid symbols that look like when-issued etc. (rare on RH)
    if s.endswith(("~", ".WI")):
        return False

    return True

def write_symbols(symbols: List[str], out_path: str) -> None:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    symbols = sorted(set(symbols))
    with open(out_path, "w", encoding="utf-8") as f:
        for s in symbols:
            f.write(f"{s}\n")
    print(f"✅ Wrote {len(symbols)} symbols -> {out_path}")

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Generate U.S. common stock symbols from Robinhood.")
    p.add_argument(
        "--out",
        default="data/symbols_robinhood.txt",
        help="Output file path (default: data/symbols_robinhood.txt)",
    )
    p.add_argument(
        "--allow-mics",
        default=",".join(ALLOWED_US_MICS),
        help="Comma-separated list of allowed MICs (default: major U.S. lit exchanges).",
    )
    p.add_argument(
        "--include-otc",
        action="store_true",
        help="If set, do NOT filter by MICs (OTC may leak in).",
    )
    args = p.parse_args(argv)

    allowed_mics = set(args.allow_mics.split(",")) if not args.include_otc else set()

    sess = requests.Session()
    symbols: List[str] = []
    count = 0

    print("🔎 Fetching instruments from Robinhood…")
    for inst in fetch_instruments(sess):
        count += 1
        if instrument_is_us_common(inst, allowed_mics):
            symbols.append(inst["symbol"].strip().upper())

    print(f"Found {len(symbols)} candidate symbols out of {count} instruments.")
    write_symbols(symbols, args.out)
    return 0

if __name__ == "__main__":
    sys.exit(main())
