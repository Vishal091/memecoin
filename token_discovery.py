"""Independent Pump.fun token discovery and rug-risk screening in shadow-only mode.

This module never signs or submits a transaction. It observes new launches and a
small, bounded set of token-trade streams, verifies the canonical bonding curve
and mint controls on chain, then records hypothetical entries for comparison.
"""
from __future__ import annotations

import json
import logging
import time
from collections import deque
from typing import Any

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
        self.min_liquidity_usd = float(getattr(settings, "token_discovery_min_liquidity_usd", 1000.0))
        self.min_score = max(0, min(100, int(getattr(settings, "token_discovery_min_score", 75))))
        self.min_unique_buyers = max(2, int(getattr(settings, "token_discovery_min_unique_buyers", 5)))
        self.shadow_window_seconds = max(60, int(getattr(settings, "token_discovery_shadow_window_seconds", 900)))
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
        rows = self.candidates(200)
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
            "shadow_entries": sum(1 for c in rows if c.get("shadow_entry_price_usd")),
            "active_watch_count": sum(
                1 for c in rows
                if c.get("subscribed") and float(c.get("subscription_expires_at", 0)) > time.time()
            ),
            "candidates": rows[:25],
            "max_watched_tokens": self.max_watched,
            "min_liquidity_usd": self.min_liquidity_usd,
            "min_score": self.min_score,
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
            "created_at": time.time(),
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
        while self._recent_scans and now - self._recent_scans[0] >= 60:
            self._recent_scans.popleft()
        if len(self._recent_scans) >= self.max_scans_per_minute:
            # New-token events can arrive many times per second. Count but do not
            # persist each skipped mint; otherwise a busy launch stream can flood
            # SQLite and evict the candidates whose shadow outcomes we are measuring.
            self.stats["rate_limit_skips"] += 1
            if now - self._last_skip_log_at >= 15:
                logger.info(
                    "SHADOW discovery scan cap reached (%d/min); skipping new launches without RPC calls",
                    self.max_scans_per_minute,
                )
                self._last_skip_log_at = now
            return
        if self._active_watch_count(now) >= self.max_watched:
            self.stats["capacity_skips"] += 1
            if now - self._last_skip_log_at >= 15:
                logger.info(
                    "SHADOW discovery watch cap reached (%d tokens); skipping launches without token-trade subscriptions",
                    self.max_watched,
                )
                self._last_skip_log_at = now
            return

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
            "created_at": time.time(),
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

