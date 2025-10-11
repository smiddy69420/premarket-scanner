import os
import io
import csv
import re
import requests
from typing import List

NASDAQLISTED_URL = "https://ftp.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHERLISTED_URL = "https://ftp.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
OUT_PATH = os.getenv("SYMBOLS_FILE", "data/symbols_robinhood.txt")

# Heuristics to skip non-common-stock securities
PREF_PAT = re.compile(r"\bPFD\b|PREFERRED", re.I)
NOTE_PAT = re.compile(r"NOTE|BOND|DEBENTURE|TRUST|RIGHTS?", re.I)
WARRANT_PAT = re.compile(r"WARRANT|WTS?\b|\bWT\b", re.I)
UNIT_PAT = re.compile(r" UNIT[S]?", re.I)
ADR_PAT = re.compile(r"ADR|AMERICAN DEPOSITARY", re.I)
FUND_PAT = re.compile(r"FUND|ETF|ETN|TRUST|CLOSED-END", re.I)
NONCOMMON_PAT = re.compile(r"SPAC|ACQUISITION CORP", re.I)

def _fetch(url: str) -> str:
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.text

def _parse_pipe_table(text: str) -> List[dict]:
    # NASDAQ files are pipe-delimited with a footer line starting with "File Creation Time:"
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("File Creation Time")]
    reader = csv.DictReader(lines, delimiter="|")
    return list(reader)

def _clean_symbol(sym: str) -> str:
    s = sym.strip().upper()
    # Strip suffixes used by some feeds (e.g., BRK.B becomes BRK-B elsewhere; keep simple, skip dotted).
    if any(ch in s for ch in (" ", "/", "^", ".")):
        return ""
    return s

def _is_common_stock(row: dict) -> bool:
    name = row.get("Security Name", "") or row.get("SecurityName", "")
    etf = (row.get("ETF") or "").upper() == "Y"
    test_issue = (row.get("Test Issue") or row.get("TestIssue") or "").upper() == "Y"
    next_shares = (row.get("NextShares") or "").upper() == "Y"

    if etf or test_issue or next_shares:
        return False

    # Heuristic filters to avoid preferreds, funds, warrants, units, etc.
    if any(p.search(name) for p in (PREF_PAT, NOTE_PAT, WARRANT_PAT, UNIT_PAT, ADR_PAT, FUND_PAT, NONCOMMON_PAT)):
        return False

    return True

def _dedupe_sorted(symbols: List[str]) -> List[str]:
    seen = set()
    out = []
    for s in symbols:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    out.sort()
    return out

def generate_symbols() -> List[str]:
    nasdaq = _parse_pipe_table(_fetch(NASDAQLISTED_URL))
    other = _parse_pipe_table(_fetch(OTHERLISTED_URL))
    rows = nasdaq + other

    syms = []
    for r in rows:
        s = _clean_symbol(r.get("Symbol") or r.get("ACT Symbol") or "")
        if not s:
            continue
        if not _is_common_stock(r):
            continue
        syms.append(s)

    return _dedupe_sorted(syms)

def main(write_file: bool = True) -> List[str]:
    syms = generate_symbols()
    if write_file:
        os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            f.write("# Auto-generated; source: NASDAQ Trader (NASDAQ/NYSE/AMEX). No OTC/ETF/Warrant/Units.\n")
            for s in syms:
                f.write(s + "\n")
    return syms

if __name__ == "__main__":
    out = main(write_file=True)
    print(f"Generated {len(out)} symbols -> {OUT_PATH}")
