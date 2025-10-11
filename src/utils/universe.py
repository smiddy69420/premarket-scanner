import os
import asyncio
import logging
from typing import List, Iterable, Optional

logger = logging.getLogger(__name__)

SYMBOLS_FILE_ENV = "SYMBOLS_FILE"
ALL_TICKERS_ENV = "ALL_TICKERS"
SCAN_UNIVERSE_ENV = "SCAN_UNIVERSE"  # optional small default

DEFAULT_UNIVERSE_FALLBACK = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "AMD", "JPM"]

def _parse_csv_symbols(text: str) -> List[str]:
    # Accept comma or whitespace separated, normalize and dedupe
    raw = [s.strip().upper() for s in text.replace("\n", ",").split(",") if s.strip()]
    out = []
    seen = set()
    for s in raw:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out

class UniverseManager:
    """
    Priority order:
      1) SYMBOLS_FILE (data/symbols_robinhood.txt) if present (recommended)
      2) ALL_TICKERS env (comma separated)
      3) SCAN_UNIVERSE env (comma separated)
      4) DEFAULT_UNIVERSE_FALLBACK
    """
    def __init__(self):
        self.symbols: List[str] = []
        self.symbols_file = os.getenv(SYMBOLS_FILE_ENV, "data/symbols_robinhood.txt")

    def _load_from_file(self) -> Optional[List[str]]:
        path = self.symbols_file
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                rows = [ln.strip().upper() for ln in f if ln.strip() and not ln.startswith("#")]
            # basic symbol hygiene
            rows = [s for s in rows if s.isascii() and all(c not in s for c in (" ", "/", "^", "."))]
            rows = sorted(set(rows))
            return rows
        except Exception:
            logger.exception("Failed reading symbols file %s", path)
            return None

    def _load_from_env(self) -> Optional[List[str]]:
        all_tickers = os.getenv(ALL_TICKERS_ENV, "").strip()
        if all_tickers:
            return sorted(set(_parse_csv_symbols(all_tickers)))
        scan_universe = os.getenv(SCAN_UNIVERSE_ENV, "").strip()
        if scan_universe:
            return sorted(set(_parse_csv_symbols(scan_universe)))
        return None

    async def initialize(self) -> None:
        # 1) Try file; if missing, generate it.
        rows = self._load_from_file()
        if rows is None:
            try:
                # create file once
                from src.generate_symbols_file import main as gen_main  # local import to avoid import cycles
            except Exception:
                try:
                    from ..generate_symbols_file import main as gen_main  # alt path if run as module
                except Exception:
                    logger.exception("Could not import generator; falling back to env universe.")
                    rows = None
                else:
                    rows = gen_main(write_file=True)
            else:
                rows = gen_main(write_file=True)

        # 2) If still no file-based rows, fall back to env.
        if not rows:
            env_rows = self._load_from_env()
            rows = env_rows if env_rows else DEFAULT_UNIVERSE_FALLBACK

        self.symbols = rows
        logger.info("Universe loaded: %s tickers", len(self.symbols))

    def get(self, limit: Optional[int] = None) -> List[str]:
        if not self.symbols:
            return DEFAULT_UNIVERSE_FALLBACK[: limit or None]
        return self.symbols[: limit or None]

    async def refresh_weekly_forever(self) -> None:
        """
        Background task: refresh symbols once a week (NASDAQ lists update daily;
        weekly is a good cadence without being noisy).
        """
        while True:
            try:
                from src.generate_symbols_file import main as gen_main
            except Exception:
                try:
                    from ..generate_symbols_file import main as gen_main
                except Exception:
                    logger.exception("Weekly refresh: failed to import generator.")
                    await asyncio.sleep(7 * 24 * 3600)
                    continue

            try:
                rows = gen_main(write_file=True)
                if rows:
                    self.symbols = rows
                    logger.info("Weekly symbols refresh complete: %s tickers", len(rows))
            except Exception:
                logger.exception("Weekly symbols refresh failed.")

            await asyncio.sleep(7 * 24 * 3600)
