# src/generate_symbols_file.py
import argparse
import time
from typing import Dict, Iterable, List, Optional, Set
import requests
from pathlib import Path

ROBINHOOD_INSTRUMENTS = "https://api.robinhood.com/instruments/"

ALLOWED_US_MICS: Set[str] = {
    "XNAS",  # NASDAQ
    "XNGS",  # NASDAQ Global Select
    "XNYS",  # NYSE
    "ARCX",  # NYSE Arca
    "XASE",  # NYSE American
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
        time.sleep(0.03)

def get_market_mic(session: requests.Session, market_field) -> Optional[str]:
    if isinstance(market_field, dict):
        return market_field.get("mic")
    if isinstance(market_field, str) and market_field.startswith("http"):
        try:
            r = session.get(market_field, timeout=20)
            r.raise_for_status()
            return r.json().get("mic")
        except Exception:
            return None
    return None

def instrument_is_us_common(inst: Dict, mic: Optional[str], allowed_mics: Set[str]) -> bool:
    if inst.get("state") != "active":
        return False
    if not inst.get("tradeable", False):
        return False
    if inst.get("type") != "stock":
        return False
    if allowed_mics and (mic is None or mic not in allowed_mics):
        return False
    sym = (inst.get("symbol") or "").strip().upper()
    if not sym or not sym.isascii():
        return False
    if any(x in sym for x in ["^", " ", "/"]):
        return False
    if sym.endswith(("~", ".WI")):
        return False
    return True

def write_symbols(symbols: List[str], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    uniq = sorted(set(symbols))
    out_path.write_text("\n".join(uniq) + "\n", encoding="utf-8")
    print(f"✅ Wrote {len(uniq)} symbols -> {out_path}")

def main(argv: Optional[List[str]] = None) -> int:
    base_dir = Path(__file__).resolve().parent  # .../src
    default_out = base_dir / ".." / "data" / "symbols_robinhood.txt"
    default_out = default_out.resolve()

    p = argparse.ArgumentParser(description="Generate U.S. common stock symbols from Robinhood.")
    p.add_argument("--out", default=str(default_out),
                   help=f"Output file (default: {default_out})")
    p.add_argument("--include-otc", action="store_true",
                   help="If set, include OTC/grey (skip MIC filtering).")
    args = p.parse_args(argv)

    allowed_mics = set() if args.include_otc else ALLOWED_US_MICS

    sess = requests.Session()
    syms: List[str] = []
    total = 0
    mic_cache: dict[str, Optional[str]] = {}

    print("🔎 Fetching instruments from Robinhood…")
    for inst in fetch_instruments(sess):
        total += 1
        mfield = inst.get("market")
        mic = None
        key = None
        if isinstance(mfield, str):
            key = mfield
        if key and key in mic_cache:
            mic = mic_cache[key]
        else:
            mic = get_market_mic(sess, mfield)
            if key:
                mic_cache[key] = mic

        if instrument_is_us_common(inst, mic, allowed_mics):
            syms.append(inst["symbol"].strip().upper())

    print(f"Found {len(syms)} candidate symbols out of {total} instruments.")
    write_symbols(syms, Path(args.out))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
