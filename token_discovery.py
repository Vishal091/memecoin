"""Independent Pump.fun token discovery and rug-risk screening in shadow-only mode.

This module never signs or submits a transaction. It observes new launches and a
small, bounded set of token-trade streams, verifies the canonical bonding curve
and mint controls on chain, then records hypothetical entries for comparison.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import requests

from solders.pubkey import Pubkey

from pump_curve import bonding_curve_address, parse_bonding_curve_data
from native_pump import TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID
from pumpportal import PUMP_PROGRAM_ID

logger = logging.getLogger("token_discovery")

STATE_PREFIX = "token_discovery_candidate:"
TOKEN_METADATA_SAFE_EXTENSIONS = {
    "metadatapointer",
    "tokenmetadata",
}
BLOCKED_EXTENSION_NAMES = {
    "transferhook",
    "permanentdelegate",
    "defaultaccountstate",
    "nontransferable",
    "confidentialtransfermint",
    "confidentialtransferfeeconfig",
    "confidentialmintburn",
    "pausable",
    "transferfeeconfig",
}


def score_candidate_metrics(
    *,
    unique_buyers: int,
    buy_trades: int,
    sell_trades: int,
    buy_volume_sol: float,
    sell_volume_sol: float,
    liquidity_usd: float,
    min_liquidity_usd: float,
) -> tuple[int, dict[str, int]]:
    """Return a bounded, deterministic 0..100 score and interpretable components."""
    buyer_score = round(30 * min(max(unique_buyers, 0) / 10, 1))
    activity_score = round(15 * min(max(buy_trades, 0) / 10, 1))
    if buy_volume_sol > max(0.1, sell_volume_sol * 1.5):
        flow_score = 20
    elif buy_volume_sol > sell_volume_sol and buy_volume_sol > 0:
        flow_score = 12
    else:
        flow_score = 0
    volume_score = round(15 * min(max(buy_volume_sol, 0.0) / 1.5, 1))
    if liquidity_usd >= min_liquidity_usd:
        liquidity_score = 20
    elif liquidity_usd >= min_liquidity_usd * 0.5:
        liquidity_score = 10
    else:
        liquidity_score = 0
    parts = {
        "buyer_diversity": buyer_score,
        "buy_activity": activity_score,
        "net_buy_pressure": flow_score,
        "buy_volume": volume_score,
        "real_curve_liquidity": liquidity_score,
    }
    return min(100, sum(parts.values())), parts


class TokenDiscoveryShadow:
    """Screen new launches and record shadow returns; live execution is owned by the opt-in bot path."""

    def __init__(self, settings, rpc, market, store, feed=None):
        self.settings = settings
        self.rpc = rpc
        self.market = market
        self.store = store
        self.feed = feed
        self.enabled = bool(getattr(settings, "token_discovery_enabled", False))
        self.live_enabled = bool(getattr(settings, "token_discovery_live_enabled", False))
        self.live_entry_cap_usd = min(0.50, float(getattr(settings, "token_discovery_max_entry_usd", 0.50)))
        self.max_watched = max(1, int(getattr(settings, "token_discovery_max_watched_tokens", 5)))
        self.watch_seconds = max(30, int(getattr(settings, "token_discovery_watch_seconds", 180)))
        self.max_scans_per_minute = max(1, int(getattr(settings, "token_discovery_max_launches_per_minute", 12)))
        self.min_liquidity_usd = max(5000.0, float(getattr(settings, "token_discovery_min_liquidity_usd", 5000.0)))
        self.min_score = max(80, min(100, int(getattr(settings, "token_discovery_min_score", 80))))
        self.min_unique_buyers = max(8, int(getattr(settings, "token_discovery_min_unique_buyers", 8)))
        self.shadow_window_seconds = max(60, int(getattr(settings, "token_discovery_shadow_window_seconds", 900)))
        self.max_token_age_hours = max(1, min(24, int(getattr(settings, "token_discovery_max_token_age_hours", 24))))
        self.max_token_age_seconds = self.max_token_age_hours * 3600
        self.max_queue_size = 100
        self.backfill_auth_token = str(getattr(settings, "pumpfun_auth_token", "") or "").strip()
        self.backfill_url = "https://frontend-api-v3.pump.fun/coins"
        self.backfill_page_size = 100
        self._backfill_offset = 0
        try:
            self._backfill_offset = max(0, int(self.store.get_state("token_discovery_backfill_offset", "0")))
        except (AttributeError, TypeError, ValueError):
            self._backfill_offset = 0
        self._last_backfill_poll_at = 0.0
        self._last_backfill_error_log_at = 0.0
        self._backfill_results = queue.SimpleQueue()
        self._backfill_thread: threading.Thread | None = None
        self._recent_scans: deque[float] = deque()
        self._last_chain_check: dict[str, float] = {}
        self._last_skip_log_at = 0.0
        self._last_subscription_refresh = 0.0
        self.stats = {
            "launch_events": 0,
            "safety_scans": 0,
            "safety_rejected": 0,
            "liquidity_rejected": 0,
            "capacity_skips": 0,
            "rate_limit_skips": 0,
            "queued_launches": 0,
            "queue_processed": 0,
            "queue_expired": 0,
            "queue_dropped": 0,
            "backfill_polls": 0,
            "backfill_candidates_seen": 0,
            "backfill_errors": 0,
            "shadow_entries": 0,
            "last_launch_at": 0.0,
            "last_would_buy_at": 0.0,
        }

    @staticmethod
    def _key(mint: str) -> str:
        return STATE_PREFIX + mint

    def _load(self, mint: str) -> dict | None:
        raw = self.store.get_state(self._key(mint), "")
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def _save(self, candidate: dict) -> None:
        candidate["last_updated_at"] = time.time()
        self.store.set_state(
            self._key(str(candidate["mint"])),
            json.dumps(candidate, sort_keys=True, separators=(",", ":")),
        )

    def _market_cap_from_event(self, event: dict) -> tuple[float, str]:
        """Read explicitly named USD market-cap fields or convert PumpPortal SOL market cap."""
        for key in ("marketCapUsd", "market_cap_usd", "marketCapUSD"):
            try:
                value = float(event.get(key) or 0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                return value, f"reported USD field: {key}"
        for key in ("marketCapSol", "market_cap_sol"):
            try:
                value = float(event.get(key) or 0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                try:
                    return value * float(self.market.sol_price_usd()), f"PumpPortal {key} × SOL/USD"
                except (TypeError, ValueError, RuntimeError):
                    return 0.0, ""
        return 0.0, ""

    def _market_cap_from_market(self, market: dict | None) -> tuple[float, str]:
        raw = (market or {}).get("raw") or {}
        for key in ("marketCap", "market_cap_usd", "marketCapUsd"):
            try:
                value = float(raw.get(key) or 0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                return value, f"indexed market reported field: {key}"
        return 0.0, ""

    @staticmethod
    def _estimated_market_cap(price_usd: float, total_supply_tokens: float) -> tuple[float, str]:
        if price_usd > 0 and total_supply_tokens > 0:
            return price_usd * total_supply_tokens, "Estimated: token price × total supply (FDV-style)"
        return 0.0, ""

    def market_cap_snapshot(self, mint: str) -> dict:
        candidate = self._load(mint) or {}
        entry_cap = float(candidate.get("entry_market_cap_usd") or 0)
        live_entry_cap = float(candidate.get("live_entry_market_cap_usd") or 0)
        # Prefer the actual live-order preflight estimate for an open live position.
        if live_entry_cap > 0:
            entry_cap = live_entry_cap
        return {
            "entry_market_cap_usd": entry_cap or None,
            "entry_market_cap_source": (
                candidate.get("live_entry_market_cap_source")
                if live_entry_cap > 0 else candidate.get("entry_market_cap_source")
            ) or "",
            "current_market_cap_usd": float(candidate.get("market_cap_usd") or 0) or None,
            "current_market_cap_source": candidate.get("market_cap_source") or "",
        }

    def candidates(self, limit: int = 25) -> list[dict]:
        if not hasattr(self.store, "list_state_prefix"):
            return []
        # Candidate history is pruned to 300 records, so read the bounded ledger
        # before sorting by last_updated_at (SQLite row order is creation order).
        rows = self.store.list_state_prefix(STATE_PREFIX, limit=10000)
        candidates = []
        for _key, raw in rows:
            try:
                candidate = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(candidate, dict):
                candidates.append(candidate)
        candidates.sort(key=lambda c: float(c.get("last_updated_at", 0)), reverse=True)
        return candidates[:limit]

    def dashboard_snapshot(self) -> dict:
        rows = self.candidates(300)
        now = time.time()
        queued = sum(1 for c in rows if c.get("status") == "queued")
        safety_passed = sum(1 for c in rows if c.get("security_passed") is True)
        qualified = sum(1 for c in rows if c.get("would_buy"))
        attempted_states = {
            "submitting", "opened", "closed", "failed", "blocked",
            "retryable", "uncertain", "needs_reconciliation", "skipped_stale",
        }
        live_attempted = sum(1 for c in rows if c.get("live_entry_status") in attempted_states)
        live_confirmed = sum(
            1 for c in rows
            if c.get("live_entry_status") in {"opened", "closed"}
            or c.get("live_entry_confirmed") is True
        )
        live_failed = sum(
            1 for c in rows
            if c.get("live_entry_status") in {"failed", "blocked", "skipped_stale"}
        )
        unfilled_peak_20 = sum(
            1 for c in rows
            if float(c.get("max_return_pct") or 0) >= 20.0
            and c.get("live_entry_status") not in {"opened", "closed"}
        )
        return {
            "enabled": self.enabled,
            "live_enabled": self.live_enabled,
            "live_entry_cap_usd": self.live_entry_cap_usd,
            "mode": "LIVE OPT-IN ENABLED" if self.live_enabled else "SHADOW ONLY",
            "launch_events": self.stats["launch_events"],
            "safety_scans": self.stats["safety_scans"],
            "safety_rejected": self.stats["safety_rejected"],
            "liquidity_rejected": self.stats["liquidity_rejected"],
            "capacity_skips": self.stats["capacity_skips"],
            "rate_limit_skips": self.stats["rate_limit_skips"],
            "queued_launches": self.stats["queued_launches"],
            "queue_processed": self.stats["queue_processed"],
            "queue_expired": self.stats["queue_expired"],
            "queue_dropped": self.stats["queue_dropped"],
            "backfill_polls": self.stats["backfill_polls"],
            "backfill_candidates_seen": self.stats["backfill_candidates_seen"],
            "backfill_errors": self.stats["backfill_errors"],
            "backfill_enabled": bool(self.backfill_auth_token),
            "backfill_offset": self._backfill_offset,
            "queue_count": queued,
            "queue_limit": self.max_queue_size,
            "max_token_age_hours": self.max_token_age_hours,
            "tracked_candidates": len(rows),
            "safety_passed_count": safety_passed,
            "qualified_count": qualified,
            "live_attempted_count": live_attempted,
            "live_confirmed_count": live_confirmed,
            "live_failed_count": live_failed,
            "unfilled_peak_20_count": unfilled_peak_20,
            "shadow_entries": sum(1 for c in rows if c.get("shadow_entry_price_usd")),
            "active_watch_count": sum(
                1 for c in rows
                if c.get("subscribed") and float(c.get("subscription_expires_at", 0)) > now
            ),
            "candidates": rows[:50],
            "max_watched_tokens": self.max_watched,
            "min_liquidity_usd": self.min_liquidity_usd,
            "min_score": self.min_score,
            "min_unique_buyers": self.min_unique_buyers,
        }

    def _update_feed_subscriptions(self) -> None:
        if not self.feed:
            return
        now = time.time()
        rows = self.candidates(500)
        live_position_mints = set()
        if hasattr(self.store, "list_positions"):
            live_position_mints = {
                str(p.get("mint") or "")
                for p in self.store.list_positions()
                if p.get("strategy", "leader_copy") == "token_discovery"
            }
        tokens = []
        # Keep a live discovery position on the trade stream after its original
        # observation window; price updates are important for automatic exits.
        for c in rows:
            mint = str(c.get("mint") or "")
            if mint in live_position_mints:
                expiry = float(c.get("subscription_expires_at", 0) or 0)
                if expiry < now + 60:
                    c["subscribed"] = True
                    c["subscription_expires_at"] = now + max(self.watch_seconds, 120)
                    self._save(c)
                tokens.append(mint)
        for c in rows:
            mint = str(c.get("mint") or "")
            if mint in live_position_mints or mint in tokens:
                continue
            if (
                c.get("subscribed")
                and float(c.get("subscription_expires_at", 0)) > now
                and c.get("status") in {"watching", "shadow_open"}
            ):
                tokens.append(mint)
        self.feed.set_discovery_subscriptions(self.enabled, tokens[:self.max_watched])

    def _active_watch_count(self, now: float) -> int:
        return sum(
            1 for c in self.candidates(500)
            if c.get("subscribed")
            and float(c.get("subscription_expires_at", 0)) > now
            and c.get("status") in {"watching", "shadow_open"}
        )

    def _record_skipped(self, mint: str, event: dict, status: str, reason: str) -> None:
        self._save({
            "mint": mint,
            "name": str(event.get("name") or "")[:80],
            "symbol": str(event.get("symbol") or "")[:24],
            "creator": str(event.get("traderPublicKey") or event.get("creator") or "")[:64],
            "created_at": float(event.get("_bot_observed_created_at") or time.time()),
            "status": status,
            "reason": reason,
            "security_passed": False,
            "score": 0,
            "score_components": {},
            "unique_buyers": 0,
            "buy_trades": 0,
            "sell_trades": 0,
            "buy_volume_sol": 0.0,
            "sell_volume_sol": 0.0,
            "subscribed": False,
        })

    @staticmethod
    def _coin_rows(payload: Any) -> list[dict]:
        """Extract coin rows from common Pump.fun API response wrappers."""
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            for key in ("coins", "data", "items", "results", "rows"):
                if key in payload:
                    rows = TokenDiscoveryShadow._coin_rows(payload[key])
                    if rows:
                        return rows
        return []

    @staticmethod
    def _coin_created_at(row: dict) -> float | None:
        """Parse a coin creation timestamp without inventing one if the API omits it."""
        for key in ("created_timestamp", "createdAt", "created_at", "creationTime", "timestamp", "blockTime"):
            raw = row.get(key)
            if raw in (None, ""):
                continue
            try:
                value = float(raw)
                if value > 1_000_000_000_000:
                    value /= 1000.0
                if value > 0:
                    return value
            except (TypeError, ValueError):
                pass
            if isinstance(raw, str):
                try:
                    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    return parsed.timestamp()
                except ValueError:
                    continue
        return None

    def _fetch_recent_launch_page(self, offset: int) -> None:
        """Fetch one catalogue page off-loop; this worker never mutates the ledger."""
        try:
            response = requests.get(
                self.backfill_url,
                params={
                    "limit": str(self.backfill_page_size),
                    "offset": str(offset),
                    "sort": "created_timestamp",
                    "searchTerm": "",
                    "order": "DESC",
                    "includeNsfw": "false",
                    "creator": "",
                    "complete": "false",
                    "meta": "false",
                },
                headers={
                    "Accept": "application/json",
                    "Origin": "https://pump.fun",
                    "Referer": "https://pump.fun/coins",
                    "Cookie": "auth_token=" + self.backfill_auth_token,
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/155.0.0.0 Safari/537.36",
                },
                timeout=max(4, min(8, int(getattr(self.settings, "request_timeout_seconds", 8)))),
            )
            if response.status_code in (401, 403):
                raise RuntimeError("Pump.fun latest-coins API rejected PUMPFUN_AUTH_TOKEN")
            if response.status_code == 429:
                raise RuntimeError("Pump.fun latest-coins API rate-limited history backfill")
            response.raise_for_status()
            rows = self._coin_rows(response.json())
            self._backfill_results.put({
                "offset": offset, "rows": rows, "error": "",
            })
        except Exception as exc:
            self._backfill_results.put({
                "offset": offset, "rows": [],
                "error": " ".join(str(exc).split())[:240],
            })

    def _start_recent_launch_backfill(self, now: float) -> None:
        """Start a bounded, non-blocking poll of Pump.fun's authenticated coin catalogue."""
        if not self.backfill_auth_token or now - self._last_backfill_poll_at < 60:
            return
        if self._backfill_thread and self._backfill_thread.is_alive():
            return
        self._last_backfill_poll_at = now
        self._backfill_thread = threading.Thread(
            target=self._fetch_recent_launch_page,
            args=(self._backfill_offset,),
            daemon=True,
            name="pumpfun-recent-launch-backfill",
        )
        self._backfill_thread.start()

    def _drain_recent_launch_backfill(self, now: float) -> None:
        """Process completed history results on the bot loop, without blocking on HTTP."""
        while True:
            try:
                result = self._backfill_results.get_nowait()
            except queue.Empty:
                return

            error = str(result.get("error") or "")
            if error:
                self.stats["backfill_errors"] += 1
                if now - self._last_backfill_error_log_at >= 60:
                    logger.warning(
                        "Recent-launch backfill unavailable (%s); live new-token stream remains active",
                        error,
                    )
                    self._last_backfill_error_log_at = now
                continue

            offset = int(result.get("offset") or 0)
            rows = result.get("rows") or []
            self.stats["backfill_polls"] += 1
            if not rows:
                self._backfill_offset = 0
                self.store.set_state("token_discovery_backfill_offset", "0")
                continue

            saw_older_than_window = False
            for row in rows:
                mint = str(row.get("mint") or row.get("address") or row.get("tokenAddress") or "").strip()
                if not mint:
                    continue
                created_at = self._coin_created_at(row)
                if created_at is None or created_at > now + 300:
                    # Do not assign a guessed age to history rows with missing timestamps.
                    continue
                age_seconds = now - created_at
                if age_seconds < 0:
                    continue
                if age_seconds > self.max_token_age_seconds:
                    saw_older_than_window = True
                    continue
                # Determine age before checking the local ledger so already-seen older
                # coins still tell pagination when it has crossed the 24-hour boundary.
                if self._load(mint):
                    continue

                creator = str(
                    row.get("creator") or row.get("user") or row.get("traderPublicKey") or ""
                )
                event = {
                    "mint": mint,
                    "name": str(row.get("name") or "")[:80],
                    "symbol": str(row.get("symbol") or "")[:24],
                    "traderPublicKey": creator,
                    "creator": creator,
                    "_bot_observed_created_at": created_at,
                    "source": "pumpfun_latest_backfill",
                }
                self.stats["backfill_candidates_seen"] += 1
                # Queue rather than synchronously inspecting several candidates in
                # one loop. The ordinary queue processor runs at most one on-chain
                # safety scan per tick and observes its normal scan-rate/watch caps.
                self._queue_launch_event(
                    mint, event, "authenticated recent-launch backfill", now
                )

            # Continue down the newest-first catalogue while it still contains launches
            # inside the configured age window. Reset once the page reaches older entries.
            if saw_older_than_window or len(rows) < self.backfill_page_size:
                next_offset = 0
            else:
                next_offset = offset + len(rows)
                if next_offset > 10000:
                    next_offset = 0
            self._backfill_offset = next_offset
            self.store.set_state("token_discovery_backfill_offset", str(next_offset))

    def _queue_launch_event(self, mint: str, event: dict, reason: str, now: float) -> bool:
        queued = [c for c in self.candidates(1000) if c.get("status") == "queued"]
        if len(queued) >= self.max_queue_size:
            self.stats["queue_dropped"] += 1
            logger.warning(
                "Opportunity queue full (%d); dropping unscanned launch %s",
                self.max_queue_size, mint[:8],
            )
            return False
        creator = str(event.get("traderPublicKey") or event.get("creator") or "")[:64]
        # PumpPortal's new-token stream is a launch-event stream. Store when this
        # bot observed that event so persisted queue entries can be age-limited across restarts.
        observed_at = float(event.get("_bot_observed_created_at") or now)
        safe_event = {
            "mint": mint,
            "name": str(event.get("name") or "")[:80],
            "symbol": str(event.get("symbol") or "")[:24],
            "traderPublicKey": creator,
            "creator": creator,
            "_bot_observed_created_at": observed_at,
        }
        self._save({
            "mint": mint,
            "name": safe_event["name"],
            "symbol": safe_event["symbol"],
            "creator": creator,
            "created_at": observed_at,
            "queued_at": now,
            "queue_expires_at": observed_at + self.max_token_age_seconds,
            "queued_event": safe_event,
            "status": "queued",
            "reason": "Waiting for scan/watch capacity; " + reason,
            "security_passed": False,
            "score": 0,
            "score_components": {},
            "unique_buyers": 0,
            "buy_trades": 0,
            "sell_trades": 0,
            "buy_volume_sol": 0.0,
            "sell_volume_sol": 0.0,
            "subscribed": False,
        })
        self.stats["queued_launches"] += 1
        return True

    def _scan_and_register(self, mint: str, event: dict, now: float) -> None:
        self._recent_scans.append(now)
        self.stats["safety_scans"] += 1
        try:
            creator = str(event.get("traderPublicKey") or event.get("creator") or "")
            inspected = self._inspect_new_token(mint, creator=creator)
        except Exception as exc:
            self.stats["safety_rejected"] += 1
            self._record_skipped(
                mint, event, "rejected",
                f"On-chain safety check unavailable ({type(exc).__name__}); failed closed",
            )
            logger.info("Shadow discovery rejected %s because on-chain checks failed: %s",
                        mint[:8], " ".join(str(exc).split())[:200])
            return

        if not inspected["safe"]:
            self.stats["safety_rejected"] += 1
            c = self._new_candidate(mint, event, inspected)
            c["status"] = "rejected"
            c["reason"] = inspected["reason"]
            self._save(c)
            logger.info("Shadow discovery rejected %s: %s", mint[:8], inspected["reason"])
            return

        c = self._new_candidate(mint, event, inspected)
        real_liquidity = float(inspected.get("liquidity_usd") or 0)
        if real_liquidity < self.min_liquidity_usd:
            c["status"] = "rejected"
            c["subscribed"] = False
            c["reason"] = (
                f"Verified real bonding-curve liquidity ${real_liquidity:.0f} "
                f"below required ${self.min_liquidity_usd:.0f}; not subscribed"
            )
            c["liquidity_eligible"] = False
            self.stats["liquidity_rejected"] += 1
            self._save(c)
            logger.info("Shadow discovery rejected %s: %s", mint[:8], c["reason"])
            return
        c["liquidity_eligible"] = True
        c["status"] = "watching"
        c["reason"] = "Mint controls and canonical SOL bonding curve verified; observing real buyer flow"
        c["subscribed"] = True
        c["subscription_expires_at"] = now + self.watch_seconds
        c["last_chain_check"] = now
        c["real_liquidity_usd"] = inspected["liquidity_usd"]
        c["price_usd"] = inspected["price_usd"] or 0.0
        self._last_chain_check[mint] = now
        self._save(c)
        self._update_feed_subscriptions()
        logger.info(
            "SHADOW discovery watching mint=%s creator=%s real_curve_liquidity=$%.0f token_trade_subscription_until=%s",
            mint[:8], c.get("creator", "")[:8], c["real_liquidity_usd"],
            int(c["subscription_expires_at"]),
        )

    def on_new_token_event(self, event: dict) -> None:
        if not self.enabled:
            return
        mint = str(event.get("mint") or "").strip()
        if not mint:
            return
        self.stats["launch_events"] += 1
        self.stats["last_launch_at"] = time.time()
        if self._load(mint):
            return
        try:
            Pubkey.from_string(mint)
        except Exception:
            self.stats["safety_rejected"] += 1
            self._record_skipped(mint, event, "rejected", "Invalid mint address")
            return

        now = time.time()
        event = dict(event)
        try:
            observed_at = float(event.get("_bot_observed_created_at") or now)
            if observed_at <= 0 or observed_at > now + 300:
                observed_at = now
        except (TypeError, ValueError):
            observed_at = now
        event["_bot_observed_created_at"] = observed_at
        while self._recent_scans and now - self._recent_scans[0] >= 60:
            self._recent_scans.popleft()
        if len(self._recent_scans) >= self.max_scans_per_minute:
            self.stats["rate_limit_skips"] += 1
            self._queue_launch_event(mint, event, "scan rate limit reached", now)
            if now - self._last_skip_log_at >= 15:
                logger.info(
                    "Launch queue received events while safety-scan cap is reached (%d/min)",
                    self.max_scans_per_minute,
                )
                self._last_skip_log_at = now
            return
        if self._active_watch_count(now) >= self.max_watched:
            self.stats["capacity_skips"] += 1
            self._queue_launch_event(mint, event, "all token-watch slots are occupied", now)
            if now - self._last_skip_log_at >= 15:
                logger.info(
                    "Launch queue received events while all %d token-watch slots are occupied",
                    self.max_watched,
                )
                self._last_skip_log_at = now
            return

        self._scan_and_register(mint, event, now)

    def _process_queued_launches(self, now: float) -> None:
        rows = [c for c in self.candidates(1000) if c.get("status") == "queued"]
        if not rows:
            return
        for candidate in rows:
            try:
                created_at = float(candidate.get("created_at") or candidate.get("queued_at") or now)
            except (TypeError, ValueError):
                created_at = now
            if now - created_at > self.max_token_age_seconds:
                candidate["status"] = "expired"
                candidate["reason"] = (
                    f"Launch event observed more than {self.max_token_age_hours} hours ago; "
                    "removed from the opportunity queue"
                )
                candidate["queued_event"] = {}
                candidate["subscribed"] = False
                self.stats["queue_expired"] += 1
                self._save(candidate)

        rows = [c for c in self.candidates(1000) if c.get("status") == "queued"]
        if not rows or self._active_watch_count(now) >= self.max_watched:
            return
        while self._recent_scans and now - self._recent_scans[0] >= 60:
            self._recent_scans.popleft()
        if len(self._recent_scans) >= self.max_scans_per_minute:
            return

        # Favor the freshest queued launches; stale ones expire and are visibly counted.
        candidate = max(
            rows,
            key=lambda c: float(c.get("created_at") or c.get("queued_at") or 0),
        )
        event = candidate.get("queued_event") or {}
        mint = str(candidate.get("mint") or "")
        if not mint or not isinstance(event, dict):
            candidate["status"] = "rejected"
            candidate["reason"] = "Queued launch metadata is missing; cannot safely scan"
            self.stats["safety_rejected"] += 1
            self._save(candidate)
            return
        self.stats["queue_processed"] += 1
        self._scan_and_register(mint, event, now)

    def _new_candidate(self, mint: str, event: dict, inspected: dict | None = None) -> dict:
        inspected = inspected or {}
        price = float(inspected.get("price_usd") or 0)
        total_supply_tokens = float(inspected.get("total_supply_tokens") or 0)
        market_cap, market_cap_source = self._market_cap_from_event(event)
        if market_cap <= 0:
            market_cap, market_cap_source = self._estimated_market_cap(price, total_supply_tokens)
        return {
            "mint": mint,
            "name": str(event.get("name") or "")[:80],
            "symbol": str(event.get("symbol") or "")[:24],
            "creator": str(event.get("traderPublicKey") or event.get("creator") or "")[:64],
            "created_at": float(event.get("_bot_observed_created_at") or time.time()),
            "status": "pending",
            "reason": "",
            "security_passed": bool(inspected.get("safe", False)),
            "security_checks": inspected.get("checks", []),
            "top5_holder_supply_pct": float(inspected.get("top5_holder_supply_pct") or 0),
            "creator_supply_pct": float(inspected.get("creator_supply_pct") or 0),
            "real_liquidity_usd": float(inspected.get("liquidity_usd") or 0),
            "price_usd": price,
            "total_supply_tokens": total_supply_tokens,
            "market_cap_usd": market_cap or None,
            "market_cap_source": market_cap_source,
            "entry_market_cap_usd": 0.0,
            "entry_market_cap_source": "",
            "live_entry_market_cap_usd": 0.0,
            "live_entry_market_cap_source": "",
            "score": 0,
            "score_components": {},
            "buy_trades": 0,
            "sell_trades": 0,
            "unique_buyers": 0,
            "buyer_wallets": [],
            "buy_volume_sol": 0.0,
            "sell_volume_sol": 0.0,
            "last_trade_signature": "",
            "last_trade_at": 0.0,
            "subscribed": False,
            "subscription_expires_at": 0.0,
            "shadow_entry_price_usd": 0.0,
            "shadow_entry_at": 0.0,
            "current_return_pct": 0.0,
            "max_return_pct": 0.0,
            "min_return_pct": 0.0,
            "return_1m_pct": None,
            "return_5m_pct": None,
            "return_15m_pct": None,
        }

    def _inspect_new_token(self, mint: str, creator: str = "") -> dict:
        from pump_curve import PumpCurveSnapshot

        mint_result = self.rpc.call(
            "getAccountInfo",
            [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        ) or {}
        mint_account = mint_result.get("value")
        if not isinstance(mint_account, dict):
            return {"safe": False, "reason": "Mint account is not indexed on chain", "checks": ["mint missing"]}
        mint_owner = str(mint_account.get("owner") or "")
        if mint_owner not in {TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID}:
            return {"safe": False, "reason": "Mint is not owned by a supported SPL token program", "checks": ["token program mismatch"]}
        parsed = (((mint_account.get("data") or {}).get("parsed") or {}).get("info") or {})
        if not parsed:
            return {"safe": False, "reason": "Mint data is not parsed; cannot verify authorities", "checks": ["mint parse unavailable"]}
        decimals = int(parsed.get("decimals", -1))
        supply = int(parsed.get("supply", "0") or 0)
        if decimals != 6 or supply <= 0:
            return {"safe": False, "reason": f"Unexpected mint decimals/supply ({decimals}/{supply}); fail closed", "checks": ["mint shape rejected"]}
        if parsed.get("mintAuthority") not in (None, ""):
            return {"safe": False, "reason": "Mint authority remains active", "checks": ["mint authority active"]}
        if parsed.get("freezeAuthority") not in (None, ""):
            return {"safe": False, "reason": "Freeze authority remains active", "checks": ["freeze authority active"]}

        extensions = parsed.get("extensions") or []
        extension_names = []
        for ext in extensions:
            if not isinstance(ext, dict):
                return {"safe": False, "reason": "Token extension metadata is malformed", "checks": ["extension parse rejected"]}
            name = str(ext.get("extension") or ext.get("type") or "").replace("_", "").lower()
            extension_names.append(name)
            if name in BLOCKED_EXTENSION_NAMES:
                return {"safe": False, "reason": f"Risky Token-2022 extension: {name}", "checks": [f"extension {name} rejected"]}
            if name not in TOKEN_METADATA_SAFE_EXTENSIONS:
                return {"safe": False, "reason": f"Unreviewed Token-2022 extension: {name or 'unknown'}", "checks": ["extension allowlist rejected"]}

        curve_address = bonding_curve_address(mint)
        largest_result = self.rpc.call(
            "getTokenLargestAccounts",
            [mint, {"commitment": "confirmed"}],
        ) or {}
        largest_accounts = largest_result.get("value")
        if not isinstance(largest_accounts, list) or not largest_accounts:
            return {
                "safe": False,
                "reason": "Largest token-holder accounts are unavailable; concentration risk cannot be checked",
                "checks": ["holder distribution unavailable"],
            }
        holder_addresses = [str(row.get("address") or "") for row in largest_accounts]
        if any(not address for address in holder_addresses):
            return {"safe": False, "reason": "Largest-holder response contains an invalid account", "checks": ["holder response malformed"]}
        holder_response = self.rpc.call(
            "getMultipleAccounts",
            [holder_addresses, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        ) or {}
        holder_rows = holder_response.get("value")
        if not isinstance(holder_rows, list) or len(holder_rows) != len(holder_addresses):
            return {"safe": False, "reason": "Holder account details could not be verified", "checks": ["holder account lookup failed"]}
        user_holders = []
        creator_holding_raw = 0
        for row in holder_rows:
            if not isinstance(row, dict):
                return {"safe": False, "reason": "A largest-holder account is not indexed; concentration check failed closed", "checks": ["holder account missing"]}
            info = ((((row.get("data") or {}).get("parsed") or {}).get("info")) or {})
            if str(info.get("mint") or "") != mint:
                return {"safe": False, "reason": "Largest-holder account does not match the candidate mint", "checks": ["holder mint mismatch"]}
            owner = str(info.get("owner") or "")
            try:
                amount_raw = int(((info.get("tokenAmount") or {}).get("amount")) or 0)
            except (TypeError, ValueError):
                return {"safe": False, "reason": "Largest-holder token amount could not be parsed", "checks": ["holder amount invalid"]}
            if owner == curve_address:
                # The canonical bonding curve's token inventory is not an insider wallet.
                continue
            user_holders.append((owner, amount_raw))
            if creator and owner == creator:
                creator_holding_raw += amount_raw

        supply_pct = lambda amount: (amount / supply * 100.0) if supply > 0 else 100.0
        top5_pct = supply_pct(sum(amount for _owner, amount in
                                  sorted(user_holders, key=lambda entry: entry[1], reverse=True)[:5]))
        if creator:
            creator_accounts_result = self.rpc.call(
                "getTokenAccountsByOwner",
                [creator, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
            ) or {}
            creator_accounts = creator_accounts_result.get("value")
            if not isinstance(creator_accounts, list):
                return {"safe": False, "reason": "Creator token accounts could not be verified", "checks": ["creator holdings unavailable"]}
            creator_holding_raw = 0
            for creator_account in creator_accounts:
                info = ((((creator_account.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info")) or {}
                if str(info.get("mint") or "") != mint:
                    return {"safe": False, "reason": "Creator token account mint mismatch", "checks": ["creator account mismatch"]}
                creator_holding_raw += int(((info.get("tokenAmount") or {}).get("amount")) or 0)
        creator_pct = supply_pct(creator_holding_raw)
        if creator and creator_pct > 20.0:
            return {
                "safe": False,
                "reason": f"Creator directly holds {creator_pct:.1f}% of total supply (limit 20%)",
                "checks": ["creator concentration rejected"],
                "top5_holder_supply_pct": round(top5_pct, 3),
                "creator_supply_pct": round(creator_pct, 3),
            }
        if top5_pct > 35.0:
            return {
                "safe": False,
                "reason": f"Top five non-curve holders control {top5_pct:.1f}% of total supply (limit 35%)",
                "checks": ["top-holder concentration rejected"],
                "top5_holder_supply_pct": round(top5_pct, 3),
                "creator_supply_pct": round(creator_pct, 3),
            }

        curve_result = self.rpc.call(
            "getAccountInfo",
            [curve_address, {"encoding": "base64", "commitment": "confirmed"}],
        ) or {}
        curve_account = curve_result.get("value")
        if not isinstance(curve_account, dict):
            return {"safe": False, "reason": "Canonical Pump bonding curve account is missing", "checks": ["curve missing"]}
        if str(curve_account.get("owner") or "") != PUMP_PROGRAM_ID:
            return {"safe": False, "reason": "Bonding curve is not owned by the canonical Pump program", "checks": ["curve owner mismatch"]}
        curve = parse_bonding_curve_data((curve_account.get("data") or [None])[0:2])
        if not isinstance(curve, PumpCurveSnapshot):
            return {"safe": False, "reason": "Canonical bonding curve data could not be verified", "checks": ["curve parse failed"]}
        default_quote_mint = "11111111111111111111111111111111"
        wrapped_sol_mint = "So11111111111111111111111111111111111111112"
        quote_mint = getattr(curve, "quote_mint", None)
        if quote_mint not in {default_quote_mint, wrapped_sol_mint}:
            return {
                "safe": False,
                "reason": "Quote mint is missing or unsupported; SOL quote could not be independently verified",
                "checks": ["quote mint missing/unsupported"],
            }
        if curve.complete:
            return {"safe": False, "reason": "Bonding curve is already complete; migrated pool is not covered by this first version", "checks": ["curve completed"]}
        sol_price = float(self.market.sol_price_usd())
        liquidity = curve.liquidity_usd(sol_price)
        price = curve.price_usd(sol_price, decimals=decimals) or 0.0
        checks = [
            "SPL token program verified",
            "mint and freeze authorities absent",
            "only recognized metadata extensions present",
            f"top five non-curve holders control {top5_pct:.1f}% of supply",
            f"creator directly holds {creator_pct:.1f}% of supply",
            "canonical Pump bonding curve verified",
            "SOL quote and real SOL reserves verified",
        ]
        if extension_names:
            checks.append("extensions=" + ",".join(extension_names))
        return {
            "safe": True,
            "reason": "On-chain controls passed",
            "checks": checks,
            "liquidity_usd": liquidity,
            "price_usd": price,
            "decimals": decimals,
            "total_supply_tokens": supply / (10 ** decimals),
            "top5_holder_supply_pct": round(top5_pct, 3),
            "creator_supply_pct": round(creator_pct, 3),
        }

    def _refresh_curve(self, c: dict) -> dict:
        from pump_curve import PumpCurveSnapshot

        mint = str(c["mint"])
        curve_result = self.rpc.call(
            "getAccountInfo",
            [bonding_curve_address(mint), {"encoding": "base64", "commitment": "confirmed"}],
        ) or {}
        account = curve_result.get("value")
        if not isinstance(account, dict):
            raise RuntimeError("canonical curve account missing")
        if str(account.get("owner") or "") != PUMP_PROGRAM_ID:
            raise RuntimeError("curve owner changed or is not canonical")
        curve = parse_bonding_curve_data((account.get("data") or [None])[0:2])
        if not isinstance(curve, PumpCurveSnapshot):
            raise RuntimeError("curve data unavailable")
        if curve.complete:
            # A shadow entry may graduate during its observation window. Continue
            # measuring its mark using indexed market data, but never claim that
            # Dexscreener's liquidity field is an on-chain reserve verification.
            if c.get("status") == "shadow_open" or c.get("live_entry_status") == "opened":
                try:
                    market = self.market.token_market(mint)
                except Exception:
                    market = None
                if market and float(market.get("price_usd") or 0) > 0:
                    c["price_usd"] = float(market["price_usd"])
                    reported_cap, reported_source = self._market_cap_from_market(market)
                    if reported_cap > 0:
                        c["market_cap_usd"] = reported_cap
                        c["market_cap_source"] = reported_source
                    else:
                        estimated_cap, estimated_source = self._estimated_market_cap(
                            float(market["price_usd"]), float(c.get("total_supply_tokens") or 0)
                        )
                        if estimated_cap > 0:
                            c["market_cap_usd"] = estimated_cap
                            c["market_cap_source"] = estimated_source
                    c["shadow_mark_source"] = "indexed market price; migrated pool reserves not independently verified"
                    c["reason"] = "Graduated; shadow performance marked from indexed market price only"
                else:
                    c["reason"] = "Graduated; waiting for an indexed price to measure shadow performance"
                c["last_chain_check"] = time.time()
                self._last_chain_check[mint] = c["last_chain_check"]
                return c
            c["status"] = "graduated_unverified"
            c["reason"] = "Bonding curve completed before shadow entry; migrated PumpSwap reserves are not verified by this version"
            c["subscribed"] = False
            c["last_chain_check"] = time.time()
            self._last_chain_check[mint] = c["last_chain_check"]
            return c
        sol_price = float(self.market.sol_price_usd())
        c["real_liquidity_usd"] = curve.liquidity_usd(sol_price)
        price = curve.price_usd(sol_price, decimals=6)
        if price and price > 0:
            c["price_usd"] = price
            cap, source = self._estimated_market_cap(
                float(price), float(c.get("total_supply_tokens") or 0)
            )
            if cap > 0:
                c["market_cap_usd"] = cap
                c["market_cap_source"] = "Estimated from latest on-chain curve price × total supply (FDV-style)"
        c["last_chain_check"] = time.time()
        self._last_chain_check[mint] = c["last_chain_check"]
        return c

    def _maybe_shadow_enter(self, c: dict, now: float) -> dict:
        c = self._apply_score(c)
        if c.get("would_buy") and float(c.get("price_usd", 0)) > 0:
            c["status"] = "shadow_open"
            c["shadow_entry_price_usd"] = float(c["price_usd"])
            c["shadow_entry_at"] = now
            if float(c.get("market_cap_usd") or 0) <= 0:
                cap, source = self._estimated_market_cap(
                    float(c["price_usd"]), float(c.get("total_supply_tokens") or 0)
                )
                c["market_cap_usd"] = cap or None
                c["market_cap_source"] = source
            c["entry_market_cap_usd"] = float(c.get("market_cap_usd") or 0) or None
            c["entry_market_cap_source"] = c.get("market_cap_source") or ""
            c["peak_price_usd"] = float(c["price_usd"])
            c["reason"] = "SHADOW ENTRY only; no wallet transaction was submitted"
            self.stats["shadow_entries"] += 1
            self.stats["last_would_buy_at"] = now
            live_mode_note = (
                "live-entry preflight enabled; order still requires fresh checks"
                if self.live_enabled and str(getattr(self.settings, "mode", "")).lower() == "live"
                else "NO LIVE ORDER (shadow-only mode)"
            )
            logger.warning(
                "SHADOW WOULD-BUY mint=%s score=%d buyers=%d real_curve_liquidity=$%.0f buy_sol=%.3f sell_sol=%.3f; %s",
                str(c["mint"])[:8], c["score"], c["unique_buyers"], c["real_liquidity_usd"],
                c["buy_volume_sol"], c["sell_volume_sol"], live_mode_note,
            )
        return c

    def _apply_score(self, c: dict) -> dict:
        score, components = score_candidate_metrics(
            unique_buyers=int(c.get("unique_buyers", 0)),
            buy_trades=int(c.get("buy_trades", 0)),
            sell_trades=int(c.get("sell_trades", 0)),
            buy_volume_sol=float(c.get("buy_volume_sol", 0)),
            sell_volume_sol=float(c.get("sell_volume_sol", 0)),
            liquidity_usd=float(c.get("real_liquidity_usd", 0)),
            min_liquidity_usd=self.min_liquidity_usd,
        )
        c["score"] = score
        c["score_components"] = components
        gates = []
        if not c.get("security_passed"):
            gates.append("on-chain safety checks not passed")
        if float(c.get("real_liquidity_usd", 0)) < self.min_liquidity_usd:
            gates.append(f"real curve liquidity below ${self.min_liquidity_usd:.0f}")
        if int(c.get("unique_buyers", 0)) < self.min_unique_buyers:
            gates.append(f"fewer than {self.min_unique_buyers} unique buyers")
        if int(c.get("buy_trades", 0)) < 5:
            gates.append("fewer than 5 observed buys")
        if float(c.get("buy_volume_sol", 0)) <= max(0.1, float(c.get("sell_volume_sol", 0))):
            gates.append("net buy pressure is insufficient")
        if score < self.min_score:
            gates.append(f"score {score} below threshold {self.min_score}")
        c["gate_reasons"] = gates
        c["would_buy"] = not gates
        c["reason"] = "All shadow entry gates passed" if not gates else "; ".join(gates)
        return c

    def on_token_trade_event(self, event: dict) -> None:
        if not self.enabled:
            return
        mint = str(event.get("mint") or "").strip()
        c = self._load(mint) if mint else None
        if not c or not c.get("security_passed"):
            return
        live_position_active = c.get("live_entry_status") == "opened"
        if c.get("status") not in {"watching", "shadow_open"} and not (
            live_position_active and c.get("status") == "shadow_closed_15m"
        ):
            return
        if not c.get("subscribed") or float(c.get("subscription_expires_at", 0)) < time.time():
            return
        signature = str(event.get("signature") or event.get("txSignature") or event.get("txid") or "")
        if signature and signature == c.get("last_trade_signature"):
            return
        side = str(event.get("txType") or event.get("side") or "").lower()
        wallet = str(event.get("traderPublicKey") or event.get("userPublicKey") or event.get("wallet") or "")
        try:
            sol_amount = abs(float(event.get("solAmount") or event.get("sol_amount") or 0))
        except (TypeError, ValueError):
            sol_amount = 0.0
        if side not in {"buy", "sell"} or sol_amount <= 0:
            return
        if signature:
            c["last_trade_signature"] = signature
        c["last_trade_at"] = time.time()
        c["observed_trades"] = int(c.get("observed_trades", 0)) + 1
        try:
            token_amount = abs(float(event.get("tokenAmount") or event.get("token_amount") or 0))
            if token_amount > 0:
                observed_price = (sol_amount / token_amount) * float(self.market.sol_price_usd())
                if observed_price > 0:
                    c["price_usd"] = observed_price
                    c["shadow_mark_source"] = "PumpPortal trade estimate"
        except (TypeError, ValueError, RuntimeError):
            pass
        reported_cap, cap_source = self._market_cap_from_event(event)
        if reported_cap > 0:
            c["market_cap_usd"] = reported_cap
            c["market_cap_source"] = cap_source
        else:
            estimated_cap, estimated_source = self._estimated_market_cap(
                float(c.get("price_usd") or 0), float(c.get("total_supply_tokens") or 0)
            )
            if estimated_cap > 0:
                c["market_cap_usd"] = estimated_cap
                c["market_cap_source"] = estimated_source
        if side == "buy":
            c["buy_trades"] = int(c.get("buy_trades", 0)) + 1
            c["buy_volume_sol"] = round(float(c.get("buy_volume_sol", 0)) + sol_amount, 9)
            buyers = list(c.get("buyer_wallets") or [])
            if wallet and wallet not in buyers and len(buyers) < 200:
                buyers.append(wallet)
            c["buyer_wallets"] = buyers
            c["unique_buyers"] = len(buyers)
        else:
            c["sell_trades"] = int(c.get("sell_trades", 0)) + 1
            c["sell_volume_sol"] = round(float(c.get("sell_volume_sol", 0)) + sol_amount, 9)

        now = time.time()
        if now - float(c.get("last_chain_check", 0)) >= 15:
            try:
                c = self._refresh_curve(c)
            except Exception as exc:
                c["status"] = "rejected"
                c["reason"] = f"On-chain recheck failed ({type(exc).__name__}); shadow entry blocked"
                c["subscribed"] = False
                self.stats["safety_rejected"] += 1
                self._save(c)
                self._update_feed_subscriptions()
                return

        if c.get("status") == "watching":
            c = self._maybe_shadow_enter(c, now)
        elif c.get("status") == "shadow_open":
            self._mark_shadow_return(c, now)
        self._save(c)
        if not c.get("subscribed") or c.get("status") not in {"watching", "shadow_open"}:
            self._update_feed_subscriptions()

    def _mark_shadow_return(self, c: dict, now: float) -> None:
        entry = float(c.get("shadow_entry_price_usd") or 0)
        mark = float(c.get("price_usd") or 0)
        if entry <= 0 or mark <= 0:
            return
        current = (mark / entry - 1) * 100
        c["current_return_pct"] = round(current, 3)
        c["max_return_pct"] = round(max(float(c.get("max_return_pct") or 0), current), 3)
        c["min_return_pct"] = round(min(float(c.get("min_return_pct") or 0), current), 3)
        age = now - float(c.get("shadow_entry_at") or now)
        for key, threshold in (("return_1m_pct", 60), ("return_5m_pct", 300), ("return_15m_pct", 900)):
            if age >= threshold and c.get(key) is None:
                c[key] = round(current, 3)
        if age >= self.shadow_window_seconds:
            c["status"] = "shadow_closed_15m"
            c["shadow_exit_price_usd"] = mark
            c["shadow_exit_at"] = now
            c["shadow_return_pct"] = round(current, 3)
            c["reason"] = "Shadow observation window completed; no real transaction was submitted"

    def tick(self) -> None:
        if not self.enabled:
            return
        now = time.time()
        self._start_recent_launch_backfill(now)
        self._drain_recent_launch_backfill(now)
        changed = False
        rows = self.candidates(500)
        # Only a bounded number of active tokens are polled. Candidates from the free
        # creation stream are capped before any RPC checks; no per-launch RPC storm.
        for c in rows:
            status = c.get("status")
            mint = str(c.get("mint") or "")
            live_position_active = c.get("live_entry_status") == "opened"
            if not mint or (
                status not in {"watching", "shadow_open"}
                and not (live_position_active and status == "shadow_closed_15m")
            ):
                continue
            if c.get("subscribed") and now >= float(c.get("subscription_expires_at", 0)):
                c["subscribed"] = False
                if status == "watching":
                    c["status"] = "watch_expired"
                    c["reason"] = "Candidate trade-watch window expired without a shadow entry"
                changed = True
            last = float(c.get("last_chain_check", 0))
            due = now - last >= (15 if c.get("subscribed") else 30)
            if due:
                try:
                    c = self._refresh_curve(c)
                    if (
                        c.get("status") == "watching"
                        and float(c.get("real_liquidity_usd") or 0) < self.min_liquidity_usd
                    ):
                        c["status"] = "rejected"
                        c["subscribed"] = False
                        c["liquidity_eligible"] = False
                        c["reason"] = (
                            f"Verified real bonding-curve liquidity fell below "
                            f"${self.min_liquidity_usd:.0f}; watch removed"
                        )
                        self.stats["liquidity_rejected"] += 1
                    elif c.get("status") == "watching":
                        c["liquidity_eligible"] = True
                        c = self._maybe_shadow_enter(c, now)
                    elif c.get("status") == "shadow_open":
                        self._mark_shadow_return(c, now)
                except Exception as exc:
                    if c.get("status") == "watching":
                        c["status"] = "rejected"
                        c["reason"] = f"On-chain recheck failed ({type(exc).__name__}); shadow entry blocked"
                        c["subscribed"] = False
                        self.stats["safety_rejected"] += 1
                    else:
                        c["reason"] = f"Shadow mark unavailable ({type(exc).__name__}); retaining last mark"
                self._save(c)
                changed = True
        # Refresh subscriptions periodically so a live held token remains streamed
        # and completed shadow-only candidates are eventually unsubscribed.
        if changed or now - self._last_subscription_refresh >= 30:
            self._update_feed_subscriptions()
            self._last_subscription_refresh = now
        self._process_queued_launches(now)
        self._trim_candidates()

    def _trim_candidates(self) -> None:
        if not hasattr(self.store, "list_state_prefix") or not hasattr(self.store, "delete_state"):
            return
        rows = self.store.list_state_prefix(STATE_PREFIX, limit=10000)
        if len(rows) <= 300:
            return
        enriched = []
        for key, raw in rows:
            try:
                c = json.loads(raw)
            except (TypeError, ValueError):
                continue
            enriched.append((float(c.get("last_updated_at", 0)), key))
        enriched.sort(reverse=True)
        for _updated_at, key in enriched[250:]:
            self.store.delete_state(key)

