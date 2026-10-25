"""Append-only, secret-safe SQLite journal for strategy research.

The journal is observation-only: it never reads credentials, signs transactions, or
changes the bot's entry permissions. Repeated reports are built from a read-only view.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from collections import Counter
from typing import Any, Iterable


_SECRET_NAME = re.compile(
    r"(?:api.?key|auth(?:orization)?|bearer|private.?key|secret|seed.?phrase|"
    r"mnemonic|password|cookie|keypair.?path|wallet.?file)",
    re.IGNORECASE,
)
_ALLOWED_STRATEGIES = {"leader_copy", "trending_discovery", "token_discovery", "market_data", "system"}
_DECISION_RE = re.compile(r"(?:reject|block|skip|filter|fail|stale|unparsed|duplicate|unknown)", re.IGNORECASE)


def finite_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _safe_json(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Bound and redact arbitrary telemetry payloads before persistence."""
    if _SECRET_NAME.search(key):
        return "[REDACTED]"
    if depth > 5:
        return "[DEPTH_LIMIT]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, dict):
        result = {}
        for name, item in list(value.items())[:80]:
            field = str(name)[:100]
            if _SECRET_NAME.search(field):
                continue
            result[field] = _safe_json(item, key=field, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        return [_safe_json(item, depth=depth + 1) for item in list(value)[:50]]
    return str(value)[:300]


def _hash_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


class EventJournal:
    """Durable event timeline, candidate decisions, and same-mint market marks."""

    RETENTION_ROWS = 500_000

    def __init__(self, connection: sqlite3.Connection, lock):
        self.db = connection
        self.lock = lock
        with self.lock, self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS event_journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    observed_at REAL NOT NULL,
                    event_type TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    mint TEXT NOT NULL DEFAULT '',
                    wallet TEXT NOT NULL DEFAULT '',
                    side TEXT NOT NULL DEFAULT '',
                    signature TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    sources_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT '',
                    decision TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    price_usd REAL,
                    liquidity_usd REAL,
                    latency_ms REAL,
                    cost_inputs_json TEXT NOT NULL DEFAULT '{}',
                    payload_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_event_journal_time
                    ON event_journal(observed_at, strategy);
                CREATE INDEX IF NOT EXISTS idx_event_journal_mint_time
                    ON event_journal(mint, observed_at);
                CREATE INDEX IF NOT EXISTS idx_event_journal_mode_type
                    ON event_journal(strategy, event_type, status);
            """)

    def record_event(
        self,
        *,
        event_type: str,
        strategy: str,
        observed_at: float | None = None,
        mint: str = "",
        wallet: str = "",
        side: str = "",
        signature: str = "",
        source: str = "",
        status: str = "",
        decision: str = "",
        reason: str = "",
        price_usd: float | None = None,
        liquidity_usd: float | None = None,
        latency_ms: float | None = None,
        cost_inputs: dict | None = None,
        payload: dict | None = None,
        dedupe_key: str = "",
    ) -> bool:
        """Record an event and return True when a new event row was inserted.

        Transaction signatures are public on-chain identifiers. Credentials and
        private key material are stripped from all JSON payloads. If the same
        event is seen again through another source, source labels are coalesced.
        """
        event_type = str(event_type or "").strip().lower()[:80]
        strategy = str(strategy or "system").strip().lower()[:80]
        if not event_type:
            raise ValueError("event_type is required")
        if strategy not in _ALLOWED_STRATEGIES:
            strategy = "system"
        timestamp = finite_number(observed_at)
        if timestamp is None or timestamp <= 0:
            timestamp = time.time()
        mint_value = str(mint or "")[:128]
        wallet_value = str(wallet or "")[:128]
        side_value = str(side or "").lower()[:24]
        signature_value = str(signature or "")[:180]
        source_value = str(source or "unknown")[:120]
        status_value = str(status or "")[:80]
        decision_value = str(decision or "")[:80]
        reason_value = " ".join(str(reason or "").split())[:1000]
        price = finite_number(price_usd)
        liquidity = finite_number(liquidity_usd)
        latency = finite_number(latency_ms)
        if price is not None and price <= 0:
            price = None
        if liquidity is not None and liquidity < 0:
            liquidity = None
        if latency is not None and latency < 0:
            latency = None

        if dedupe_key:
            raw_key = "dedupe|" + str(dedupe_key)
        elif signature_value:
            key_fields = ["tx", signature_value, mint_value, wallet_value, side_value, event_type]
            # A lifecycle update (candidate/entry decision) must not overwrite an
            # earlier rejection with a later status; ordinary feed duplicates still
            # coalesce when their canonical transaction identity matches.
            if event_type in {"candidate_decision", "signal_decision", "entry_decision"}:
                key_fields.extend((status_value, decision_value, reason_value))
            raw_key = "|".join(key_fields)
        else:
            raw_key = "|".join((
                "obs", event_type, strategy, mint_value, wallet_value, side_value,
                str(int(timestamp * 1000)), status_value, decision_value, reason_value,
            ))
        event_key = _hash_key(raw_key)
        safe_payload = _safe_json(payload if isinstance(payload, dict) else {})
        safe_costs = _safe_json(cost_inputs if isinstance(cost_inputs, dict) else {})
        payload_json = json.dumps(safe_payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        costs_json = json.dumps(safe_costs, sort_keys=True, separators=(",", ":"), allow_nan=False)

        with self.lock, self.db:
            existing = self.db.execute(
                "SELECT id, sources_json FROM event_journal WHERE event_key=?", (event_key,)
            ).fetchone()
            inserted = existing is None
            sources = set()
            if existing:
                try:
                    sources.update(json.loads(existing["sources_json"] or "[]"))
                except (TypeError, ValueError):
                    pass
            sources.add(source_value)
            sources_json = json.dumps(sorted(sources)[:30], separators=(",", ":"))
            cur = self.db.execute(
                """
                INSERT INTO event_journal (
                    event_key, observed_at, event_type, strategy, mint, wallet, side,
                    signature, source, sources_json, status, decision, reason,
                    price_usd, liquidity_usd, latency_ms, cost_inputs_json, payload_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(event_key) DO UPDATE SET
                    sources_json=excluded.sources_json,
                    source=CASE WHEN event_journal.source=excluded.source
                                THEN event_journal.source ELSE 'multi-source' END,
                    price_usd=COALESCE(event_journal.price_usd, excluded.price_usd),
                    liquidity_usd=COALESCE(event_journal.liquidity_usd, excluded.liquidity_usd),
                    latency_ms=COALESCE(event_journal.latency_ms, excluded.latency_ms),
                    payload_json=CASE WHEN event_journal.payload_json='{}'
                                      THEN excluded.payload_json ELSE event_journal.payload_json END
                """,
                (
                    event_key, timestamp, event_type, strategy, mint_value, wallet_value,
                    side_value, signature_value, source_value, sources_json, status_value,
                    decision_value, reason_value, price, liquidity, latency, costs_json, payload_json,
                ),
            )
            _ = cur.rowcount  # rowcount also returns 1 for an UPSERT update.
            # Prune at most once per UTC hour; keep the most recent bounded timeline.
            hour = int(time.time() // 3600)
            marker = self.db.execute(
                "SELECT value FROM state WHERE key='event_journal_prune_hour'"
            ).fetchone()
            if not marker or str(marker["value"]) != str(hour):
                count = int(self.db.execute("SELECT COUNT(*) FROM event_journal").fetchone()[0])
                excess = max(0, count - self.RETENTION_ROWS)
                if excess:
                    self.db.execute(
                        "DELETE FROM event_journal WHERE id IN ("
                        "SELECT id FROM event_journal ORDER BY observed_at ASC,id ASC LIMIT ?)",
                        (excess,),
                    )
                self.db.execute(
                    "INSERT INTO state(key,value) VALUES('event_journal_prune_hour',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(hour),)
                )
        return inserted

    def list_events(
        self,
        *,
        strategies: Iterable[str] | None = None,
        event_types: Iterable[str] | None = None,
        mint: str | None = None,
        start_at: float | None = None,
        end_at: float | None = None,
        limit: int = 100_000,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        for field, values in (("strategy", strategies), ("event_type", event_types)):
            chosen = tuple(str(v).strip() for v in (values or ()) if str(v).strip())
            if chosen:
                clauses.append(field + " IN (" + ",".join("?" for _ in chosen) + ")")
                params.extend(chosen)
        if mint:
            clauses.append("mint=?")
            params.append(str(mint))
        if start_at is not None:
            clauses.append("observed_at>=?")
            params.append(float(start_at))
        if end_at is not None:
            clauses.append("observed_at<=?")
            params.append(float(end_at))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        bounded = max(1, min(self.RETENTION_ROWS, int(limit)))
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM event_journal" + where +
                " ORDER BY observed_at ASC,id ASC LIMIT ?",
                (*params, bounded),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            for column, new_name in (("payload_json", "payload"), ("cost_inputs_json", "cost_inputs"),
                                     ("sources_json", "sources")):
                try:
                    item[new_name] = json.loads(item.pop(column))
                except (TypeError, ValueError):
                    item[new_name] = {} if new_name != "sources" else []
            results.append(item)
        return results

    def summary(self) -> dict:
        """Compact per-mode audit counters and paper-ledger aggregates for the UI."""
        with self.lock:
            grouped = self.db.execute(
                "SELECT strategy,event_type,COUNT(*) AS count,MAX(observed_at) AS last_at "
                "FROM event_journal GROUP BY strategy,event_type"
            ).fetchall()
            rejection_rows = self.db.execute(
                "SELECT strategy,reason,COUNT(*) AS count FROM event_journal "
                "WHERE (LOWER(decision) LIKE '%reject%' OR LOWER(decision) LIKE '%block%' "
                "OR LOWER(decision) LIKE '%skip%' OR LOWER(status) IN "
                "('rejected','filtered','blocked','skipped','skipped_stale','failed','unknown')) "
                "AND reason<>'' GROUP BY strategy,reason ORDER BY count DESC LIMIT 60"
            ).fetchall()
            ledger_rows = self.db.execute(
                "SELECT strategy,COUNT(*) AS trade_rows,"
                "SUM(CASE WHEN UPPER(action)='SELL' THEN 1 ELSE 0 END) AS sell_rows,"
                "SUM(CASE WHEN UPPER(action)='SELL' THEN pnl_usd ELSE 0 END) AS realized_pnl_usd "
                "FROM trades WHERE LOWER(mode)='paper' GROUP BY strategy"
            ).fetchall()
            last_row = self.db.execute("SELECT MAX(observed_at) AS last_at FROM event_journal").fetchone()
            total = int(self.db.execute("SELECT COUNT(*) FROM event_journal").fetchone()[0])
        by_strategy: dict[str, dict] = {
            name: {"events": 0, "last_event_at": None, "event_types": {}, "rejections": 0,
                   "top_rejections": [], "paper_trade_rows": 0, "paper_sell_rows": 0,
                   "paper_realized_pnl_usd": 0.0}
            for name in ("leader_copy", "trending_discovery", "token_discovery", "market_data")
        }
        for row in grouped:
            target = by_strategy.setdefault(str(row["strategy"]), {
                "events": 0, "last_event_at": None, "event_types": {}, "rejections": 0,
                "top_rejections": [], "paper_trade_rows": 0, "paper_sell_rows": 0,
                "paper_realized_pnl_usd": 0.0,
            })
            target["events"] += int(row["count"] or 0)
            target["last_event_at"] = max(float(target["last_event_at"] or 0), float(row["last_at"] or 0)) or None
            target["event_types"][str(row["event_type"])] = int(row["count"] or 0)
        for row in rejection_rows:
            target = by_strategy.setdefault(str(row["strategy"]), {
                "events": 0, "last_event_at": None, "event_types": {}, "rejections": 0,
                "top_rejections": [], "paper_trade_rows": 0, "paper_sell_rows": 0,
                "paper_realized_pnl_usd": 0.0,
            })
            count = int(row["count"] or 0)
            target["rejections"] += count
            if len(target["top_rejections"]) < 5:
                target["top_rejections"].append({"reason": str(row["reason"])[:300], "count": count})
        for row in ledger_rows:
            target = by_strategy.setdefault(str(row["strategy"]), {
                "events": 0, "last_event_at": None, "event_types": {}, "rejections": 0,
                "top_rejections": [], "paper_trade_rows": 0, "paper_sell_rows": 0,
                "paper_realized_pnl_usd": 0.0,
            })
            target["paper_trade_rows"] = int(row["trade_rows"] or 0)
            target["paper_sell_rows"] = int(row["sell_rows"] or 0)
            target["paper_realized_pnl_usd"] = float(row["realized_pnl_usd"] or 0.0)
        return {
            "total_events": total,
            "last_event_at": float(last_row["last_at"]) if last_row and last_row["last_at"] is not None else None,
            "by_strategy": by_strategy,
            "retention_rows": self.RETENTION_ROWS,
            "rejections_by_strategy": {name: data["top_rejections"] for name, data in by_strategy.items()},
            "note": "Paper ledger PnL is separate from counterfactual shadow outcomes; neither proves live expectancy.",
        }
