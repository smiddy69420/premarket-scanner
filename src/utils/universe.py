# src/utils/universe.py
import os
import sys
import time
import threading
import subprocess
from pathlib import Path
from typing import List, Optional

BASE_DIR = Path(__file__).resolve().parents[1]   # .../src
DEFAULT_SYMBOLS_FILE = (BASE_DIR / ".." / "data" / "symbols_robinhood.txt").resolve()
GEN_SCRIPT = (BASE_DIR / "generate_symbols_file.py").resolve()

class UniverseManager:
    """
    Priority:
      1) ALL_TICKERS env (comma-separated) — hard override
      2) SYMBOLS_FILE path (defaults to data/symbols_robinhood.txt)
      3) SCAN_UNIVERSE env (comma-separated) — minimal fallback
      4) tiny hard fallback
    The file is hot-reloaded on mtime change.
    """

    def __init__(self) -> None:
        self._symbols_file = Path(os.getenv("SYMBOLS_FILE", str(DEFAULT_SYMBOLS_FILE))).resolve()
        self._cache: List[str] = []
        self._mtime: Optional[float] = None
        self._lock = threading.Lock()

    @staticmethod
    def _load_csv_env(key: str) -> List[str]:
        raw = os.getenv(key, "")
        if not raw:
            return []
        return [s.strip().upper() for s in raw.split(",") if s.strip()]

    def _load_file(self) -> List[str]:
        if not self._symbols_file.exists():
            return []
        lines = self._symbols_file.read_text(encoding="utf-8").splitlines()
        out = []
        for ln in lines:
            s = ln.strip().upper()
            if s:
                out.append(s)
        return out

    def get_universe(self) -> List[str]:
        # 1) explicit env override
        all_env = self._load_csv_env("ALL_TICKERS")
        if all_env:
            return all_env

        # 2) file (with hot-reload cache)
        if self._symbols_file.exists():
            m = self._symbols_file.stat().st_mtime
            with self._lock:
                if self._mtime != m or not self._cache:
                    self._cache = self._load_file()
                    self._mtime = m
            if self._cache:
                return self._cache

        # 3) minimal env fallback
        tiny = self._load_csv_env("SCAN_UNIVERSE")
        if tiny:
            return tiny

        # 4) hard fallback (never empty)
        return ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "AMD", "JPM"]

    def ensure_file_exists(self) -> None:
        """Generate symbols file if missing (best-effort; non-fatal)."""
        if self._symbols_file.exists():
            return
        try:
            self._symbols_file.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [sys.executable, str(GEN_SCRIPT), "--out", str(self._symbols_file)],
                check=True,
            )
            print(f"[INFO] UniverseManager: generated {self._symbols_file}")
        except Exception as e:
            print(f"[WARN] UniverseManager: could not generate file: {e}")

    def refresh_once(self) -> bool:
        """Regenerate file once; returns True on success."""
        try:
            self._symbols_file.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [sys.executable, str(GEN_SCRIPT), "--out", str(self._symbols_file)],
                check=True,
            )
            print("[INFO] UniverseManager: symbols refreshed.")
            return True
        except Exception as e:
            print(f"[WARN] UniverseManager: refresh failed: {e}")
            return False

    def weekly_refresh_forever(self) -> None:
        """Blocking loop; run in a worker thread."""
        while True:
            self.refresh_once()
            time.sleep(7 * 24 * 3600)  # 7 days
