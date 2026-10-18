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
from pumpportal import PUMP_PROGRAM_ID, PUMPSWAP_PROGRAM_ID
from native_pump import (
    NativePumpExecutor,
    TransactionBuildFailed,
    TransactionExpiredNotLanded,
    TransactionOutcomeUncertain,
)
from solana_rpc import SolanaRPC, SolanaRPCRateLimited, SolanaTransactionNotReady
from pump_curve import fetch_pump_curve
from storage import Store
from token_discovery import TokenDiscoveryShadow

logger = logging.getLogger("pumpfun")


def raw_to_units(amount_raw: int | str, decimals: int) -> float:
    return int(amount_raw) / (10 ** decimals)


def native_build_retry_delay_seconds(detail: str) -> int:
    """Back off pre-submission builder failures without retrying every management tick."""
    normalized = str(detail or "").lower()
    if "unsupported pumpswap quote mint" in normalized or "token-quoted coin" in normalized:
        return 6 * 60 * 60
    # ATA/account metadata can be temporarily stale on an RPC; retry slowly, not
    # every 1.5 seconds. The position and pending exit stay recorded in the ledger.
    return 60


def _pubkey_string(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("pubkey", ""))
    return str(value)


class PumpFunBot:
    def __init__(self, settings: PumpFunSettings):
        self.s = settings
        # Used to prevent old persisted shadow entries from unexpectedly becoming
        # live orders after a restart or after live discovery is newly enabled.
        self.run_started_at = time.time()
        db_path = self.s.database_path
        if not db_path.is_absolute():
            db_path = Path(__file__).resolve().parent / db_path
        self.store = Store(db_path, self.s.starting_paper_cash_usd, mode=self.s.mode)
        self.rpc = SolanaRPC(self.s.rpc_url, self.s.request_timeout_seconds)
        # Aggressive launchpad filters; still require a tracked market pair for price marks.
        self.market = MarketData(self.s.request_timeout_seconds, self.s.min_liquidity_usd, self.s.min_pair_age_seconds)
        self.executor = None
        self.keypair = None
        self.wallet_address = None
        if self.s.mode == "live":
            self.keypair = NativePumpExecutor.load_keypair(self.s.keypair_path)
            self.wallet_address = str(self.keypair.pubkey())
            self.executor = NativePumpExecutor(
                self.rpc,
                self.s.rpc_url,
                self.s.request_timeout_seconds,
                self.s.pumpportal_priority_fee_sol,
                self.s.min_sol_reserve,
            )
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
        self.token_discovery = TokenDiscoveryShadow(
            self.s, self.rpc, self.market, self.store, self.feed
        )
        if self.feed:
            self.feed.set_discovery_subscriptions(self.token_discovery.enabled, [])
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

    def _activate_manual_review_halt(self, reason: str, *, append_reason: bool = False) -> None:
        """Latch a manual-review halt without losing the first actionable diagnosis."""
        already_halted = self.store.get_state("manual_review_halt", "0") == "1"
        existing = self.store.get_state("manual_review_reason", "").strip()
        incoming = " ".join(str(reason or "").split()).strip()
        if already_halted and existing:
            if append_reason and incoming and incoming not in existing:
                combined = f"{existing}; additionally: {incoming}"
            else:
                combined = existing
                if incoming and incoming not in existing:
                    logger.warning(
                        "Preserving existing manual-review reason while recording another issue: %s",
                        incoming[:240],
                    )
        else:
            combined = incoming
        self.store.set_state("manual_review_halt", "1")
        self.store.set_state("manual_review_reason", combined)

    def _copy_block_reason(self) -> str:
        """Explain durable safety gates that must be cleared before new copy entries."""
        if self.store.get_state("halted", "0") == "1":
            return "Overall loss halt active: " + self.store.get_state("halt_reason", "reconcile the account before resuming")
        if self.store.get_state("manual_review_halt", "0") == "1":
            return "Manual-review halt active: " + self.store.get_state(
                "manual_review_reason", "reconcile wallet and transactions before resuming"
            )
        if self.store.get_state("experiment_expired", "0") == "1":
            return "The seven-day experiment has expired."
        if self.store.get_state("daily_paused", "0") == "1":
            return "Daily loss pause is active until the UTC day resets."
        return ""

    def _refresh_feed_subscriptions(self) -> None:
        held_leaders = {
            str(p.get("source_wallet")) for p in self.store.list_positions()
            if p.get("source_wallet")
        }
        if (
            self.auto_copy_enabled
            and self.store.get_state("copy_paused", "0") != "1"
            and not self._copy_block_reason()
        ):
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
                "strategy": p.get("strategy", "leader_copy"),
            })
        trades = self.store.recent_trades(15)
        leaders = {}
        for period, rows in self.leaderboards.items():
            leaders[period] = [{
                "rank": item.get("rank"), "wallet": item.get("wallet"), "label": item.get("label", ""),
                "profit": item.get("profit"), "rank_score": item.get("rank_score"),
            } for item in rows]
        paused = self.store.get_state("copy_paused", "0") == "1"
        token_discovery_state = self.token_discovery.dashboard_snapshot()
        token_discovery_state.update({
            "live_enabled": bool(self.s.mode == "live" and self.s.token_discovery_live_enabled),
            "live_entry_cap_usd": min(0.50, float(self.s.token_discovery_max_entry_usd)),
            "live_open_positions": sum(
                1 for p in self.store.list_positions()
                if p.get("strategy", "leader_copy") == "token_discovery"
            ),
            "live_position_limit": 1,
        })
        state = {
            "mode": self.s.mode,
            "message": self.message,
            "leaderboard_error": self.last_leaderboard_error,
            "leaderboards": leaders,
            "selected_window": self.selected_window,
            "auto_copy": bool(self.auto_copy_enabled and not paused and not self._copy_block_reason()),
            "copy_paused": paused,
            "copy_block_reason": self._copy_block_reason(),
            "active_wallets": sorted(self.active_wallets),
            "equity_usd": self.last_equity_usd,
            "positions": positions,
            "trades": trades,
            "token_discovery": token_discovery_state,
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

    def _publish_dashboard_state_after_error(self) -> None:
        """Keep the dashboard current even when a loop iteration raises after a trade."""
        try:
            self._publish_dashboard_state(self.last_equity_usd)
        except Exception:
            # Preserve the original loop error; a secondary dashboard refresh error
            # must not hide it or terminate the monitor.
            logger.exception("Could not refresh dashboard state after loop error")

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
                block_reason = self._copy_block_reason()
                if block_reason:
                    self.auto_copy_enabled = False
                    self.store.set_state("auto_copy_enabled", "0")
                    self.store.set_state("copy_paused", "1")
                    self._refresh_feed_subscriptions()
                    self.message = "Auto-copy blocked: " + block_reason
                    logger.warning("%s", self.message)
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
            source = str(event.get("source") or "")
            if source == "token_discovery_new":
                self.token_discovery.on_new_token_event(event)
                continue
            if source == "token_trade":
                mint = str(event.get("mint") or "")
                price = self._event_price_usd(event)
                if mint and price and price > 0:
                    self.price_cache[mint] = price
                    self.price_cache_at[mint] = time.time()
                self.token_discovery.on_token_trade_event(event)
                continue
            price = self._event_price_usd(event)
            mint = str(event.get("mint") or "")
            if mint and price and price > 0:
                self.price_cache[mint] = price
                self.price_cache_at[mint] = time.time()
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
            event.setdefault("_first_seen_at", time.time())
            try:
                signals = self.detect_pump_signal(wallet, signature)
            except SolanaRPCRateLimited:
                self.solana_log_events.put(event)
                break
            except SolanaTransactionNotReady:
                retry_count = int(event.get("_retry_count", 0))
                age_seconds = time.time() - float(event["_first_seen_at"])
                if retry_count < 8 and age_seconds < 45:
                    event["_retry_count"] = retry_count + 1
                    self.solana_log_events.put(event)
                    if retry_count in (0, 2, 5):
                        logger.info(
                            "Transaction details not indexed yet for %s; retry %d/8",
                            signature[:10], retry_count + 1,
                        )
                    break
                logger.warning(
                    "Transaction details remained unavailable for %s after retries; "
                    "leaving it unmarked so signature polling can retry", signature[:10],
                )
                continue
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
        if tx is None or tx.get("meta") is None:
            raise SolanaTransactionNotReady(
                f"Transaction details are not indexed yet for signature {signature[:10]}"
            )
        if tx.get("meta", {}).get("err") is not None:
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
        signal = {
            "wallet": wallet, "signature": signature, "mint": mint, "side": side,
            "amount_ui": abs(float(data["delta_ui"])),
            "amount_raw": abs(int(data["delta_raw"])),
            "decimals": int(data["decimals"]),
            "leader_pre_raw": pre_raw, "leader_post_raw": post_raw,
            "native_delta_sol": sol_delta, "block_time": tx.get("blockTime"),
            "venue": venue,
        }
        # New Pump bonding curves often have no external market index yet. Read
        # their on-chain reserves for a spot-price/liquidity estimate instead of
        # emitting an unpriced RPC signal that buy() must always reject.
        if venue == "pump":
            try:
                curve = fetch_pump_curve(self.rpc, mint)
                if curve is not None and not curve.complete:
                    sol_price = self.market.sol_price_usd()
                    curve_price = curve.price_usd(sol_price, int(data["decimals"]))
                    if curve_price is not None:
                        signal["observed_price_usd"] = curve_price
                        signal["curve_liquidity_usd"] = curve.liquidity_usd(sol_price)
                        signal["curve_state_verified"] = True
            except SolanaRPCRateLimited:
                raise
            except Exception as exc:
                logger.debug("Bonding-curve quote unavailable for %s (%s)", mint[:8], type(exc).__name__)
        return [signal]

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

    def _can_enter(
        self, equity: float, *, strategy: str = "leader_copy",
        entry_cap_usd: float | None = None,
    ) -> tuple[bool, str]:
        if self.store.get_state("experiment_expired", "0") == "1":
            return False, "Seven-day experiment is over"
        if strategy != "token_discovery" and self.store.get_state("copy_paused", "0") == "1":
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
        configured_cap = self.s.max_entry_usd if entry_cap_usd is None else float(entry_cap_usd)
        if strategy == "token_discovery":
            configured_cap = min(0.50, configured_cap, float(self.s.token_discovery_max_entry_usd))
        spend = min(self.s.max_entry_usd, configured_cap, room)
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

    def _native_build_retry_deferred(self, mint: str) -> bool:
        if self.s.mode != "live":
            return False
        try:
            retry_at = float(self.store.get_state(f"native_build_retry_after:{mint}", "0") or 0)
        except (TypeError, ValueError):
            return False
        return retry_at > time.time()

    def _defer_native_build_retry(self, mint: str, detail: str) -> int:
        delay = native_build_retry_delay_seconds(detail)
        self.store.set_state(f"native_build_retry_after:{mint}", str(time.time() + delay))
        self.store.set_state(f"native_build_failure:{mint}", " ".join(str(detail).split())[:500])
        return delay

    def _handle_expired_not_landed_trade(
        self,
        exc: TransactionExpiredNotLanded,
        *,
        source_wallet: str | None = None,
        source_signature: str | None = None,
        reason: str = "",
    ) -> None:
        """Record a verified non-landed transaction without halting or replaying its source signal."""
        record = {
            "signature": exc.signature,
            "action": exc.action,
            "mint": exc.mint,
            "detail": exc.detail,
            "source_wallet": source_wallet or "",
            "source_signature": source_signature or "",
            "resolved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "resolution": "blockhash_expired_status_history_absent_token_balance_unchanged",
        }
        self.store.set_state(
            f"reconciled_not_landed_trade:{exc.mint}:{exc.signature}",
            json.dumps(record, sort_keys=True, separators=(",", ":")),
        )
        # Consume this leader event even though our transaction did not land. The
        # policy is to follow future signals, not replay a stale leader trade.
        if source_wallet and source_signature:
            self.store.mark_signature_seen(
                self._dedupe_key(source_wallet, source_signature), source_wallet
            )

        # A followed-wallet sell temporarily creates exit_pending before submission.
        # If that exact source-driven sell is verified not to have landed, remove
        # only its own pending marker so position management cannot turn it into an
        # unintended full liquidation on the next cycle. Preserve unrelated exits.
        if exc.action == "sell" and reason.startswith("followed wallet "):
            pending_key = f"exit_pending:{exc.mint}"
            if self.store.get_state(pending_key, "") == reason:
                self.store.set_state(pending_key, "")

        logger.warning(
            "Transaction expired and reconciled as NOT LANDED: action=%s mint=%s signature=%s; "
            "position unchanged and source signal consumed",
            exc.action, exc.mint, exc.signature,
        )

    def _handle_uncertain_trade(
        self,
        exc: TransactionOutcomeUncertain,
        *,
        source_wallet: str | None = None,
        source_signature: str | None = None,
    ) -> None:
        """Persist ambiguous submitted trades and fail closed without replaying the source signal."""
        record = {
            "signature": exc.signature,
            "action": exc.action,
            "mint": exc.mint,
            "detail": exc.detail,
            "source_wallet": source_wallet or "",
            "source_signature": source_signature or "",
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.store.set_state(
            f"unresolved_trade:{exc.mint}",
            json.dumps(record, sort_keys=True, separators=(",", ":")),
        )
        self._activate_manual_review_halt(
            f"{exc.action.upper()} outcome unresolved for mint {exc.mint}; "
            f"signature {exc.signature}; reconcile wallet/transaction before resuming",
            append_reason=True,
        )
        self.auto_copy_enabled = False
        self.store.set_state("auto_copy_enabled", "0")
        self.store.set_state("copy_paused", "1")

        # Consume the triggering leader event so clearing the halt later cannot
        # replay the same stale signal. poll_wallet also advances its cursor.
        if source_wallet and source_signature:
            self.store.mark_signature_seen(
                self._dedupe_key(source_wallet, source_signature), source_wallet
            )

        try:
            self._refresh_feed_subscriptions()
        except Exception:
            logger.exception("Could not refresh subscriptions after uncertain transaction outcome")

        logger.critical(
            "Manual reconciliation required: action=%s mint=%s signature=%s; "
            "no automatic retry will be attempted for this mint",
            exc.action, exc.mint, exc.signature,
        )

    def buy(
        self, signal: dict, *, strategy: str = "leader_copy",
        entry_cap_usd: float | None = None,
    ):
        mint = signal["mint"]
        if strategy == "token_discovery":
            if (
                self.s.mode != "live"
                or not self.s.token_discovery_enabled
                or not self.s.token_discovery_live_enabled
                or signal.get("source") != "token_discovery"
                or signal.get("fresh_security_verified") is not True
            ):
                logger.warning("Reject discovery buy %s: live discovery opt-in or fresh security verification missing", mint[:8])
                return
            discovery_positions = [
                p for p in self.store.list_positions()
                if p.get("strategy", "leader_copy") == "token_discovery"
            ]
            if discovery_positions:
                logger.info("Reject discovery buy %s: one live discovery position is already open", mint[:8])
                return
            entry_cap_usd = min(
                0.50, float(self.s.token_discovery_max_entry_usd),
                float(entry_cap_usd if entry_cap_usd is not None else self.s.token_discovery_max_entry_usd),
            )
            if entry_cap_usd <= 0:
                logger.warning("Reject discovery buy %s: discovery entry cap is zero", mint[:8])
                return
        elif strategy != "leader_copy":
            logger.warning("Reject buy %s: unsupported entry strategy %s", mint[:8], strategy)
            return
        source_wallet = str(signal.get("wallet") or "") or None
        source_signature = str(signal.get("signature") or "") or None
        if self.store.get_state(f"unresolved_trade:{mint}", ""):
            logger.warning(
                "Skipping buy for %s because an earlier transaction outcome remains unresolved",
                mint[:8],
            )
            return
        if self.store.get_position(mint):
            return
        if self._native_build_retry_deferred(mint):
            logger.debug("Skipping repeated build for mint=%s during pre-submission cooldown", mint[:8])
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
        if strategy == "token_discovery" and curve_liquidity < self.s.token_discovery_min_liquidity_usd:
            logger.info(
                "Reject discovery buy %s: fresh real curve liquidity %.0f USD below %.0f USD",
                mint[:8], curve_liquidity, self.s.token_discovery_min_liquidity_usd,
            )
            return
        is_realtime_signal = signal.get("source") == "account_trade"
        curve_state_verified = bool(signal.get("curve_state_verified"))
        venue = str(signal.get("venue", "pump"))
        # Keep the legacy PumpPortal exception only for account-trade events.
        # RPC notifications may bypass a young/unindexed pair-age filter only
        # when an on-chain curve snapshot independently confirms sufficient liquidity.
        allow_unindexed_account_trade = (
            self.s.allow_unindexed_pump_curve and is_realtime_signal
            and venue in {"pump", "pump-amm"}
        )
        allow_verified_curve_age_bypass = (
            self.s.allow_unindexed_pump_curve and curve_state_verified
            and curve_liquidity >= self.s.min_liquidity_usd
            and venue == "pump"
        )
        unindexed_pump_entry = (
            (allow_unindexed_account_trade or allow_verified_curve_age_bypass)
            and (market is None or int(market.get("pair_age_seconds") or 0) < self.s.min_pair_age_seconds)
        )
        if actual_liquidity < self.s.min_liquidity_usd and not allow_unindexed_account_trade:
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
        allowed, reason = self._can_enter(
            equity, strategy=strategy, entry_cap_usd=entry_cap_usd
        )
        if not allowed:
            logger.info("Reject buy %s: %s", mint, reason)
            return
        effective_entry_cap = self.s.max_entry_usd
        if strategy == "token_discovery":
            effective_entry_cap = min(
                0.50, float(self.s.token_discovery_max_entry_usd),
                float(entry_cap_usd if entry_cap_usd is not None else self.s.token_discovery_max_entry_usd),
            )
        spend = min(effective_entry_cap, self.s.max_total_exposure_usd - self._current_positions_value())
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
            try:
                result = self.executor.trade(
                    owner=self.wallet_address, keypair=self.keypair, mint=mint, action="buy",
                    amount=round(spend / sol_price, 9), denominated_in_sol=True,
                    slippage_bps=self.s.slippage_bps, preferred_pool=venue,
                )
            except TransactionExpiredNotLanded as exc:
                self._handle_expired_not_landed_trade(
                    exc,
                    source_wallet=source_wallet,
                    source_signature=source_signature,
                )
                return
            except TransactionOutcomeUncertain as exc:
                self._handle_uncertain_trade(
                    exc,
                    source_wallet=source_wallet,
                    source_signature=source_signature,
                )
                return
            except TransactionBuildFailed as exc:
                delay = self._defer_native_build_retry(mint, str(exc))
                logger.warning(
                    "Skip copied buy for %s: %s. No transaction was signed or submitted; "
                    "retry for this mint is deferred %ds.",
                    mint[:8], " ".join(str(exc).split())[:220], delay,
                )
                return
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
            "invested_usd": cost_basis, "source_wallet": source_wallet,
            "source_signature": source_signature, "strategy": strategy,
        }
        self.store.upsert_position(position)
        self.store.set_state(f"pool:{mint}", used_pool)
        self.store.increment_daily_entries()
        trade_reason = (
            f"independent token discovery {used_pool} buy"
            if strategy == "token_discovery"
            else f"copied Pump.fun {used_pool} buy"
        )
        self.store.add_trade(
            mode=self.s.mode, action="BUY", mint=mint, amount_raw=str(raw_out),
            price_usd=effective_price, value_usd=cost_basis, reason=trade_reason,
            source_wallet=source_wallet, source_signature=source_signature,
            tx_signature=tx_sig, strategy=strategy,
        )
        self.price_cache[mint] = observed_price
        self.price_cache_at[mint] = time.time()
        logger.warning(
            "BUY %s raw=%s cost=%.4f USD strategy=%s source=%s pool=%s",
            mint, raw_out, cost_basis, strategy, source_wallet or "token-discovery", used_pool,
        )
        self._refresh_feed_subscriptions()
        if self.s.mode == "live":
            logger.warning("Live transaction confirmed: https://solscan.io/tx/%s", tx_sig)

    def sell(self, mint: str, amount_raw: int, reason: str,
             source_wallet: str | None = None, source_signature: str | None = None) -> bool:
        if self.store.get_state(f"unresolved_trade:{mint}", ""):
            logger.debug(
                "Skipping sell retry for %s because an earlier transaction outcome remains unresolved",
                mint[:8],
            )
            return False
        position = self.store.get_position(mint)
        if not position:
            return False
        if self._native_build_retry_deferred(mint):
            logger.debug("Sell builder for mint=%s is in pre-submission cooldown", mint[:8])
            return False
        # Position management wraps a persisted exit reason for dispatch. Unwrap
        # repeated wrappers before storing/logging it again so failed retries do not
        # accumulate "pending exit: pending exit: ..." indefinitely.
        was_pending_exit = reason.startswith("pending exit:")
        while reason.startswith("pending exit:"):
            reason = reason[len("pending exit:"):].strip()
        full_exit = (was_pending_exit or reason in {"marked loss stop", "trailing stop"} or
                     reason.startswith("followed wallet ") or reason.startswith("experiment expiry") or
                     reason.startswith("total loss limit"))
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
            try:
                result = self.executor.trade(
                    owner=self.wallet_address, keypair=self.keypair, mint=mint, action="sell",
                    amount=units_requested, denominated_in_sol=False,
                    slippage_bps=self.s.slippage_bps, preferred_pool=preferred,
                    amount_raw=requested_raw,
                )
            except TransactionExpiredNotLanded as exc:
                self._handle_expired_not_landed_trade(
                    exc,
                    source_wallet=source_wallet,
                    source_signature=source_signature,
                    reason=reason,
                )
                return False
            except TransactionOutcomeUncertain as exc:
                self._handle_uncertain_trade(
                    exc,
                    source_wallet=source_wallet,
                    source_signature=source_signature,
                )
                return False
            except TransactionBuildFailed as exc:
                delay = self._defer_native_build_retry(mint, str(exc))
                logger.error(
                    "Could not build sell for %s; no transaction was signed or submitted. "
                    "Tracked position and pending exit are retained; retry deferred %ds: %s",
                    mint[:8], delay, " ".join(str(exc).split())[:220],
                )
                return False
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
        strategy = position.get("strategy", "leader_copy")
        self.store.add_trade(
            mode=self.s.mode, action="SELL", mint=mint, amount_raw=str(sold_raw),
            price_usd=proceeds / units_sold if units_sold else mark_price,
            value_usd=proceeds, pnl_usd=realized_pnl, reason=reason,
            source_wallet=position.get("source_wallet") or source_wallet,
            source_signature=source_signature or position.get("source_signature"),
            tx_signature=tx_sig, strategy=strategy,
        )
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

    def _set_discovery_live_status(self, candidate: dict, status: str, reason: str) -> None:
        candidate["live_entry_status"] = status
        candidate["live_entry_reason"] = " ".join(str(reason or "").split())[:500]
        self.token_discovery._save(candidate)

    def _process_discovery_live_entries(self) -> None:
        """Turn only fresh, newly-qualified shadow entries into small opt-in live buys."""
        if (
            self.s.mode != "live"
            or not self.s.token_discovery_enabled
            or not self.s.token_discovery_live_enabled
        ):
            return

        now = time.time()
        hard_cap = min(0.50, float(self.s.token_discovery_max_entry_usd))
        for candidate in self.token_discovery.candidates(300):
            if candidate.get("status") != "shadow_open" or not candidate.get("would_buy"):
                continue
            # Candidate attempts are one-shot and persisted before any send to avoid
            # duplicate buys across loops or restarts after an ambiguous outcome.
            if candidate.get("live_entry_status"):
                continue

            mint = str(candidate.get("mint") or "")
            entry_at = float(candidate.get("shadow_entry_at") or 0)
            if (
                not mint
                or entry_at < self.run_started_at
                or now < entry_at
                or now - entry_at > 30
            ):
                self._set_discovery_live_status(
                    candidate, "skipped_stale",
                    "Shadow entry is not fresh in this bot run; no catch-up live buy",
                )
                continue

            positions = self.store.list_positions()
            if any(p.get("strategy", "leader_copy") == "token_discovery" for p in positions):
                self._set_discovery_live_status(
                    candidate, "blocked",
                    "One live discovery position is already open",
                )
                continue
            if self.store.get_position(mint):
                self._set_discovery_live_status(
                    candidate, "blocked",
                    "A tracked position already exists for this mint",
                )
                continue

            # Recompute metrics and repeat every on-chain launch gate immediately
            # before spending, rather than trusting the earlier shadow snapshot.
            candidate = self.token_discovery._apply_score(candidate)
            if not candidate.get("would_buy"):
                self._set_discovery_live_status(
                    candidate, "blocked",
                    "Entry gates no longer pass: " + "; ".join(candidate.get("gate_reasons") or []),
                )
                continue

            candidate["live_entry_status"] = "checking"
            candidate["live_entry_reason"] = "Repeating on-chain safety checks before live entry"
            self.token_discovery._save(candidate)
            try:
                inspected = self.token_discovery._inspect_new_token(
                    mint, creator=str(candidate.get("creator") or "")
                )
            except Exception as exc:
                self._set_discovery_live_status(
                    candidate, "blocked",
                    f"Fresh on-chain safety recheck failed ({type(exc).__name__}); no order submitted",
                )
                logger.warning("Discovery live entry blocked mint=%s: on-chain recheck failed: %s",
                               mint[:8], type(exc).__name__)
                continue

            if not inspected.get("safe"):
                candidate["security_passed"] = False
                self._set_discovery_live_status(
                    candidate, "blocked",
                    "Fresh on-chain safety recheck rejected token: " + str(inspected.get("reason") or "unknown reason"),
                )
                logger.info("Discovery live entry rejected %s: %s",
                            mint[:8], inspected.get("reason") or "safety recheck failed")
                continue

            liquidity = float(inspected.get("liquidity_usd") or 0)
            price = float(inspected.get("price_usd") or 0)
            decimals = int(inspected.get("decimals") or 0)
            candidate.update({
                "security_passed": True,
                "security_checks": inspected.get("checks", []),
                "top5_holder_supply_pct": float(inspected.get("top5_holder_supply_pct") or 0),
                "creator_supply_pct": float(inspected.get("creator_supply_pct") or 0),
                "real_liquidity_usd": liquidity,
                "price_usd": price,
            })
            if decimals != 6 or liquidity < self.s.token_discovery_min_liquidity_usd or price <= 0:
                self._set_discovery_live_status(
                    candidate, "blocked",
                    f"Fresh entry requirements failed (decimals={decimals}, real curve liquidity=${liquidity:.0f}, price valid={price > 0})",
                )
                continue

            candidate = self.token_discovery._apply_score(candidate)
            if not candidate.get("would_buy"):
                self._set_discovery_live_status(
                    candidate, "blocked",
                    "Entry gates failed after fresh on-chain recheck: " + "; ".join(candidate.get("gate_reasons") or []),
                )
                continue

            equity = self.equity()
            self._refresh_risk_state(equity)
            allowed, gate_reason = self._can_enter(
                equity, strategy="token_discovery", entry_cap_usd=hard_cap
            )
            if not allowed:
                self._set_discovery_live_status(
                    candidate, "blocked",
                    "Portfolio risk gate blocked discovery entry: " + gate_reason,
                )
                continue

            # Persist a one-shot marker before handing off to the executor. If the
            # process crashes after submission, this candidate is never replayed.
            candidate["live_entry_status"] = "submitting"
            candidate["live_entry_reason"] = f"Submitting at most ${hard_cap:.2f}; awaiting confirmed result"
            self.token_discovery._save(candidate)
            signal = {
                "mint": mint,
                "wallet": "",
                "signature": f"token-discovery-{mint}-{int(entry_at * 1000)}",
                "side": "buy",
                "amount_ui": 0.0,
                "decimals": decimals,
                "observed_price_usd": price,
                "curve_liquidity_usd": liquidity,
                "venue": "pump",
                "source": "token_discovery",
                "curve_state_verified": True,
                "fresh_security_verified": True,
            }
            self.buy(signal, strategy="token_discovery", entry_cap_usd=hard_cap)

            opened = self.store.get_position(mint)
            if opened and opened.get("strategy", "leader_copy") == "token_discovery":
                self._set_discovery_live_status(
                    candidate, "opened",
                    f"Live buy confirmed and tracked; entry cap was ${hard_cap:.2f}",
                )
                logger.warning(
                    "Live discovery BUY confirmed mint=%s strategy_cap=$%.2f; position manager will handle exits",
                    mint[:8], hard_cap,
                )
            elif self.store.get_state(f"unresolved_trade:{mint}", ""):
                self._set_discovery_live_status(
                    candidate, "uncertain",
                    "Transaction outcome unresolved; manual reconciliation required and this candidate will not retry",
                )
            else:
                self._set_discovery_live_status(
                    candidate, "failed",
                    "No live position was confirmed; inspect bot logs. This candidate will not auto-retry",
                )
            self.token_discovery._update_feed_subscriptions()
            # At most one discovery candidate may initiate execution per loop.
            return

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
            except SolanaTransactionNotReady:
                logger.info("Transaction details not indexed for %s; keeping cursor for retry.", signature[:10])
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
                    self._activate_manual_review_halt(
                        f"RPC wallet monitoring error for {wallet}; reconcile transactions before resuming"
                    )
                    self.auto_copy_enabled = False
                    self.store.set_state("auto_copy_enabled", "0")
                    self.store.set_state("copy_paused", "1")
                    self._refresh_feed_subscriptions()

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
                    self.token_discovery.tick()
                    self._process_discovery_live_entries()
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
                        self._activate_manual_review_halt(
                            "Runtime exception; reconcile wallet and transactions before resuming"
                        )
                        self.auto_copy_enabled = False
                        self.store.set_state("auto_copy_enabled", "0")
                        self.store.set_state("copy_paused", "1")
                        try:
                            self._refresh_feed_subscriptions()
                        except Exception:
                            logger.exception("Could not refresh subscriptions after entering manual-review halt")
                    self.message = "Runtime error; inspect console. Live entries are halted pending reconciliation."
                    # The normal refresh at the bottom of the loop is skipped when
                    # manage_positions() or another step raises. Publish directly from
                    # the ledger so confirmed buys do not remain invisible in the UI.
                    self._publish_dashboard_state_after_error()

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