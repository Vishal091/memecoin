"""Secret-safe event journal for research, shadow decisions and counterfactual paths.

The journal stores an allowlisted subset of public market/event fields in the bot's
existing mode-specific SQLite database. It never serializes whole provider payloads,
environment variables, keypairs, authorization headers or arbitrary exception text.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any

STRATEGIES = ("leader_copy", "trending_discovery", "token_discovery")
DEFAULT_PATH_WINDOW_SECONDS = 900

# Public data only. Keep this list deliberately narrow; do not add generic headers,
# credentials, request URLs, environment values, private keys or arbitrary raw payloads.
SAFE_FIELDS = {
    "source", "signature", "tx_signature", "source_signature", "slot", "blockTime",
    "timestamp", "txType", "side", "mint", "wallet", "traderPublicKey",
    "userPublicKey", "bondingCurveKey", "pool", "venue", "tokenAmount", "solAmount",
    "marketCapSol", "marketCapUsd", "market_cap_usd", "price_usd", "priceUsd",
    "liquidity_usd", "liquidityUsd", "volume_1h_usd", "volume_24h_usd",
    "buys_5m", "sells_5m", "buys_1h", "sells_1h", "unique_buyers",
    "status", "reason", "decision", "score", "momentum_score", "shadow_eligible",
    "security_passed", "security_checks", "risk_flags", "gate_reasons",
    "same_pool_crosscheck", "dexscreener_confirmed", "market_price_source",
    "market_data_sources", "market_data_discrepancy", "pool_address",
    "pool_created_at", "dexscreener_pair_address", "real_curve_liquidity_usd",
    "curve_liquidity_usd", "buy_trades", "sell_trades", "buy_volume_sol",
    "sell_volume_sol", "unique_buyers", "creator_supply_pct", "top5_holder_supply_pct",
    "top_holders_pct", "creator", "name", "symbol", "change_5m_pct",
    "change_1h_pct", "change_6h_pct", "change_24h_pct", "fdv_usd",
    "market_cap_source", "shadow_mark_source", "return_1m_pct", "return_5m_pct",
    "return_15m_pct", "current_return_pct", "max_return_pct", "min_return_pct",
    "shadow_entry_at", "shadow_entry_price_usd", "shadow_exit_at",
    "shadow_exit_price_usd", "entry_market_cap_usd", "entry_market_cap_source",
    "latency_ms", "elapsed_ms", "price_impact_pct", "slippage_pct", "signer_verified", "program_verified",
    "action", "value_usd", "pnl_usd", "observed_received_at", "block_time",
    "priority_fee_lamports", "failed_transaction", "fill_price_usd",
}

def _finite(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None

def _json_safe(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        # Prevent unbounded provider text from consuming unbounded database space.
        return value[:500]
    if isinstance(value, list):
        return [_json_safe(item, depth + 1) for item in value[:30]]
    if isinstance(value, dict):
        # Nested content is also allowlisted; arbitrary nested data is not copied.
        return {
            str(key)[:80]: _json_safe(item, depth + 1)
            for key, item in list(value.items())[:40]
            if str(key) in SAFE_FIELDS
        }
    return str(value)[:200]

def safe_payload(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {}
    return {
        str(key): _json_safe(value)
        for key, value in payload.items()
        if str(key) in SAFE_FIELDS
    }

class ResearchJournal:
    """Append-oriented research journal backed by a Store's SQLite connection."""

    def __init__(self, store, *, path_window_seconds: int = DEFAULT_PATH_WINDOW_SECONDS):
        self.store = store
        self.db = store.db
        self.lock = store.lock
        self.path_window_seconds = max(60, min(3600, int(path_window_seconds)))
        with self.lock, self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS research_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    observed_at REAL NOT NULL,
                    mode TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    mint TEXT NOT NULL DEFAULT '',
                    wallet TEXT NOT NULL DEFAULT '',
                    signature TEXT NOT NULL DEFAULT '',
                    side TEXT NOT NULL DEFAULT '',
                    price_usd REAL,
                    liquidity_usd REAL,
                    amount_usd REAL,
                    latency_ms REAL,
                    rejection_reason TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_research_events_time
                    ON research_events(observed_at, strategy, event_type);
                CREATE INDEX IF NOT EXISTS idx_research_events_mint
                    ON research_events(mint, observed_at);
                CREATE TABLE IF NOT EXISTS research_signals (
                    signal_id TEXT PRIMARY KEY,
                    observed_at REAL NOT NULL,
                    mode TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    signal_type TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    mint TEXT NOT NULL,
                    rejection_reason TEXT NOT NULL DEFAULT '',
                    price_usd REAL,
                    liquidity_usd REAL,
                    source_wallet TEXT NOT NULL DEFAULT '',
                    source_signature TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_research_signals_mint_time
                    ON research_signals(mint, observed_at, strategy);
                CREATE TABLE IF NOT EXISTS research_marks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    observed_minute INTEGER NOT NULL,
                    price_usd REAL NOT NULL,
                    liquidity_usd REAL,
                    market_source TEXT NOT NULL DEFAULT '',
                    UNIQUE(signal_id, observed_minute)
                );
                CREATE INDEX IF NOT EXISTS idx_research_marks_signal_time
                    ON research_marks(signal_id, observed_at);
                CREATE TABLE IF NOT EXISTS research_cost_estimates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at REAL NOT NULL,
                    strategy TEXT NOT NULL,
                    mint TEXT NOT NULL,
                    notional_usd REAL NOT NULL,
                    pumpportal_fee_usd REAL NOT NULL,
                    protocol_creator_fee_usd REAL,
                    priority_fee_usd REAL,
                    jito_tip_usd REAL,
                    slippage_pct REAL,
                    failed_tx_cost_usd REAL,
                    latency_ms REAL,
                    total_known_cost_usd REAL,
                    missing_components_json TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'assumption'
                );
                CREATE INDEX IF NOT EXISTS idx_research_costs_strategy_time
                    ON research_cost_estimates(strategy, observed_at);
            """)
        self._maybe_prune()

    @staticmethod
    def _key(*parts: Any) -> str:
        material = "|".join("" if part is None else str(part) for part in parts)
        return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()

    def _maybe_prune(self) -> None:
        """Bound research tables; once/hour only, preserving the newest records."""
        hour = int(time.time() // 3600)
        key = "research_journal_prune_hour"
        with self.lock:
            row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            if row and str(row["value"]) == str(hour):
                return
            with self.db:
                for table, cap, order in (
                    ("research_events", 500000, "observed_at ASC, id ASC"),
                    ("research_signals", 250000, "observed_at ASC, rowid ASC"),
                    ("research_marks", 1000000, "observed_at ASC, id ASC"),
                    ("research_cost_estimates", 250000, "observed_at ASC, id ASC"),
                ):
                    count = int(self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    excess = max(0, count - cap)
                    if excess:
                        if table == "research_signals":
                            self.db.execute(
                                "DELETE FROM research_signals WHERE signal_id IN "
                                "(SELECT signal_id FROM research_signals ORDER BY " + order + " LIMIT ?)",
                                (excess,),
                            )
                        else:
                            self.db.execute(
                                "DELETE FROM " + table + " WHERE id IN "
                                "(SELECT id FROM " + table + " ORDER BY " + order + " LIMIT ?)",
                                (excess,),
                            )
                self.db.execute(
                    "INSERT INTO state(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(hour)),
                )

    def record_event(
        self, *, mode: str, strategy: str, event_type: str, event: dict | None = None,
        observed_at: float | None = None, rejection_reason: str = "",
        latency_ms: float | None = None, amount_usd: float | None = None,
        dedupe_key: str = "",
    ) -> str:
        item = event if isinstance(event, dict) else {}
        stamp = _finite(observed_at) or _finite(item.get("timestamp")) or time.time()
        source = str(item.get("source") or "")
        mint = str(item.get("mint") or "")[:128]
        wallet = str(item.get("wallet") or item.get("traderPublicKey") or item.get("userPublicKey") or "")[:128]
        signature = str(item.get("signature") or item.get("tx_signature") or "")[:160]
        side = str(item.get("side") or item.get("txType") or "")[:24].lower()
        price = _finite(item.get("price_usd") if "price_usd" in item else item.get("priceUsd"))
        liquidity = _finite(item.get("liquidity_usd") if "liquidity_usd" in item else item.get("liquidityUsd"))
        amount = _finite(amount_usd)
        latency = _finite(latency_ms if latency_ms is not None else item.get("latency_ms"))
        clean_reason = " ".join(str(rejection_reason or "").split())[:500]
        safe = safe_payload(item)
        # Prefer real public transaction signatures for deduplication; otherwise retain
        # events within distinct timestamps, not a single mutable "latest event" record.
        if dedupe_key:
            event_key = dedupe_key
        elif signature:
            # A repeated delivery of the same signed transaction is one observed
            # event, even when websocket/RPC ingestion timestamps differ.
            event_key = self._key(mode, strategy, event_type, signature, mint, wallet, side)
        else:
            # Events lacking a chain signature must retain a timestamped fingerprint.
            event_key = self._key(mode, strategy, event_type, source, mint, wallet, side, round(stamp, 2), safe)
        with self.lock, self.db:
            self.db.execute(
                """INSERT OR IGNORE INTO research_events
                (event_key,observed_at,mode,strategy,event_type,mint,wallet,signature,side,
                 price_usd,liquidity_usd,amount_usd,latency_ms,rejection_reason,payload_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event_key, stamp, str(mode)[:12], str(strategy)[:40], str(event_type)[:60],
                 mint, wallet, signature, side, price, liquidity, amount, latency,
                 clean_reason, json.dumps(safe, sort_keys=True, separators=(",", ":"), allow_nan=False)),
            )
        if mint and price is not None and price > 0:
            self.record_mark_for_active_signals(
                mint=mint, observed_at=stamp, price_usd=price,
                liquidity_usd=liquidity, market_source=source or str(item.get("market_price_source") or ""),
            )
        return event_key

    def record_signal(
        self, *, mode: str, strategy: str, mint: str, signal_type: str, decision: str,
        observed_at: float | None = None, rejection_reason: str = "",
        price_usd: float | None = None, liquidity_usd: float | None = None,
        source_wallet: str = "", source_signature: str = "", payload: dict | None = None,
        signal_id: str = "",
    ) -> str:
        stamp = _finite(observed_at) or time.time()
        strategy_value = str(strategy or "").strip()
        mint_value = str(mint or "").strip()
        if not strategy_value or not mint_value:
            return ""
        price = _finite(price_usd)
        liquidity = _finite(liquidity_usd)
        clean_decision = str(decision or "observed").strip().lower()[:40]
        clean_reason = " ".join(str(rejection_reason or "").split())[:500]
        safe = safe_payload(payload)
        if not signal_id:
            if source_signature:
                signal_id = self._key(mode, strategy_value, mint_value, signal_type, source_signature, clean_decision)
            else:
                signal_id = self._key(
                    mode, strategy_value, mint_value, signal_type, clean_decision,
                    round(stamp, 2), clean_reason,
                )
        with self.lock, self.db:
            self.db.execute(
                """INSERT OR IGNORE INTO research_signals
                (signal_id,observed_at,mode,strategy,signal_type,decision,mint,rejection_reason,
                 price_usd,liquidity_usd,source_wallet,source_signature,payload_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (signal_id, stamp, str(mode)[:12], strategy_value, str(signal_type)[:60],
                 clean_decision, mint_value, clean_reason, price, liquidity,
                 str(source_wallet or "")[:128], str(source_signature or "")[:160],
                 json.dumps(safe, sort_keys=True, separators=(",", ":"), allow_nan=False)),
            )
        if price is not None and price > 0:
            self.record_mark_for_active_signals(
                mint=mint_value, observed_at=stamp, price_usd=price,
                liquidity_usd=liquidity, market_source=str((payload or {}).get("market_price_source") or ""),
            )
        return signal_id

    def record_candidate(self, *, mode: str, strategy: str, candidate: dict,
                         observed_at: float | None = None) -> str:
        item = candidate if isinstance(candidate, dict) else {}
        status = str(item.get("status") or "observed").strip().lower()
        reason = str(item.get("reason") or item.get("decision") or "")
        if isinstance(item.get("gate_reasons"), list):
            reason = "; ".join(str(x) for x in item["gate_reasons"] if x) or reason
        price = _finite(item.get("price_usd"))
        liquidity = _finite(item.get("real_curve_liquidity_usd"))
        if liquidity is None:
            liquidity = _finite(item.get("liquidity_usd"))
        hard_reject = status in {"rejected", "filtered", "rejected_risk", "shadow_rejected"}
        would_enter = (
            status == "shadow_open"
            or (strategy == "trending_discovery"
                and status == "screened"
                and bool(item.get("shadow_eligible"))
                and (_finite(item.get("momentum_score")) or 0) >= 75)
        )
        if hard_reject:
            decision = "rejected"
        elif would_enter:
            decision = "would_enter_shadow"
        elif status == "screened" and (price is None or price <= 0):
            decision = "rejected"
            reason = reason or "Missing valid same-pool USD price; excluded from return evaluation."
        else:
            decision = "observed"
        stamp = _finite(observed_at) or _finite(item.get("last_updated_at")) or _finite(item.get("updated_at")) or time.time()
        signal_type = "candidate_state"
        signal_id = self.record_signal(
            mode=mode, strategy=strategy, mint=str(item.get("mint") or ""),
            signal_type=signal_type, decision=decision, observed_at=stamp,
            rejection_reason=reason if decision == "rejected" else "",
            price_usd=price, liquidity_usd=liquidity, payload=item,
        )
        event_type = "rejected_signal" if decision == "rejected" else ("shadow_signal" if decision == "would_enter_shadow" else "candidate_observation")
        self.record_event(
            mode=mode, strategy=strategy, event_type=event_type, event=item,
            observed_at=stamp, rejection_reason=reason if decision == "rejected" else "",
        )
        return signal_id

    def record_mark_for_active_signals(
        self, *, mint: str, observed_at: float, price_usd: float,
        liquidity_usd: float | None = None, market_source: str = "",
    ) -> int:
        stamp, price = _finite(observed_at), _finite(price_usd)
        if stamp is None or stamp <= 0 or price is None or price <= 0:
            return 0
        minute = int(stamp // 60)
        with self.lock:
            rows = self.db.execute(
                """SELECT signal_id FROM research_signals
                   WHERE mint=? AND observed_at<=? AND observed_at>=?""",
                (str(mint), stamp, stamp - self.path_window_seconds),
            ).fetchall()
            inserted = 0
            with self.db:
                for row in rows:
                    cur = self.db.execute(
                        """INSERT INTO research_marks
                        (signal_id,observed_at,observed_minute,price_usd,liquidity_usd,market_source)
                        VALUES(?,?,?,?,?,?)
                        ON CONFLICT(signal_id,observed_minute) DO UPDATE SET
                            observed_at=excluded.observed_at, price_usd=excluded.price_usd,
                            liquidity_usd=excluded.liquidity_usd, market_source=excluded.market_source""",
                        (str(row["signal_id"]), stamp, minute, price, _finite(liquidity_usd), str(market_source or "")[:160]),
                    )
                    inserted += max(0, cur.rowcount)
            return inserted

    def record_cost_estimate(
        self, *, strategy: str, mint: str, notional_usd: float,
        protocol_creator_fee_usd: float | None = None, priority_fee_usd: float | None = None,
        jito_tip_usd: float | None = None, slippage_pct: float | None = None,
        failed_tx_cost_usd: float | None = None, latency_ms: float | None = None,
        source: str = "assumption", observed_at: float | None = None,
    ) -> dict:
        """Record known costs separately and expose unknown components instead of guessing.

        PumpPortal's stated 0.5% fee is modeled here as an assumption. Protocol/creator
        fees, priority/tip, impact/slippage and failed-transaction cost must be supplied
        from route-specific evidence or left unknown.
        """
        stamp = _finite(observed_at) or time.time()
        notional = _finite(notional_usd)
        if notional is None or notional <= 0:
            raise ValueError("notional_usd must be a finite positive number")
        values = {
            "protocol_creator_fee_usd": _finite(protocol_creator_fee_usd),
            "priority_fee_usd": _finite(priority_fee_usd),
            "jito_tip_usd": _finite(jito_tip_usd),
            "slippage_pct": _finite(slippage_pct),
            "failed_tx_cost_usd": _finite(failed_tx_cost_usd),
            "latency_ms": _finite(latency_ms),
        }
        required = ["protocol_creator_fee_usd", "priority_fee_usd", "jito_tip_usd",
                    "slippage_pct", "failed_tx_cost_usd"]
        missing = [key for key in required if values[key] is None]
        if values["latency_ms"] is None:
            missing.append("latency_ms")
        pumpportal_fee = notional * 0.005
        # Do not publish a misleading complete total while any cost component is unknown.
        total = None
        if not missing:
            slippage_usd = notional * max(0.0, values["slippage_pct"]) / 100.0
            total = pumpportal_fee + sum(float(values[k]) for k in required if k != "slippage_pct") + slippage_usd
        with self.lock, self.db:
            self.db.execute(
                """INSERT INTO research_cost_estimates
                (observed_at,strategy,mint,notional_usd,pumpportal_fee_usd,protocol_creator_fee_usd,
                 priority_fee_usd,jito_tip_usd,slippage_pct,failed_tx_cost_usd,latency_ms,
                 total_known_cost_usd,missing_components_json,source)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (stamp, str(strategy)[:40], str(mint)[:128], notional, pumpportal_fee,
                 values["protocol_creator_fee_usd"], values["priority_fee_usd"], values["jito_tip_usd"],
                 values["slippage_pct"], values["failed_tx_cost_usd"], values["latency_ms"],
                 total, json.dumps(missing), str(source)[:80]),
            )
        return {
            "pumpportal_fee_usd": pumpportal_fee, **values, "total_known_cost_usd": total,
            "missing_components": missing, "source": str(source)[:80],
        }

    def snapshot(self) -> dict:
        """Small dashboard-safe counts; never return raw event payloads."""
        with self.lock:
            def count(table: str, where: str = "", params: tuple = ()) -> int:
                return int(self.db.execute(f"SELECT COUNT(*) FROM {table} {where}", params).fetchone()[0])
            return {
                "events": count("research_events"),
                "signals": count("research_signals"),
                "rejected_signals": count("research_signals", "WHERE decision='rejected'"),
                "observed_marks": count("research_marks"),
                "cost_estimates": count("research_cost_estimates"),
                "cost_estimates_missing_components": count(
                    "research_cost_estimates", "WHERE total_known_cost_usd IS NULL"
                ),
            }


// Reuse one writer per Store to keep table setup/pruning out of hot paths.
def get_research_journal(store, *, path_window_seconds: int = DEFAULT_PATH_WINDOW_SECONDS) -> ResearchJournal:
    journal = getattr(store, "_research_journal", None)
    if journal is None:
        journal = ResearchJournal(store, path_window_seconds=path_window_seconds)
        setattr(store, "_research_journal", journal)
    return journal
