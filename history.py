# history.py - tiny SQLite store for recent signals
import sqlite3, os, time

DB_PATH = os.getenv("HISTORY_DB_PATH", "signals.db")

def _conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with _conn() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER,
            source TEXT,        -- 'scan_now' | 'scan_ticker'
            ticker TEXT,
            bias TEXT,          -- 'CALL' | 'PUT'
            price REAL,
            exp TEXT,
            target TEXT,
            stop TEXT,
            why TEXT
        )
        """)

def log_signal(source, ticker, bias, price, exp, target, stop, why):
    with _conn() as con:
        con.execute(
            "INSERT INTO signals (ts,source,ticker,bias,price,exp,target,stop,why) VALUES (?,?,?,?,?,?,?,?,?)",
            (int(time.time()), source, ticker.upper(), bias, float(price or 0), exp or "", str(target or ""), str(stop or ""), why or "")
        )

def recent_for_ticker(ticker, limit=3):
    with _conn() as con:
        cur = con.execute(
            "SELECT ts,source,ticker,bias,price,exp,target,stop,why FROM signals WHERE ticker=? ORDER BY ts DESC LIMIT ?",
            (ticker.upper(), int(limit))
        )
        return cur.fetchall()
