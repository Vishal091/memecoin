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
from market_data import MarketData, WSOL_MINT
from pumpfun_config import PumpFunSettings
from pumpportal_feed import PumpPortalAccountFeed
from solana_logs_feed import SolanaLogsFeed
from pumpportal import PUMP_PROGRAM_ID, PUMPSWAP_PROGRAM_ID
from native_pump import (
    NativePumpExecutor,
    TransactionBuildFailed,
    TransactionExpiredNotLanded,
    TransactionFailedOnChain,
    TransactionOutcomeUncertain,
    TransactionSimulationFailed,
)
from solana_rpc import SolanaRPC, SolanaRPCRateLimited, SolanaTransactionNotReady
from pump_curve import fetch_pump_curve
from storage import Store
from token_discovery import TokenDiscoveryShadow
from trending_scanner import TrendingTokenScanner
from jupiter_ultra import JupiterUltraExecutor
from risk_engine import RiskEngine
from research_journal import get_research_journal
from mode_evaluation import auto_disable_unprofitable_modes

logger = logging.getLogger("pumpfun")


def raw_to_units(amount_raw: int | str, decimals: int) -> float:
    return int(amount_raw) / (10 ** decimals)


def loss_exit_hold_remaining_seconds(
    position: dict,
    mark_price: float,
    reason: str,
    minimum_hold_seconds: int,
    *,
    emergency_loss_bypass_pct: float = 0.50,
    now: float | None = None,
) -> float:
    """Seconds remaining in the minimum holding period for a loss-making exit."""
    entry_price = float(position.get("entry_price_usd") or 0)
    normalized_reason = str(reason or "").strip().lower()
    # Risk stops and leader-triggered sells must attempt an exit immediately.
    # A hold grace after the followed wallet sells can leave this bot exposed
    # while the leader is already out, amplifying divergence during fast dumps.
    if normalized_reason.startswith((
        "total loss limit", "experiment expiry", "manual exit",
        "marked loss stop", "trailing stop", "followed wallet ",
    )):
        return 0.0
    if entry_price <= 0 or mark_price <= 0 or mark_price >= entry_price:
        return 0.0
    emergency_threshold = max(0.40, min(0.90, float(emergency_loss_bypass_pct)))
    if mark_price <= entry_price * (1 - emergency_threshold):
        return 0.0
    try:
        opened_at = datetime.fromisoformat(
            str(position.get("opened_at") or "").replace("Z", "+00:00")
        )
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        age_seconds = (time.time() if now is None else float(now)) - opened_at.timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        # Legacy/malformed timestamps must not lock an existing position forever.
        return 0.0
    required_seconds = max(120, int(minimum_hold_seconds))
    return max(0.0, required_seconds - age_seconds)


def native_build_retry_delay_seconds(detail: str) -> int:
    """Back off pre-submission builder failures without retrying every management tick."""
    normalized = str(detail or "").lower()
    if "unsupported pumpswap quote mint" in normalized or "token-quoted coin" in normalized:
        return 6 * 60 * 60
    # ATA/account metadata can be temporarily stale on an RPC; retry slowly, not
    # every 1.5 seconds. The position and pending exit stay recorded in the ledger.
    return 60


def verified_curve_fallback_allowed(
    signal: dict,
    *,
    strategy: str,
    venue: str,
    curve_liquidity_usd: float,
    minimum_liquidity_usd: float,
) -> bool:
    """Permit pre-indexed DEX fallback only for freshly certified Pump discovery signals."""
    try:
        liquidity = float(curve_liquidity_usd)
        floor = float(minimum_liquidity_usd)
    except (TypeError, ValueError, OverflowError):
        return False
    return (
        strategy == "token_discovery"
        and isinstance(signal, dict)
        and signal.get("source") == "token_discovery"
        and signal.get("fresh_security_verified") is True
        and signal.get("curve_state_verified") is True
        and venue == "pump"
        and liquidity >= floor > 0
    )


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
        self.research = get_research_journal(
            self.store, path_window_seconds=int(getattr(self.s, "research_path_window_seconds", 900))
        )
        self.risk_engine = RiskEngine(self.store, self.s)
        self._next_mode_policy_check_at = 0.0
        self.risk_engine.engage_on_startup()
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
        self.trending_executor = JupiterUltraExecutor(
            self.rpc, self.s.jupiter_api_key, timeout=self.s.request_timeout_seconds
        )

        self.leaderboard_client = PumpFunLeaderboard(timeout=self.s.request_timeout_seconds)
        self.leaderboards = {}
        self.selected_window = self.store.get_state("leaderboard_window", self.s.default_leaderboard_window)
        saved_auto = self.store.get_state("auto_copy_enabled", "1" if self.s.auto_copy_on_start else "0")
        # Migrate old automatic safety-halt flags. Do not clear the persisted
        # consecutive-loss circuit breaker: a same-day pause must survive restarts.
        # Explicit user stop-copy state remains respected unless coupled to an old
        # automatic halt latch.
        legacy_auto_halt = any(
            self.store.get_state(key, "0") == "1"
            for key in ("manual_review_halt", "halted", "experiment_expired")
        )
        if legacy_auto_halt:
            self.store.set_state("manual_review_halt", "0")
            self.store.set_state("halted", "0")
            self.store.set_state("experiment_expired", "0")
            if saved_auto == "0" and self.store.get_state("copy_paused", "0") == "1":
                # These flags were also written by automatic halt handlers; restore
                # the prior default running state while leaving normal dashboard stop
                # controls available afterward.
                saved_auto = "1"
                self.store.set_state("auto_copy_enabled", "1")
                self.store.set_state("copy_paused", "0")
            logger.warning("Cleared legacy automatic halt flags; diagnostic reason is retained.")
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
        self.curve_price_cache = {}
        self.curve_price_cache_at = {}
        self.curve_mark_checked_at = {}
        self.curve_completed_cache = set()
        self.curve_unpriced_cache = set()
        self._market_cap_supply_retry_at = {}
        self.state_lock = threading.RLock()
        self.commands = queue.Queue()
        self.trade_events = queue.Queue()
        self.feed = (PumpPortalAccountFeed(self.s.pumpportal_api_key, self.trade_events,
                                           self.s.request_timeout_seconds)
                     if self.s.pumpportal_api_key else None)
        self.token_discovery = TokenDiscoveryShadow(
            self.s, self.rpc, self.market, self.store, self.feed
        )
        self.trending_scanner = TrendingTokenScanner(
            self.s, self.rpc, timeout=self.s.request_timeout_seconds, store=self.store,
            on_screened=self._queue_trending_candidate,
        )
        # Dashboard scanner state is persistent, but live-buy authorization is
        # intentionally session-only: every process restart requires an explicit
        # operator click on each live-buy switch.
        self.token_discovery.enabled = self.store.get_state(
            "dashboard_token_discovery_enabled",
            "1" if self.s.token_discovery_enabled else "0",
        ) == "1"
        self.trending_scanner.enabled = self.store.get_state(
            "dashboard_trending_discovery_enabled",
            "1" if self.s.trending_discovery_enabled else "0",
        ) == "1"
        self.token_discovery.live_enabled = False
        self.trending_scanner.live_enabled = False
        self.store.set_state("dashboard_token_discovery_live_enabled", "0")
        self.store.set_state("dashboard_trending_discovery_live_enabled", "0")
        if self.feed:
            self.token_discovery._update_feed_subscriptions()
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
        """Persist the diagnosis and engage the independent global entry gate."""
        incoming = " ".join(str(reason or "").split()).strip()
        existing = self.store.get_state("manual_review_reason", "").strip()
        if existing and incoming and incoming not in existing and append_reason:
            combined = f"{existing}; additionally: {incoming}"
        else:
            # Preserve the first diagnosis unless an explicitly appended transaction
            # outcome provides additional detail.
            combined = existing or incoming
        # Preserve the old state marker for compatibility; enforce a separate entry gate.
        self.store.set_state("manual_review_halt", "0")
        self.store.set_state("manual_review_reason", combined)
        if incoming:
            self.risk_engine.activate_kill_switch(incoming)
            logger.error("Global entry kill switch engaged: %s", incoming[:500])

    def _loss_streak_pause_reason(self) -> str:
        """Persist a same-UTC-day entry pause after the configured consecutive-loss threshold."""
        today = datetime.now(timezone.utc).date().isoformat()
        pause_day = str(self.store.get_state("consecutive_loss_pause_day", "") or "")
        if pause_day and pause_day != today:
            self.store.set_state("consecutive_loss_pause_day", "")
            self.store.set_state("consecutive_losing_exits", "0")
            self.store.set_state("consecutive_loss_warning_day", "")
            pause_day = ""
        if pause_day == today:
            return f"Consecutive losing-exit limit reached; new entries paused until the next UTC day ({today})"
        return ""

    def _record_realized_exit_risk(self, realized_pnl: float) -> None:
        """Track losing exits and pause new entries for the UTC day at the threshold."""
        try:
            streak = int(self.store.get_state("consecutive_losing_exits", "0") or 0)
        except (TypeError, ValueError):
            streak = 0
        streak = streak + 1 if float(realized_pnl) < 0 else 0
        self.store.set_state("consecutive_losing_exits", str(streak))
        threshold = max(2, int(getattr(self.s, "max_consecutive_losses", 3)))
        today = datetime.now(timezone.utc).date().isoformat()
        if streak >= threshold:
            self.store.set_state("consecutive_loss_pause_day", today)
            if self.store.get_state("consecutive_loss_warning_day", "") != today:
                self.store.set_state("consecutive_loss_warning_day", today)
                logger.warning(
                    "Consecutive losing exits reached %d (threshold %d); pausing new entries until the next UTC day",
                    streak, threshold,
                )
        elif streak == 0:
            self.store.set_state("consecutive_loss_pause_day", "")
            self.store.set_state("consecutive_loss_warning_day", "")

    def _copy_block_reason(self) -> str:
        """Report the global entry gate and persisted circuit breakers."""
        gate_reason = self.risk_engine.gate_reason(strategy="leader_copy")
        if gate_reason:
            return gate_reason
        if self.store.get_state("daily_paused", "0") == "1":
            return "Daily-loss circuit breaker is active; new entries resume after the next UTC day"
        return self._loss_streak_pause_reason()

    def _refresh_feed_subscriptions(self) -> None:
        held_leaders = {
            str(p.get("source_wallet")) for p in self.store.list_positions()
            if p.get("source_wallet")
        }
        if (
            self.auto_copy_enabled
            and self.store.get_state("copy_paused", "0") != "1"
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

    def _hydrate_position_market_cap(self, position: dict) -> dict:
        """Backfill total supply/entry-cap metadata for positions from earlier DB schema."""
        mint = str(position.get("mint") or "")
        try:
            supply_tokens = float(position.get("total_supply_tokens") or 0)
        except (TypeError, ValueError):
            supply_tokens = 0.0
        if not mint or supply_tokens > 0:
            return position

        now = time.time()
        retry_map = getattr(self, "_market_cap_supply_retry_at", None)
        if retry_map is None:
            retry_map = {}
            self._market_cap_supply_retry_at = retry_map
        if now < float(retry_map.get(mint, 0)):
            return position
        retry_map[mint] = now + 60
        try:
            result = self.rpc.call(
                "getTokenSupply", [mint, {"commitment": "confirmed"}]
            ) or {}
            value = result.get("value") or {}
            raw_supply = int(value.get("amount") or 0)
            decimals = int(value.get("decimals") or 0)
            if raw_supply <= 0 or decimals < 0 or decimals > 18:
                return position
            supply_tokens = raw_supply / (10 ** decimals)
            if supply_tokens <= 0:
                return position
            position["total_supply_tokens"] = supply_tokens
            if float(position.get("entry_market_cap_usd") or 0) <= 0:
                entry_price = float(position.get("entry_price_usd") or 0)
                if entry_price > 0:
                    position["entry_market_cap_usd"] = entry_price * supply_tokens
                    position["entry_market_cap_source"] = (
                        "Estimated historical entry price × current total supply (FDV-style)"
                    )
            self.store.upsert_position(position)
            logger.info("Backfilled market-cap supply metadata for position %s", mint[:8])
        except Exception as exc:
            logger.debug("Market-cap supply lookup unavailable for %s: %s", mint[:8], type(exc).__name__)
        return position

    def _publish_dashboard_state(self, equity: float | None = None) -> None:
        if equity is not None:
            self.last_equity_usd = equity
        positions = []
        for p in self.store.list_positions():
            p = self._hydrate_position_market_cap(p)
            entry = float(p["entry_price_usd"])
            mark = self._mark_price(p["mint"], float(p["last_price_usd"]))
            if mark <= 0:
                mark = float(p["last_price_usd"])
            move = ((mark / entry) - 1.0) * 100.0 if entry > 0 else 0.0
            cap_snapshot = self.token_discovery.market_cap_snapshot(p["mint"])
            supply_tokens = float(p.get("total_supply_tokens") or 0)
            entry_mc = float(p.get("entry_market_cap_usd") or 0)
            entry_mc_source = str(p.get("entry_market_cap_source") or "")
            if entry_mc <= 0:
                entry_mc = float(cap_snapshot.get("entry_market_cap_usd") or 0)
                entry_mc_source = str(cap_snapshot.get("entry_market_cap_source") or entry_mc_source)
            current_mc = float(cap_snapshot.get("current_market_cap_usd") or 0)
            current_mc_source = str(cap_snapshot.get("current_market_cap_source") or "")
            if current_mc <= 0 and mark > 0 and supply_tokens > 0:
                current_mc = mark * supply_tokens
                current_mc_source = "Estimated: current mark price × total supply (FDV-style)"
            positions.append({
                "mint": p["mint"], "amount_raw": p["amount_raw"], "decimals": p["decimals"],
                "entry_price_usd": entry, "last_price_usd": mark, "move_pct": move,
                "source_wallet": p.get("source_wallet", ""),
                "strategy": p.get("strategy", "leader_copy"),
                "entry_market_cap_usd": entry_mc or None,
                "entry_market_cap_source": entry_mc_source,
                "current_market_cap_usd": current_mc or None,
                "current_market_cap_source": current_mc_source,
                "exit_blocked_reason": self.store.get_state(
                    f"exit_blocked_balance_zero:{p['mint']}", ""
                ),
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
            "live_enabled": bool(self.s.mode == "live" and self.token_discovery.live_enabled),
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
            "trending_discovery": self.trending_scanner.dashboard_snapshot(),
            "risk_control": self.risk_engine.snapshot(),
            "research_journal": self.research.snapshot(),
            "strategy_accounts": self.store.strategy_accounts_snapshot(mode=self.s.mode),
            "mode_policy_auto_disable_enabled": bool(self.s.research_auto_disable_enabled),
            "strategy_accounts": self.store.strategy_accounts_snapshot(mode=self.s.mode),
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
                "subscription_requests_sent": self.logs_feed.subscription_requests_sent,
                "subscription_acks_received": self.logs_feed.subscription_acks_received,
                "pending_wallet_count": self.logs_feed.pending_wallet_count,
                "error": self.logs_feed.last_error,
            } if self.logs_feed else {
                "enabled": False, "connected": False, "subscribed_wallet_count": 0,
                "messages_received": 0, "notifications_received": 0,
                "last_message_at": 0, "last_notification_at": 0,
                "last_signature": "", "subscription_errors": 0,
                "subscription_requests_sent": 0, "subscription_acks_received": 0,
                "pending_wallet_count": 0,
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

    def _runtime_discovery_flag(self, tracker_name: str, flag_name: str, settings_name: str) -> bool:
        """Prefer an explicit runtime boolean, while supporting legacy/mock objects."""
        tracker = getattr(self, tracker_name, None)
        runtime_value = getattr(tracker, flag_name, None) if tracker is not None else None
        if isinstance(runtime_value, bool):
            return runtime_value
        return bool(getattr(self.s, settings_name, False))

    def _discovery_live_toggle_block_reason(self, strategy: str) -> str:
        """Fail closed before enabling either dashboard live-buy switch."""
        if self.s.mode != "live":
            return "BOT_MODE must be live before enabling real-money discovery buys."
        if self.s.live_confirmation != "I_ACCEPT_THE_RISK_AND_UNDERSTAND_LIVE_TRADING":
            return "LIVE_CONFIRMATION is missing or invalid."
        if not getattr(self, "wallet_address", "") or not getattr(self, "keypair", None):
            return "A loaded live wallet/keypair is required."
        if strategy == "token_discovery":
            if not self.token_discovery.enabled:
                return "Enable the new-token scanner before enabling its live buys."
            if not (0.10 <= float(self.s.token_discovery_max_entry_usd) <= 0.50):
                return "TOKEN_DISCOVERY_MAX_ENTRY_USD must be between $0.10 and $0.50."
            if not self.s.pumpfun_auth_token or not self.s.pumpportal_api_key:
                return "New-token live buys require Pump.fun and PumpPortal credentials."
        elif strategy == "trending_discovery":
            if not self.trending_scanner.enabled:
                return "Enable the trending scanner before enabling its live buys."
            if not (0.10 <= float(self.s.trending_discovery_max_entry_usd) <= 0.50):
                return "TRENDING_DISCOVERY_MAX_ENTRY_USD must be between $0.10 and $0.50."
            if not self.s.jupiter_api_key or not getattr(self.trending_executor, "api_key", ""):
                return "JUPITER_API_KEY is required for trending live buys."
        else:
            return "Unknown discovery strategy."
        return ""

    def _process_dashboard_commands(self) -> None:
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                break
            action = command.get("action")
            if action == "set_mode_kill_switch":
                strategy = str(command.get("strategy") or "")
                requested = command.get("enabled")
                if not isinstance(requested, bool):
                    self.message = "Invalid mode kill-switch toggle; expected true or false."
                    continue
                ok, detail = self.risk_engine.set_mode_kill_switch(
                    strategy, requested,
                    "Operator paused the mode from the local dashboard.",
                )
                self.message = detail
                logger.warning("Mode kill switch action: %s", detail)
                continue
            if action == "set_entry_kill_switch":
                requested = command.get("enabled")
                if not isinstance(requested, bool):
                    self.message = "Invalid entry-gate toggle; expected true or false."
                    continue
                if requested:
                    self.risk_engine.activate_kill_switch("Operator engaged the global entry kill switch from the dashboard.")
                    self.message = "Global entry kill switch engaged. Existing positions remain managed."
                else:
                    ok, detail = self.risk_engine.resume_entries()
                    self.message = detail if ok else "Entries remain halted: " + detail
                self._refresh_feed_subscriptions()
                logger.warning("%s", self.message)
                continue
            if action == "acknowledge_reconciled_trade":
                ok, detail = self.risk_engine.acknowledge_reconciled_trade(
                    str(command.get("mint") or ""), str(command.get("signature") or ""),
                    str(command.get("confirmation") or ""),
                )
                self.message = detail
                logger.warning("Reconciliation acknowledgement: %s", detail)
                continue
            if action == "acknowledge_risk_reservation":
                ok, detail = self.risk_engine.acknowledge_uncertain_reservation(
                    str(command.get("reservation_id") or ""), str(command.get("confirmation") or ""),
                )
                self.message = detail
                logger.warning("Reservation reconciliation acknowledgement: %s", detail)
                continue
            discovery_actions = {
                "set_token_discovery_enabled", "set_token_discovery_live_enabled",
                "set_trending_discovery_enabled", "set_trending_discovery_live_enabled",
            }
            if action in discovery_actions:
                requested = command.get("enabled")
                if not isinstance(requested, bool):
                    self.message = "Invalid discovery toggle value; expected a boolean."
                    continue

                if action == "set_token_discovery_enabled":
                    self.token_discovery.enabled = requested
                    self.store.set_state("dashboard_token_discovery_enabled", "1" if requested else "0")
                    if not requested:
                        self.token_discovery.live_enabled = False
                        self.store.set_state("dashboard_token_discovery_live_enabled", "0")
                    self.token_discovery._update_feed_subscriptions()
                    self.message = (
                        "New-token discovery scanner enabled. Live buys remain off until separately enabled."
                        if requested else
                        "New-token discovery scanner disabled; new-token live buys are also off. Existing positions remain managed."
                    )
                    logger.warning("%s", self.message)
                elif action == "set_trending_discovery_enabled":
                    self.trending_scanner.enabled = requested
                    self.store.set_state("dashboard_trending_discovery_enabled", "1" if requested else "0")
                    if not requested:
                        self.trending_scanner.live_enabled = False
                        self.store.set_state("dashboard_trending_discovery_live_enabled", "0")
                        self.trending_scanner.stop()
                    else:
                        self.trending_scanner.start()
                    self.message = (
                        "Trending-token scanner enabled. Live buys remain off until separately enabled."
                        if requested else
                        "Trending-token scanner disabled; trending live buys are also off. Existing positions remain managed."
                    )
                    logger.warning("%s", self.message)
                else:
                    strategy = "token_discovery" if action == "set_token_discovery_live_enabled" else "trending_discovery"
                    tracker = self.token_discovery if strategy == "token_discovery" else self.trending_scanner
                    if requested:
                        block_reason = self._discovery_live_toggle_block_reason(strategy)
                        if block_reason:
                            self.message = "Live buys NOT enabled: " + block_reason
                            logger.warning("%s", self.message)
                            continue
                    tracker.live_enabled = requested
                    state_key = (
                        "dashboard_token_discovery_live_enabled"
                        if strategy == "token_discovery"
                        else "dashboard_trending_discovery_live_enabled"
                    )
                    # This marker is diagnostic only; live authorization itself resets
                    # to OFF during startup regardless of any stale state value.
                    self.store.set_state(state_key, "1" if requested else "0")
                    self.message = (
                        ("LIVE BUYS ENABLED for " if requested else "Live buys disabled for ")
                        + strategy + ". Existing positions remain managed."
                    )
                    logger.warning("%s", self.message)
                continue

            if action == "copy_top10":
                window = str(command.get("window", "day")).lower()
                if window not in {"overall", "day", "week", "month"}:
                    self.message = "Invalid leaderboard window."
                    continue
                # Subscribing to leader activity is observation, not order authorization.
                # Resulting buys still pass global and per-strategy risk gates.
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
                # A human full-exit request overrides a deferred partial leader sell.
                # Clear its amount/source metadata so we liquidate the remaining
                # tracked balance, not just the earlier partial-sell quantity.
                reason = "manual exit requested from dashboard"
                # A deliberate retry clears the previous zero-balance block and
                # submits a fresh read, but never overrides unresolved transactions.
                self.store.set_state(f"exit_blocked_balance_zero:{mint}", "")
                self.store.set_state(f"exit_pending:{mint}", reason)
                self.store.set_state(f"exit_pending_amount_raw:{mint}", "")
                self.store.set_state(f"exit_pending_source_wallet:{mint}", "")
                self.store.set_state(f"exit_pending_source_signature:{mint}", "")
                # A previous pre-submission builder failure must not suppress the
                # fresh, explicit manual attempt. If it fails again, sell() will
                # install a new cooldown, preventing a tight retry loop.
                self.store.set_state(f"native_build_retry_after:{mint}", "")
                unresolved = self.store.get_state(f"unresolved_trade:{mint}", "")
                if unresolved:
                    self.message = (
                        f"Manual full exit queued for {mint}, but submission is blocked until "
                        "the earlier transaction outcome is reconciled."
                    )
                else:
                    self.message = (
                        f"Manual full exit queued for {mint}; attempting on the next bot cycle."
                    )
                logger.warning("%s", self.message)
            elif action == "exit_all":
                positions = self.store.list_positions()
                unresolved_count = 0
                for position in positions:
                    mint = str(position["mint"])
                    self.store.set_state(f"exit_blocked_balance_zero:{mint}", "")
                    self.store.set_state(
                        f"exit_pending:{mint}", "manual exit all requested from dashboard"
                    )
                    self.store.set_state(f"exit_pending_amount_raw:{mint}", "")
                    self.store.set_state(f"exit_pending_source_wallet:{mint}", "")
                    self.store.set_state(f"exit_pending_source_signature:{mint}", "")
                    self.store.set_state(f"native_build_retry_after:{mint}", "")
                    if self.store.get_state(f"unresolved_trade:{mint}", ""):
                        unresolved_count += 1
                self.message = f"Manual exit-all queued for {len(positions)} tracked position(s)."
                if unresolved_count:
                    self.message += (
                        f" {unresolved_count} position(s) cannot submit a new sell until "
                        "their earlier transaction outcome(s) are reconciled."
                    )
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

    def _queue_trending_candidate(self, candidate: dict) -> None:
        """Move scanner results onto the main bot loop; scanner threads never trade."""
        signal = dict(candidate)
        signal["source"] = "trending_discovery"
        signal["fresh_security_verified"] = False
        self.trade_events.put({
            "source": "trending_discovery_candidate",
            "signal": signal,
        })

    def _consume_feed_events(self) -> None:
        while True:
            try:
                event = self.trade_events.get_nowait()
            except queue.Empty:
                break
            self.signal_stats["queued_events"] += 1
            source = str(event.get("source") or "")
            journal_strategy = {
                "account_trade": "leader_copy", "token_trade": "token_discovery",
                "token_discovery_new": "token_discovery",
                "trending_discovery_candidate": "trending_discovery",
            }.get(source, "market_data")
            journal_type = {
                "account_trade": "leader_trade_observation", "token_trade": "trade_tick",
                "token_discovery_new": "token_launch",
                "trending_discovery_candidate": "trending_candidate_queued",
            }.get(source, "provider_event")
            journal_event = dict(event)
            try:
                event_price = self._event_price_usd(event)
                if event_price is not None and event_price > 0:
                    journal_event["price_usd"] = event_price
                self.research.record_event(
                    mode=self.s.mode, strategy=journal_strategy, event_type=journal_type,
                    event=journal_event, observed_at=time.time(),
                )
            except Exception as exc:
                logger.warning("Research event journal write failed (%s)", type(exc).__name__)
            if source == "trending_discovery_candidate":
                signal = event.get("signal")
                if isinstance(signal, dict):
                    self._buy_trending_discovery(signal)
                continue
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
                try:
                    self.research.record_signal(
                        mode=self.s.mode, strategy="leader_copy",
                        mint=mint or str(event.get("mint") or ""),
                        signal_type="leader_trade", decision="rejected",
                        rejection_reason="Required mint/wallet/side/amount fields could not be parsed.",
                        source_wallet=str(event.get("wallet") or event.get("traderPublicKey") or ""),
                        source_signature=str(event.get("signature") or ""),
                        payload=event,
                    )
                except Exception as exc:
                    logger.warning("Research rejected-signal journal write failed (%s)", type(exc).__name__)
                self.signal_stats["unparsed_events"] += 1
                logger.warning("Ignoring PumpPortal trade event because required mint/wallet/side/amount fields could not be parsed; fields=%s",
                               ",".join(sorted(str(k) for k in event.keys())))
                continue
            if signal["wallet"] not in self.active_wallets:
                try:
                    self.research.record_signal(
                        mode=self.s.mode, strategy="leader_copy", mint=signal.get("mint", ""),
                        signal_type="leader_trade", decision="rejected",
                        rejection_reason="Wallet is not in the current active subscription set.",
                        source_wallet=signal.get("wallet", ""), source_signature=signal.get("signature", ""),
                        payload=signal,
                    )
                except Exception as exc:
                    logger.warning("Research rejected-signal journal write failed (%s)", type(exc).__name__)
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
            # Never action an account-feed event until Solana RPC independently
            # confirms the wallet signature and Pump/PumpSwap program invocation.
            # Leave it unconsumed so the separate RPC notification can validate it.
            if signal.get("source") != "solana_logs" or signal.get("signer_verified") is not True or signal.get("program_verified") is not True:
                try:
                    self.research.record_signal(
                        mode=self.s.mode, strategy="leader_copy", mint=signal.get("mint", ""),
                        signal_type="leader_trade", decision="observed_unverified",
                        rejection_reason="Awaiting independent Solana signer/program verification.",
                        source_wallet=signal.get("wallet", ""), source_signature=signal.get("signature", ""),
                        payload=signal,
                    )
                except Exception as exc:
                    logger.warning("Research unverified-signal journal write failed (%s)", type(exc).__name__)
                continue
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
            initial_reason = (
                "Solana log event is missing wallet or signature."
                if not wallet or not signature
                else ("Wallet is not in the active observation set." if wallet not in self.active_wallets else "")
            )
            try:
                self.research.record_event(
                    mode=self.s.mode, strategy="leader_copy",
                    event_type="solana_log_notification", event=event,
                    observed_at=time.time(), rejection_reason=initial_reason,
                )
            except Exception as exc:
                logger.warning("Research Solana-log event write failed (%s)", type(exc).__name__)
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
            if not signals:
                try:
                    self.research.record_event(
                        mode=self.s.mode, strategy="leader_copy",
                        event_type="rejected_chain_signal", event=event,
                        observed_at=time.time(),
                        rejection_reason="Transaction did not yield a canonical, signer-verified Pump/PumpSwap trade signal.",
                    )
                except Exception as exc:
                    logger.warning("Research rejected-chain-event write failed (%s)", type(exc).__name__)
            for signal in signals:
                signal["source"] = "solana_logs"
                signal["signer_verified"] = True
                signal["program_verified"] = True
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

        # Pump bonding curves are directly observable on-chain before DEX indexers
        # list the token. Prefer a canonical curve quote for those positions; otherwise
        # the stop manager can keep using the stale entry mark until indexing catches up.
        try:
            preferred_pool = self.store.get_state(f"pool:{mint}", "pump")
        except Exception:
            preferred_pool = "pump"
        curve_price = 0.0
        if preferred_pool == "pump":
            curve_price = float(getattr(self, "curve_price_cache", {}).get(mint, 0) or 0)
            curve_age = now - float(getattr(self, "curve_price_cache_at", {}).get(mint, 0) or 0)
            if curve_price > 0 and 0 <= curve_age <= 1.0:
                return curve_price

            # Do not hammer RPC on every dashboard refresh if a curve is migrated,
            # unsupported, or temporarily unavailable. Completed curves stay completed.
            completed_cache = getattr(self, "curve_completed_cache", set())
            unpriced_cache = getattr(self, "curve_unpriced_cache", set())
            checked_cache = getattr(self, "curve_mark_checked_at", {})
            last_checked = float(checked_cache.get(mint, 0) or 0)
            should_read_curve = (
                mint not in completed_cache
                and mint not in unpriced_cache
                and now - last_checked >= 1.0
            )
            if should_read_curve:
                if not hasattr(self, "curve_mark_checked_at"):
                    self.curve_mark_checked_at = {}
                self.curve_mark_checked_at[mint] = now
                try:
                    curve = fetch_pump_curve(
                        self.rpc, mint,
                        timeout=min(2.0, float(getattr(self.s, "request_timeout_seconds", 2))),
                    )
                    if curve is not None and curve.complete:
                        completed_cache.add(mint)
                        curve_price = 0.0
                        getattr(self, "curve_price_cache", {}).pop(mint, None)
                        getattr(self, "curve_price_cache_at", {}).pop(mint, None)
                    elif curve is not None and curve.quote_mint and curve.quote_mint != WSOL_MINT:
                        # This bot's curve pricing only supports SOL-quoted curves.
                        unpriced_cache.add(mint)
                        curve_price = 0.0
                    elif curve is not None:
                        position = self.store.get_position(mint) or {}
                        decimals = int(position.get("decimals") or 6)
                        sol_price = self.market.sol_price_usd(
                            timeout=min(2.0, float(getattr(self.s, "request_timeout_seconds", 2)))
                        )
                        candidate = float(curve.price_usd(sol_price, decimals=decimals) or 0)
                        if candidate > 0:
                            if not hasattr(self, "curve_price_cache"):
                                self.curve_price_cache = {}
                                self.curve_price_cache_at = {}
                            self.curve_price_cache[mint] = candidate
                            self.curve_price_cache_at[mint] = time.time()
                            return candidate
                except Exception as exc:
                    logger.debug("On-chain Pump curve mark unavailable for %s (%s)", mint[:8], type(exc).__name__)

        market_price = float(self.market_price_cache.get(mint, 0) or 0)
        market_age = now - float(self.market_price_cache_at.get(mint, 0) or 0)
        if market_price > 0 and 0 <= market_age <= 3:
            return market_price

        try:
            market = self.market.token_market(
                mint, timeout=min(2.0, float(getattr(self.s, "request_timeout_seconds", 2)))
            )
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
        if curve_price > 0:
            return curve_price
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
        """Refresh drawdown baselines and warn when an entry circuit breaker is active."""
        self.store.reset_day_if_needed(equity)
        initial = float(self.store.get_state("initial_equity", str(self.s.starting_paper_cash_usd)))
        total_loss = max(0.0, initial - equity)
        today = datetime.now(timezone.utc).date().isoformat()
        if total_loss >= self.s.total_loss_limit_usd:
            if self.store.get_state("total_loss_warning_day", "") != today:
                self.store.set_state("total_loss_warning_day", today)
                logger.warning(
                    "Configured total-loss limit reached (drawdown %.2f USD); new entries are blocked. "
                    "Existing positions remain managed; no automatic mass liquidation is performed.",
                    total_loss,
                )
        # Independent persistent peak-to-trough equity circuit breaker.
        try:
            peak_equity = float(self.store.get_state("equity_high_watermark", str(max(0.0, equity))))
        except (TypeError, ValueError, OverflowError):
            peak_equity = max(0.0, equity)
        if equity > peak_equity:
            peak_equity = equity
            self.store.set_state("equity_high_watermark", peak_equity)
        elif not self.store.get_state("equity_high_watermark", ""):
            self.store.set_state("equity_high_watermark", peak_equity)
        if peak_equity > 0:
            peak_drawdown_pct = max(0.0, (peak_equity - equity) / peak_equity)
            if peak_drawdown_pct >= float(self.s.max_portfolio_drawdown_pct):
                self.risk_engine.activate_kill_switch(
                    "Portfolio peak-to-trough drawdown reached configured "
                    f"{self.s.max_portfolio_drawdown_pct:.1%} ceiling (observed {peak_drawdown_pct:.1%})."
                )
        daily_start = float(self.store.get_state("day_start_equity", str(equity)))
        daily_loss = max(0.0, daily_start - equity)
        if daily_loss >= self.s.daily_loss_limit_usd:
            self.store.set_state("daily_paused", "1")
            if self.store.get_state("daily_loss_warning_day", "") != today:
                self.store.set_state("daily_loss_warning_day", today)
                logger.warning(
                    "Configured daily-loss limit reached (drawdown %.2f USD); new entries are blocked "
                    "until the next UTC day. Existing positions remain managed.",
                    daily_loss,
                )

    def _can_enter(
        self, equity: float, *, strategy: str = "leader_copy",
        entry_cap_usd: float | None = None,
    ) -> tuple[bool, str]:
        gate_reason = self.risk_engine.gate_reason(strategy=strategy)
        if gate_reason:
            return False, gate_reason
        if strategy not in {"token_discovery", "trending_discovery"} and self.store.get_state("copy_paused", "0") == "1":
            return False, "Copying is paused from the dashboard"
        if self.store.daily_entries() >= self.s.max_entries_per_day:
            return False, "Daily entry cap reached"
        if len(self.store.list_positions()) >= self.s.max_open_positions:
            return False, "Open position cap reached"
        if equity <= 0:
            return False, "Equity is zero or less"
        # Make UTC-day rollover deterministic even when an event arrives before the
        # next periodic heartbeat refreshes the risk baselines.
        if hasattr(self.store, "reset_day_if_needed"):
            self.store.reset_day_if_needed(equity)
        if self.store.get_state("daily_paused", "0") == "1":
            return False, "Daily-loss limit was reached; new entries are paused until the next UTC day"
        loss_streak_reason = self._loss_streak_pause_reason()
        if loss_streak_reason:
            return False, loss_streak_reason
        try:
            initial_equity = float(self.store.get_state("initial_equity", str(self.s.starting_paper_cash_usd)))
            day_start_equity = float(self.store.get_state("day_start_equity", str(equity)))
        except (TypeError, ValueError, OverflowError):
            return False, "Risk baseline is invalid; refusing new entry until reconciled"
        total_drawdown = max(0.0, initial_equity - equity)
        if total_drawdown >= float(self.s.total_loss_limit_usd):
            return False, (
                f"Total-loss limit reached: drawdown ${total_drawdown:.2f} >= "
                f"${float(self.s.total_loss_limit_usd):.2f}; new entries blocked"
            )
        daily_drawdown = max(0.0, day_start_equity - equity)
        if daily_drawdown >= float(self.s.daily_loss_limit_usd):
            self.store.set_state("daily_paused", "1")
            return False, (
                f"Daily-loss limit reached: drawdown ${daily_drawdown:.2f} >= "
                f"${float(self.s.daily_loss_limit_usd):.2f}; new entries blocked until next UTC day"
            )
        exposure = self._current_positions_value()
        room = self.s.max_total_exposure_usd - exposure
        configured_cap = self.s.max_entry_usd if entry_cap_usd is None else float(entry_cap_usd)
        if strategy == "token_discovery":
            configured_cap = min(0.50, configured_cap, float(self.s.token_discovery_max_entry_usd))
        elif strategy == "trending_discovery":
            configured_cap = min(0.50, configured_cap, float(self.s.trending_discovery_max_entry_usd))
        spend = min(self.s.max_entry_usd, configured_cap, room)
        minimum_practical_spend = 0.10 if strategy in {"token_discovery", "trending_discovery"} else 0.50
        if spend < minimum_practical_spend:
            return False, "Too little exposure headroom for a practical trade"
        if self.s.mode == "paper":
            account = self.store.get_strategy_account(self.s.mode, strategy)
            if not account:
                return False, "Strategy-specific paper account is missing; refusing unbudgeted entry"
            cash = float(account.get("cash_usd") or 0.0)
            if cash < spend + self.s.paper_swap_cost_usd:
                return False, "Insufficient strategy-specific paper cash after simulated costs"
        else:
            assert self.wallet_address
            sol_price = self.market.sol_price_usd()
            spend_lamports = int(spend / sol_price * 1_000_000_000)
            balance = self.rpc.sol_balance_lamports(self.wallet_address)
            reserve = int((self.s.min_sol_reserve + 0.002 + self.s.pumpportal_priority_fee_sol) * 1_000_000_000)
            if balance < spend_lamports + reserve:
                return False, "SOL balance insufficient for entry plus fee reserve"
        return True, "allowed"

    def _read_total_supply_tokens(self, mint: str) -> float:
        try:
            result = self.rpc.call(
                "getTokenSupply", [mint, {"commitment": "confirmed"}]
            ) or {}
            value = result.get("value") or {}
            raw_supply = int(value.get("amount") or 0)
            decimals = int(value.get("decimals") or 0)
            if raw_supply <= 0 or decimals < 0 or decimals > 18:
                return 0.0
            return raw_supply / (10 ** decimals)
        except Exception as exc:
            # Market-cap metadata is optional; failure must never suppress or delay
            # the existing execution/risk result beyond this one best-effort lookup.
            logger.debug("Could not read token supply for market-cap estimate %s: %s",
                         str(mint)[:8], type(exc).__name__)
            return 0.0

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
                self.store.set_state(f"exit_pending_amount_raw:{exc.mint}", "")
                self.store.set_state(f"exit_pending_source_wallet:{exc.mint}", "")
                self.store.set_state(f"exit_pending_source_signature:{exc.mint}", "")

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
        self.risk_engine.mark_uncertain(
            exc.mint,
            f"{exc.action.upper()} outcome unresolved for mint {exc.mint}; signature {exc.signature}.",
        )
        self._activate_manual_review_halt(
            f"{exc.action.upper()} outcome unresolved for mint {exc.mint}; "
            f"signature {exc.signature}; reconcile wallet/transaction before resuming",
            append_reason=True,
        )
        # The independent global entry gate now blocks every new buy until the
        # ambiguity is reconciled. Existing position monitoring and exit handling remain active.
        # Consume the triggering leader event so it cannot be replayed as a stale signal.
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

    def _buy_trending_discovery(
        self, signal: dict, *, entry_cap_usd: float | None = None,
    ) -> None:
        """Execute one freshly re-screened trending-token entry through Jupiter Ultra."""
        mint = str(signal.get("mint") or "")
        if (
            self.s.mode != "live"
            or not self._runtime_discovery_flag("trending_scanner", "enabled", "trending_discovery_enabled")
            or not self._runtime_discovery_flag("trending_scanner", "live_enabled", "trending_discovery_live_enabled")
            or signal.get("source") != "trending_discovery"
        ):
            self.trending_scanner.set_live_status(
                mint, "blocked", "Trending live opt-in is off or candidate source is invalid"
            )
            return
        try:
            signal = self.trending_scanner.verify_live_candidate(signal)
            signal["source"] = "trending_discovery"
            signal["fresh_security_verified"] = True
        except Exception as exc:
            self.trending_scanner.set_live_status(
                mint, "blocked", f"Fresh pre-trade security/market recheck failed ({type(exc).__name__}): {str(exc)[:300]}"
            )
            logger.warning("Trending live entry blocked by fresh recheck mint=%s: %s", mint[:8], exc)
            return
        if not mint or self.store.get_state(f"unresolved_trade:{mint}", ""):
            self.trending_scanner.set_live_status(
                mint, "uncertain", "An unresolved transaction exists for this mint; no repeat order was sent"
            )
            return
        if self.store.get_position(mint):
            self.trending_scanner.set_live_status(mint, "blocked", "A tracked position already exists for this mint")
            return
        discovery_positions = [
            p for p in self.store.list_positions()
            if p.get("strategy", "leader_copy") in {"token_discovery", "trending_discovery"}
        ]
        if discovery_positions:
            self.trending_scanner.set_live_status(
                mint, "blocked", "One other discovery position is already open"
            )
            return

        # Use the exact DEX Screener pool that was cross-checked by the scanner;
        # if the best pair changed, require a new full scan instead of guessing.
        try:
            market = self.market.token_market(mint)
        except Exception as exc:
            self.trending_scanner.set_live_status(
                mint, "blocked", f"Fresh market-data request failed ({type(exc).__name__})"
            )
            return
        if not market:
            self.trending_scanner.set_live_status(mint, "blocked", "Fresh DEX market pair is unavailable")
            return
        if str(market.get("pair_address") or "") != str(signal.get("dexscreener_pair_address") or ""):
            self.trending_scanner.set_live_status(
                mint, "blocked", "Best DEX Screener pool changed since the security scan; rescan required"
            )
            return
        raw_market = market.get("raw") or {}
        liquidity = float(market.get("liquidity_usd") or 0)
        price = float(market.get("price_usd") or 0)
        try:
            pair_age = int(market.get("pair_age_seconds") or 0)
        except (TypeError, ValueError):
            pair_age = 0
        volume1 = float((raw_market.get("volume") or {}).get("h1") or 0)
        if liquidity < self.s.trending_min_liquidity_usd:
            self.trending_scanner.set_live_status(
                mint, "blocked",
                f"Fresh same-pool liquidity ${liquidity:,.0f} is below ${self.s.trending_min_liquidity_usd:,.0f}",
            )
            return
        if volume1 < self.s.trending_min_volume_1h_usd:
            self.trending_scanner.set_live_status(
                mint, "blocked",
                f"Fresh same-pool 1h volume ${volume1:,.0f} is below ${self.s.trending_min_volume_1h_usd:,.0f}",
            )
            return
        max_pair_age = max(1, int(self.s.trending_max_pool_age_hours)) * 3600
        if pair_age < self.s.min_pair_age_seconds or pair_age > max_pair_age or price <= 0:
            self.trending_scanner.set_live_status(
                mint, "blocked", "Fresh DEX pair is too young/old or has no reliable USD price"
            )
            return
        if bool(signal.get("market_data_discrepancy")) or signal.get("same_pool_crosscheck") is not True:
            self.trending_scanner.set_live_status(
                mint, "blocked", "Independent market sources did not pass the same-pool consistency check"
            )
            return
        try:
            decimals = int(signal.get("decimals") or -1)
        except (TypeError, ValueError):
            decimals = -1
        if decimals < 0 or decimals > 18:
            self.trending_scanner.set_live_status(mint, "blocked", "Token decimals are invalid")
            return
        if int(signal.get("momentum_score") or 0) < self.s.trending_min_score:
            self.trending_scanner.set_live_status(
                mint, "blocked",
                f"Fresh momentum score is below configured floor {self.s.trending_min_score}/100",
            )
            return
        try:
            fresh_txns = (raw_market.get("txns") or {}).get("h1") or {}
            buys = int(fresh_txns["buys"])
            sells = int(fresh_txns["sells"])
        except (KeyError, TypeError, ValueError, OverflowError):
            self.trending_scanner.set_live_status(
                mint, "blocked", "Fresh DEX market response has no usable one-hour buy/sell counts"
            )
            return
        if buys <= sells:
            self.trending_scanner.set_live_status(
                mint, "blocked", "Fresh one-hour trade flow does not show more buys than sells"
            )
            return

        sol_price = self.market.sol_price_usd()
        hard_cap = min(
            0.50,
            float(self.s.trending_discovery_max_entry_usd),
            float(entry_cap_usd if entry_cap_usd is not None else self.s.trending_discovery_max_entry_usd),
            float(self.s.max_entry_usd),
        )
        equity = self.equity()
        self._refresh_risk_state(equity)
        allowed, gate_reason = self._can_enter(
            equity, strategy="trending_discovery", entry_cap_usd=hard_cap
        )
        if not allowed:
            self.trending_scanner.set_live_status(
                mint, "blocked", "Portfolio risk gate blocked trending entry: " + gate_reason
            )
            return
        room = max(0.0, float(self.s.max_total_exposure_usd) - self._current_positions_value())
        spend = min(hard_cap, room)
        if spend < 0.10:
            self.trending_scanner.set_live_status(
                mint, "blocked", "Less than $0.10 portfolio exposure headroom remains"
            )
            return
        amount_lamports = int(spend / sol_price * 1_000_000_000)
        if amount_lamports <= 0:
            self.trending_scanner.set_live_status(mint, "blocked", "SOL input rounds to zero")
            return

        reservation_id, reserve_reason = self.risk_engine.reserve_entry(
            mint, "trending_discovery", spend,
            exposure_usd=spend + (0.05 if self.s.mode == "live" else self.s.paper_swap_cost_usd),
        )
        if not reservation_id:
            self.trending_scanner.set_live_status(mint, "blocked", "Risk reservation denied: " + reserve_reason)
            return

        self.trending_scanner.set_live_status(
            mint, "submitting", f"Submitting Jupiter Ultra buy with swap input capped at ${spend:.2f}"
        )
        source_signature = str(signal.get("signature") or f"trending-discovery-{mint}-{int(time.time())}")
        try:
            before_token = self.executor.token_balance_raw(self.wallet_address, mint)
            before_sol = self.rpc.sol_balance_lamports(self.wallet_address)
        except Exception as exc:
            self.risk_engine.release_entry(
                reservation_id, status="failed",
                detail=f"Pre-trade balance read failed ({type(exc).__name__}).",
            )
            self.trending_scanner.set_live_status(
                mint, "blocked", f"Could not establish pre-trade wallet balances ({type(exc).__name__})"
            )
            return
        admitted, admission_reason = self.risk_engine.mark_submitting(reservation_id)
        if not admitted:
            self.risk_engine.release_entry(reservation_id, status="released", detail=admission_reason)
            self.trending_scanner.set_live_status(mint, "blocked", admission_reason)
            return
        try:
            result = self.trending_executor.swap(
                owner=self.wallet_address,
                keypair=self.keypair,
                input_mint="So11111111111111111111111111111111111111112",
                output_mint=mint,
                amount_raw=amount_lamports,
                action="buy",
                output_decimals=decimals,
                expected_price_usd=price,
                input_value_usd=spend,
                max_price_impact_pct=float(self.s.trending_max_price_impact_pct),
            )
        except TransactionOutcomeUncertain as exc:
            self._handle_uncertain_trade(exc, source_wallet=None, source_signature=source_signature)
            self.trending_scanner.set_live_status(
                mint, "uncertain", "Jupiter submission outcome is uncertain; verify signature and wallet balances before any retry"
            )
            return
        except TransactionFailedOnChain as exc:
            self.risk_engine.release_entry(
                reservation_id, status="failed",
                detail=f"Jupiter confirmed on-chain failure: {str(exc.detail)[:300]}",
            )
            self.trending_scanner.set_live_status(
                mint, "failed", f"Jupiter confirmed on-chain failure: {str(exc.detail)[:350]}"
            )
            logger.warning("Trending live buy failed on-chain mint=%s signature=%s", mint[:8], exc.signature)
            return
        except (TransactionBuildFailed, TransactionSimulationFailed) as exc:
            self.risk_engine.release_entry(
                reservation_id, status="failed",
                detail=f"Jupiter pre-submit checks blocked the order: {str(exc)[:300]}",
            )
            self.trending_scanner.set_live_status(
                mint, "failed", f"Jupiter pre-submit checks blocked the order: {str(exc)[:350]}"
            )
            logger.warning("Trending live buy blocked before submission mint=%s: %s", mint[:8], exc)
            return
        except Exception as exc:
            uncertain = TransactionOutcomeUncertain(
                "signature-unavailable", "buy", mint,
                f"Unclassified execution exception after submission boundary ({type(exc).__name__}); reconcile before retry.",
            )
            self._handle_uncertain_trade(uncertain, source_wallet=None, source_signature=source_signature)
            self.trending_scanner.set_live_status(mint, "uncertain", "Unclassified execution exception; global entry gate engaged.")
            return

        tx_sig = str(result.get("signature") or "")
        try:
            after_token = self.executor.token_balance_raw(self.wallet_address, mint)
            after_sol = self.rpc.sol_balance_lamports(self.wallet_address)
        except Exception as exc:
            uncertain = TransactionOutcomeUncertain(
                tx_sig, "buy", mint,
                f"Swap reported success but wallet balance delta could not be verified ({type(exc).__name__})",
            )
            self._handle_uncertain_trade(uncertain, source_wallet=None, source_signature=source_signature)
            self.trending_scanner.set_live_status(
                mint, "uncertain", "Jupiter reported success, but actual output balance could not be reconciled"
            )
            return
        raw_out = int(after_token) - int(before_token)
        sol_delta_lamports = int(after_sol) - int(before_sol)
        if raw_out <= 0 or sol_delta_lamports >= 0 or not tx_sig:
            uncertain = TransactionOutcomeUncertain(
                tx_sig or "signature-unavailable", "buy", mint,
                "Jupiter reported success but token output/SOL debit did not reconcile to the wallet",
            )
            self._handle_uncertain_trade(uncertain, source_wallet=None, source_signature=source_signature)
            self.trending_scanner.set_live_status(
                mint, "uncertain", "Reported swap did not reconcile to a positive token balance and SOL debit"
            )
            return

        total_supply_tokens = float(signal.get("total_supply_tokens") or self._read_total_supply_tokens(mint) or 0)
        effective_price = spend / raw_to_units(raw_out, decimals)
        cost_basis = abs(sol_delta_lamports) / 1_000_000_000 * sol_price
        timestamp = datetime.now(timezone.utc).isoformat()
        position = {
            "mint": mint, "decimals": decimals, "amount_raw": str(raw_out),
            "entry_price_usd": effective_price, "initial_amount_raw": str(raw_out),
            "peak_price_usd": effective_price, "tp1_done": 0, "tp2_done": 0,
            "opened_at": timestamp, "last_price_usd": effective_price,
            "invested_usd": cost_basis, "source_wallet": None,
            "source_signature": source_signature, "strategy": "trending_discovery",
            "total_supply_tokens": total_supply_tokens,
            "entry_market_cap_usd": effective_price * total_supply_tokens if total_supply_tokens > 0 else 0.0,
            "entry_market_cap_source": (
                "Estimated: actual Jupiter swap notional per token × total supply (FDV-style)"
                if total_supply_tokens > 0 else ""
            ),
        }
        try:
            self.store.upsert_position(position)
            self.store.set_state(f"pool:{mint}", "jupiter")
            self.store.increment_daily_entries()
            self.store.add_trade(
                mode=self.s.mode, action="BUY", mint=mint, amount_raw=str(raw_out),
                price_usd=effective_price, value_usd=cost_basis,
                reason="trending-token Jupiter Ultra buy",
                source_wallet=None, source_signature=source_signature,
                tx_signature=tx_sig, strategy="trending_discovery",
            )
        except Exception as exc:
            self._handle_uncertain_trade(
                TransactionOutcomeUncertain(
                    tx_sig, "buy", mint,
                    f"Confirmed swap could not be persisted to the local ledger ({type(exc).__name__}).",
                ),
                source_wallet=None, source_signature=source_signature,
            )
            self.trending_scanner.set_live_status(
                mint, "uncertain", "Swap confirmed but ledger persistence failed; reconcile before resuming."
            )
            return
        self.risk_engine.release_entry(
            reservation_id, status="completed",
            detail="Swap and wallet balance deltas reconciled into the position/trade ledger.",
        )
        self.price_cache[mint] = effective_price
        self.price_cache_at[mint] = time.time()
        self.store.set_state(f"exit_blocked_balance_zero:{mint}", "")
        self.trending_scanner.set_live_status(
            mint, "opened", f"Jupiter Ultra buy confirmed and reconciled; swap input cap ${spend:.2f}"
        )
        self._refresh_feed_subscriptions()
        logger.warning(
            "TRENDING LIVE BUY confirmed mint=%s input_cap=$%.2f actual_wallet_debit=$%.4f",
            mint[:8], spend, cost_basis,
        )
        logger.warning("Live transaction confirmed: https://solscan.io/tx/%s", tx_sig)

    def _journal_entry_decision(
        self, signal: dict, *, strategy: str, decision: str, reason: str = "",
        price_usd: float | None = None, liquidity_usd: float | None = None,
    ) -> None:
        """Persist a strategy entry decision and its reason without raw secrets."""
        try:
            mint = str(signal.get("mint") or "")
            signature = str(signal.get("signature") or signal.get("source_signature") or "")
            wallet = str(signal.get("wallet") or signal.get("source_wallet") or "")
            self.research.record_signal(
                mode=self.s.mode, strategy=strategy, mint=mint,
                signal_type="entry_decision", decision=decision,
                rejection_reason=reason, price_usd=price_usd,
                liquidity_usd=liquidity_usd, source_wallet=wallet,
                source_signature=signature,
                payload={**signal, "decision": decision, "reason": reason},
            )
        except Exception as exc:
            logger.warning("Research entry-decision journal write failed (%s)", type(exc).__name__)

    def buy(
        self, signal: dict, *, strategy: str = "leader_copy",
        entry_cap_usd: float | None = None,
    ):
        if strategy == "trending_discovery":
            return self._buy_trending_discovery(signal, entry_cap_usd=entry_cap_usd)
        mint = signal["mint"]
        if strategy == "token_discovery":
            if (
                self.s.mode != "live"
                or not self._runtime_discovery_flag("token_discovery", "enabled", "token_discovery_enabled")
                or not self._runtime_discovery_flag("token_discovery", "live_enabled", "token_discovery_live_enabled")
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
            self._journal_entry_decision(signal, strategy=strategy, decision="rejected", reason="An earlier transaction outcome remains unresolved.")
            logger.warning(
                "Skipping buy for %s because an earlier transaction outcome remains unresolved",
                mint[:8],
            )
            return
        if self.store.get_position(mint):
            self._journal_entry_decision(signal, strategy=strategy, decision="rejected", reason="A position for this mint is already tracked.")
            return
        if self._native_build_retry_deferred(mint):
            self._journal_entry_decision(signal, strategy=strategy, decision="rejected", reason="Pre-submission build retry is deferred.")
            logger.debug("Skipping repeated build for mint=%s during pre-submission cooldown", mint[:8])
            return
        sol_price = self.market.sol_price_usd()
        market = self.market.token_market(mint)
        observed_price = float(signal.get("observed_price_usd") or 0)
        if observed_price <= 0 and market:
            observed_price = float(market.get("price_usd") or 0)
        if observed_price <= 0:
            self._journal_entry_decision(signal, strategy=strategy, decision="rejected", reason="No reliable trade or market USD price yet.", liquidity_usd=signal.get("curve_liquidity_usd"))
            logger.info("Reject Pump.fun buy %s: no reliable trade or market price yet", mint)
            return

        curve_liquidity = float(signal.get("curve_liquidity_usd") or 0)
        pair_liquidity = float(market.get("liquidity_usd") or 0) if market else 0.0
        actual_liquidity = max(curve_liquidity, pair_liquidity)
        discovery_liquidity_floor = max(
            5000.0, float(getattr(self.s, "token_discovery_min_liquidity_usd", 5000.0))
        )
        portfolio_liquidity_floor = max(
            5000.0, float(getattr(self.s, "min_liquidity_usd", 5000.0))
        )
        if strategy == "token_discovery" and curve_liquidity < discovery_liquidity_floor:
            self._journal_entry_decision(signal, strategy=strategy, decision="rejected",
                reason=f"Fresh real curve liquidity {curve_liquidity:.2f} USD below {discovery_liquidity_floor:.2f} USD.",
                price_usd=observed_price, liquidity_usd=curve_liquidity)
            logger.info(
                "Reject discovery buy %s: fresh real curve liquidity %.0f USD below %.0f USD",
                mint[:8], curve_liquidity, discovery_liquidity_floor,
            )
            return
        curve_state_verified = bool(signal.get("curve_state_verified"))
        venue = str(signal.get("venue", "pump"))
        # Do not allow a websocket signal to bypass the minimum liquidity floor.
        # Discovery can use the canonical on-chain curve only after its fresh
        # security/liquidity checks; regular leader-copy entries need indexed market data.
        # A scanner-certified discovery candidate may use its verified on-chain
        # bonding curve as the market source before DEX Screener indexes the pair.
        # This does not apply to leader-copy buys and never bypasses security,
        # real-curve liquidity, price, portfolio-risk, or transaction checks.
        allow_verified_curve_age_bypass = verified_curve_fallback_allowed(
            signal,
            strategy=strategy,
            venue=venue,
            curve_liquidity_usd=curve_liquidity,
            minimum_liquidity_usd=max(self.s.min_liquidity_usd, portfolio_liquidity_floor),
        )
        unindexed_pump_entry = (
            allow_verified_curve_age_bypass
            and (market is None or int(market.get("pair_age_seconds") or 0) < self.s.min_pair_age_seconds)
        )
        if market is None and not allow_verified_curve_age_bypass:
            self._journal_entry_decision(signal, strategy=strategy, decision="rejected",
                reason="Indexed market pair unavailable and verified on-chain curve fallback criteria were not met.",
                price_usd=observed_price, liquidity_usd=actual_liquidity)
            logger.info(
                "Reject buy %s: indexed market pair unavailable and fresh verified on-chain curve fallback criteria were not met",
                mint[:8],
            )
            return
        if actual_liquidity < portfolio_liquidity_floor:
            self._journal_entry_decision(signal, strategy=strategy, decision="rejected",
                reason=f"Observed liquidity {actual_liquidity:.2f} USD below {portfolio_liquidity_floor:.2f} USD.",
                price_usd=observed_price, liquidity_usd=actual_liquidity)
            logger.info("Reject Pump.fun buy %s: verified/observed liquidity %.0f USD below %.0f USD",
                        mint, actual_liquidity, portfolio_liquidity_floor)
            return
        if market and int(market.get("pair_age_seconds") or 0) < self.s.min_pair_age_seconds and not unindexed_pump_entry:
            self._journal_entry_decision(signal, strategy=strategy, decision="rejected",
                reason="Market pair age is below the configured minimum.",
                price_usd=observed_price, liquidity_usd=actual_liquidity)
            logger.info("Reject Pump.fun buy %s: pair age below filter", mint)
            return
        if unindexed_pump_entry:
            logger.warning(
                "Verified-curve Pump launch entry for %s without a DEX-indexed pair: "
                "on-chain curve liquidity passed; fixed entry/exposure caps apply, but exit liquidity is not assured",
                mint,
            )

        equity = self.equity()
        self._refresh_risk_state(equity)
        allowed, reason = self._can_enter(
            equity, strategy=strategy, entry_cap_usd=entry_cap_usd
        )
        if not allowed:
            self._journal_entry_decision(signal, strategy=strategy, decision="rejected",
                reason=reason, price_usd=observed_price, liquidity_usd=actual_liquidity)
            logger.info("Reject buy %s: %s", mint, reason)
            return
        effective_entry_cap = self.s.max_entry_usd
        if strategy == "token_discovery":
            effective_entry_cap = min(
                self.s.max_entry_usd, 0.50, float(self.s.token_discovery_max_entry_usd),
                float(entry_cap_usd if entry_cap_usd is not None else self.s.token_discovery_max_entry_usd),
            )
        spend = min(effective_entry_cap, self.s.max_total_exposure_usd - self._current_positions_value())
        minimum_practical_spend = 0.10 if strategy == "token_discovery" else 0.50
        if spend < minimum_practical_spend:
            self._journal_entry_decision(signal, strategy=strategy, decision="rejected",
                reason="Insufficient exposure headroom for a practical trade.",
                price_usd=observed_price, liquidity_usd=actual_liquidity)
            return

        reservation_id, reserve_reason = self.risk_engine.reserve_entry(
            mint, strategy, spend,
            exposure_usd=spend + (self.s.paper_swap_cost_usd if self.s.mode == "paper" else 0.05),
        )
        if not reservation_id:
            self._journal_entry_decision(signal, strategy=strategy, decision="rejected",
                reason=f"Risk reservation denied: {reserve_reason}",
                price_usd=observed_price, liquidity_usd=actual_liquidity)
            logger.warning("Reject buy %s: risk reservation denied: %s", str(mint)[:8], reserve_reason)
            return

        # Entry is now strategy-qualified: after all local filters and atomic risk
        # reservation. Separate actual fills and transaction failures in the ledger.
        self._journal_entry_decision(signal, strategy=strategy, decision="would_copy",
            price_usd=observed_price, liquidity_usd=actual_liquidity)
        try:
            self.research.record_event(
                mode=self.s.mode, strategy=strategy, event_type="qualified_entry",
                event={**signal, "price_usd": observed_price, "liquidity_usd": actual_liquidity,
                       "decision": "would_copy"},
                observed_at=time.time(),
            )
            if strategy in {"leader_copy", "token_discovery"}:
                self.research.record_cost_estimate(
                    strategy=strategy, mint=mint, notional_usd=spend,
                    source="PumpPortal 0.5% provider-fee assumption; other cost components left unknown",
                )
        except Exception as exc:
            logger.warning("Research qualified-entry/cost journal write failed (%s)", type(exc).__name__)

        decimals = int(signal.get("decimals") or 6)
        total_supply_tokens = float(signal.get("total_supply_tokens") or 0)
        if total_supply_tokens <= 0:
            total_supply_tokens = self._read_total_supply_tokens(mint)
        venue = str(signal.get("venue", "pump"))
        if venue not in {"pump", "pump-amm"}:
            venue = "pump"
        entry_started_at = time.time()

        if self.s.mode == "paper":
            cost = self.s.paper_swap_cost_usd
            raw_out = int(spend / observed_price * (10 ** decimals))
            if raw_out <= 0:
                self.risk_engine.release_entry(
                    reservation_id, status="failed", detail="Paper output amount rounded to zero."
                )
                logger.info("Reject %s: estimated output rounds to zero", mint)
                return
            admitted, admission_reason = self.risk_engine.mark_submitting(reservation_id)
            if not admitted:
                self.risk_engine.release_entry(reservation_id, status="released", detail=admission_reason)
                logger.warning("Reject %s: %s", str(mint)[:8], admission_reason)
                return
            cash = float(self.store.get_state("paper_cash", "0"))
            self.store.set_state("paper_cash", cash - spend - cost)
            tx_sig = "PAPER-" + signal["signature"][:20]
            cost_basis = spend + cost
            # Token entry price is swap notional per token. Simulated costs belong
            # in P&L basis, not in the token price used by stops and profit targets.
            effective_price = spend / raw_to_units(raw_out, decimals)
            used_pool = venue
        else:
            admitted, admission_reason = self.risk_engine.mark_submitting(reservation_id)
            if not admitted:
                self.risk_engine.release_entry(reservation_id, status="released", detail=admission_reason)
                logger.warning("Reject %s: %s", str(mint)[:8], admission_reason)
                return
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
                self.risk_engine.release_entry(
                    reservation_id, status="failed",
                    detail="Transaction was verified expired and not landed.",
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
                self.risk_engine.release_entry(
                    reservation_id, status="failed",
                    detail=f"Transaction was blocked before submission: {str(exc)[:250]}",
                )
                # Discovery slippage failures are known pre-submit events. Let the
                # discovery controller redo fresh checks while the candidate remains
                # inside its short freshness window; do not turn them into a global halt.
                if strategy == "token_discovery" and isinstance(exc, TransactionSimulationFailed):
                    logger.warning(
                        "Discovery buy simulation failed for %s before signing/submission; "
                        "delegating to fresh-entry recovery (retryable_slippage=%s)",
                        mint[:8], exc.retryable,
                    )
                    raise
                delay = self._defer_native_build_retry(mint, str(exc))
                logger.warning(
                    "Skip copied buy for %s: %s. No transaction was signed or submitted; "
                    "retry for this mint is deferred %ds.",
                    mint[:8], " ".join(str(exc).split())[:220], delay,
                )
                return
            except Exception as exc:
                self._handle_uncertain_trade(
                    TransactionOutcomeUncertain(
                        "signature-unavailable", "buy", mint,
                        f"Unclassified executor exception after submission boundary ({type(exc).__name__}); reconcile before retry.",
                    ),
                    source_wallet=source_wallet,
                    source_signature=source_signature,
                )
                return
            tx_sig = ""
            try:
                if not isinstance(result, dict):
                    raise ValueError("Executor returned a non-object result")
                tx_sig = str(result.get("signature") or "")
                used_pool = str(result.get("pool") or venue)
                raw_out = int(result.get("total_output_amount") or 0)
                if not tx_sig or raw_out <= 0:
                    raise ValueError("Missing signature or non-positive token output")
                # Wallet SOL delta includes swap input plus network/priority fees
                # and account rent; retain its all-in amount in P&L basis.
                effective_spend = max(0, -int(result.get("sol_delta_lamports") or 0)) / 1_000_000_000 * sol_price
                cost_basis = effective_spend if effective_spend > 0 else spend
                effective_price = spend / raw_to_units(raw_out, decimals)
            except Exception as exc:
                self._handle_uncertain_trade(
                    TransactionOutcomeUncertain(
                        tx_sig or "signature-unavailable", "buy", mint,
                        f"Executor returned but fill details could not be safely reconciled ({type(exc).__name__}).",
                    ),
                    source_wallet=source_wallet,
                    source_signature=source_signature,
                )
                return

        # Start the hold timer only after the buy has succeeded and its fill is known.
        timestamp = datetime.now(timezone.utc).isoformat()
        position = {
            "mint": mint, "decimals": decimals, "amount_raw": str(raw_out),
            "entry_price_usd": effective_price, "initial_amount_raw": str(raw_out),
            "peak_price_usd": effective_price, "tp1_done": 0, "tp2_done": 0,
            "opened_at": timestamp, "last_price_usd": effective_price,
            "invested_usd": cost_basis, "source_wallet": source_wallet,
            "source_signature": source_signature, "strategy": strategy,
            "total_supply_tokens": total_supply_tokens,
            "entry_market_cap_usd": effective_price * total_supply_tokens if total_supply_tokens > 0 else 0.0,
            "entry_market_cap_source": (
                "Estimated: actual swap notional per token × total supply (FDV-style)"
                if total_supply_tokens > 0 else ""
            ),
        }
        try:
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
        except Exception as exc:
            self._handle_uncertain_trade(
                TransactionOutcomeUncertain(
                    str(tx_sig), "buy", mint,
                    f"Buy fill exists but local position/trade persistence failed ({type(exc).__name__}).",
                ),
                source_wallet=source_wallet,
                source_signature=source_signature,
            )
            return
        self.risk_engine.release_entry(
            reservation_id, status="completed", detail="Buy fill and ledger record completed."
        )
        # Do not overwrite a fresher trade tick that arrived while the swap was
        # being built/submitted. Otherwise seed the mark from the actual swap fill.
        if float(self.price_cache_at.get(mint, 0) or 0) <= entry_started_at:
            self.price_cache[mint] = effective_price
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
        # Unwrap first so dashboard-originated manual exits remain identifiable
        # even when position management dispatches them as "pending exit: ...".
        was_pending_exit = reason.startswith("pending exit:")
        while reason.startswith("pending exit:"):
            reason = reason[len("pending exit:"):].strip()
        manual_exit = reason.lower().startswith("manual exit")

        if self.store.get_state(f"unresolved_trade:{mint}", ""):
            if manual_exit:
                self.message = (
                    f"Manual exit for {mint[:8]} is queued but blocked: an earlier transaction "
                    "outcome is unresolved. Reconcile it before another sell; no transaction submitted."
                )
                logger.warning("%s", self.message)
            else:
                logger.debug(
                    "Skipping sell retry for %s because an earlier transaction outcome remains unresolved",
                    mint[:8],
                )
            return False
        position = self.store.get_position(mint)
        if not position:
            if manual_exit:
                self.message = f"Manual exit for {mint[:8]} stopped: no tracked open position remains."
            return False
        full_exit = (was_pending_exit or manual_exit or
                     reason in {"marked loss stop", "trailing stop"} or
                     reason.startswith("followed wallet ") or reason.startswith("experiment expiry") or
                     reason.startswith("total loss limit"))
        pending_key = f"exit_pending:{mint}"
        pending_amount_key = f"exit_pending_amount_raw:{mint}"
        pending_wallet_key = f"exit_pending_source_wallet:{mint}"
        pending_signature_key = f"exit_pending_source_signature:{mint}"
        if full_exit:
            self.store.set_state(pending_key, reason)
            if source_wallet:
                self.store.set_state(pending_wallet_key, source_wallet)
            elif not was_pending_exit:
                self.store.set_state(pending_wallet_key, "")
            if source_signature:
                self.store.set_state(pending_signature_key, source_signature)
            elif not was_pending_exit:
                self.store.set_state(pending_signature_key, "")
        current_raw = int(position["amount_raw"])
        requested_raw = min(max(0, int(amount_raw)), current_raw)
        if requested_raw <= 0:
            return False
        if full_exit:
            # Persist the desired amount before any cooldown or other retry guard so
            # a leader sell cannot be consumed and forgotten before submission.
            self.store.set_state(pending_amount_key, str(requested_raw))
        if self._native_build_retry_deferred(mint):
            if manual_exit:
                try:
                    retry_at = float(self.store.get_state(f"native_build_retry_after:{mint}", "0") or 0)
                except (TypeError, ValueError):
                    retry_at = time.time()
                remaining = max(0, int(retry_at - time.time()))
                last_failure = str(self.store.get_state(f"native_build_failure:{mint}", "") or "")
                detail = f" Last builder error: {last_failure[:180]}" if last_failure else ""
                self.message = (
                    f"Manual exit for {mint[:8]} remains pending; retry cooldown {remaining}s, "
                    f"no transaction submitted.{detail}"
                )
            logger.debug("Sell builder for mint=%s is in pre-submission cooldown; pending exit retained", mint[:8])
            return False
        decimals = int(position["decimals"])
        # A sell must not wait on a fresh DexScreener HTTP request. For live exits,
        # confirmed SOL proceeds determine realized P&L; this mark is only a fallback
        # for stop bookkeeping and the dashboard. Prefer a fresh trade tick, then a
        # recent market cache, then the last persisted mark/entry price.
        mark_now = time.time()
        price_cache = getattr(self, "price_cache", {})
        price_cache_at = getattr(self, "price_cache_at", {})
        market_price_cache = getattr(self, "market_price_cache", {})
        market_price_cache_at = getattr(self, "market_price_cache_at", {})
        try:
            feed_price = float(price_cache.get(mint, 0) or 0)
            feed_age = mark_now - float(price_cache_at.get(mint, 0) or 0)
            cached_market_price = float(market_price_cache.get(mint, 0) or 0)
            cached_market_age = mark_now - float(market_price_cache_at.get(mint, 0) or 0)
            stored_price = max(
                0.0,
                float(position.get("last_price_usd") or position.get("entry_price_usd") or 0.0),
            )
        except (TypeError, ValueError, OverflowError):
            feed_price, feed_age, cached_market_price, cached_market_age = 0.0, float("inf"), 0.0, float("inf")
            stored_price = 0.0
        if feed_price > 0 and 0 <= feed_age <= 10:
            mark_price = feed_price
        elif cached_market_price > 0 and 0 <= cached_market_age <= 3:
            mark_price = cached_market_price
        else:
            mark_price = stored_price
        if mark_price <= 0 and manual_exit:
            # Live execution does not require a market quote; a stored entry mark is
            # only for bookkeeping. Never invent proceeds from this fallback.
            mark_price = max(0.0, stored_price)
        if mark_price <= 0:
            if manual_exit:
                self.message = (
                    f"Manual exit for {mint[:8]} not submitted: no usable stored or current price "
                    "is available to value this sale."
                )
            return False

        minimum_hold = max(
            120, int(getattr(self.s, "min_hold_before_loss_exit_seconds", 120))
        )
        wait_seconds = loss_exit_hold_remaining_seconds(
            position,
            mark_price,
            reason,
            minimum_hold,
            emergency_loss_bypass_pct=float(
                getattr(self.s, "emergency_loss_bypass_pct", 0.50)
            ),
        )
        if wait_seconds > 0:
            # Recheck automatic stop conditions each tick instead of freezing a stale
            # trigger, so a recovery above entry naturally cancels the loss stop.
            if reason in {"marked loss stop", "trailing stop"}:
                self.store.set_state(pending_key, "")
                self.store.set_state(pending_amount_key, "")
                self.store.set_state(pending_wallet_key, "")
                self.store.set_state(pending_signature_key, "")
            notice_key = f"loss_exit_hold_notice_at:{mint}"
            try:
                last_notice = float(self.store.get_state(notice_key, "0") or 0)
            except (TypeError, ValueError):
                last_notice = 0.0
            now = time.time()
            if now - last_notice >= 30:
                logger.warning(
                    "Deferring loss exit for %s by %.1fs to enforce the %ds minimum hold",
                    mint[:8], wait_seconds, minimum_hold,
                )
                self.store.set_state(notice_key, str(now))
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
            if position.get("strategy", "leader_copy") == "trending_discovery":
                try:
                    available = self.executor.token_balance_raw(self.wallet_address, mint)
                except Exception as exc:
                    logger.error("Cannot confirm trending-token balance for %s: %s", mint[:8], type(exc).__name__)
                    return False
                requested_raw = min(requested_raw, available)
                if requested_raw <= 0:
                    self.store.set_state(
                        f"exit_blocked_balance_zero:{mint}",
                        "Wallet reports zero trending-token balance; inspect Jupiter transaction history before reconciling.",
                    )
                    self.store.set_state(pending_key, "")
                    self.store.set_state(pending_amount_key, "")
                    self.store.set_state(pending_wallet_key, "")
                    self.store.set_state(pending_signature_key, "")
                    if manual_exit:
                        self.message = (
                            f"Manual exit for {mint[:8]} paused: token balance is zero and no "
                            "confirmed Jupiter sale has been reconciled."
                        )
                    return False
                before_raw = available
                try:
                    sell_sol_price_usd = self.market.sol_price_usd()
                    before_sol = self.rpc.sol_balance_lamports(self.wallet_address)
                except Exception as exc:
                    logger.error("Cannot read SOL balance before trending sell %s: %s", mint[:8], type(exc).__name__)
                    return False
                try:
                    jupiter_result = self.trending_executor.swap(
                        owner=self.wallet_address,
                        keypair=self.keypair,
                        input_mint=mint,
                        output_mint="So11111111111111111111111111111111111111112",
                        amount_raw=requested_raw,
                        action="sell",
                        output_decimals=9,
                    )
                    tx_sig = str(jupiter_result.get("signature") or "")
                    if not tx_sig:
                        raise TransactionOutcomeUncertain(
                            "signature-unavailable", "sell", mint,
                            "Jupiter reported success but did not provide a transaction signature",
                        )
                except TransactionOutcomeUncertain as exc:
                    self._handle_uncertain_trade(
                        exc, source_wallet=source_wallet, source_signature=source_signature,
                    )
                    return False
                except TransactionFailedOnChain as exc:
                    delay = self._defer_native_build_retry(mint, str(exc))
                    logger.warning(
                        "Jupiter trending sell failed on-chain mint=%s; retry deferred %ds: %s",
                        mint[:8], delay, str(exc)[:250],
                    )
                    return False
                except (TransactionBuildFailed, TransactionSimulationFailed) as exc:
                    detail = " ".join(str(exc).split())[:300]
                    delay = self._defer_native_build_retry(mint, detail)
                    if manual_exit:
                        self.message = (
                            f"Manual trending exit for {mint[:8]} was not submitted; "
                            f"retry deferred {delay}s. {detail}"
                        )
                    logger.warning("Jupiter trending sell not submitted mint=%s: %s", mint[:8], detail)
                    return False
                try:
                    after_raw = self.executor.token_balance_raw(self.wallet_address, mint)
                    after_sol = self.rpc.sol_balance_lamports(self.wallet_address)
                except Exception as exc:
                    uncertain = TransactionOutcomeUncertain(
                        tx_sig or "signature-unavailable", "sell", mint,
                        f"Jupiter reported success but balance reconciliation failed ({type(exc).__name__})",
                    )
                    self._handle_uncertain_trade(
                        uncertain, source_wallet=source_wallet, source_signature=source_signature,
                    )
                    return False
                sold_raw = before_raw - int(after_raw)
                sol_delta_lamports = int(after_sol) - int(before_sol)
                if sold_raw <= 0 or sold_raw > requested_raw or sol_delta_lamports <= 0:
                    uncertain = TransactionOutcomeUncertain(
                        tx_sig or "signature-unavailable", "sell", mint,
                        "Jupiter reported success but token reduction/native SOL proceeds did not reconcile",
                    )
                    self._handle_uncertain_trade(
                        uncertain, source_wallet=source_wallet, source_signature=source_signature,
                    )
                    return False
                tx_sig = self.store.get_state(f"unresolved_trade_signature:{mint}", "") or "JUPITER_CONFIRMED"
                proceeds = (sol_delta_lamports / 1_000_000_000) * sell_sol_price_usd
                units_sold = raw_to_units(sold_raw, decimals)
            else:
                available = self.executor.token_balance_raw(self.wallet_address, mint)
                requested_raw = min(requested_raw, available)
                if requested_raw <= 0:
                    # Before pausing, look for an on-chain full Pump/PumpSwap sale after
                    # this position opened. Only an exact quantity match, wallet signature,
                    # successful execution and zero post-balance can reconcile the ledger.
                    # This is read-only chain inspection; it never retries/submits a sale.
                    evidence = None
                    if available <= 0:
                        finder = getattr(self.executor, "find_confirmed_full_sale", None)
                        if callable(finder):
                            try:
                                evidence = finder(
                                    owner=self.wallet_address,
                                    mint=mint,
                                    expected_raw=int(position.get("amount_raw") or 0),
                                    opened_at=str(position.get("opened_at") or ""),
                                )
                            except Exception as exc:
                                logger.warning(
                                    "Could not inspect prior full-sale history for %s: %s",
                                    mint[:8], " ".join(str(exc).split())[:220],
                                )
                    if evidence:
                        tx_signature = str(evidence.get("signature") or "")
                        try:
                            exited_at = datetime.fromtimestamp(
                                int(evidence.get("block_time") or 0), timezone.utc
                            ).isoformat(timespec="seconds")
                        except (TypeError, ValueError, OverflowError, OSError):
                            exited_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        reconcile_reason = (
                            "Verified on-chain full Pump/PumpSwap sell; stale position reconciled "
                            f"from confirmed signature {tx_signature}. Historical USD proceeds and "
                            "realized P&L were not recorded and are intentionally left unestimated."
                        )
                        reconciled = self.store.reconcile_confirmed_full_exit(
                            position,
                            mode=self.s.mode,
                            tx_signature=tx_signature,
                            ts=exited_at,
                            reason=reconcile_reason,
                        )
                        if reconciled:
                            self.price_cache.pop(mint, None)
                            self.price_cache_at.pop(mint, None)
                            self.message = (
                                f"Reconciled confirmed on-chain full exit for {mint[:8]}; no new sell "
                                "was submitted. Historical USD proceeds/P&L are unavailable in the ledger."
                            )
                            logger.warning(
                                "Reconciled stale position mint=%s from confirmed full-sale signature=%s; "
                                "no transaction submitted and no historical P&L inferred",
                                mint[:8], tx_signature,
                            )
                            try:
                                self._refresh_feed_subscriptions()
                            except Exception:
                                logger.exception("Could not refresh subscriptions after exit reconciliation")
                            return True
    
                    detail = (
                        "Configured wallet reports zero token balance; no matching full Pump/PumpSwap "
                        "sale was verified after the position opened."
                    )
                    self.store.set_state(f"exit_blocked_balance_zero:{mint}", detail)
                    # Stop repeated attempts. Keep the ledger position visible until a
                    # human can reconcile it or a later fresh history check succeeds.
                    self.store.set_state(f"exit_pending:{mint}", "")
                    self.store.set_state(f"exit_pending_amount_raw:{mint}", "")
                    self.store.set_state(f"exit_pending_source_wallet:{mint}", "")
                    self.store.set_state(f"exit_pending_source_signature:{mint}", "")
                    if manual_exit:
                        self.message = (
                            f"Manual exit for {mint[:8]} paused: zero wallet balance and no matching full-sale "
                            "transaction could be verified. Position retained for reconciliation."
                        )
                    logger.error(
                        "Sell paused for %s: zero token balance and no verified matching full-sale transaction; "
                        "position retained and automatic retry suppressed",
                        mint,
                    )
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
                    detail = " ".join(str(exc).split())[:220]
                    delay = self._defer_native_build_retry(mint, detail)
                    if manual_exit:
                        self.message = (
                            f"Manual exit for {mint[:8]} was not submitted: builder failed before signing; "
                            f"retry deferred {delay}s. {detail}"
                        )
                    logger.error(
                        "Could not build sell for %s; no transaction was signed or submitted. "
                        "Tracked position and pending exit are retained; retry deferred %ds: %s",
                        mint[:8], delay, detail,
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
        self._record_realized_exit_risk(realized_pnl)
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
            if strategy == "token_discovery":
                discovery = getattr(self, "token_discovery", None)
                candidate = discovery._load(mint) if discovery else None
                if candidate:
                    candidate["live_entry_status"] = "closed"
                    candidate["live_entry_reason"] = (
                        f"Live position fully exited via {reason}; realized P&L ${realized_pnl:+.4f}"
                    )
                    discovery._save(candidate)
            elif strategy == "trending_discovery":
                self.trending_scanner.set_live_status(
                    mint, "closed",
                    f"Trending live position fully exited via {reason}; realized P&L ${realized_pnl:+.4f}",
                )
        else:
            self.price_cache[mint] = mark_price
            self.price_cache_at[mint] = time.time()
        self.store.set_state(f"exit_blocked_balance_zero:{mint}", "")
        self.store.set_state(pending_key, "")
        self.store.set_state(pending_amount_key, "")
        self.store.set_state(pending_wallet_key, "")
        self.store.set_state(pending_signature_key, "")
        self.store.set_state(f"loss_exit_hold_notice_at:{mint}", "")
        self._refresh_feed_subscriptions()
        logger.warning("SELL %s raw=%s proceeds=%.4f PnL=%.4f reason=%s", mint, sold_raw, proceeds, realized_pnl, reason)
        if self.s.mode == "live":
            logger.warning("Live transaction confirmed: https://solscan.io/tx/%s", tx_sig)
        return True

    def _set_discovery_live_status(self, candidate: dict, status: str, reason: str) -> None:
        candidate["live_entry_status"] = status
        candidate["live_entry_status_updated_at"] = time.time()
        candidate["live_entry_reason"] = " ".join(str(reason or "").split())[:500]
        self.token_discovery._save(candidate)

    def _schedule_discovery_live_preflight_retry(self, candidate: dict, reason: str) -> None:
        """Retry transient pre-submission failures only while the original entry is fresh."""
        now = time.time()
        entry_at = float(candidate.get("shadow_entry_at") or 0)
        retries = int(candidate.get("live_entry_retry_count") or 0)
        fresh = (
            entry_at >= self.run_started_at
            and entry_at <= now
            and now - entry_at <= 30
        )
        candidate["live_entry_preflight_at"] = now
        checked_at = datetime.now().astimezone().strftime("%H:%M:%S")
        detail = f"{reason}; preflight checked at {checked_at} local; no transaction submitted"
        if fresh and retries < 2:
            retry_count = retries + 1
            candidate["live_entry_status"] = "retryable"
            candidate["live_entry_status_updated_at"] = now
            candidate["live_entry_retry_count"] = retry_count
            candidate["live_entry_retry_at"] = now + 4
            candidate["live_entry_reason"] = (
                f"{detail}; retry {retry_count}/2 in 4s while inside the original 30s freshness window"
            )[:500]
            self.token_discovery._save(candidate)
            logger.warning(
                "Discovery live preflight will retry mint=%s attempt=%d/2: %s",
                str(candidate.get("mint") or "")[:8], retry_count, reason,
            )
            return

        if not fresh:
            status = "skipped_stale"
            detail += "; original 30s entry freshness window expired, no catch-up buy"
        else:
            status = "blocked"
            detail += "; transient preflight retry limit reached, no buy submitted"
        self._set_discovery_live_status(candidate, status, detail)

    def _process_trending_live_entries(self) -> None:
        """Consider only freshly screened trending candidates; each mint gets one live attempt."""
        if (
            self.s.mode != "live"
            or not self._runtime_discovery_flag("trending_scanner", "enabled", "trending_discovery_enabled")
            or not self._runtime_discovery_flag("trending_scanner", "live_enabled", "trending_discovery_live_enabled")
        ):
            return

        # Do not burn candidate one-shot state while another discovery strategy
        # already owns the single discovery slot. Re-evaluate after it exits.
        if any(
            p.get("strategy", "leader_copy") in {"token_discovery", "trending_discovery"}
            for p in self.store.list_positions()
        ):
            return

        now = time.time()
        freshness_seconds = max(90, min(180, self.s.trending_discovery_poll_seconds + 30))
        candidates = self.trending_scanner.dashboard_snapshot().get("candidates", [])
        for original in candidates:
            if str(original.get("status") or "") != "SCREENED":
                continue
            mint = str(original.get("mint") or "")
            if not mint:
                continue
            prior_status = self.store.get_state(f"trending_live_status:{mint}", "")
            # Submitted/checking candidates surviving a process restart are ambiguous;
            # reconcile rather than leaving the UI stuck or accidentally replaying.
            if prior_status:
                if prior_status in {"checking", "submitting"}:
                    _, _, prior_at = self.trending_scanner._stored_live_status(mint)
                    if prior_at < self.run_started_at:
                        self.trending_scanner.set_live_status(
                            mint, "needs_reconciliation",
                            "Previous process ended during a live entry attempt; inspect wallet balances and transaction history. No retry.",
                        )
                continue
            try:
                updated_at = float(original.get("updated_at") or 0)
                pool_created_at = float(original.get("pool_created_at") or 0)
            except (TypeError, ValueError):
                updated_at, pool_created_at = 0.0, 0.0
            if updated_at <= 0 or now - updated_at > freshness_seconds or updated_at > now + 5:
                self.trending_scanner.set_live_status(
                    mint, "skipped_stale", "Trending/risk screen is stale; a fresh scan is required"
                )
                continue
            if (
                pool_created_at <= 0
                or pool_created_at > now
                or now - pool_created_at > self.s.trending_max_pool_age_hours * 3600
            ):
                self.trending_scanner.set_live_status(
                    mint, "blocked", "Pool creation time is missing or outside the configured age window"
                )
                continue
            if (
                not original.get("dexscreener_confirmed")
                or original.get("same_pool_crosscheck") is not True
                or original.get("market_data_discrepancy")
                or original.get("onchain_status") != "PASS"
                or original.get("rugcheck_status") != "SAFE_CANDIDATE"
            ):
                self.trending_scanner.set_live_status(
                    mint, "blocked", "Independent market, on-chain mint or RugCheck result is not trade-qualified"
                )
                continue

            # Verify the same DEX pool again just before order construction.
            try:
                market = self.market.token_market(mint)
            except Exception as exc:
                self.trending_scanner.set_live_status(
                    mint, "blocked", f"Fresh market recheck failed ({type(exc).__name__})"
                )
                continue
            if not market:
                self.trending_scanner.set_live_status(mint, "blocked", "Fresh DEX pool is unavailable")
                continue
            if str(market.get("pair_address") or "") != str(original.get("dexscreener_pair_address") or ""):
                self.trending_scanner.set_live_status(
                    mint, "blocked", "Highest-liquidity DEX pair changed since screening"
                )
                continue
            raw_market = market.get("raw") or {}
            liquidity = float(market.get("liquidity_usd") or 0)
            price = float(market.get("price_usd") or 0)
            volume = raw_market.get("volume") or {}
            txns = raw_market.get("txns") or {}
            changes = raw_market.get("priceChange") or {}
            buys = int(float(((txns.get("h1") or {}).get("buys")) or 0))
            sells = int(float(((txns.get("h1") or {}).get("sells")) or 0))
            volume1 = float(volume.get("h1") or 0)
            pair_age = int(market.get("pair_age_seconds") or 0)
            if (
                liquidity < self.s.trending_min_liquidity_usd
                or volume1 < self.s.trending_min_volume_1h_usd
                or price <= 0
                or pair_age < self.s.min_pair_age_seconds
            ):
                self.trending_scanner.set_live_status(
                    mint, "blocked", "Fresh same-pool price/liquidity/volume/age requirements no longer pass"
                )
                continue
            if buys <= sells:
                self.trending_scanner.set_live_status(
                    mint, "blocked", "Fresh DEX one-hour trade flow no longer favors buys"
                )
                continue

            gecko_liquidity = float(original.get("gecko_liquidity_usd") or original.get("liquidity_usd") or 0)
            gecko_volume = float(original.get("volume_1h_usd") or 0)
            dex_liquidity = liquidity
            if gecko_liquidity > 0 and max(gecko_liquidity, dex_liquidity) / min(gecko_liquidity, dex_liquidity) > 3:
                self.trending_scanner.set_live_status(
                    mint, "blocked", "Fresh DEX and GeckoTerminal liquidity now disagree by more than 3x"
                )
                continue
            if gecko_volume > 0 and volume1 > 0 and max(gecko_volume, volume1) / min(gecko_volume, volume1) > 5:
                self.trending_scanner.set_live_status(
                    mint, "blocked", "Fresh DEX and GeckoTerminal one-hour volume now disagree by more than 5x"
                )
                continue
            if (
                changes.get("h1") is not None
                and abs(float(original.get("change_1h_pct") or 0) - float(changes.get("h1") or 0)) > 40
            ):
                self.trending_scanner.set_live_status(
                    mint, "blocked", "Fresh DEX and GeckoTerminal one-hour price changes disagree by more than 40 points"
                )
                continue

            fresh_candidate = dict(original)
            fresh_candidate.update({
                "liquidity_usd": min(gecko_liquidity, dex_liquidity) if gecko_liquidity > 0 else dex_liquidity,
                "volume_1h_usd": volume1,
                "volume_24h_usd": float(volume.get("h24") or 0),
                "change_1h_pct": float(changes.get("h1") or 0),
                "change_24h_pct": float(changes.get("h24") or 0),
                "buys_1h": buys,
                "sells_1h": sells,
                "dexscreener_buys_1h": buys,
                "dexscreener_sells_1h": sells,
                "risk_flags": list(original.get("risk_flags") or []),
            })
            # Refresh on-chain mint controls and RugCheck again immediately before trading.
            try:
                fresh_candidate = self.trending_scanner._evaluate(fresh_candidate)
            except Exception as exc:
                self.trending_scanner.set_live_status(
                    mint, "blocked", f"Fresh mint/RugCheck recheck failed ({type(exc).__name__})"
                )
                continue
            if fresh_candidate.get("status") != "SCREENED":
                self.trending_scanner.set_live_status(
                    mint, "blocked",
                    "Fresh security recheck did not pass: " + str(fresh_candidate.get("decision") or "unknown"),
                )
                continue
            if int(fresh_candidate.get("momentum_score") or 0) < self.s.trending_min_score:
                self.trending_scanner.set_live_status(
                    mint, "blocked", f"Fresh momentum score is below {self.s.trending_min_score}/100"
                )
                continue

            try:
                mint_account = self.rpc.call(
                    "getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}]
                ) or {}
                account = mint_account.get("value") or {}
                info = ((((account.get("data") or {}).get("parsed") or {}).get("info")) or {})
                decimals = int(info.get("decimals", -1))
                supply_raw = int(info.get("supply") or 0)
                if decimals < 0 or decimals > 18 or supply_raw <= 0:
                    raise ValueError("invalid token decimals/supply")
                total_supply_tokens = supply_raw / (10 ** decimals)
            except Exception as exc:
                self.trending_scanner.set_live_status(
                    mint, "blocked", f"Could not verify fresh mint decimals/supply ({type(exc).__name__})"
                )
                continue

            # Every candidate is recorded as checked before the one-shot buy path begins.
            self.trending_scanner.set_live_status(
                mint, "checking", "Fresh same-pool, mint, holder-risk and RugCheck gates passed; verifying portfolio budget"
            )
            signal = {
                "mint": mint,
                "wallet": "",
                "signature": f"trending-discovery-{mint}-{int(updated_at * 1000)}",
                "side": "buy",
                "amount_ui": 0.0,
                "decimals": decimals,
                "total_supply_tokens": total_supply_tokens,
                "observed_price_usd": price,
                "source": "trending_discovery",
                "fresh_security_verified": True,
                "status": "SCREENED",
                "momentum_score": int(fresh_candidate.get("momentum_score") or 0),
                "real_liquidity_usd": liquidity,
                "volume_1h_usd": volume1,
                "dexscreener_buys_1h": buys,
                "dexscreener_sells_1h": sells,
                "dexscreener_pair_address": str(market.get("pair_address") or ""),
                "same_pool_crosscheck": True,
                "market_data_discrepancy": False,
                "rugcheck_status": fresh_candidate.get("rugcheck_status"),
                "onchain_status": fresh_candidate.get("onchain_status"),
                "market_cap_usd": fresh_candidate.get("market_cap_usd"),
            }
            try:
                self.buy(
                    signal,
                    strategy="trending_discovery",
                    entry_cap_usd=min(0.50, float(self.s.trending_discovery_max_entry_usd)),
                )
            except Exception as exc:
                # A failure after an external order call may be ambiguous. Never
                # auto-retry a candidate that has been marked checking/submitting.
                if self.store.get_state(f"unresolved_trade:{mint}", ""):
                    status = "uncertain"
                    reason = "Live submission remains unresolved; reconcile signature and wallet balance before any retry"
                elif self.store.get_position(mint):
                    status = "opened"
                    reason = "Position exists in the ledger after the live call; inspect the confirmed transaction"
                else:
                    status = "failed"
                    reason = f"Unexpected live-entry error ({type(exc).__name__}); candidate consumed with no automatic retry"
                self.trending_scanner.set_live_status(mint, status, reason)
                logger.exception("Trending live entry failed mint=%s status=%s", mint[:8], status)
            # At most one candidate can initiate execution per bot loop.
            return

    def _process_discovery_live_entries(self) -> None:
        """Turn only fresh, newly-qualified shadow entries into small opt-in live buys."""
        if (
            self.s.mode != "live"
            or not self._runtime_discovery_flag("token_discovery", "enabled", "token_discovery_enabled")
            or not self._runtime_discovery_flag("token_discovery", "live_enabled", "token_discovery_live_enabled")
        ):
            return

        now = time.time()
        hard_cap = min(0.50, float(self.s.token_discovery_max_entry_usd))
        for candidate in self.token_discovery.candidates(10000):
            if candidate.get("status") != "shadow_open" or not candidate.get("would_buy"):
                continue
            live_status = str(candidate.get("live_entry_status") or "")
            mint = str(candidate.get("mint") or "")
            entry_at = float(candidate.get("shadow_entry_at") or 0)

            # A previous process may have stopped after marking submission but
            # before persisting the result. Never replay that candidate.
            if live_status == "submitting":
                if entry_at < self.run_started_at:
                    self._set_discovery_live_status(
                        candidate, "needs_reconciliation",
                        "Previous process ended during a live submission; verify its transaction signature and wallet balance. No automatic retry.",
                    )
                continue
            # A crash during preflight happens before any transaction is prepared or
            # sent. Such a candidate is safe to skip as stale, but never catch up.
            if live_status == "checking":
                if entry_at < self.run_started_at:
                    self._set_discovery_live_status(
                        candidate, "skipped_stale",
                        "Previous process ended during preflight before submission; original entry is stale and will not be bought",
                    )
                continue

            retrying = live_status == "retryable"
            # Only transient preflight failures are retryable. All submitted,
            # uncertain, failed, and non-transient blocked attempts remain one-shot.
            if live_status and not retrying:
                continue

            max_age_hours = max(
                1, min(24, int(getattr(self.s, "token_discovery_max_token_age_hours", 24)))
            )
            try:
                observed_launch_at = float(candidate.get("created_at") or entry_at)
            except (TypeError, ValueError):
                observed_launch_at = entry_at
            max_entry_age_seconds = max(
                60, min(600, int(getattr(self.s, "token_discovery_max_entry_age_seconds", 300)))
            )
            if (
                observed_launch_at > now
                or now - observed_launch_at > max_age_hours * 3600
                or now - observed_launch_at > max_entry_age_seconds
            ):
                self._set_discovery_live_status(
                    candidate, "skipped_stale",
                    f"Launch is older than the {max_entry_age_seconds}s sniper entry window; no live buy",
                )
                continue

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

            if retrying and now < float(candidate.get("live_entry_retry_at") or 0):
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
            # For a retry, the previous preflight may have stored low liquidity.
            # Re-read the chain first instead of letting that stale snapshot suppress
            # a still-fresh candidate before we can verify its current reserves.
            if not retrying:
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
                self._schedule_discovery_live_preflight_retry(
                    candidate,
                    f"Fresh on-chain safety recheck unavailable ({type(exc).__name__})",
                )
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
            total_supply_tokens = float(inspected.get("total_supply_tokens") or candidate.get("total_supply_tokens") or 0)
            live_entry_market_cap = price * total_supply_tokens if price > 0 and total_supply_tokens > 0 else 0.0
            candidate.update({
                "security_passed": True,
                "security_checks": inspected.get("checks", []),
                "top5_holder_supply_pct": float(inspected.get("top5_holder_supply_pct") or 0),
                "creator_supply_pct": float(inspected.get("creator_supply_pct") or 0),
                "real_liquidity_usd": liquidity,
                "price_usd": price,
                "total_supply_tokens": total_supply_tokens,
            })
            if live_entry_market_cap > 0:
                candidate["market_cap_usd"] = live_entry_market_cap
                candidate["market_cap_source"] = "Estimated from fresh on-chain curve price × total supply (FDV-style)"
                candidate["live_entry_candidate_market_cap_usd"] = live_entry_market_cap
                candidate["live_entry_candidate_market_cap_source"] = "Estimated at live-entry preflight: price × total supply (FDV-style)"
            if decimals != 6:
                self._set_discovery_live_status(
                    candidate, "blocked",
                    f"Fresh entry rejected: unsupported token decimals={decimals}; no order submitted",
                )
                continue

            discovery_liquidity_floor = max(
                5000.0, float(getattr(self.s, "token_discovery_min_liquidity_usd", 5000.0))
            )
            if liquidity < discovery_liquidity_floor or price <= 0:
                self._schedule_discovery_live_preflight_retry(
                    candidate,
                    f"Fresh entry requirements failed (decimals={decimals}, real curve liquidity=${liquidity:.0f}, minimum=${discovery_liquidity_floor:.0f}, price valid={price > 0})",
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
                "total_supply_tokens": total_supply_tokens,
            }
            try:
                self.buy(signal, strategy="token_discovery", entry_cap_usd=hard_cap)
            except TransactionSimulationFailed as exc:
                # The native executor has not signed or submitted this transaction.
                # For a moving-curve/slippage rejection, refresh all safety gates and
                # attempt the candidate again once inside its original freshness window.
                if exc.retryable:
                    self._schedule_discovery_live_preflight_retry(
                        candidate,
                        "Pump SDK slippage simulation failed after one fresh unsigned rebuild; "
                        "no transaction was signed or submitted",
                    )
                    logger.warning(
                        "Discovery buy delayed for fresh re-preflight mint=%s; simulation rejected slippage before submission",
                        mint[:8],
                    )
                else:
                    self._set_discovery_live_status(
                        candidate, "failed",
                        "Pre-submit transaction simulation failed; no transaction was signed or submitted: "
                        + " ".join(str(exc.detail).split())[:350],
                    )
                    logger.warning(
                        "Discovery buy skipped mint=%s: simulation failed before signing/submission: %s",
                        mint[:8], " ".join(str(exc.detail).split())[:220],
                    )
                self.token_discovery._update_feed_subscriptions()
                return
            except TransactionFailedOnChain as exc:
                # This specific outcome is definitive: the chain reports the swap
                # failed. Record it and keep the candidate consumed, without turning
                # a known slippage rejection into a generic runtime/manual-review halt.
                # Leader-copy and sell paths retain their existing fail-closed handling.
                detail = f"Confirmed on-chain swap failure: {exc.detail}; signature={exc.signature}; no token fill confirmed"
                self._set_discovery_live_status(candidate, "failed", detail)
                logger.warning(
                    "Live discovery BUY failed on-chain mint=%s signature=%s error=%s; candidate will not auto-retry",
                    mint[:8], exc.signature, exc.detail,
                )
                return

            opened = self.store.get_position(mint)
            if opened and opened.get("strategy", "leader_copy") == "token_discovery":
                # Use executed swap price for the live entry market-cap estimate,
                # not the preflight quote observed before confirmation.
                executed_cap = float(opened.get("entry_market_cap_usd") or 0)
                if executed_cap > 0:
                    candidate["live_entry_market_cap_usd"] = executed_cap
                    candidate["live_entry_market_cap_source"] = (
                        opened.get("entry_market_cap_source")
                        or "Estimated from actual swap notional per token × total supply (FDV-style)"
                    )
                    candidate["entry_market_cap_usd"] = executed_cap
                    candidate["entry_market_cap_source"] = candidate["live_entry_market_cap_source"]
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
            if self.store.get_state(f"exit_blocked_balance_zero:{mint}", ""):
                # An exit can't be executed while the configured wallet reports
                # no tokens. Wait for an explicit manual retry after reconciliation.
                continue
            pending = self.store.get_state(f"exit_pending:{mint}", "")
            if pending:
                current_raw = int(position["amount_raw"])
                # Emergency liquidation overrides a previously deferred partial sell.
                if pending.lower().startswith(("total loss limit", "experiment expiry")):
                    pending_amount = current_raw
                else:
                    pending_amount_text = self.store.get_state(
                        f"exit_pending_amount_raw:{mint}", str(current_raw)
                    )
                    try:
                        pending_amount = int(pending_amount_text or current_raw)
                    except (TypeError, ValueError):
                        pending_amount = current_raw
                pending_wallet = self.store.get_state(
                    f"exit_pending_source_wallet:{mint}", ""
                ) or None
                pending_signature = self.store.get_state(
                    f"exit_pending_source_signature:{mint}", ""
                ) or None
                self.sell(
                    mint, pending_amount, f"pending exit: {pending}",
                    pending_wallet, pending_signature,
                )
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
            # Do not trail a small unbanked bounce into an early full exit. Arm
            # the trailing stop only after TP1 has actually sold part of the position.
            if (
                int(position["tp1_done"])
                and peak > entry
                and price <= peak * (1 - self.s.trailing_stop_pct)
            ):
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
        # PumpPortal trade events are observations, not proof that the named wallet
        # signed a canonical Pump/PumpSwap transaction. Only the Solana RPC detector
        # sets these flags after checking the signer and invoked program IDs.
        verified = (
            signal.get("source") == "solana_logs"
            and signal.get("signer_verified") is True
            and signal.get("program_verified") is True
        )
        side = str(signal.get("side") or "").lower()
        mint = str(signal.get("mint") or "")
        position_exists = bool(mint and self.store.get_position(mint))
        decision = (
            "verified_buy_signal" if side == "buy" and verified
            else "would_mirror_sell" if side == "sell" and verified and position_exists
            else "observed_unverified" if not verified
            else "rejected"
        )
        reason = "" if decision in {"verified_buy_signal", "would_mirror_sell"} else (
            "Leader transaction lacks independent signer/program verification."
            if not verified else "Leader sell observed without a matching tracked position."
        )
        try:
            price = float(signal.get("observed_price_usd") or self.price_cache.get(mint, 0) or 0)
            self.research.record_signal(
                mode=self.s.mode, strategy="leader_copy", mint=mint,
                signal_type="leader_trade", decision=decision, rejection_reason=reason,
                price_usd=price if price > 0 else None,
                liquidity_usd=signal.get("curve_liquidity_usd"),
                source_wallet=str(signal.get("wallet") or ""),
                source_signature=str(signal.get("signature") or ""),
                payload={**signal, "signer_verified": verified, "program_verified": verified},
            )
        except Exception as exc:
            logger.warning("Research leader-signal journal write failed (%s)", type(exc).__name__)
        if self.s.mode == "live" and not verified:
            logger.warning(
                "Reject unverified live leader signal side=%s wallet=%s mint=%s; waiting for signer-verified Solana RPC signal",
                side, str(signal.get("wallet") or "")[:8], mint[:8],
            )
            return
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

        # Do not let old signatures consume the fresh-signal budget. Known-stale
        # activity is intentionally ignored and advanced in the cursor; recent signals
        # are inspected oldest-to-newest so a prolonged feed outage cannot starve current
        # trades behind hundreds of obsolete signatures.
        try:
            max_signal_age = max(
                5,
                min(300, int(__import__("os").getenv("MAX_COPY_SIGNAL_AGE_SECONDS", "30"))),
            )
        except (TypeError, ValueError):
            max_signal_age = 30
        try:
            max_per_poll = max(1, min(50, int(__import__("os").getenv("SOLANA_MAX_SIGNATURES_PER_WALLET_POLL", "8"))))
        except ValueError:
            max_per_poll = 8

        chronological = list(reversed(new_rows))
        cursor_candidate = cursor
        handled = 0
        fresh_attempts = 0
        rate_limited = False
        stopped_before_end = False
        for row in chronological:
            signature = row.get("signature")
            if not signature:
                continue
            dedupe_key = self._dedupe_key(wallet, signature)
            if self.store.signature_seen(dedupe_key):
                cursor_candidate = signature
                continue

            block_time = row.get("blockTime")
            signal_age = None
            if block_time is not None:
                try:
                    signal_age = time.time() - float(block_time)
                except (TypeError, ValueError, OverflowError):
                    signal_age = None

            if signal_age is not None and (signal_age < 0 or signal_age > max_signal_age):
                logger.debug(
                    "Skipping stale watched-wallet signature %s (age=%.1fs); advancing stale cursor",
                    signature[:12], signal_age,
                )
                self.store.mark_signature_seen(dedupe_key, wallet)
                cursor_candidate = signature
                handled += 1
                continue

            if fresh_attempts >= max_per_poll:
                stopped_before_end = True
                break

            try:
                signals = self.detect_pump_signal(wallet, signature)
            except SolanaRPCRateLimited:
                logger.warning(
                    "RPC fallback paused for %s; keeping cursor at the last inspected transaction.",
                    wallet[:8],
                )
                rate_limited = True
                break
            except SolanaTransactionNotReady:
                logger.info(
                    "Transaction details not indexed for %s; keeping cursor for retry.",
                    signature[:10],
                )
                rate_limited = True
                break

            fresh_attempts += 1
            for signal in signals:
                actual_block_time = signal.get("block_time")
                actual_age = None
                if actual_block_time is not None:
                    try:
                        actual_age = time.time() - float(actual_block_time)
                    except (TypeError, ValueError, OverflowError):
                        actual_age = None
                # Signature listings can have a missing blockTime. Inspect the
                # transaction, then require its own confirmed block time before copying.
                if actual_age is None or actual_age < 0 or actual_age > max_signal_age:
                    logger.info(
                        "Skipping unverified/stale Pump signal %s (age=%s); not copying",
                        signature[:12],
                        "unknown" if actual_age is None else f"{actual_age:.1f}s",
                    )
                    continue
                logger.info(
                    "Fresh RPC fallback signal: side=%s wallet=%s mint=%s age=%.1fs",
                    signal.get("side", "?"), wallet[:8], str(signal.get("mint") or "")[:8],
                    actual_age,
                )
                self.process_signal(signal)

            self.store.mark_signature_seen(dedupe_key, wallet)
            cursor_candidate = signature
            handled += 1

        if not rate_limited and not stopped_before_end:
            # Every returned row has either been inspected or proven stale.
            cursor_candidate = latest
        if cursor_candidate and cursor_candidate != cursor:
            self.store.set_cursor(wallet, cursor_candidate)
        return handled

    def status(self):
        equity = self.equity()
        logger.warning("MODE=%s wallet=%s equity=%.4f USD initial=%.4f automatic_global_halts=disabled legacy_halt_state=%s reason=%s",
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
                        f"RPC wallet monitoring error for {wallet}; polling will retry; new entries are halted pending review"
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
            self.trending_scanner.start()
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
        next_equity_refresh = 0.0

        try:
            while True:
                try:
                    self._process_dashboard_commands()
                    self._consume_feed_events()
                    self._consume_solana_log_events()
                    now = time.time()
                    # Manage exits before discovery/scanner work can consume time.
                    # Network-heavy tasks below must not get first priority over stops.
                    if now >= next_position_check:
                        self.manage_positions()
                        next_position_check = time.time() + 1.5

                    self.token_discovery.tick()
                    self._process_discovery_live_entries()
                    self._process_trending_live_entries()
                    now = time.time()

                    if self.s.research_auto_disable_enabled and now >= self._next_mode_policy_check_at:
                        self._next_mode_policy_check_at = now + 60.0
                        try:
                            policy_result = auto_disable_unprofitable_modes(
                                self.store, mode=self.s.mode,
                                round_trip_cost_pct=float(getattr(self.s, "research_round_trip_cost_pct", 5.0)),
                            )
                            for disabled_strategy in policy_result.get("disabled", []):
                                logger.warning(
                                    "AUTO-DISABLED strategy=%s mode=%s after mature counterfactual sample failed positive lower-CI gate",
                                    disabled_strategy, self.s.mode,
                                )
                        except Exception as exc:
                            logger.warning("Per-mode evidence evaluation unavailable (%s)", type(exc).__name__)

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
                        # The feed can be connected while account subscriptions are
                        # rejected (for example, an unfunded PumpPortal linked wallet).
                        # Keep RPC fallback responsive instead of waiting 20+ seconds.
                        fallback_interval = max(10, self.s.poll_seconds * 2)
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
                        logger.warning(
                            "Configured experiment duration elapsed; automatic entry halt and "
                            "experiment-expiry liquidation are disabled."
                        )

                    if now >= next_equity_refresh:
                        equity = self.equity()
                        self.last_equity_usd = equity
                        self._refresh_risk_state(equity)
                        self._refresh_feed_subscriptions()
                        self._publish_dashboard_state(equity)
                        open_count = len(self.store.list_positions())
                        logger.info("Heartbeat equity=%.4f positions=%d auto_copy=%s active_wallets=%d",
                                    equity, open_count, self.auto_copy_enabled, len(self.active_wallets))
                        next_equity_refresh = time.time() + 1.5

                    self._publish_dashboard_state(self.last_equity_usd)
                except KeyboardInterrupt:
                    logger.warning("Stopped by user.")
                    break
                except Exception as exc:
                    logger.exception("Loop error; this cycle failed; next cycle will retry")
                    if self.s.mode == "live":
                        self._activate_manual_review_halt(
                            f"Runtime exception: {exc}; new entries halted pending diagnosis and operator review"
                        )
                    self.message = "Runtime error logged; the main loop will retry and automatic global halts are disabled."
                    # The normal refresh at the bottom of the loop is skipped when
                    # manage_positions() or another step raises. Publish directly from
                    # the ledger so confirmed buys do not remain invisible in the UI.
                    self._publish_dashboard_state_after_error()

                if once:
                    break
                time.sleep(0.20)
        finally:
            self.trending_scanner.stop()
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