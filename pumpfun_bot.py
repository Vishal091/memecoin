"""Pump.fun/PumpSwap copy-trading bot.

Paper mode is the default. Live mode is a high-risk opt-in requiring a local,
dedicated Solana keypair and an explicit arming string. Watchlist monitoring uses
Solana RPC; only watched-wallet transactions invoking Pump or PumpSwap programs
are eligible copy signals.
"""
from __future__ import annotations

import argparse
import json
import logging
import queue
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from solders.pubkey import Pubkey

from dashboard import DashboardServer
from leaderboard import LeaderboardError, PumpFunLeaderboard
from market_data import MarketData
from pumpfun_config import PumpFunSettings
from pumpportal_feed import PumpPortalAccountFeed
from solana_logs_feed import SolanaLogsFeed
from pumpportal import (
    PUMP_PROGRAM_ID,
    PUMPSWAP_PROGRAM_ID,
    PumpPortalExecutor,
)
from solana_rpc import SolanaRPC, SolanaRPCRateLimited
from storage import Store

logger = logging.getLogger("pumpfun")


def raw_to_units(amount_raw: int | str, decimals: int) -> float:
    return int(amount_raw) / (10 ** decimals)


def _pubkey_string(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("pubkey", ""))
    return str(value)


class PumpFunBot:
    def __init__(self, settings: PumpFunSettings):
        self.s = settings
        db_path = self.s.database_path
        if not db_path.is_absolute():
            db_path = Path(__file__).resolve().parent / db_path
        self.store = Store(db_path, self.s.starting_paper_cash_usd, mode=self.s.mode)
        self.rpc = SolanaRPC(self.s.rpc_url, self.s.request_timeout_seconds)
        # Aggressive launchpad filters; still require a tracked market pair for price marks.
        self.market = MarketData(self.s.request_timeout_seconds, self.s.min_liquidity_usd, self.s.min_pair_age_seconds)
        self.executor = PumpPortalExecutor(
            self.rpc, self.s.request_timeout_seconds, self.s.pumpportal_priority_fee_sol
        )
        self.keypair = None
        self.wallet_address = None
        if self.s.mode == "live":
            self.keypair = PumpPortalExecutor.load_keypair(self.s.keypair_path)
            self.wallet_address = str(self.keypair.pubkey())
        self._validate_addresses()

        self.leaderboard_client = PumpFunLeaderboard(timeout=self.s.request_timeout_seconds)
        self.leaderboards = {}
        self.selected_window = self.store.get_state("leaderboard_window", self.s.default_leaderboard_window)
        saved_auto = self.store.get_state("auto_copy_enabled", "1" if self.s.auto_copy_on_start else "0")
        self.auto_copy_enabled = saved_auto == "1"
        self.active_wallets = set(self.s.watch_wallets)
        self.last_leaderboard_refresh = 0.0
        self.last_leaderboard_error = ""
        self.message = "Ready. Configure Pump.fun leaderboard access to discover traders automatically."
        self.last_equity_usd = None
        self.price_cache = {}
        self.price_cache_at = {}
        self.market_price_cache = {}
        self.market_price_cache_at = {}
        self.state_lock = threading.RLock()
        self.commands = queue.Queue()
        self.trade_events = queue.Queue()
        self.feed = (PumpPortalAccountFeed(self.s.pumpportal_api_key, self.trade_events,
                                           self.s.request_timeout_seconds)
                     if self.s.pumpportal_api_key else None)
        self.solana_log_events = queue.Queue()
        self.logs_feed = (
            SolanaLogsFeed(self.s.rpc_url, self.solana_log_events, self.s.request_timeout_seconds)
            if self.s.solana_logs_ws_enabled else None
        )
        self.dashboard = None
        self.last_processed_event = set()
        self.signal_stats = {
            "queued_events": 0, "parsed_signals": 0, "unparsed_events": 0,
            "duplicate_events": 0, "unwatched_events": 0,
            "buy_signals": 0, "sell_signals": 0, "sell_without_position": 0,
            "rpc_log_notifications": 0, "rpc_log_signals": 0,
        }
        self.store.set_state("auto_copy_enabled", 1 if self.auto_copy_enabled else 0)
        self.store.set_state("leaderboard_window", self.selected_window)

    def _validate_addresses(self):
        for wallet in self.s.watch_wallets:
            try:
                Pubkey.from_string(wallet)
            except Exception as exc:
                raise ValueError(f"Invalid WATCH_WALLETS Solana address: {wallet}") from exc

    def dashboard_snapshot(self) -> dict:
        with self.state_lock:
            return json.loads(json.dumps(getattr(self, "dashboard_state", {
                "mode": self.s.mode, "message": self.message, "leaderboards": {},
                "positions": [], "trades": [], "active_wallets": [], "auto_copy": False,
                "equity_usd": None,
            }), default=str))

    def _refresh_feed_subscriptions(self) -> None:
        held_leaders = {
            str(p.get("source_wallet")) for p in self.store.list_positions()
            if p.get("source_wallet")
        }
        if self.auto_copy_enabled and self.store.get_state("copy_paused", "0") != "1":
            base_wallets = set(self.s.watch_wallets) | held_leaders
            rows = self.leaderboards.get(self.selected_window, [])
            base_wallets.update(str(row["wallet"]) for row in rows[:self.s.top_traders_count] if row.get("wallet"))
        else:
            base_wallets = held_leaders
        self.active_wallets = base_wallets
        self.store.set_state("active_wallets_json", json.dumps(sorted(self.active_wallets)))
        if self.feed:
            token_mints = [p["mint"] for p in self.store.list_positions()] if self.s.stream_token_prices else []
            self.feed.set_subscriptions(sorted(self.active_wallets), token_mints)
        if self.logs_feed:
            self.logs_feed.set_subscriptions(sorted(self.active_wallets))

    def refresh_leaderboards(self, force: bool = False) -> bool:
        now = time.time()
        if not force and now - self.last_leaderboard_refresh < self.s.leaderboard_refresh_seconds:
            return bool(self.leaderboards)
        self.last_leaderboard_refresh = now
        if not self.leaderboard_client.ready:
            self.last_leaderboard_error = (
                "Automatic discovery needs PUMPFUN_AUTH_TOKEN in the local .env. "
                "No trader wallet addresses need to be entered."
            )
            if not self.s.watch_wallets:
                self.message = self.last_leaderboard_error
            return False
        try:
            boards = self.leaderboard_client.fetch_all()
            self.leaderboards = boards
            self.last_leaderboard_error = ""
            self.message = "Leaderboard refreshed. Select a window and copy its Top 10."
            if self.auto_copy_enabled and self.store.get_state("copy_paused", "0") != "1":
                self._refresh_feed_subscriptions()
            return True
        except LeaderboardError as exc:
            self.last_leaderboard_error = str(exc)
            self.message = "Leaderboard error: " + str(exc)
            logger.warning("%s", self.message)
            return False
        except Exception:
            logger.exception("Unexpected leaderboard error")
            self.last_leaderboard_error = "Unexpected leaderboard error; inspect the console log."
            self.message = self.last_leaderboard_error
            return False

    def _publish_dashboard_state(self, equity: float | None = None) -> None:
        if equity is not None:
            self.last_equity_usd = equity
        positions = []
        for p in self.store.list_positions():
            entry = float(p["entry_price_usd"])
            mark = self._mark_price(p["mint"], float(p["last_price_usd"]))
            if mark <= 0:
                mark = float(p["last_price_usd"])
            move = ((mark / entry) - 1.0) * 100.0 if entry > 0 else 0.0
            positions.append({
                "mint": p["mint"], "amount_raw": p["amount_raw"], "decimals": p["decimals"],
                "entry_price_usd": entry, "last_price_usd": mark, "move_pct": move,
                "source_wallet": p.get("source_wallet", ""),
            })
        trades = self.store.recent_trades(15)
        leaders = {}
        for period, rows in self.leaderboards.items():
            leaders[period] = [{
                "rank": item.get("rank"), "wallet": item.get("wallet"), "label": item.get("label", ""),
                "profit": item.get("profit"), "rank_score": item.get("rank_score"),
            } for item in rows]
        paused = self.store.get_state("copy_paused", "0") == "1"
        state = {
            "mode": self.s.mode,
            "message": self.message,
            "leaderboard_error": self.last_leaderboard_error,
            "leaderboards": leaders,
            "selected_window": self.selected_window,
            "auto_copy": bool(self.auto_copy_enabled and not paused),
            "copy_paused": paused,
            "active_wallets": sorted(self.active_wallets),
            "equity_usd": self.last_equity_usd,
            "positions": positions,
            "trades": trades,
            "feed_connected": bool(self.feed and self.feed.connected.is_set()),
            "feed_error": self.feed.last_error if self.feed else "",
            "feed_stats": ({
                "messages_received": self.feed.messages_received,
                "trade_events_received": self.feed.trade_events_received,
                "account_trade_events_received": self.feed.account_trade_events_received,
                "last_message_at": self.feed.last_message_at,
                "connected_at": self.feed.connected_at,
                "last_account_trade_at": self.feed.last_account_trade_at,
                "last_event_summary": self.feed.last_event_summary,
                "last_notice": self.feed.last_notice,
                "subscribed_wallet_count": self.feed.subscribed_wallet_count,
                "subscription_updates": self.feed.subscription_updates,
                "connected": self.feed.connected.is_set(),
                "error": self.feed.last_error,
            } if self.feed else {
                "messages_received": 0, "trade_events_received": 0,
                "account_trade_events_received": 0, "last_message_at": 0,
                "connected_at": 0, "last_account_trade_at": 0,
                "last_event_summary": "", "last_notice": "PumpPortal API key not configured",
                "subscribed_wallet_count": 0, "subscription_updates": 0,
                "connected": False, "error": "PumpPortal API key not configured",
            }),
            "solana_logs_stats": ({
                "enabled": True,
                "connected": self.logs_feed.connected.is_set(),
                "subscribed_wallet_count": self.logs_feed.subscribed_wallet_count,
                "messages_received": self.logs_feed.messages_received,
                "notifications_received": self.logs_feed.notifications_received,
                "last_message_at": self.logs_feed.last_message_at,
                "last_notification_at": self.logs_feed.last_notification_at,
                "last_signature": self.logs_feed.last_signature,
                "subscription_errors": self.logs_feed.subscription_errors,
                "error": self.logs_feed.last_error,
            } if self.logs_feed else {
                "enabled": False, "connected": False, "subscribed_wallet_count": 0,
                "messages_received": 0, "notifications_received": 0,
                "last_message_at": 0, "last_notification_at": 0,
                "last_signature": "", "subscription_errors": 0,
                "error": "Disabled by SOLANA_LOGS_WS_ENABLED",
            }),
            "signal_stats": dict(self.signal_stats),
        }
        with self.state_lock:
            self.dashboard_state = state

    def _process_dashboard_commands(self) -> None:
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                break
            action = command.get("action")
            if action == "copy_top10":
                window = str(command.get("window", "day")).lower()
                if window not in {"overall", "day", "week", "month"}:
                    self.message = "Invalid leaderboard window."
                    continue
                if not self.leaderboards:
                    self.refresh_leaderboards(force=True)
                if not self.leaderboards.get(window):
                    self.message = self.last_leaderboard_error or "No rankings available for this window."
                    continue
                self.selected_window = window
                self.auto_copy_enabled = True
                self.store.set_state("leaderboard_window", window)
                self.store.set_state("auto_copy_enabled", "1")
                self.store.set_state("copy_paused", "0")
                self._refresh_feed_subscriptions()
                self.message = f"Auto-copy enabled for the top {len(self.leaderboards[window])} traders in {window}."
                logger.warning("%s", self.message)
            elif action == "stop_copy":
                self.auto_copy_enabled = False
                self.store.set_state("auto_copy_enabled", "0")
                self.store.set_state("copy_paused", "1")
                self._refresh_feed_subscriptions()
                self.message = "New copied entries paused. Existing positions remain managed."
                logger.warning("%s", self.message)
            elif action == "manual_exit":
                mint = str(command.get("mint", "")).strip()
                position = self.store.get_position(mint)
                if not position:
                    self.message = "No tracked open position exists for that mint."
                    continue
                self.store.set_state(f"exit_pending:{mint}", "manual exit requested from dashboard")
                self.message = f"Manual exit queued for {mint}; attempting on the next bot cycle."
                logger.warning("%s", self.message)
            elif action == "exit_all":
                positions = self.store.list_positions()
                for position in positions:
                    self.store.set_state(f"exit_pending:{position['mint']}", "manual exit all requested from dashboard")
                self.message = f"Manual exit-all queued for {len(positions)} tracked position(s)."
                logger.warning("%s", self.message)

    def _event_price_usd(self, event: dict) -> float | None:
        try:
            token_amount = float(event.get("tokenAmount") or event.get("token_amount") or 0)
            sol_amount = float(event.get("solAmount") or event.get("sol_amount") or 0)
            if token_amount <= 0 or sol_amount <= 0:
                return None
            return (sol_amount / token_amount) * self.market.sol_price_usd()
        except (TypeError, ValueError, RuntimeError):
            return None

    def _signal_from_feed(self, event: dict) -> dict | None:
        mint = str(event.get("mint") or "")
        wallet = str(event.get("wallet") or event.get("traderPublicKey") or "")
        side = str(event.get("side") or event.get("txType") or "").lower()
        if not mint or not wallet or side not in {"buy", "sell"}:
            return None
        try:
            observed_amount = float(event.get("tokenAmount") or event.get("token_amount") or 0)
            if observed_amount <= 0:
                return None
            decimals = int(event.get("decimals") or 6)
            raw = event.get("tokenAmountRaw") or event.get("token_amount_raw")
            amount_raw = abs(int(raw)) if raw is not None else abs(int(observed_amount * (10 ** decimals)))
            sol_amount = float(event.get("solAmount") or event.get("sol_amount") or 0)
        except (TypeError, ValueError):
            return None
        if amount_raw <= 0:
            return None
        tx_sig = (event.get("signature") or event.get("txSignature") or event.get("txid")
                  or event.get("transactionSignature"))
        if not tx_sig:
            # Stable per-event de-duplication key when provider doesn't include a signature.
            seed = "|".join(str(event.get(k, "")) for k in (
                "wallet", "mint", "side", "timestamp", "solAmount", "tokenAmount", "marketCapSol"
            ))
            import hashlib
            tx_sig = "ws-" + hashlib.sha256(seed.encode()).hexdigest()[:40]
        observed_price = self._event_price_usd(event)
        new_balance = event.get("newTokenBalance") or event.get("new_token_balance")
        post_raw = 0
        if new_balance is not None:
            try:
                post_raw = max(0, int(float(new_balance) * (10 ** decimals)))
            except (TypeError, ValueError):
                post_raw = 0
        pre_raw = post_raw + amount_raw if side == "sell" else max(0, post_raw - amount_raw)
        # Use real reserves only. vSolInBondingCurve is a synthetic pricing reserve,
        # not actual withdrawable liquidity, and must not be used as a liquidity filter.
        real_sol = event.get("realSolReserves") or event.get("real_sol_reserves")
        try:
            real_sol_value = float(real_sol or 0)
            # PumpPortal integrations may serialize lamports as large integers or SOL as decimals.
            if real_sol_value > 1_000_000:
                real_sol_value /= 1_000_000_000
            curve_liquidity_usd = real_sol_value * 2 * self.market.sol_price_usd()
        except (TypeError, ValueError, RuntimeError):
            curve_liquidity_usd = 0.0
        return {
            "wallet": wallet, "signature": str(tx_sig), "mint": mint, "side": side,
            "amount_ui": observed_amount, "amount_raw": amount_raw, "decimals": decimals,
            "leader_pre_raw": pre_raw, "leader_post_raw": post_raw,
            "native_delta_sol": (-sol_amount if side == "buy" else sol_amount),
            "venue": str(event.get("pool") or "pump"),
            "observed_price_usd": observed_price or 0.0,
            "curve_liquidity_usd": curve_liquidity_usd,
            "source": str(event.get("source") or "account_trade"),
            "block_time": event.get("timestamp"),
        }

    @staticmethod
    def _dedupe_key(wallet: str, signature: str) -> str:
        # A transaction can mention several watched wallets (for example, fee distributions).
        # Deduplicate per wallet so one recipient cannot suppress a signer actual trade.
        return f"{wallet}|{signature}"

    def _consume_feed_events(self) -> None:
        while True:
            try:
                event = self.trade_events.get_nowait()
            except queue.Empty:
                break
            self.signal_stats["queued_events"] += 1
            price = self._event_price_usd(event)
            mint = str(event.get("mint") or "")
            if mint and price and price > 0:
                self.price_cache[mint] = price
                self.price_cache_at[mint] = time.time()
            if event.get("source") == "token_trade":
                continue
            signal = self._signal_from_feed(event)
            if not signal:
                self.signal_stats["unparsed_events"] += 1
                logger.warning("Ignoring PumpPortal trade event because required mint/wallet/side/amount fields could not be parsed; fields=%s",
                               ",".join(sorted(str(k) for k in event.keys())))
                continue
            if signal["wallet"] not in self.active_wallets:
                self.signal_stats["unwatched_events"] += 1
                logger.warning("Ignoring PumpPortal %s event for a wallet not in active subscriptions (%s)",
                               signal["side"], signal["wallet"][:8])
                continue
            dedupe_key = self._dedupe_key(signal["wallet"], signal["signature"])
            if self.store.signature_seen(dedupe_key):
                self.signal_stats["duplicate_events"] += 1
                continue
            self.signal_stats["parsed_signals"] += 1
            logger.info("Copy signal parsed: side=%s wallet=%s mint=%s source=%s",
                        signal["side"], signal["wallet"][:8], signal["mint"][:8], signal.get("source"))
            self.process_signal(signal)
            self.store.mark_signature_seen(dedupe_key, signal["wallet"])

    def _consume_solana_log_events(self) -> None:
        """Process Solana logs notifications through the same strict signal detector."""
        while True:
            try:
                event = self.solana_log_events.get_nowait()
            except queue.Empty:
                break
            self.signal_stats["rpc_log_notifications"] += 1
            wallet = str(event.get("wallet") or "")
            signature = str(event.get("signature") or "")
            if not wallet or not signature:
                continue
            if wallet not in self.active_wallets:
                self.signal_stats["unwatched_events"] += 1
                continue
            dedupe_key = self._dedupe_key(wallet, signature)
            if self.store.signature_seen(dedupe_key):
                self.signal_stats["duplicate_events"] += 1
                continue
            if self.rpc.backoff_remaining > 0:
                self.solana_log_events.put(event)
                break
            try:
                signals = self.detect_pump_signal(wallet, signature)
            except SolanaRPCRateLimited:
                self.solana_log_events.put(event)
                break
            except Exception as exc:
                logger.warning("Could not inspect Solana log notification (%s)", type(exc).__name__)
                continue
            for signal in signals:
                signal["source"] = "solana_logs"
                self.signal_stats["parsed_signals"] += 1
                self.signal_stats["rpc_log_signals"] += 1
                logger.info(
                    "Solana logs copy signal parsed: side=%s wallet=%s mint=%s",
                    signal["side"], wallet[:8], signal["mint"][:8],
                )
                self.process_signal(signal)
            self.store.mark_signature_seen(dedupe_key, wallet)

    def _tx_programs(self, tx: dict) -> tuple[set[str], set[str]]:
        """Return all invoked IDs and top-level IDs (parsed or compiled JSON-RPC form)."""
        message = ((tx.get("transaction") or {}).get("message") or {})
        meta = tx.get("meta") or {}
        account_keys = [_pubkey_string(k) for k in message.get("accountKeys", [])]

        def program_id(inst: dict) -> str | None:
            pid = inst.get("programId")
            if pid:
                return _pubkey_string(pid)
            idx = inst.get("programIdIndex")
            if isinstance(idx, int) and 0 <= idx < len(account_keys):
                return account_keys[idx]
            return None

        top = set()
        all_ids = set()
        for inst in message.get("instructions", []) or []:
            if isinstance(inst, dict):
                pid = program_id(inst)
                if pid:
                    top.add(pid)
                    all_ids.add(pid)
        for group in meta.get("innerInstructions", []) or []:
            for inst in group.get("instructions", []) or []:
                if isinstance(inst, dict):
                    pid = program_id(inst)
                    if pid:
                        all_ids.add(pid)
        return all_ids, top

    def detect_pump_signal(self, wallet: str, signature: str) -> list[dict]:
        tx = self.rpc.transaction(signature)
        if not tx or (tx.get("meta") or {}).get("err") is not None:
            return []
        account_keys = (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or [])
        wallet_signed = any(
            isinstance(key, dict)
            and _pubkey_string(key) == wallet
            and key.get("signer") is True
            for key in account_keys
        )
        if not wallet_signed:
            return []
        all_programs, top_programs = self._tx_programs(tx)
        # Strictly exclude generic DEX swaps. We copy only Pump bonding-curve/PumpSwap activity.
        if PUMP_PROGRAM_ID not in all_programs and PUMPSWAP_PROGRAM_ID not in all_programs:
            return []
        venue = "pump-amm" if PUMPSWAP_PROGRAM_ID in top_programs else "pump"
        deltas, native_delta = self.rpc.wallet_token_deltas(wallet, tx)
        sol_delta = native_delta / 1_000_000_000
        stable_mints = {
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
        }
        wsol = "So11111111111111111111111111111111111111112"
        non_quote = {m: d for m, d in deltas.items() if m not in stable_mints and m != wsol}
        quote_deltas = {m: d for m, d in deltas.items() if m in stable_mints}
        added = [(m, d) for m, d in non_quote.items() if d["delta_raw"] > 0]
        removed = [(m, d) for m, d in non_quote.items() if d["delta_raw"] < 0]
        stable_spent = any(d["delta_raw"] < 0 for d in quote_deltas.values())
        stable_received = any(d["delta_raw"] > 0 for d in quote_deltas.values())

        if len(added) == 1 and not removed and (sol_delta < -0.001 or stable_spent):
            mint, data = added[0]
            side = "buy"
        elif len(removed) == 1 and not added and (sol_delta > 0.001 or stable_received):
            mint, data = removed[0]
            side = "sell"
        else:
            return []
        def token_balance(rows):
            total = 0
            for row in rows or []:
                if row.get("owner") == wallet and row.get("mint") == mint:
                    total += int((row.get("uiTokenAmount") or {}).get("amount", "0"))
            return total

        meta = tx.get("meta") or {}
        pre_raw = token_balance(meta.get("preTokenBalances"))
        post_raw = token_balance(meta.get("postTokenBalances"))
        return [{
            "wallet": wallet, "signature": signature, "mint": mint, "side": side,
            "amount_ui": abs(float(data["delta_ui"])),
            "amount_raw": abs(int(data["delta_raw"])),
            "decimals": int(data["decimals"]),
            "leader_pre_raw": pre_raw, "leader_post_raw": post_raw,
            "native_delta_sol": sol_delta, "block_time": tx.get("blockTime"),
            "venue": venue,
        }]

    def _mark_price(self, mint: str, fallback: float) -> float:
        """Prefer recent PumpPortal trade ticks; cache market quotes to reduce HTTP churn."""
        now = time.time()
        feed_price = float(self.price_cache.get(mint, 0) or 0)
        feed_age = now - float(self.price_cache_at.get(mint, 0) or 0)
        if feed_price > 0 and feed_age <= 10:
            return feed_price

        market_price = float(self.market_price_cache.get(mint, 0) or 0)
        market_age = now - float(self.market_price_cache_at.get(mint, 0) or 0)
        if market_price > 0 and market_age <= 3:
            return market_price

        try:
            market = self.market.token_market(mint)
            candidate = float((market or {}).get("price_usd") or 0)
            if candidate > 0:
                self.market_price_cache[mint] = candidate
                self.market_price_cache_at[mint] = now
                return candidate
        except Exception:
            logger.debug("Market price unavailable for %s", mint, exc_info=True)

        # When a coin is not indexed by a market-data provider, this falls back to
        # the last observed trade or prior mark; the dashboard still exposes the mark
        # as an estimate rather than a guaranteed executable exit price.
        if feed_price > 0:
            return feed_price
        if market_price > 0:
            return market_price
        return max(0.0, float(fallback or 0))

    def _current_positions_value(self) -> float:
        value = 0.0
        for pos in self.store.list_positions():
            price = self._mark_price(pos["mint"], float(pos["last_price_usd"]))
            value += raw_to_units(pos["amount_raw"], int(pos["decimals"])) * price
        return value

    def _live_equity(self) -> float:
        assert self.wallet_address
        sol_price = self.market.sol_price_usd()
        sol_value = self.rpc.sol_balance_lamports(self.wallet_address) / 1_000_000_000 * sol_price
        return sol_value + self._current_positions_value()

    def equity(self) -> float:
        if self.s.mode == "live":
            return self._live_equity()
        cash = float(self.store.get_state("paper_cash", str(self.s.starting_paper_cash_usd)))
        return cash + self._current_positions_value()

    def _initialize_live_baseline(self):
        if self.s.mode != "live" or self.store.get_state("live_equity_initialized", "0") == "1":
            return
        value = self._live_equity()
        self.store.set_state("initial_equity", value)
        self.store.set_state("live_equity_initialized", "1")
        logger.warning("Recorded live starting equity baseline %.4f USD", value)

    def _refresh_risk_state(self, equity: float):
        self.store.reset_day_if_needed(equity)
        if self.store.get_state("halted", "0") == "1":
            return
        initial = float(self.store.get_state("initial_equity", str(self.s.starting_paper_cash_usd)))
        total_loss = initial - equity
        if total_loss >= self.s.total_loss_limit_usd:
            self.store.set_state("halted", "1")
            self.store.set_state("liquidate_all_pending", "1")
            self.store.set_state("halt_reason", f"Total loss limit reached: drawdown {total_loss:.2f} USD")
            logger.critical("%s; new entries blocked and liquidation will be attempted", self.store.get_state("halt_reason"))
            return
        daily_loss = float(self.store.get_state("day_start_equity", str(equity))) - equity
        if daily_loss >= self.s.daily_loss_limit_usd and self.store.get_state("daily_paused", "0") != "1":
            self.store.set_state("daily_paused", "1")
            logger.error("Daily drawdown limit reached (%.2f USD); entries paused until next UTC day", daily_loss)

    def _can_enter(self, equity: float) -> tuple[bool, str]:
        if self.store.get_state("experiment_expired", "0") == "1":
            return False, "Seven-day experiment is over"
        if self.store.get_state("copy_paused", "0") == "1":
            return False, "Copying is paused from the dashboard"
        if self.store.get_state("manual_review_halt", "0") == "1":
            return False, "Manual review halt active: " + self.store.get_state("manual_review_reason", "")
        if self.store.get_state("halted", "0") == "1":
            return False, "Overall loss halt active: " + self.store.get_state("halt_reason")
        if self.store.get_state("daily_paused", "0") == "1":
            return False, "Daily loss pause active"
        if self.store.daily_entries() >= self.s.max_entries_per_day:
            return False, "Daily entry cap reached"
        if len(self.store.list_positions()) >= self.s.max_open_positions:
            return False, "Open position cap reached"
        if equity <= 0:
            return False, "Equity is zero or less"
        exposure = self._current_positions_value()
        room = self.s.max_total_exposure_usd - exposure
        spend = min(self.s.max_entry_usd, room)
        if spend < 0.50:
            return False, "Too little exposure headroom for a practical trade"
        if self.s.mode == "paper":
            cash = float(self.store.get_state("paper_cash", "0"))
            if cash < spend + self.s.paper_swap_cost_usd:
                return False, "Insufficient paper cash after simulated costs"
        else:
            assert self.wallet_address
            sol_price = self.market.sol_price_usd()
            spend_lamports = int(spend / sol_price * 1_000_000_000)
            balance = self.rpc.sol_balance_lamports(self.wallet_address)
            reserve = int((self.s.min_sol_reserve + 0.002 + self.s.pumpportal_priority_fee_sol) * 1_000_000_000)
            if balance < spend_lamports + reserve:
                return False, "SOL balance insufficient for entry plus fee reserve"
        return True, "allowed"

    def buy(self, signal: dict):
        mint = signal["mint"]
        if self.store.get_position(mint):
            return
        sol_price = self.market.sol_price_usd()
        market = self.market.token_market(mint)
        observed_price = float(signal.get("observed_price_usd") or 0)
        if observed_price <= 0 and market:
            observed_price = float(market.get("price_usd") or 0)
        if observed_price <= 0:
            logger.info("Reject Pump.fun buy %s: no reliable trade or market price yet", mint)
            return

        curve_liquidity = float(signal.get("curve_liquidity_usd") or 0)
        pair_liquidity = float(market.get("liquidity_usd") or 0) if market else 0.0
        actual_liquidity = max(curve_liquidity, pair_liquidity)
        is_realtime_signal = signal.get("source") == "account_trade"
        venue = str(signal.get("venue", "pump"))
        unindexed_pump_entry = (
            self.s.allow_unindexed_pump_curve
            and is_realtime_signal
            and venue in {"pump", "pump-amm"}
            and (market is None or int(market.get("pair_age_seconds") or 0) < self.s.min_pair_age_seconds)
        )
        if actual_liquidity < self.s.min_liquidity_usd and not unindexed_pump_entry:
            logger.info("Reject Pump.fun buy %s: verified/observed liquidity %.0f USD below %.0f USD",
                        mint, actual_liquidity, self.s.min_liquidity_usd)
            return
        if market and int(market.get("pair_age_seconds") or 0) < self.s.min_pair_age_seconds and not unindexed_pump_entry:
            logger.info("Reject Pump.fun buy %s: pair age below filter", mint)
            return
        if unindexed_pump_entry:
            logger.warning(
                "HIGH-RISK unindexed Pump launch entry for %s: no verified indexed liquidity; "
                "fixed entry/exposure caps apply, but exit liquidity is not assured",
                mint,
            )

        equity = self.equity()
        self._refresh_risk_state(equity)
        allowed, reason = self._can_enter(equity)
        if not allowed:
            logger.info("Reject buy %s: %s", mint, reason)
            return
        spend = min(self.s.max_entry_usd, self.s.max_total_exposure_usd - self._current_positions_value())
        if spend < 0.50:
            return

        decimals = int(signal.get("decimals") or 6)
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        venue = str(signal.get("venue", "pump"))
        if venue not in {"pump", "pump-amm"}:
            venue = "pump"

        if self.s.mode == "paper":
            cost = self.s.paper_swap_cost_usd
            raw_out = int(spend / observed_price * (10 ** decimals))
            if raw_out <= 0:
                logger.info("Reject %s: estimated output rounds to zero", mint)
                return
            cash = float(self.store.get_state("paper_cash", "0"))
            self.store.set_state("paper_cash", cash - spend - cost)
            tx_sig = "PAPER-" + signal["signature"][:20]
            cost_basis = spend + cost
            effective_price = cost_basis / raw_to_units(raw_out, decimals)
            used_pool = venue
        else:
            assert self.keypair is not None and self.wallet_address is not None
            result = self.executor.trade(
                owner=self.wallet_address, keypair=self.keypair, mint=mint, action="buy",
                amount=round(spend / sol_price, 9), denominated_in_sol=True,
                slippage_bps=self.s.slippage_bps, preferred_pool=venue,
            )
            tx_sig = result["signature"]
            used_pool = result["pool"]
            raw_out = int(result["total_output_amount"])
            if raw_out <= 0:
                raise RuntimeError("PumpPortal confirmed a buy without a positive token balance delta")
            effective_spend = max(0, -int(result["sol_delta_lamports"])) / 1_000_000_000 * sol_price
            cost_basis = effective_spend if effective_spend > 0 else spend
            effective_price = cost_basis / raw_to_units(raw_out, decimals)

        position = {
            "mint": mint, "decimals": decimals, "amount_raw": str(raw_out),
            "entry_price_usd": effective_price, "initial_amount_raw": str(raw_out),
            "peak_price_usd": observed_price, "tp1_done": 0, "tp2_done": 0,
            "opened_at": timestamp, "last_price_usd": observed_price,
            "invested_usd": cost_basis, "source_wallet": signal["wallet"],
            "source_signature": signal["signature"],
        }
        self.store.upsert_position(position)
        self.store.set_state(f"pool:{mint}", used_pool)
        self.store.increment_daily_entries()
        self.store.add_trade(mode=self.s.mode, action="BUY", mint=mint, amount_raw=str(raw_out),
                             price_usd=effective_price, value_usd=cost_basis, reason=f"copied Pump.fun {used_pool} buy",
                             source_wallet=signal["wallet"], source_signature=signal["signature"], tx_signature=tx_sig)
        self.price_cache[mint] = observed_price
        self.price_cache_at[mint] = time.time()
        logger.warning("BUY %s raw=%s cost=%.4f USD leader=%s pool=%s", mint, raw_out, cost_basis, signal["wallet"], used_pool)
        self._refresh_feed_subscriptions()
        if self.s.mode == "live":
            logger.warning("Live transaction confirmed: https://solscan.io/tx/%s", tx_sig)

    def sell(self, mint: str, amount_raw: int, reason: str,
             source_wallet: str | None = None, source_signature: str | None = None) -> bool:
        position = self.store.get_position(mint)
        if not position:
            return False
        full_exit = (reason in {"marked loss stop", "trailing stop"} or
                     reason.startswith("followed wallet ") or reason.startswith("experiment expiry") or
                     reason.startswith("total loss limit") or reason.startswith("pending exit:"))
        pending_key = f"exit_pending:{mint}"
        if full_exit:
            self.store.set_state(pending_key, reason)
        current_raw = int(position["amount_raw"])
        requested_raw = min(max(0, int(amount_raw)), current_raw)
        if requested_raw <= 0:
            return False
        decimals = int(position["decimals"])
        market = self.market.token_market(mint)
        mark_price = float(market["price_usd"]) if market else float(position["last_price_usd"])
        if mark_price <= 0:
            return False

        if self.s.mode == "paper":
            sold_raw = requested_raw
            units_sold = raw_to_units(sold_raw, decimals)
            proceeds = max(0.0, units_sold * mark_price - self.s.paper_swap_cost_usd)
            cash = float(self.store.get_state("paper_cash", "0"))
            self.store.set_state("paper_cash", cash + proceeds)
            tx_sig = "PAPER-SELL"
        else:
            assert self.keypair is not None and self.wallet_address is not None
            available = self.executor.token_balance_raw(self.wallet_address, mint)
            requested_raw = min(requested_raw, available)
            if requested_raw <= 0:
                logger.error("No live token balance for %s; retaining position for manual reconciliation", mint)
                return False
            units_requested = raw_to_units(requested_raw, decimals)
            preferred = self.store.get_state(f"pool:{mint}", "pump")
            result = self.executor.trade(
                owner=self.wallet_address, keypair=self.keypair, mint=mint, action="sell",
                amount=units_requested, denominated_in_sol=False,
                slippage_bps=self.s.slippage_bps, preferred_pool=preferred,
            )
            tx_sig = result["signature"]
            sold_raw = abs(int(result["token_delta_raw"]))
            sol_received = max(0, int(result["sol_delta_lamports"])) / 1_000_000_000
            proceeds = sol_received * self.market.sol_price_usd()
            units_sold = raw_to_units(sold_raw, decimals)
            if sold_raw <= 0:
                raise RuntimeError("PumpPortal confirmed sell without a token balance decrease")

        basis = float(position["invested_usd"]) * (sold_raw / current_raw)
        realized_pnl = proceeds - basis
        remaining = max(0, current_raw - sold_raw)
        remaining_basis = max(0.0, float(position["invested_usd"]) - basis)
        self.store.set_state("daily_realized_pnl", float(self.store.get_state("daily_realized_pnl", "0")) + realized_pnl)
        self.store.add_trade(mode=self.s.mode, action="SELL", mint=mint, amount_raw=str(sold_raw),
                             price_usd=proceeds / units_sold if units_sold else mark_price,
                             value_usd=proceeds, pnl_usd=realized_pnl, reason=reason,
                             source_wallet=position.get("source_wallet") or source_wallet,
                             source_signature=source_signature or position.get("source_signature"), tx_signature=tx_sig)
        if remaining == 0:
            self.store.delete_position(mint)
            self.store.set_state(f"pool:{mint}", "")
        else:
            position["amount_raw"] = str(remaining)
            position["invested_usd"] = remaining_basis
            position["last_price_usd"] = mark_price
            self.store.upsert_position(position)
        if remaining == 0:
            self.price_cache.pop(mint, None)
            self.price_cache_at.pop(mint, None)
        else:
            self.price_cache[mint] = mark_price
            self.price_cache_at[mint] = time.time()
        self.store.set_state(pending_key, "")
        self._refresh_feed_subscriptions()
        logger.warning("SELL %s raw=%s proceeds=%.4f PnL=%.4f reason=%s", mint, sold_raw, proceeds, realized_pnl, reason)
        if self.s.mode == "live":
            logger.warning("Live transaction confirmed: https://solscan.io/tx/%s", tx_sig)
        return True

    def manage_positions(self):
        for original in self.store.list_positions():
            mint = original["mint"]
            position = self.store.get_position(mint)
            if not position:
                continue
            pending = self.store.get_state(f"exit_pending:{mint}", "")
            if pending:
                self.sell(mint, int(position["amount_raw"]), f"pending exit: {pending}")
                continue
            price = self._mark_price(mint, float(position["last_price_usd"]))
            if price <= 0:
                continue
            peak = max(float(position["peak_price_usd"]), price)
            position["peak_price_usd"], position["last_price_usd"] = peak, price
            self.store.upsert_position(position)
            current_raw = int(position["amount_raw"])
            initial_raw = max(1, int(position["initial_amount_raw"]))
            entry = float(position["entry_price_usd"])
            if price <= entry * (1 - self.s.max_marked_loss_pct):
                self.sell(mint, current_raw, "marked loss stop")
                continue
            if peak > entry and price <= peak * (1 - self.s.trailing_stop_pct):
                self.sell(mint, current_raw, "trailing stop")
                continue
            if not int(position["tp1_done"]) and price >= entry * (1 + self.s.take_profit_1_pct):
                qty = min(current_raw, int(initial_raw * self.s.take_profit_1_sell_fraction))
                if self.sell(mint, qty, "take profit 1 (+30% target)"):
                    pos = self.store.get_position(mint)
                    if pos:
                        pos["tp1_done"] = 1
                        self.store.upsert_position(pos)
                continue
            if not int(position["tp2_done"]) and price >= entry * (1 + self.s.take_profit_2_pct):
                qty = min(current_raw, int(initial_raw * self.s.take_profit_2_sell_fraction))
                if self.sell(mint, qty, "take profit 2 (+60% target)"):
                    pos = self.store.get_position(mint)
                    if pos:
                        pos["tp2_done"] = 1
                        self.store.upsert_position(pos)

    def process_signal(self, signal: dict):
        if signal.get("wallet") not in self.active_wallets:
            return
        self.store.add_signal(signal["wallet"], signal["signature"], signal["mint"], signal["side"],
                              signal["amount_ui"], signal["decimals"])
        if self.wallet_address and signal["wallet"] == self.wallet_address:
            return
        if signal["side"] == "buy":
            self.signal_stats["buy_signals"] += 1
            self.buy(signal)
        elif signal["side"] == "sell" and self.store.get_position(signal["mint"]):
            self.signal_stats["sell_signals"] += 1
            position = self.store.get_position(signal["mint"])
            our_raw = int(position["amount_raw"])
            leader_remaining = int(signal.get("leader_post_raw", 0))
            leader_sold = int(signal.get("amount_raw", 0))
            if leader_remaining <= 0:
                our_sell_raw = our_raw
            else:
                denominator = leader_sold + leader_remaining
                fraction = min(1.0, leader_sold / denominator) if denominator > 0 else 1.0
                our_sell_raw = min(our_raw, max(1, int(our_raw * fraction)))
            self.sell(signal["mint"], our_sell_raw,
                      f"followed wallet {signal['wallet']} partial/full sell on {signal['venue']}",
                      signal["wallet"], signal["signature"])
        elif signal["side"] == "sell":
            self.signal_stats["sell_without_position"] += 1
            logger.info("Leader sell observed but no matching paper position is open for mint=%s; no sell copied",
                        signal["mint"][:8])

    def poll_wallet(self, wallet: str) -> int:
        sigs = self.rpc.signatures(wallet, limit=1000)
        if not sigs:
            return 0
        latest = sigs[0].get("signature")
        cursor = self.store.get_cursor(wallet)
        if cursor is None:
            self.store.set_cursor(wallet, latest)
            logger.info("Initial sync %s at %s; old activity will not be copied", wallet, latest)
            return 0
        new_rows = []
        cursor_found = False
        for row in sigs:
            if row.get("signature") == cursor:
                cursor_found = True
                break
            new_rows.append(row)
        if not cursor_found and cursor != latest and len(sigs) >= 1000:
            logger.error("More than 1000 signatures may have arrived for %s between polls; oldest missed signals cannot be recovered by this pass", wallet)

        # Bound RPC bursts. Process new signatures oldest-to-newest and advance the
        # cursor only through successfully inspected rows, so a 429 never skips newer trades.
        try:
            max_per_poll = max(1, min(50, int(__import__("os").getenv("SOLANA_MAX_SIGNATURES_PER_WALLET_POLL", "8"))))
        except ValueError:
            max_per_poll = 8
        chronological = list(reversed(new_rows))
        batch = chronological[:max_per_poll]
        cursor_candidate = cursor
        handled = 0
        rate_limited = False
        for row in batch:
            signature = row.get("signature")
            if not signature:
                continue
            dedupe_key = self._dedupe_key(wallet, signature)
            if self.store.signature_seen(dedupe_key):
                cursor_candidate = signature
                continue
            try:
                signals = self.detect_pump_signal(wallet, signature)
            except SolanaRPCRateLimited:
                logger.warning("RPC fallback paused for %s; keeping cursor at the last inspected transaction.", wallet[:8])
                rate_limited = True
                break
            for signal in signals:
                self.process_signal(signal)
            self.store.mark_signature_seen(dedupe_key, wallet)
            cursor_candidate = signature
            handled += 1

        if not rate_limited and len(batch) == len(chronological):
            # All new signatures present in this response have now been inspected.
            cursor_candidate = latest
        if cursor_candidate and cursor_candidate != cursor:
            self.store.set_cursor(wallet, cursor_candidate)
        return handled

    def status(self):
        equity = self.equity()
        logger.warning("MODE=%s wallet=%s equity=%.4f USD initial=%.4f halted=%s reason=%s",
                       self.s.mode, self.wallet_address or "(paper)", equity,
                       float(self.store.get_state("initial_equity", "10")),
                       self.store.get_state("halted", "0"), self.store.get_state("halt_reason", ""))
        logger.warning("Copy enabled=%s window=%s active traders=%d positions=%d entries_today=%d",
                       self.auto_copy_enabled, self.selected_window, len(self.active_wallets),
                       len(self.store.list_positions()), self.store.daily_entries())
        if self.last_leaderboard_error:
            logger.warning("Leaderboard status: %s", self.last_leaderboard_error)
        if self.feed:
            logger.warning("PumpPortal data feed connected=%s error=%s",
                           self.feed.connected.is_set(), self.feed.last_error)
        for p in self.store.list_positions():
            mark = float(self.price_cache.get(p["mint"], p["last_price_usd"]))
            logger.warning("POSITION %s qty_raw=%s entry=%.8g mark=%.8g pool=%s",
                           p["mint"], p["amount_raw"], p["entry_price_usd"], mark,
                           self.store.get_state(f"pool:{p['mint']}", "pump"))
        for t in self.store.recent_trades(10):
            logger.warning("TRADE #%s %s %s value=%.4f pnl=%.4f %s",
                           t["id"], t["action"], t["mint"], t["value_usd"], t["pnl_usd"], t["reason"])

    def _poll_active_wallets(self):
        if self.rpc.backoff_remaining > 0:
            logger.debug("Skipping RPC fallback during rate-limit cooldown (%.0fs remaining).",
                         self.rpc.backoff_remaining)
            return
        for wallet in sorted(self.active_wallets):
            try:
                self.poll_wallet(wallet)
            except SolanaRPCRateLimited as exc:
                logger.warning("RPC fallback paused: %s", exc)
                break
            except Exception as exc:
                logger.error("RPC fallback monitor failed for %s: %s", wallet[:8], exc)
                if self.s.mode == "live":
                    self.store.set_state("manual_review_halt", "1")
                    self.store.set_state(
                        "manual_review_reason",
                        f"RPC wallet monitoring error for {wallet}; reconcile transactions before resuming",
                    )

    def run(self, once=False):
        logger.warning("Pump.fun bot mode=%s; automatic leaderboard discovery=%s",
                       self.s.mode, "configured" if self.leaderboard_client.ready else "not configured")
        if self.s.mode == "live":
            logger.warning("Live wallet: %s", self.wallet_address)
            self._initialize_live_baseline()

        self.refresh_leaderboards(force=True)
        self._refresh_feed_subscriptions()
        if not once:
            self.dashboard = DashboardServer(self.s.dashboard_port, self.dashboard_snapshot, self.commands)
            self.dashboard.start()
        if self.feed and not self.feed.is_alive() and not once:
            self.feed.start()
        if self.logs_feed and not self.logs_feed.is_alive() and not once:
            self.logs_feed.start()

        started_at = self.store.get_state("experiment_started_at", "")
        if not once and not started_at:
            started_at = str(time.time())
            self.store.set_state("experiment_started_at", started_at)
            logger.warning("Seven-day experiment timer started.")
        self._refresh_risk_state(self.equity())
        next_rpc_poll = 0.0
        next_position_check = 0.0

        try:
            while True:
                try:
                    self._process_dashboard_commands()
                    self._consume_feed_events()
                    self._consume_solana_log_events()
                    now = time.time()

                    if self.leaderboard_client.ready and now - self.last_leaderboard_refresh >= self.s.leaderboard_refresh_seconds:
                        self.refresh_leaderboards(force=True)

                    feed_live = bool(self.feed and self.feed.connected.is_set())
                    if feed_live:
                        # An open WebSocket is not proof that the provider accepted
                        # account subscriptions. If no account-trade event arrives,
                        # use a slower RPC safety scan; signatures are de-duplicated.
                        last_activity = max(
                            float(self.feed.connected_at or 0),
                            float(self.feed.last_account_trade_at or 0),
                        )
                        fallback_interval = max(20, self.s.poll_seconds * 5)
                        if now >= next_rpc_poll and now - last_activity >= fallback_interval:
                            logger.warning(
                                "No PumpPortal account-trade events for %.0fs; running slower RPC fallback scan across %d wallets",
                                now - last_activity, len(self.active_wallets),
                            )
                            self._poll_active_wallets()
                            next_rpc_poll = now + fallback_interval
                    elif now >= next_rpc_poll:
                        self._poll_active_wallets()
                        next_rpc_poll = now + self.s.poll_seconds

                    started_at = self.store.get_state("experiment_started_at", "")
                    expired = self.store.get_state("experiment_expired", "0") == "1"
                    if not once and started_at and not expired:
                        try:
                            expired = (time.time() - float(started_at) >= self.s.experiment_duration_days * 86400)
                        except ValueError:
                            expired = False
                    if expired and self.store.get_state("experiment_expired", "0") != "1":
                        self.store.set_state("experiment_expired", "1")
                        self.auto_copy_enabled = False
                        self.store.set_state("auto_copy_enabled", "0")
                        self.store.set_state("copy_paused", "1")
                        self._refresh_feed_subscriptions()
                        logger.warning("Seven-day experiment expired; new entries disabled.")
                    if expired:
                        for pos in self.store.list_positions():
                            self.store.set_state(f"exit_pending:{pos['mint']}", "experiment expiry liquidation")

                    if now >= next_position_check:
                        self.manage_positions()
                        equity = self.equity()
                        self.last_equity_usd = equity
                        self._refresh_risk_state(equity)
                        if self.store.get_state("halted", "0") == "1":
                            self.auto_copy_enabled = False
                            self.store.set_state("auto_copy_enabled", "0")
                            self.store.set_state("copy_paused", "1")
                            self._refresh_feed_subscriptions()
                            for pos in self.store.list_positions():
                                self.store.set_state(f"exit_pending:{pos['mint']}", "total loss limit liquidation")
                            self.manage_positions()
                        self._refresh_feed_subscriptions()
                        self._publish_dashboard_state(equity)
                        open_count = len(self.store.list_positions())
                        logger.info("Heartbeat equity=%.4f positions=%d auto_copy=%s active_wallets=%d",
                                    equity, open_count, self.auto_copy_enabled, len(self.active_wallets))
                        if expired and open_count == 0:
                            logger.warning("Seven-day experiment finished; no tracked positions remain.")
                            break
                        next_position_check = now + 1.5

                    self._publish_dashboard_state(self.last_equity_usd)
                except KeyboardInterrupt:
                    logger.warning("Stopped by user.")
                    break
                except Exception:
                    logger.exception("Loop error; inspect logs before re-arming after any ambiguous transaction")
                    if self.s.mode == "live":
                        self.store.set_state("manual_review_halt", "1")
                        self.store.set_state(
                            "manual_review_reason",
                            "Runtime exception; reconcile wallet and transactions before resuming",
                        )
                    self.message = "Runtime error; inspect console. Live entries are halted pending reconciliation."

                if once:
                    break
                time.sleep(0.20)
        finally:
            if self.feed and self.feed.is_alive():
                self.feed.stop()
            if self.logs_feed and self.logs_feed.is_alive():
                self.logs_feed.stop()
            if self.dashboard:
                self.dashboard.stop()

    def close(self):
        self.store.close()


def main():
    parser = argparse.ArgumentParser(description="Pump.fun / PumpSwap wallet copy-trading bot")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="validate RPC/data endpoints without trading")
    group.add_argument("--once", action="store_true", help="one polling/management cycle")
    group.add_argument("--run", action="store_true", help="continuous monitoring (default)")
    group.add_argument("--status", action="store_true", help="show local ledger status")
    args = parser.parse_args()
    try:
        settings = PumpFunSettings.load()
        logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO),
                            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        bot = PumpFunBot(settings)
    except Exception as exc:
        print(f"Startup/configuration error: {exc}", file=sys.stderr)
        return 2
    try:
        if args.check:
            slot = bot.rpc.call("getSlot", [{"commitment":"confirmed"}])
            sol_price = bot.market.sol_price_usd()
            logger.warning("RPC OK slot=%s; SOL ref price %.4f USD; mode=%s",
                           slot, sol_price, settings.mode)
            logger.warning("Check mode does not trade.")
        elif args.status:
            bot.status()
        else:
            bot.run(once=args.once)
    except Exception:
        logger.exception("Fatal error; reconcile the wallet before restarting live mode")
        return 1
    finally:
        bot.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
