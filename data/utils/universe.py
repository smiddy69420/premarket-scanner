# utils/universe.py
import os
from pathlib import Path
from typing import List, Optional
import time
import threading
import subprocess

DEFAULT_SYMBOLS_FILE = "data/symbols_robinhood.txt"

class UniverseManager:
    """
    Loads the ticker universe from:
      1) ALL_TICKERS env (comma-separated), if present
      2) SYMBOLS_FILE (default data/symbols_robinhood.txt), if exists
      3) SCAN_UNIVERSE env (comma-separated), as a minimal fallback
    Also keeps a cached list and auto-reloads if the file changes.
    """

    def __init__(self) -> None:
        self._symbols_file = os.getenv("SYMBOLS_FILE", DEFAULT_SYMBOLS_FILE)
        self._cache: List[str] = []
        self._mtime: Optional[float] = None
        self._lock = threading.Lock()

    def _load_from_file(self) -> List[str]:
        p = Path(self._symbols_file)
        if not p.exists():
            return []
        txt = p.read_text(encoding="utf-8")
        out = []
        for line in txt.splitlines():
            s = line.strip().upper()
            if s:
                out.append(s)
        return out

    def _load_from_env(self, key: str) -> List[str]:
        raw = os.getenv(key, "")
        if not raw:
            return []
        return [s.strip().upper() for s in raw.split(",") if s.strip()]

    def get_universe(self) -> List[str]:
        # 1) ALL_TICKERS overrides everything
        all_env = self._load_from_env("ALL_TICKERS")
        if all_env:
            return all_env

        # 2) file (with mtime watch)
        p = Path(self._symbols_file)
        if p.exists():
            mtime = p.stat().st_mtime
            with self._lock:
                if self._mtime != mtime or not self._cache:
                    self._cache = self._load_from_file()
                    self._mtime = mtime
            if self._cache:
                return self._cache

        # 3) fallback to SCAN_UNIVERSE env
        minimal = self._load_from_env("SCAN_UNIVERSE")
        if minimal:
            return minimal

        # 4) hard fallback to ultra-small set
        return ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "AMD", "JPM"]

    def ensure_file_exists(self) -> None:
        """If file path is set but missing, try to generate it once."""
        p = Path(self._symbols_file)
        if p.exists():
            return
        try:
            subprocess.run(
                ["python", "generate_symbols_file.py", "--out", str(p)],
                check=True,
            )
        except Exception as e:
            print(f"[WARN] Could not generate symbols file: {e}")

    def weekly_refresh_forever(self) -> None:
        """Blocking: run generator weekly."""
        while True:
            try:
                subprocess.run(
                    ["python", "generate_symbols_file.py", "--out", self._symbols_file],
                    check=True,
                )
                print("[INFO] UniverseManager: symbols refreshed.")
            except Exception as e:
                print(f"[WARN] UniverseManager: refresh failed: {e}")
            # sleep 7 days
            time.sleep(7 * 24 * 3600)
