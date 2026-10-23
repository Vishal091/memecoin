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
    def __init__(self, path: Path, starting_cash: float, mode: str = "paper"):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._init(starting_cash)
        existing_mode = self.get_state("database_mode", "")
        has_history = (
            self.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] > 0 or
            self.db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] > 0
        )
        if existing_mode and existing_mode != mode:
            self.db.close()
            raise RuntimeError(
                f"This database is locked to {existing_mode!r} mode, not {mode!r}. "
                "Use a different DATABASE_PATH for paper and live trading."
            )
        if not existing_mode and has_history:
            self.db.close()
            raise RuntimeError(
                "This database contains old trading history but has no mode marker. "
                "Use a new, separate DATABASE_PATH for this mode."
            )
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO state(key,value) VALUES('database_mode',?)", (mode,))

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
                invested_usd REAL NOT NULL, source_wallet TEXT, source_signature TEXT,
                strategy TEXT NOT NULL DEFAULT 'leader_copy',
                entry_market_cap_usd REAL NOT NULL DEFAULT 0,
                total_supply_tokens REAL NOT NULL DEFAULT 0,
                entry_market_cap_source TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
                mode TEXT NOT NULL, action TEXT NOT NULL, mint TEXT NOT NULL,
                amount_raw TEXT NOT NULL, price_usd REAL NOT NULL,
                value_usd REAL NOT NULL, pnl_usd REAL NOT NULL DEFAULT 0,
                reason TEXT NOT NULL, source_wallet TEXT, source_signature TEXT,
                tx_signature TEXT, strategy TEXT NOT NULL DEFAULT 'leader_copy'
            );
            CREATE TABLE IF NOT EXISTS risk_reservations (
                reservation_id TEXT PRIMARY KEY,
                mint TEXT NOT NULL,
                strategy TEXT NOT NULL,
                amount_usd REAL NOT NULL,
                status TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_risk_reservations_status
                ON risk_reservations(status, created_at);
            CREATE TABLE IF NOT EXISTS shadow_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observed_at REAL NOT NULL,
                observed_minute INTEGER NOT NULL,
                strategy TEXT NOT NULL,
                mint TEXT NOT NULL,
                status TEXT NOT NULL,
                price_usd REAL,
                liquidity_usd REAL,
                volume_1h_usd REAL,
                score REAL,
                shadow_eligible INTEGER NOT NULL DEFAULT 0,
                market_source TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(strategy, mint, observed_minute, status)
            );
            CREATE INDEX IF NOT EXISTS idx_shadow_observations_time
                ON shadow_observations(observed_at, strategy);
            CREATE INDEX IF NOT EXISTS idx_shadow_observations_candidate
                ON shadow_observations(strategy, mint, observed_at);
            """)
            self.db.execute("INSERT OR IGNORE INTO state(key,value) VALUES('paper_cash',?)", (str(starting_cash),))
            self.db.execute("INSERT OR IGNORE INTO state(key,value) VALUES('initial_equity',?)", (str(starting_cash),))
            self.db.execute("INSERT OR IGNORE INTO state(key,value) VALUES('halted','0')")
            self.db.execute("INSERT OR IGNORE INTO state(key,value) VALUES('halt_reason','')")
            self.db.execute("INSERT OR IGNORE INTO state(key,value) VALUES('day_key','')")
            self.db.execute("INSERT OR IGNORE INTO state(key,value) VALUES('day_start_equity','0')")
            self.db.execute("INSERT OR IGNORE INTO state(key,value) VALUES('daily_entries','0')")
            self.db.execute("INSERT OR IGNORE INTO state(key,value) VALUES('daily_realized_pnl','0')")
            self.db.execute("INSERT OR IGNORE INTO state(key,value) VALUES('live_equity_initialized','0')")
            self.db.execute("INSERT OR IGNORE INTO state(key,value) VALUES('entry_kill_switch_active','1')")
            self.db.execute(
                "INSERT OR IGNORE INTO state(key,value) VALUES('entry_kill_switch_reason',?)",
                ("Entry gate requires explicit operator resume after startup.",),
            )
            self.db.execute(
                "INSERT OR IGNORE INTO state(key,value) VALUES('entry_kill_switch_updated_at',?)",
                (utc_now(),),
            )
            # Add columns when upgrading an existing local database created by an earlier version.
            pos_columns = {row[1] for row in self.db.execute("PRAGMA table_info(positions)").fetchall()}
            if "source_wallet" not in pos_columns:
                self.db.execute("ALTER TABLE positions ADD COLUMN source_wallet TEXT")
            if "source_signature" not in pos_columns:
                self.db.execute("ALTER TABLE positions ADD COLUMN source_signature TEXT")
            if "strategy" not in pos_columns:
                self.db.execute("ALTER TABLE positions ADD COLUMN strategy TEXT NOT NULL DEFAULT 'leader_copy'")
            if "entry_market_cap_usd" not in pos_columns:
                self.db.execute("ALTER TABLE positions ADD COLUMN entry_market_cap_usd REAL NOT NULL DEFAULT 0")
            if "total_supply_tokens" not in pos_columns:
                self.db.execute("ALTER TABLE positions ADD COLUMN total_supply_tokens REAL NOT NULL DEFAULT 0")
            if "entry_market_cap_source" not in pos_columns:
                self.db.execute("ALTER TABLE positions ADD COLUMN entry_market_cap_source TEXT NOT NULL DEFAULT ''")
            trade_columns = {row[1] for row in self.db.execute("PRAGMA table_info(trades)").fetchall()}
            if "strategy" not in trade_columns:
                self.db.execute("ALTER TABLE trades ADD COLUMN strategy TEXT NOT NULL DEFAULT 'leader_copy'")

    def close(self) -> None:
        self.db.close()

    def get_state(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_state(self, key: str, value: Any) -> None:
        with self.db:
            self.db.execute("INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, str(value)))

    def list_state_prefix(self, prefix: str, limit: int = 1000) -> list[tuple[str, str]]:
        """List namespaced state values; callers must still validate decoded JSON."""
        bounded_limit = max(1, min(10000, int(limit)))
        rows = self.db.execute(
            "SELECT key,value FROM state "
            "WHERE substr(key,1,length(?))=? ORDER BY rowid DESC LIMIT ?",
            (str(prefix), str(prefix), bounded_limit),
        ).fetchall()
        return [(str(row["key"]), str(row["value"])) for row in rows]

    def delete_state(self, key: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM state WHERE key=?", (str(key),))

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
                   "peak_price_usd", "tp1_done", "tp2_done", "opened_at", "last_price_usd", "invested_usd",
                   "source_wallet", "source_signature", "strategy", "entry_market_cap_usd",
                   "total_supply_tokens", "entry_market_cap_source")
        with self.db:
            self.db.execute(
                "INSERT INTO positions (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ") "
                "ON CONFLICT(mint) DO UPDATE SET " +
                ",".join(f"{c}=excluded.{c}" for c in columns if c != "mint"),
                tuple(
                    p.get(c, {
                        "strategy": "leader_copy",
                        "entry_market_cap_usd": 0.0,
                        "total_supply_tokens": 0.0,
                        "entry_market_cap_source": "",
                    }.get(c))
                    for c in columns
                ))

    def delete_position(self, mint: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM positions WHERE mint=?", (mint,))

    def reconcile_confirmed_full_exit(
        self,
        position: dict,
        *,
        mode: str,
        tx_signature: str,
        ts: str,
        reason: str,
    ) -> bool:
        """Atomically remove a stale position after an independently verified on-chain full sale.

        The reconciliation row deliberately uses zero/N/A ledger values. Historical USD
        proceeds and realized P&L must not be fabricated when the original fill was not
        persisted. A transaction signature is the idempotency key for the audit row.
        """
        mint = str(position.get("mint") or "")
        amount_raw = str(position.get("amount_raw") or "")
        if not mint or not amount_raw or not tx_signature:
            return False

        with self.db:
            current = self.db.execute(
                "SELECT amount_raw FROM positions WHERE mint=?", (mint,)
            ).fetchone()
            if not current:
                return False
            if str(current["amount_raw"]) != amount_raw:
                # Do not remove a position that changed while chain reconciliation ran.
                return False

            existing = self.db.execute(
                "SELECT 1 FROM trades WHERE mint=? AND tx_signature=? LIMIT 1",
                (mint, tx_signature),
            ).fetchone()
            if not existing:
                self.db.execute(
                    """INSERT INTO trades
                    (ts,mode,action,mint,amount_raw,price_usd,value_usd,pnl_usd,reason,
                     source_wallet,source_signature,tx_signature,strategy)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(ts or utc_now()), str(mode), "RECONCILED_EXIT", mint,
                        amount_raw, 0.0, 0.0, 0.0, str(reason),
                        position.get("source_wallet"), position.get("source_signature"),
                        tx_signature, str(position.get("strategy") or "leader_copy"),
                    ),
                )

            self.db.execute("DELETE FROM positions WHERE mint=?", (mint,))
            state_values = {
                f"exit_reconciled:{mint}": tx_signature,
                f"exit_pending:{mint}": "",
                f"exit_pending_amount_raw:{mint}": "",
                f"exit_pending_source_wallet:{mint}": "",
                f"exit_pending_source_signature:{mint}": "",
                f"exit_blocked_balance_zero:{mint}": "",
                f"native_build_retry_after:{mint}": "",
                f"native_build_failure:{mint}": "",
                f"loss_exit_hold_notice_at:{mint}": "",
                f"pool:{mint}": "",
            }
            for key, value in state_values.items():
                self.db.execute(
                    """INSERT INTO state(key,value) VALUES(?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                    (key, value),
                )
        return True

    def add_trade(self, *, mode: str, action: str, mint: str, amount_raw: str,
                  price_usd: float, value_usd: float, pnl_usd: float = 0.0,
                  reason: str, source_wallet: str | None = None,
                  source_signature: str | None = None, tx_signature: str | None = None,
                  strategy: str = "leader_copy") -> None:
        with self.db:
            self.db.execute("""INSERT INTO trades
                (ts,mode,action,mint,amount_raw,price_usd,value_usd,pnl_usd,reason,source_wallet,source_signature,tx_signature,strategy)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (utc_now(), mode, action, mint, str(amount_raw), float(price_usd), float(value_usd),
                 float(pnl_usd), reason, source_wallet, source_signature, tx_signature, str(strategy or "leader_copy")))

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

    def trader_performance(self) -> list[dict]:
        rows = self.db.execute("""
            SELECT source_wallet,
                   SUM(CASE WHEN action='BUY' THEN 1 ELSE 0 END) AS buys,
                   SUM(CASE WHEN action='SELL' THEN 1 ELSE 0 END) AS sells,
                   SUM(CASE WHEN action='SELL' THEN pnl_usd ELSE 0 END) AS realized_pnl,
                   SUM(CASE WHEN action='SELL' AND pnl_usd > 0 THEN 1 ELSE 0 END) AS winning_exits,
                   SUM(CASE WHEN action='SELL' AND pnl_usd <= 0 THEN 1 ELSE 0 END) AS losing_exits
            FROM trades WHERE source_wallet IS NOT NULL AND source_wallet != ''
            GROUP BY source_wallet ORDER BY realized_pnl DESC
        """).fetchall()
        return [dict(r) for r in rows]

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

    def set_entry_kill_switch(self, active: bool, reason: str) -> None:
        """Persist the global entry gate state and its latest operator/safety reason."""
        clean_reason = " ".join(str(reason or "").split())[:1000]
        with self.lock:
            with self.db:
                values = {
                    "entry_kill_switch_active": "1" if active else "0",
                    "entry_kill_switch_reason": clean_reason,
                    "entry_kill_switch_updated_at": utc_now(),
                }
                for key, value in values.items():
                    self.db.execute(
                        "INSERT INTO state(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, value),
                    )

    def recover_entry_reservations_on_startup(self, *, mode: str) -> None:
        """Recover interrupted admission conservatively; live submission becomes uncertain."""
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if str(mode).lower() != "live":
                    self.db.execute(
                        "UPDATE risk_reservations SET status='released', detail=?, updated_at=? "
                        "WHERE status IN ('reserved','submitting','uncertain')",
                        ("Paper process restarted; no real transaction was submitted by this reservation.", utc_now()),
                    )
                else:
                    self.db.execute(
                        "UPDATE risk_reservations SET status='released', detail=?, updated_at=? WHERE status='reserved'",
                        ("Recovered at startup before submission began; released.", utc_now()),
                    )
                    self.db.execute(
                        "UPDATE risk_reservations SET status='uncertain', detail=?, updated_at=? WHERE status='submitting'",
                        ("Process restarted after submission began; inspect chain history and wallet balances.", utc_now()),
                    )
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

    def try_reserve_entry_atomic(
        self,
        *,
        reservation_id: str,
        mint: str,
        strategy: str,
        amount_usd: float,
        max_total_exposure_usd: float,
        max_open_positions: int,
        max_entries_per_day: int,
    ) -> str:
        """Atomically account for tracked positions, pending intents, duplicate mints and daily capacity."""
        from math import isfinite
        amount = float(amount_usd)
        if not isfinite(amount) or amount <= 0:
            return "Requested exposure is invalid."
        today = datetime.now(timezone.utc).date().isoformat()
        active_statuses = ("reserved", "submitting", "uncertain")
        placeholders = ",".join("?" for _ in active_statuses)
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                def state_value(key: str, default: str = "") -> str:
                    row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
                    return str(row["value"]) if row else default

                if state_value("entry_kill_switch_active", "1") != "0":
                    reason = state_value("entry_kill_switch_reason", "Global entry kill switch is active.")
                    self.db.rollback()
                    return "Global entry kill switch is active: " + reason
                if state_value("daily_paused", "0") == "1":
                    self.db.rollback()
                    return "Daily-loss circuit breaker is active."
                if state_value("consecutive_loss_pause_day", "") == today:
                    self.db.rollback()
                    return "Consecutive-loss circuit breaker is active for the UTC day."
                if self.db.execute(
                    "SELECT 1 FROM state WHERE key LIKE 'unresolved_trade:%' LIMIT 1"
                ).fetchone():
                    self.db.rollback()
                    return "Unresolved transaction state remains; reconcile before entry."

                if self.db.execute("SELECT 1 FROM positions WHERE mint=? LIMIT 1", (mint,)).fetchone():
                    self.db.rollback()
                    return "A position for this mint is already tracked."
                if self.db.execute(
                    "SELECT 1 FROM risk_reservations WHERE mint=? AND status IN (" + placeholders + ") LIMIT 1",
                    (mint, *active_statuses),
                ).fetchone():
                    self.db.rollback()
                    return "An active reservation already exists for this mint."

                position_row = self.db.execute(
                    "SELECT COUNT(*) AS count, COALESCE(SUM(invested_usd),0) AS exposure FROM positions"
                ).fetchone()
                active_row = self.db.execute(
                    "SELECT COUNT(*) AS count, COALESCE(SUM(amount_usd),0) AS exposure, "
                    "SUM(CASE WHEN substr(created_at,1,10)=? THEN 1 ELSE 0 END) AS today_count "
                    "FROM risk_reservations WHERE status IN (" + placeholders + ")",
                    (today, *active_statuses),
                ).fetchone()
                position_count = int(position_row["count"] or 0)
                active_count = int(active_row["count"] or 0)
                if position_count + active_count >= max(1, int(max_open_positions)):
                    self.db.rollback()
                    return "Open-position limit including pending reservations has been reached."

                day_key = state_value("day_key", "")
                daily_entries = int(state_value("daily_entries", "0") or 0) if day_key == today else 0
                active_today = int(active_row["today_count"] or 0)
                if daily_entries + active_today >= max(1, int(max_entries_per_day)):
                    self.db.rollback()
                    return "Daily entry limit including pending reservations has been reached."

                exposure = float(position_row["exposure"] or 0) + float(active_row["exposure"] or 0)
                if exposure + amount > float(max_total_exposure_usd) + 1e-9:
                    self.db.rollback()
                    return "Total exposure reservation would exceed configured maximum."

                timestamp = utc_now()
                self.db.execute(
                    "INSERT INTO risk_reservations "
                    "(reservation_id,mint,strategy,amount_usd,status,detail,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (reservation_id, mint, strategy, amount, "reserved", "", timestamp, timestamp),
                )
                self.db.commit()
                return ""
            except Exception:
                self.db.rollback()
                return "Atomic risk reservation failed; refusing to submit."

    def mark_entry_reservation_submitting(self, reservation_id: str) -> tuple[bool, str]:
        """Persist the submission boundary and atomically re-check the global kill switch."""
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute(
                    "SELECT status FROM risk_reservations WHERE reservation_id=?",
                    (reservation_id,),
                ).fetchone()
                state = self.db.execute(
                    "SELECT value FROM state WHERE key='entry_kill_switch_active'"
                ).fetchone()
                if not row:
                    self.db.rollback()
                    return False, "Risk reservation no longer exists."
                if not state or str(state["value"]) != "0":
                    self.db.rollback()
                    return False, "Global kill switch was engaged before submission."
                if str(row["status"]) != "reserved":
                    self.db.rollback()
                    return False, "Risk reservation is not in the reserved state."
                self.db.execute(
                    "UPDATE risk_reservations SET status='submitting', detail='', updated_at=? WHERE reservation_id=?",
                    (utc_now(), reservation_id),
                )
                self.db.commit()
                return True, ""
            except Exception:
                self.db.rollback()
                return False, "Submission boundary could not be persisted."

    def get_entry_reservation(self, reservation_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM risk_reservations WHERE reservation_id=?", (reservation_id,)
            ).fetchone()
        return dict(row) if row else None

    def set_entry_reservation_status(self, reservation_id: str, status: str, detail: str = "") -> str:
        allowed = {"reserved", "submitting", "uncertain", "completed", "released", "failed", "reconciled"}
        if status not in allowed:
            raise ValueError("Unsupported risk reservation status.")
        with self.lock:
            row = self.db.execute(
                "SELECT mint FROM risk_reservations WHERE reservation_id=?", (reservation_id,)
            ).fetchone()
            if not row:
                return ""
            with self.db:
                self.db.execute(
                    "UPDATE risk_reservations SET status=?,detail=?,updated_at=? WHERE reservation_id=?",
                    (status, " ".join(str(detail or "").split())[:1000], utc_now(), reservation_id),
                )
            return str(row["mint"] or "")

    def active_entry_reservations(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT reservation_id,mint,strategy,amount_usd,status,detail,created_at,updated_at "
                "FROM risk_reservations WHERE status IN ('reserved','submitting','uncertain') "
                "ORDER BY created_at ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def record_shadow_observation(
        self,
        *,
        strategy: str,
        mint: str,
        observed_at: float,
        status: str,
        price_usd: float | None,
        liquidity_usd: float | None = None,
        volume_1h_usd: float | None = None,
        score: float | None = None,
        shadow_eligible: bool = False,
        market_source: str = "",
        payload: dict | None = None,
    ) -> None:
        """Persist read-only market observations for chronological paper/shadow evaluation.

        Records are coalesced by candidate, strategy, minute and status: multiple
        updates inside a minute update that bucket instead of growing the database
        without bound. The payload retains source fields needed for later audit.
        """
        import math

        strategy_value = str(strategy or "").strip()
        mint_value = str(mint or "").strip()
        status_value = str(status or "").strip()
        timestamp = float(observed_at)
        if (
            not strategy_value
            or not mint_value
            or not status_value
            or not math.isfinite(timestamp)
            or timestamp <= 0
        ):
            raise ValueError("Shadow observation requires strategy, mint, status and a valid timestamp.")

        def finite_or_none(value):
            if value is None or value == "":
                return None
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                return None
            return number if math.isfinite(number) else None

        safe_payload = payload if isinstance(payload, dict) else {}
        payload_json = json.dumps(
            safe_payload, sort_keys=True, separators=(",", ":"), default=str
        )
        minute = int(timestamp // 60)
        with self.db:
            self.db.execute(
                """
                INSERT INTO shadow_observations (
                    observed_at, observed_minute, strategy, mint, status,
                    price_usd, liquidity_usd, volume_1h_usd, score,
                    shadow_eligible, market_source, payload_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(strategy,mint,observed_minute,status) DO UPDATE SET
                    observed_at=excluded.observed_at,
                    price_usd=excluded.price_usd,
                    liquidity_usd=excluded.liquidity_usd,
                    volume_1h_usd=excluded.volume_1h_usd,
                    score=excluded.score,
                    shadow_eligible=excluded.shadow_eligible,
                    market_source=excluded.market_source,
                    payload_json=excluded.payload_json
                """,
                (
                    timestamp, minute, strategy_value, mint_value, status_value,
                    finite_or_none(price_usd), finite_or_none(liquidity_usd),
                    finite_or_none(volume_1h_usd), finite_or_none(score),
                    1 if shadow_eligible else 0, str(market_source or "")[:160],
                    payload_json,
                ),
            )

    def list_shadow_observations(
        self,
        *,
        strategies: tuple[str, ...] | list[str] | None = None,
        start_at: float | None = None,
        end_at: float | None = None,
        limit: int = 100000,
    ) -> list[dict]:
        """Read a bounded, chronologically ordered sample of shadow observations."""
        clauses = []
        params: list[Any] = []
        if strategies:
            chosen = tuple(str(item) for item in strategies if str(item))
            if chosen:
                clauses.append("strategy IN (" + ",".join("?" for _ in chosen) + ")")
                params.extend(chosen)
        if start_at is not None:
            clauses.append("observed_at >= ?")
            params.append(float(start_at))
        if end_at is not None:
            clauses.append("observed_at <= ?")
            params.append(float(end_at))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        bounded_limit = max(1, min(500000, int(limit)))
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM shadow_observations" + where +
                " ORDER BY observed_at ASC, strategy ASC, mint ASC LIMIT ?",
                (*params, bounded_limit),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item.pop("payload_json"))
            except (TypeError, ValueError):
                item["payload"] = {}
            item["shadow_eligible"] = bool(item.get("shadow_eligible"))
            results.append(item)
        return results

    def claim_once(self, key: str) -> bool:
        """Atomically add a one-time action key; returns False if it was already claimed."""
        with self.db:
            try:
                self.db.execute("INSERT INTO state(key,value) VALUES(?,?)", (key, "1"))
                return True
            except sqlite3.IntegrityError:
                return False
