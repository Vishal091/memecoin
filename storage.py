"""SQLite persistence: watch cursors, deduplication, positions and trade ledger."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path, starting_cash: float):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._init(starting_cash)

    def _init(self, starting_cash: float) -> None:
        with self.db:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cursors (
                wallet TEXT PRIMARY KEY, signature TEXT, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS seen_signatures (
                signature TEXT PRIMARY KEY, wallet TEXT NOT NULL, seen_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, wallet TEXT NOT NULL,
                signature TEXT NOT NULL, mint TEXT NOT NULL, side TEXT NOT NULL,
                amount_ui REAL NOT NULL, decimals INTEGER NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(wallet, signature, mint, side)
            );
            CREATE TABLE IF NOT EXISTS positions (
                mint TEXT PRIMARY KEY, decimals INTEGER NOT NULL,
                amount_raw TEXT NOT NULL, entry_price_usd REAL NOT NULL,
                initial_amount_raw TEXT NOT NULL, peak_price_usd REAL NOT NULL,
                tp1_done INTEGER NOT NULL DEFAULT 0, tp2_done INTEGER NOT NULL DEFAULT 0,
                opened_at TEXT NOT NULL, last_price_usd REAL NOT NULL,
                invested_usd REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
                mode TEXT NOT NULL, action TEXT NOT NULL, mint TEXT NOT NULL,
                amount_raw TEXT NOT NULL, price_usd REAL NOT NULL,
                value_usd REAL NOT NULL, pnl_usd REAL NOT NULL DEFAULT 0,
                reason TEXT NOT NULL, source_wallet TEXT, source_signature TEXT,
                tx_signature TEXT
            );
            """)
            self.db.execute("INSERT OR IGNORE INTO state(key,value) VALUES('paper_cash',?)", (str(starting_cash),))
            self.db.execute("INSERT OR IGNORE INTO state(key,value) VALUES('initial_equity',?)", (str(starting_cash),))
            self.db.execute("INSERT OR IGNORE INTO state(key,value) VALUES('halted','0')")
            self.db.execute("INSERT OR IGNORE INTO state(key,value) VALUES('halt_reason','')")
            self.db.execute("INSERT OR IGNORE INTO state(key,value) VALUES('day_key','')")
            self.db.execute("INSERT OR IGNORE INTO state(key,value) VALUES('day_start_equity','0')")
            self.db.execute("INSERT OR IGNORE INTO state(key,value) VALUES('daily_entries','0')")
            self.db.execute("INSERT OR IGNORE INTO state(key,value) VALUES('daily_realized_pnl','0')")

    def close(self) -> None:
        self.db.close()

    def get_state(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_state(self, key: str, value: Any) -> None:
        with self.db:
            self.db.execute("INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, str(value)))

    def get_cursor(self, wallet: str) -> str | None:
        row = self.db.execute("SELECT signature FROM cursors WHERE wallet=?", (wallet,)).fetchone()
        return row["signature"] if row else None

    def set_cursor(self, wallet: str, signature: str | None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO cursors(wallet,signature,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(wallet) DO UPDATE SET signature=excluded.signature,updated_at=excluded.updated_at",
                (wallet, signature, utc_now()))

    def signature_seen(self, signature: str) -> bool:
        return self.db.execute("SELECT 1 FROM seen_signatures WHERE signature=?", (signature,)).fetchone() is not None

    def mark_signature_seen(self, signature: str, wallet: str) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO seen_signatures(signature,wallet,seen_at) VALUES(?,?,?)",
                            (signature, wallet, utc_now()))

    def add_signal(self, wallet: str, signature: str, mint: str, side: str,
                   amount_ui: float, decimals: int) -> None:
        with self.db:
            self.db.execute("""INSERT OR IGNORE INTO signals
                (wallet,signature,mint,side,amount_ui,decimals,created_at)
                VALUES(?,?,?,?,?,?,?)""",
                (wallet, signature, mint, side, amount_ui, decimals, utc_now()))

    def list_positions(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM positions").fetchall()
        return [dict(r) for r in rows]

    def get_position(self, mint: str) -> dict | None:
        row = self.db.execute("SELECT * FROM positions WHERE mint=?", (mint,)).fetchone()
        return dict(row) if row else None

    def upsert_position(self, p: dict) -> None:
        columns = ("mint", "decimals", "amount_raw", "entry_price_usd", "initial_amount_raw",
                   "peak_price_usd", "tp1_done", "tp2_done", "opened_at", "last_price_usd", "invested_usd")
        with self.db:
            self.db.execute(
                "INSERT INTO positions (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ") "
                "ON CONFLICT(mint) DO UPDATE SET " +
                ",".join(f"{c}=excluded.{c}" for c in columns if c != "mint"),
                tuple(p[c] for c in columns))

    def delete_position(self, mint: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM positions WHERE mint=?", (mint,))

    def add_trade(self, *, mode: str, action: str, mint: str, amount_raw: str,
                  price_usd: float, value_usd: float, pnl_usd: float = 0.0,
                  reason: str, source_wallet: str | None = None,
                  source_signature: str | None = None, tx_signature: str | None = None) -> None:
        with self.db:
            self.db.execute("""INSERT INTO trades
                (ts,mode,action,mint,amount_raw,price_usd,value_usd,pnl_usd,reason,source_wallet,source_signature,tx_signature)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (utc_now(), mode, action, mint, str(amount_raw), float(price_usd), float(value_usd),
                 float(pnl_usd), reason, source_wallet, source_signature, tx_signature))

    def equity_snapshot(self) -> dict:
        return {
            "paper_cash": float(self.get_state("paper_cash", "0")),
            "initial_equity": float(self.get_state("initial_equity", "10")),
            "halted": self.get_state("halted", "0") == "1",
            "halt_reason": self.get_state("halt_reason", ""),
            "daily_realized_pnl": float(self.get_state("daily_realized_pnl", "0")),
        }

    def recent_trades(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def daily_entries(self) -> int:
        return int(self.get_state("daily_entries", "0"))

    def increment_daily_entries(self) -> None:
        self.set_state("daily_entries", self.daily_entries() + 1)

    def reset_day_if_needed(self, equity_usd: float) -> None:
        day = datetime.now(timezone.utc).date().isoformat()
        if self.get_state("day_key") != day:
            self.set_state("day_key", day)
            self.set_state("day_start_equity", equity_usd)
            self.set_state("daily_entries", 0)
            self.set_state("daily_realized_pnl", 0)
            self.set_state("daily_paused", 0)

    def claim_once(self, key: str) -> bool:
        """Atomically add a one-time action key; returns False if it was already claimed."""
        with self.db:
            try:
                self.db.execute("INSERT INTO state(key,value) VALUES(?,?)", (key, "1"))
                return True
            except sqlite3.IntegrityError:
                return False
