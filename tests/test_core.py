import queue
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from market_data import MarketData, WSOL_MINT
from pumpfun_bot import (
    PumpFunBot,
    loss_exit_hold_remaining_seconds,
    native_build_retry_delay_seconds,
    verified_curve_fallback_allowed,
)
from trending_scanner import live_candidate_retry_allowed
from native_pump import (
    NativePumpExecutor,
    TransactionBuildFailed,
    TransactionExpiredNotLanded,
    TransactionOutcomeUncertain,
    TransactionSimulationFailed,
)
from solana_rpc import SolanaRPC
from storage import Store
from token_discovery import TokenDiscoveryShadow



class NativeSimulationFailureTests(unittest.TestCase):
    class FakeTransaction:
        def __bytes__(self):
            return b"unsigned-transaction"

    class FakeRPC:
        def __init__(self, err, logs):
            self.err = err
            self.logs = logs

        def call(self, method, params):
            if method != "simulateTransaction":
                raise AssertionError(f"unexpected RPC method: {method}")
            return {"value": {"err": self.err, "logs": self.logs}}

    def _executor(self, err, logs):
        executor = object.__new__(NativePumpExecutor)
        executor.rpc = self.FakeRPC(err, logs)
        return executor

    def test_slippage_6042_is_typed_as_retryable_pre_submit_failure(self):
        executor = self._executor(
            {"InstructionError": [3, {"Custom": 6042}]},
            ["Program log: Error Code: BuySlippageBelowMinTokensOut"],
        )
        with self.assertRaises(TransactionSimulationFailed) as raised:
            executor._simulate(self.FakeTransaction(), "pump-program")
        self.assertTrue(raised.exception.retryable)
        self.assertIn("6042", raised.exception.detail)

    def test_other_simulation_failure_is_pre_submit_but_not_slippage_retryable(self):
        executor = self._executor(
            {"InstructionError": [1, {"Custom": 9999}]},
            ["Program log: Invalid account state"],
        )
        with self.assertRaises(TransactionSimulationFailed) as raised:
            executor._simulate(self.FakeTransaction(), "pump-program")
        self.assertFalse(raised.exception.retryable)
        self.assertIn("simulation failed", raised.exception.detail.lower())

class LossStreakCircuitBreakerTests(unittest.TestCase):
    class FakeStore:
        def __init__(self, state=None):
            self.state = dict(state or {})

        def get_state(self, key, default=""):
            return self.state.get(key, default)

        def set_state(self, key, value):
            self.state[str(key)] = str(value)

    def _bot(self, state=None):
        bot = object.__new__(PumpFunBot)
        bot.store = self.FakeStore(state)
        bot.s = SimpleNamespace(max_consecutive_losses=3)
        return bot

    def test_three_losing_exits_pause_new_entries_for_utc_day(self):
        bot = self._bot()
        with self.assertLogs("pumpfun", level="WARNING") as logs:
            bot._record_realized_exit_risk(-0.10)
            bot._record_realized_exit_risk(-0.05)
            bot._record_realized_exit_risk(-0.02)

        today = datetime.now(timezone.utc).date().isoformat()
        self.assertEqual(bot.store.get_state("consecutive_losing_exits"), "3")
        self.assertEqual(bot.store.get_state("consecutive_loss_pause_day"), today)
        self.assertIn("pausing new entries", "\n".join(logs.output))
        self.assertIn("paused until the next UTC day", bot._copy_block_reason())

    def test_positive_realized_exit_resets_the_loss_streak(self):
        bot = self._bot()
        bot._record_realized_exit_risk(-0.10)
        bot._record_realized_exit_risk(-0.05)
        bot._record_realized_exit_risk(0.02)
        self.assertEqual(bot.store.get_state("consecutive_losing_exits"), "0")
        self.assertEqual(bot.store.get_state("consecutive_loss_pause_day"), "")

    def test_profitable_exit_resets_loss_streak_and_pause(self):
        bot = self._bot()
        bot._record_realized_exit_risk(-0.10)
        bot._record_realized_exit_risk(-0.05)
        bot._record_realized_exit_risk(-0.02)
        bot._record_realized_exit_risk(0.50)
        self.assertEqual(bot.store.get_state("consecutive_loss_pause_day"), "")
        self.assertEqual(bot.store.get_state("consecutive_losing_exits"), "0")
        self.assertEqual(bot._loss_streak_pause_reason(), "")

    def test_stale_pause_is_cleared_on_a_later_utc_day(self):
        bot = self._bot({
            "consecutive_loss_pause_day": "2000-01-01",
            "consecutive_losing_exits": "3",
        })
        self.assertEqual(bot._loss_streak_pause_reason(), "")
        self.assertEqual(bot.store.get_state("consecutive_loss_pause_day"), "")
        self.assertEqual(bot.store.get_state("consecutive_losing_exits"), "0")


class DiscoveryQueueTests(unittest.TestCase):
    class FakeStore:
        def __init__(self):
            self.state = {}

        def get_state(self, key, default=""):
            return self.state.get(key, default)

        def set_state(self, key, value):
            self.state[str(key)] = str(value)

        def list_state_prefix(self, prefix, limit=1000):
            rows = [(k, v) for k, v in self.state.items() if k.startswith(prefix)]
            return rows[:limit]

        def delete_state(self, key):
            self.state.pop(key, None)

        def list_positions(self):
            return []

    class FakeMarket:
        def sol_price_usd(self):
            return 150.0

    def _discovery(self, *, liquidity=6500.0, max_age_hours=24):
        settings = SimpleNamespace(
            token_discovery_enabled=True,
            token_discovery_live_enabled=False,
            token_discovery_max_entry_usd=0.50,
            token_discovery_max_watched_tokens=5,
            token_discovery_watch_seconds=180,
            token_discovery_max_launches_per_minute=12,
            token_discovery_min_liquidity_usd=5000.0,
            token_discovery_min_score=80,
            token_discovery_min_unique_buyers=8,
            token_discovery_shadow_window_seconds=900,
            token_discovery_max_token_age_hours=max_age_hours,
            pumpfun_auth_token="",
            request_timeout_seconds=5,
        )
        discovery = TokenDiscoveryShadow(
            settings, object(), self.FakeMarket(), self.FakeStore(), None
        )
        discovery._inspect_new_token = lambda mint, creator="": {
            "safe": True,
            "reason": "test safe",
            "checks": ["test"],
            "liquidity_usd": liquidity,
            "price_usd": 0.01,
            "decimals": 6,
            "total_supply_tokens": 1_000_000_000,
            "top5_holder_supply_pct": 10.0,
            "creator_supply_pct": 1.0,
        }
        return discovery

    def test_full_watch_capacity_queues_launch_then_processes_when_slot_frees(self):
        from token_discovery import STATE_PREFIX
        from market_data import WSOL_MINT
        import json
        import time

        discovery = self._discovery()
        now = time.time()
        for i in range(5):
            row = {
                "mint": f"watch-mint-{i}",
                "created_at": now,
                "status": "watching",
                "subscribed": True,
                "subscription_expires_at": now + 300,
                "last_updated_at": now,
            }
            discovery.store.set_state(STATE_PREFIX + row["mint"], json.dumps(row))

        discovery.on_new_token_event({
            "mint": WSOL_MINT, "name": "Queued coin", "symbol": "QUEUE",
            "traderPublicKey": "creator-wallet",
        })
        queued = discovery._load(WSOL_MINT)
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(discovery.dashboard_snapshot()["queue_count"], 1)

        for row in discovery.candidates(100):
            if str(row.get("mint", "")).startswith("watch-mint-"):
                row["status"] = "watch_expired"
                row["subscribed"] = False
                discovery._save(row)

        discovery._process_queued_launches(time.time())
        processed = discovery._load(WSOL_MINT)
        self.assertEqual(processed["status"], "watching")
        self.assertEqual(discovery.stats["queue_processed"], 1)
        self.assertEqual(discovery.stats["safety_scans"], 1)

    def test_launches_older_than_24_hours_expire_instead_of_scanning(self):
        from market_data import WSOL_MINT
        import time

        discovery = self._discovery(max_age_hours=24)
        now = time.time()
        discovery._queue_launch_event(
            WSOL_MINT,
            {
                "mint": WSOL_MINT,
                "name": "Old coin",
                "symbol": "OLD",
                "_bot_observed_created_at": now - (25 * 3600),
            },
            "test queue",
            now,
        )
        discovery._process_queued_launches(now)
        candidate = discovery._load(WSOL_MINT)
        self.assertEqual(candidate["status"], "expired")
        self.assertEqual(discovery.stats["queue_expired"], 1)
        self.assertEqual(discovery.stats["safety_scans"], 0)

    def test_creation_timestamp_parser_accepts_milliseconds(self):
        now = datetime.now(timezone.utc).timestamp()
        parsed = TokenDiscoveryShadow._coin_created_at({
            "created_timestamp": int(now * 1000)
        })
        self.assertIsNotNone(parsed)
        self.assertAlmostEqual(parsed, now, delta=0.01)

    def test_backfill_queues_recent_candidates_without_scanning_inline(self):
        from market_data import WSOL_MINT
        import time

        discovery = self._discovery()
        now = time.time()
        created_at = now - 3600
        discovery.backfill_page_size = 1
        discovery._backfill_results.put({
            "offset": 0,
            "rows": [{
                "mint": WSOL_MINT,
                "name": "Backfill coin",
                "symbol": "BACKFILL",
                "created_timestamp": int(created_at * 1000),
            }],
            "error": "",
        })

        discovery._drain_recent_launch_backfill(now)

        candidate = discovery._load(WSOL_MINT)
        self.assertEqual(candidate["status"], "queued")
        self.assertIn("authenticated recent-launch backfill", candidate["reason"])
        self.assertAlmostEqual(candidate["created_at"], created_at, delta=0.01)
        self.assertEqual(discovery.stats["backfill_polls"], 1)
        self.assertEqual(discovery.stats["backfill_candidates_seen"], 1)
        self.assertEqual(discovery.stats["safety_scans"], 0)
        self.assertEqual(discovery.dashboard_snapshot()["queue_count"], 1)

    def test_backfill_stops_paging_when_existing_rows_cross_24_hour_cutoff(self):
        from market_data import WSOL_MINT
        import time

        discovery = self._discovery()
        now = time.time()
        old_at = now - (25 * 3600)
        other_mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        for mint in (WSOL_MINT, other_mint):
            discovery._save({
                "mint": mint,
                "created_at": old_at,
                "status": "expired",
                "subscribed": False,
            })
        discovery.backfill_page_size = 2
        discovery._backfill_offset = 200
        discovery._backfill_results.put({
            "offset": 200,
            "rows": [
                {"mint": WSOL_MINT, "created_timestamp": int(old_at * 1000)},
                {"mint": other_mint, "created_timestamp": int(old_at * 1000)},
            ],
            "error": "",
        })

        discovery._drain_recent_launch_backfill(now)

        self.assertEqual(discovery._backfill_offset, 0)
        self.assertEqual(discovery.stats["backfill_polls"], 1)
        self.assertEqual(discovery.dashboard_snapshot()["queue_count"], 0)


    def test_low_real_curve_liquidity_waits_for_liquidity_at_five_thousand_floor(self):
        from market_data import WSOL_MINT
        import time

        discovery = self._discovery(liquidity=4999.0)
        now = time.time()
        discovery._scan_and_register(
            WSOL_MINT,
            {
                "mint": WSOL_MINT,
                "name": "Low-liquidity coin",
                "symbol": "LOW",
                "_bot_observed_created_at": now,
            },
            now,
        )
        candidate = discovery._load(WSOL_MINT)
        self.assertEqual(candidate["status"], "waiting_liquidity")
        self.assertFalse(candidate["subscribed"])
        self.assertIn("below $5000", candidate["reason"])
        self.assertEqual(discovery.stats["liquidity_rejected"], 0)



class PumpCurveMarkPriceTests(unittest.TestCase):
    def _bot(self):
        bot = object.__new__(PumpFunBot)
        bot.s = SimpleNamespace(request_timeout_seconds=12)
        bot.price_cache = {}
        bot.price_cache_at = {}
        bot.market_price_cache = {}
        bot.market_price_cache_at = {}
        bot.curve_price_cache = {}
        bot.curve_price_cache_at = {}
        bot.curve_mark_checked_at = {}
        bot.curve_completed_cache = set()
        bot.curve_unpriced_cache = set()
        bot.rpc = object()

        class FakeStore:
            def get_state(self, key, default=""):
                return "pump" if key == "pool:test-mint" else default

            def get_position(self, mint):
                return {"mint": mint, "decimals": 6}

        class FakeMarket:
            def __init__(self):
                self.dex_calls = 0
                self.sol_timeouts = []

            def sol_price_usd(self, timeout=None):
                self.sol_timeouts.append(timeout)
                return 150.0

            def token_market(self, mint, timeout=None):
                self.dex_calls += 1
                return {"price_usd": 9.0}

        bot.store = FakeStore()
        bot.market = FakeMarket()
        return bot

    def test_unindexed_pump_position_uses_bounded_on_chain_curve_mark(self):
        from unittest.mock import patch

        bot = self._bot()

        class FakeCurve:
            complete = False
            quote_mint = WSOL_MINT

            def price_usd(self, sol_price_usd, decimals=6):
                self.seen_sol_price = sol_price_usd
                self.seen_decimals = decimals
                return 0.000123

        with patch("pumpfun_bot.fetch_pump_curve", return_value=FakeCurve()) as fetch:
            mark = bot._mark_price("test-mint", 0.0002)

        self.assertAlmostEqual(mark, 0.000123)
        fetch.assert_called_once_with(bot.rpc, "test-mint", timeout=2.0)
        self.assertEqual(bot.market.sol_timeouts, [2.0])
        self.assertEqual(bot.market.dex_calls, 0)
        self.assertEqual(bot.curve_price_cache["test-mint"], 0.000123)

    def test_completed_curve_falls_back_to_indexed_market_quote(self):
        from unittest.mock import patch

        bot = self._bot()

        class CompletedCurve:
            complete = True
            quote_mint = WSOL_MINT

            def price_usd(self, sol_price_usd, decimals=6):
                return None

        with patch("pumpfun_bot.fetch_pump_curve", return_value=CompletedCurve()) as fetch:
            mark = bot._mark_price("test-mint", 0.0002)
            second_mark = bot._mark_price("test-mint", 0.0002)

        self.assertAlmostEqual(mark, 9.0)
        self.assertAlmostEqual(second_mark, 9.0)
        self.assertEqual(bot.market.dex_calls, 1)
        self.assertEqual(fetch.call_count, 1)
        self.assertIn("test-mint", bot.curve_completed_cache)


class LossExitGracePeriodTests(unittest.TestCase):
    def test_marked_loss_stop_bypasses_minimum_hold_immediately(self):
        now = datetime(2026, 10, 18, 12, 2, tzinfo=timezone.utc)
        position = {
            "entry_price_usd": 1.0,
            "opened_at": (now - timedelta(seconds=5)).isoformat(timespec="seconds"),
        }
        self.assertEqual(
            loss_exit_hold_remaining_seconds(
                position, 0.82, "marked loss stop", 120, now=now.timestamp()
            ),
            0.0,
        )

    def test_trailing_stop_bypasses_minimum_hold_immediately(self):
        now = datetime(2026, 10, 18, 12, 2, tzinfo=timezone.utc)
        position = {
            "entry_price_usd": 1.0,
            "opened_at": (now - timedelta(seconds=5)).isoformat(timespec="seconds"),
        }
        self.assertEqual(
            loss_exit_hold_remaining_seconds(
                position, 0.95, "trailing stop", 120, now=now.timestamp()
            ),
            0.0,
        )

    def test_losing_leader_sell_bypasses_minimum_hold_immediately(self):
        now = datetime(2026, 10, 18, 12, 2, tzinfo=timezone.utc)
        position = {
            "entry_price_usd": 1.0,
            "opened_at": (now - timedelta(seconds=5)).isoformat(timespec="seconds"),
        }
        self.assertEqual(
            loss_exit_hold_remaining_seconds(
                position, 0.75, "followed wallet leader partial/full sell on pump",
                120, now=now.timestamp(),
            ),
            0.0,
        )

    def test_other_losing_exits_still_use_remaining_grace_period(self):
        now = datetime(2026, 10, 18, 12, 2, tzinfo=timezone.utc)
        position = {
            "entry_price_usd": 1.0,
            "opened_at": (now - timedelta(seconds=30)).isoformat(timespec="seconds"),
        }
        self.assertEqual(
            loss_exit_hold_remaining_seconds(
                position, 0.75, "strategy discretionary sell", 120,
                now=now.timestamp()
            ),
            90.0,
        )

    def test_loss_exit_allowed_after_two_minutes(self):
        now = datetime(2026, 10, 18, 12, 2, tzinfo=timezone.utc)
        position = {
            "entry_price_usd": 1.0,
            "opened_at": (now - timedelta(seconds=121)).isoformat(timespec="seconds"),
        }
        self.assertEqual(
            loss_exit_hold_remaining_seconds(
                position, 0.75, "marked loss stop", 120, now=now.timestamp()
            ),
            0.0,
        )

    def test_catastrophic_price_drop_bypasses_minimum_hold(self):
        now = datetime(2026, 10, 18, 12, 2, tzinfo=timezone.utc)
        position = {
            "entry_price_usd": 1.0,
            "opened_at": (now - timedelta(seconds=10)).isoformat(timespec="seconds"),
        }
        self.assertEqual(
            loss_exit_hold_remaining_seconds(
                position, 0.49, "marked loss stop", 120,
                emergency_loss_bypass_pct=0.50, now=now.timestamp(),
            ),
            0.0,
        )

    def test_explicit_manual_exit_bypasses_minimum_hold_when_losing(self):
        now = datetime(2026, 10, 18, 12, 2, tzinfo=timezone.utc)
        position = {
            "entry_price_usd": 1.0,
            "opened_at": (now - timedelta(seconds=10)).isoformat(timespec="seconds"),
        }
        self.assertEqual(
            loss_exit_hold_remaining_seconds(
                position, 0.75, "manual exit requested from dashboard", 120,
                now=now.timestamp(),
            ),
            0.0,
        )

    def test_profit_and_emergency_liquidation_bypass_loss_grace_period(self):
        now = datetime(2026, 10, 18, 12, 2, tzinfo=timezone.utc)
        position = {
            "entry_price_usd": 1.0,
            "opened_at": (now - timedelta(seconds=10)).isoformat(timespec="seconds"),
        }
        self.assertEqual(
            loss_exit_hold_remaining_seconds(
                position, 1.05, "followed wallet leader-wallet sell", 120,
                now=now.timestamp(),
            ),
            0.0,
        )
        self.assertEqual(
            loss_exit_hold_remaining_seconds(
                position, 0.50, "total loss limit liquidation", 120,
                now=now.timestamp(),
            ),
            0.0,
        )

    class FakeStore:
        def __init__(self, position):
            self.position = position
            self.state = {}
            self.trades = []

        def get_state(self, key, default=""):
            return self.state.get(key, default)

        def set_state(self, key, value):
            self.state[key] = str(value)

        def get_position(self, mint):
            return self.position if self.position and self.position.get("mint") == mint else None

        def add_trade(self, **trade):
            self.trades.append(trade)

        def delete_position(self, mint):
            if self.position and self.position.get("mint") == mint:
                self.position = None

        def upsert_position(self, position):
            self.position = position

    class FakeMarket:
        def __init__(self):
            self.token_market_calls = 0

        def token_market(self, mint, timeout=None):
            self.token_market_calls += 1
            return {"price_usd": 0.75}

    def _bot_with_fresh_position(self):
        position = {
            "mint": "test-mint",
            "decimals": 6,
            "amount_raw": "1000",
            "entry_price_usd": 1.0,
            "initial_amount_raw": "1000",
            "peak_price_usd": 1.0,
            "tp1_done": 0,
            "tp2_done": 0,
            "opened_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "last_price_usd": 1.0,
            "invested_usd": 1.0,
            "source_wallet": "entry-wallet",
            "source_signature": "entry-signature",
        }
        bot = object.__new__(PumpFunBot)
        bot.s = SimpleNamespace(
            mode="paper", min_hold_before_loss_exit_seconds=120,
            paper_swap_cost_usd=0.02,
        )
        bot.store = self.FakeStore(position)
        bot.market = self.FakeMarket()
        bot.price_cache = {}
        bot.price_cache_at = {}
        bot.market_price_cache = {}
        bot.market_price_cache_at = {}
        # The sale behavior is under test; avoid exercising the unrelated feed-subscription loop.
        bot._refresh_feed_subscriptions = lambda: None
        return bot

    def test_automatic_marked_loss_stop_attempts_exit_immediately(self):
        bot = self._bot_with_fresh_position()
        bot.price_cache = {}
        bot.price_cache_at = {}
        self.assertTrue(bot.sell("test-mint", 1000, "marked loss stop"))
        self.assertIsNone(bot.store.get_position("test-mint"))
        self.assertEqual(bot.store.get_state("exit_pending:test-mint", ""), "")
        self.assertEqual(bot.store.get_state("exit_pending_amount_raw:test-mint", ""), "")
        self.assertEqual(len(bot.store.trades), 1)
        self.assertEqual(bot.store.trades[0]["action"], "SELL")
        self.assertEqual(bot.market.token_market_calls, 0)

    def test_losing_leader_sell_exits_immediately_and_preserves_partial_quantity(self):
        bot = self._bot_with_fresh_position()
        reason = "followed wallet leader-wallet partial/full sell on pump"
        self.assertTrue(
            bot.sell(
                "test-mint", 300, reason,
                "leader-wallet", "leader-sell-signature",
            )
        )
        position = bot.store.get_position("test-mint")
        self.assertEqual(position["amount_raw"], "700")
        self.assertAlmostEqual(position["invested_usd"], 0.7)
        self.assertEqual(bot.store.trades[-1]["action"], "SELL")
        self.assertEqual(bot.store.trades[-1]["amount_raw"], "300")
        self.assertEqual(bot.store.get_state("exit_pending:test-mint", ""), "")
        self.assertEqual(bot.market.token_market_calls, 0)


class DashboardErrorRefreshTests(unittest.TestCase):
    def test_refreshes_dashboard_from_current_state_after_loop_error(self):
        bot = object.__new__(PumpFunBot)
        bot.last_equity_usd = 9.57
        published = []
        bot._publish_dashboard_state = lambda equity=None: published.append(equity)

        bot._publish_dashboard_state_after_error()

        self.assertEqual(published, [9.57])

    def test_dashboard_refresh_error_does_not_escape_error_handler(self):
        bot = object.__new__(PumpFunBot)
        bot.last_equity_usd = None

        def fail_refresh(equity=None):
            raise RuntimeError("test dashboard failure")

        bot._publish_dashboard_state = fail_refresh
        with self.assertLogs("pumpfun", level="ERROR") as captured:
            bot._publish_dashboard_state_after_error()

        self.assertTrue(any("Could not refresh dashboard state" in line for line in captured.output))



class NativeBuildFailureHandlingTests(unittest.TestCase):
    class FakeStore:
        def __init__(self, position):
            self.position = position
            self.state = {}

        def get_position(self, mint):
            return self.position if self.position and self.position.get("mint") == mint else None

        def get_state(self, key, default=""):
            return self.state.get(key, default)

        def set_state(self, key, value):
            self.state[key] = str(value)

    class FakeMarket:
        def token_market(self, mint):
            return {"price_usd": 1.0}

        def sol_price_usd(self):
            return 150.0

    class FakeExecutor:
        def token_balance_raw(self, wallet, mint):
            return 1000

        def trade(self, **kwargs):
            raise TransactionBuildFailed(
                "Native Pump SDK transaction build failed: Associated token account not found"
            )

    def test_failed_sell_build_retains_position_and_pending_exit_without_raising(self):
        position = {
            "mint": "test-mint", "amount_raw": "1000", "decimals": 6,
            "invested_usd": 1.0, "last_price_usd": 1.0,
            "source_wallet": "leader-wallet", "source_signature": "leader-signature",
        }
        bot = object.__new__(PumpFunBot)
        bot.s = SimpleNamespace(mode="live", slippage_bps=100)
        bot.store = self.FakeStore(position)
        bot.market = self.FakeMarket()
        bot.executor = self.FakeExecutor()
        bot.wallet_address = "test-wallet"
        bot.keypair = object()

        result = bot.sell("test-mint", 1000, "marked loss stop")

        self.assertFalse(result)
        self.assertEqual(bot.store.get_position("test-mint"), position)
        self.assertEqual(bot.store.get_state("exit_pending:test-mint"), "marked loss stop")
        self.assertGreater(float(bot.store.get_state("native_build_retry_after:test-mint")), 0)
        self.assertIn("Associated token account not found", bot.store.get_state("native_build_failure:test-mint"))
        self.assertNotIn("manual_review_halt", bot.store.state)

    def test_sell_builder_cooldown_preserves_new_leader_exit_for_retry(self):
        position = {
            "mint": "test-mint", "amount_raw": "1000", "decimals": 6,
            "invested_usd": 1.0, "last_price_usd": 1.0,
            "source_wallet": "leader-wallet", "source_signature": "leader-signature",
        }
        bot = object.__new__(PumpFunBot)
        bot.s = SimpleNamespace(mode="live", slippage_bps=100)
        bot.store = self.FakeStore(position)
        bot.market = self.FakeMarket()
        bot.executor = self.FakeExecutor()
        bot.wallet_address = "test-wallet"
        bot.keypair = object()
        bot.store.set_state("native_build_retry_after:test-mint", str(__import__("time").time() + 120))

        result = bot.sell(
            "test-mint", 300,
            "followed wallet leader-wallet partial/full sell on pump",
            "leader-wallet", "leader-sell-signature",
        )

        self.assertFalse(result)
        self.assertEqual(
            bot.store.get_state("exit_pending:test-mint"),
            "followed wallet leader-wallet partial/full sell on pump",
        )
        self.assertEqual(bot.store.get_state("exit_pending_amount_raw:test-mint"), "300")
        self.assertEqual(bot.store.get_state("exit_pending_source_wallet:test-mint"), "leader-wallet")
        self.assertEqual(bot.store.get_state("exit_pending_source_signature:test-mint"), "leader-sell-signature")

    def test_pending_exit_reason_does_not_accumulate_wrappers_on_retry(self):
        position = {
            "mint": "test-mint", "amount_raw": "1000", "decimals": 6,
            "invested_usd": 1.0, "last_price_usd": 1.0,
            "source_wallet": "leader-wallet", "source_signature": "leader-signature",
        }
        bot = object.__new__(PumpFunBot)
        bot.s = SimpleNamespace(mode="live", slippage_bps=100)
        bot.store = self.FakeStore(position)
        bot.store.set_state(
            "exit_pending:test-mint",
            "pending exit: pending exit: marked loss stop",
        )
        bot.market = self.FakeMarket()
        bot.executor = self.FakeExecutor()
        bot.wallet_address = "test-wallet"
        bot.keypair = object()

        result = bot.sell(
            "test-mint", 1000,
            "pending exit: pending exit: marked loss stop",
        )

        self.assertFalse(result)
        self.assertEqual(
            bot.store.get_state("exit_pending:test-mint"),
            "marked loss stop",
        )
        self.assertEqual(bot.store.get_position("test-mint"), position)

    def test_manual_exit_clears_builder_cooldown_and_overrides_deferred_partial_sell(self):
        position = {
            "mint": "test-mint", "amount_raw": "1000", "decimals": 6,
            "invested_usd": 1.0, "last_price_usd": 1.0,
        }
        bot = object.__new__(PumpFunBot)
        bot.store = self.FakeStore(position)
        bot.commands = queue.Queue()
        bot.commands.put({"action": "manual_exit", "mint": "test-mint"})
        bot.message = ""
        bot.store.set_state("native_build_retry_after:test-mint", "9999999999")
        bot.store.set_state("native_build_failure:test-mint", "old build failure")
        bot.store.set_state("exit_blocked_balance_zero:test-mint", "previous zero-balance block")
        bot.store.set_state("exit_pending_amount_raw:test-mint", "300")
        bot.store.set_state("exit_pending_source_wallet:test-mint", "leader-wallet")
        bot.store.set_state("exit_pending_source_signature:test-mint", "leader-signature")

        bot._process_dashboard_commands()

        self.assertEqual(
            bot.store.get_state("exit_pending:test-mint"),
            "manual exit requested from dashboard",
        )
        self.assertEqual(bot.store.get_state("exit_pending_amount_raw:test-mint"), "")
        self.assertEqual(bot.store.get_state("exit_pending_source_wallet:test-mint"), "")
        self.assertEqual(bot.store.get_state("exit_pending_source_signature:test-mint"), "")
        self.assertEqual(bot.store.get_state("native_build_retry_after:test-mint"), "")
        self.assertEqual(bot.store.get_state("exit_blocked_balance_zero:test-mint"), "")
        self.assertIn("Manual full exit queued", bot.message)

    def test_manual_exit_stays_queued_but_surfaces_unresolved_transaction_block(self):
        position = {
            "mint": "test-mint", "amount_raw": "1000", "decimals": 6,
            "invested_usd": 1.0, "last_price_usd": 1.0,
        }
        bot = object.__new__(PumpFunBot)
        bot.store = self.FakeStore(position)
        bot.commands = queue.Queue()
        bot.commands.put({"action": "manual_exit", "mint": "test-mint"})
        bot.message = ""
        bot.store.set_state("unresolved_trade:test-mint", "signature-status-unknown")
        bot.store.set_state("native_build_retry_after:test-mint", "9999999999")
        bot.store.set_state("exit_pending_amount_raw:test-mint", "300")

        bot._process_dashboard_commands()

        self.assertEqual(
            bot.store.get_state("exit_pending:test-mint"),
            "manual exit requested from dashboard",
        )
        self.assertEqual(bot.store.get_state("exit_pending_amount_raw:test-mint"), "")
        self.assertEqual(bot.store.get_state("native_build_retry_after:test-mint"), "")
        self.assertIn("blocked until", bot.message)
        self.assertIn("reconciled", bot.message)

    def test_zero_live_balance_pauses_pending_exit_instead_of_retrying_forever(self):
        position = {
            "mint": "test-mint", "amount_raw": "1000", "decimals": 6,
            "invested_usd": 1.0, "last_price_usd": 1.0,
        }
        class ZeroBalanceExecutor:
            def token_balance_raw(self, wallet, mint):
                return 0

            def trade(self, **kwargs):
                raise AssertionError("A sell must not be submitted with zero token balance")

        bot = object.__new__(PumpFunBot)
        bot.s = SimpleNamespace(mode="live", slippage_bps=100)
        bot.store = self.FakeStore(position)
        bot.market = self.FakeMarket()
        bot.executor = ZeroBalanceExecutor()
        bot.wallet_address = "test-wallet"
        bot.keypair = object()
        bot.message = ""
        bot.store.set_state("exit_pending:test-mint", "manual exit requested from dashboard")
        bot.store.set_state("exit_pending_amount_raw:test-mint", "1000")

        result = bot.sell("test-mint", 1000, "manual exit requested from dashboard")

        self.assertFalse(result)
        self.assertEqual(bot.store.get_state("exit_pending:test-mint"), "")
        self.assertEqual(bot.store.get_state("exit_pending_amount_raw:test-mint"), "")
        self.assertIn("exit_blocked_balance_zero:test-mint", bot.store.state)
        self.assertIn("paused", bot.message)
        self.assertEqual(bot.store.get_position("test-mint"), position)

    def test_position_manager_skips_zero_balance_block_until_manual_retry(self):
        position = {"mint": "test-mint", "amount_raw": "1000"}
        class BlockedStore:
            def list_positions(self):
                return [position]

            def get_position(self, mint):
                return position if mint == "test-mint" else None

            def get_state(self, key, default=""):
                return "reconcile first" if key == "exit_blocked_balance_zero:test-mint" else default

        bot = object.__new__(PumpFunBot)
        bot.store = BlockedStore()
        # It must skip before attempting to read prices or call the sell path.
        bot.manage_positions()

    def test_unsupported_quote_build_is_deferred_for_six_hours(self):
        self.assertEqual(
            native_build_retry_delay_seconds("Unsupported PumpSwap quote mint"),
            6 * 60 * 60,
        )

    def test_missing_associated_token_account_build_gets_one_minute_cooldown(self):
        self.assertEqual(
            native_build_retry_delay_seconds("Associated token account not found for mint"),
            60,
        )



class EntryDrawdownCircuitBreakerTests(unittest.TestCase):
    class FakeStore:
        def __init__(self, state):
            self.state = dict(state)
        def get_state(self, key, default=""):
            return self.state.get(key, default)
        def set_state(self, key, value):
            self.state[str(key)] = str(value)
        def daily_entries(self):
            return 0
        def list_positions(self):
            return []

    def _bot(self, *, equity=10.0, initial=10.0, day_start=10.0):
        bot = object.__new__(PumpFunBot)
        bot.store = self.FakeStore({
            "initial_equity": str(initial),
            "day_start_equity": str(day_start),
            "consecutive_losing_exits": "0",
        })
        bot.s = SimpleNamespace(
            starting_paper_cash_usd=10.0,
            max_entries_per_day=3,
            max_open_positions=1,
            max_entry_usd=0.50,
            max_total_exposure_usd=1.0,
            token_discovery_max_entry_usd=0.50,
            trending_discovery_max_entry_usd=0.50,
            daily_loss_limit_usd=0.50,
            total_loss_limit_usd=1.00,
            mode="paper",
            paper_swap_cost_usd=0.02,
        )
        bot._current_positions_value = lambda: 0.0
        bot.store.set_state("paper_cash", "10")
        return bot

    def test_daily_loss_limit_blocks_leader_and_discovery_entries(self):
        bot = self._bot(equity=9.50)
        for strategy in ("leader_copy", "token_discovery", "trending_discovery"):
            allowed, reason = bot._can_enter(9.50, strategy=strategy)
            self.assertFalse(allowed)
            self.assertIn("Daily-loss limit", reason)

    def test_daily_loss_pause_remains_after_equity_recovers_until_new_utc_day(self):
        bot = self._bot(equity=9.50)
        allowed, reason = bot._can_enter(9.50, strategy="leader_copy")
        self.assertFalse(allowed)
        self.assertIn("Daily-loss limit", reason)
        self.assertEqual(bot.store.get_state("daily_paused"), "1")

        allowed, reason = bot._can_enter(9.90, strategy="trending_discovery")
        self.assertFalse(allowed)
        self.assertIn("paused until the next UTC day", reason)

    def test_total_loss_limit_blocks_entries_even_when_daily_loss_is_small(self):
        bot = self._bot(equity=8.99, day_start=9.40)
        allowed, reason = bot._can_enter(8.99, strategy="trending_discovery")
        self.assertFalse(allowed)
        self.assertIn("Total-loss limit", reason)

    def test_entries_are_allowed_below_both_loss_limits(self):
        bot = self._bot(equity=9.70, initial=10.0, day_start=9.80)
        allowed, reason = bot._can_enter(9.70, strategy="trending_discovery")
        self.assertTrue(allowed, reason)

    def test_invalid_loss_baseline_fails_closed(self):
        bot = self._bot(equity=10.0)
        bot.store.set_state("initial_equity", "not-a-number")
        allowed, reason = bot._can_enter(10.0, strategy="trending_discovery")
        self.assertFalse(allowed)
        self.assertIn("Risk baseline is invalid", reason)



class DashboardDiscoveryToggleTests(unittest.TestCase):
    class FakeStore:
        def __init__(self):
            self.state = {}
        def get_state(self, key, default=""):
            return self.state.get(key, default)
        def set_state(self, key, value):
            self.state[str(key)] = str(value)

    class FakeTracker:
        def __init__(self, enabled=False, live_enabled=False):
            self.enabled = enabled
            self.live_enabled = live_enabled
            self.subscription_updates = 0
            self.started = 0
            self.stopped = 0
        def _update_feed_subscriptions(self):
            self.subscription_updates += 1
        def start(self):
            self.started += 1
        def stop(self):
            self.stopped += 1

    def _bot(self, mode="paper"):
        bot = PumpFunBot.__new__(PumpFunBot)
        bot.s = SimpleNamespace(
            mode=mode,
            live_confirmation="I_ACCEPT_THE_RISK_AND_UNDERSTAND_LIVE_TRADING",
            token_discovery_enabled=False,
            token_discovery_live_enabled=False,
            token_discovery_max_entry_usd=0.50,
            trending_discovery_enabled=False,
            trending_discovery_live_enabled=False,
            trending_discovery_max_entry_usd=0.50,
            pumpfun_auth_token="test-auth",
            pumpportal_api_key="test-pumpportal",
            jupiter_api_key="test-jupiter",
        )
        bot.store = self.FakeStore()
        bot.commands = queue.Queue()
        bot.token_discovery = self.FakeTracker()
        bot.trending_scanner = self.FakeTracker()
        bot.trending_executor = SimpleNamespace(api_key="test-jupiter")
        bot.wallet_address="TestWallet"
        bot.keypair=object()
        bot.message=""
        return bot

    def test_dashboard_exposes_separate_scan_and_live_buy_controls(self):
        from dashboard import _PAGE
        for control_id in (
            "newScanToggle", "newLiveToggle",
            "trendingScanToggle", "trendingLiveToggle",
        ):
            self.assertIn(control_id, _PAGE)
        for action_name in (
            "set_token_discovery_enabled",
            "set_token_discovery_live_enabled",
            "set_trending_discovery_enabled",
            "set_trending_discovery_live_enabled",
        ):
            self.assertIn(action_name, _PAGE)

    def test_enabling_scanner_does_not_enable_live_buys(self):
        bot = self._bot()
        bot.commands.put({"action": "set_token_discovery_enabled", "enabled": True})
        bot._process_dashboard_commands()
        self.assertTrue(bot.token_discovery.enabled)
        self.assertFalse(bot.token_discovery.live_enabled)
        self.assertEqual(bot.store.get_state("dashboard_token_discovery_enabled"), "1")
        self.assertEqual(bot.token_discovery.subscription_updates, 1)

        bot.commands.put({"action": "set_trending_discovery_enabled", "enabled": True})
        bot._process_dashboard_commands()
        self.assertTrue(bot.trending_scanner.enabled)
        self.assertFalse(bot.trending_scanner.live_enabled)
        self.assertEqual(bot.trending_scanner.started, 1)
        self.assertEqual(bot.store.get_state("dashboard_trending_discovery_enabled"), "1")

    def test_turning_off_scanner_also_turns_off_its_live_buy_switch(self):
        bot = self._bot(mode="live")
        bot.token_discovery.enabled = True
        bot.token_discovery.live_enabled = True
        bot.trending_scanner.enabled = True
        bot.trending_scanner.live_enabled = True

        bot.commands.put({"action": "set_token_discovery_enabled", "enabled": False})
        bot.commands.put({"action": "set_trending_discovery_enabled", "enabled": False})
        bot._process_dashboard_commands()

        self.assertFalse(bot.token_discovery.enabled)
        self.assertFalse(bot.token_discovery.live_enabled)
        self.assertEqual(bot.store.get_state("dashboard_token_discovery_live_enabled"), "0")
        self.assertFalse(bot.trending_scanner.enabled)
        self.assertFalse(bot.trending_scanner.live_enabled)
        self.assertEqual(bot.trending_scanner.stopped, 1)
        self.assertEqual(bot.store.get_state("dashboard_trending_discovery_live_enabled"), "0")

    def test_live_buy_switch_is_rejected_in_paper_mode(self):
        bot = self._bot(mode="paper")
        bot.token_discovery.enabled = True
        bot.trending_scanner.enabled = True
        bot.commands.put({"action": "set_token_discovery_live_enabled", "enabled": True})
        bot.commands.put({"action": "set_trending_discovery_live_enabled", "enabled": True})
        bot._process_dashboard_commands()

        self.assertFalse(bot.token_discovery.live_enabled)
        self.assertFalse(bot.trending_scanner.live_enabled)
        self.assertIn("BOT_MODE must be live", bot.message)

    def test_live_buy_switch_requires_scanner_and_credentials(self):
        bot = self._bot(mode="live")
        bot.keypair = None
        bot.commands.put({"action": "set_trending_discovery_live_enabled", "enabled": True})
        bot._process_dashboard_commands()
        self.assertFalse(bot.trending_scanner.live_enabled)
        self.assertIn("A loaded live wallet/keypair is required", bot.message)

        bot.keypair = object()
        bot.commands.put({"action": "set_trending_discovery_live_enabled", "enabled": True})
        bot._process_dashboard_commands()
        self.assertFalse(bot.trending_scanner.live_enabled)
        self.assertIn("Enable the trending scanner", bot.message)

    def test_trending_live_buy_can_be_enabled_after_all_gates_pass(self):
        bot = self._bot(mode="live")
        bot.trending_scanner.enabled = True
        bot.commands.put({"action": "set_trending_discovery_live_enabled", "enabled": True})
        bot._process_dashboard_commands()
        self.assertTrue(bot.trending_scanner.live_enabled)
        self.assertEqual(bot.store.get_state("dashboard_trending_discovery_live_enabled"), "1")
        self.assertIn("LIVE BUYS ENABLED", bot.message)

    def test_disabling_live_buy_switch_does_not_disable_scanner(self):
        bot = self._bot(mode="live")
        bot.token_discovery.enabled = True
        bot.token_discovery.live_enabled = True
        bot.commands.put({"action": "set_token_discovery_live_enabled", "enabled": False})
        bot._process_dashboard_commands()
        self.assertTrue(bot.token_discovery.enabled)
        self.assertFalse(bot.token_discovery.live_enabled)
        self.assertEqual(bot.store.get_state("dashboard_token_discovery_enabled"), "")
        self.assertEqual(bot.store.get_state("dashboard_token_discovery_live_enabled"), "0")


class CopySafetyGateTests(unittest.TestCase):
    class FakeStore:
        def __init__(self, state=None, positions=None):
            self.state = dict(state or {})
            self.positions = list(positions or [])

        def get_state(self, key, default=""):
            return self.state.get(key, default)

        def set_state(self, key, value):
            self.state[key] = str(value)

        def list_positions(self):
            return list(self.positions)

    def _bot(self, state=None, positions=None):
        bot = object.__new__(PumpFunBot)
        bot.store = self.FakeStore(state, positions)
        bot.s = SimpleNamespace(
            watch_wallets=(),
            top_traders_count=10,
            stream_token_prices=False,
        )
        bot.auto_copy_enabled = True
        bot.selected_window = "day"
        bot.leaderboards = {"day": [{"wallet": "leader-wallet"}]}
        bot.feed = None
        bot.logs_feed = None
        return bot

    def test_legacy_overall_loss_flag_does_not_block_copy_entries(self):
        bot = self._bot({"halted": "1", "halt_reason": "Total loss limit reached"})
        self.assertEqual(bot._copy_block_reason(), "")

    def test_legacy_manual_review_flag_does_not_block_copy_entries(self):
        bot = self._bot({
            "manual_review_halt": "1",
            "manual_review_reason": "Reconcile wallet before resuming",
        })
        self.assertEqual(bot._copy_block_reason(), "")

    def test_rpc_manual_review_flag_does_not_block_new_leader_subscriptions(self):
        bot = self._bot({
            "manual_review_halt": "1",
            "manual_review_reason": "RPC wallet monitoring error",
        })
        bot._refresh_feed_subscriptions()
        self.assertEqual(bot.active_wallets, {"leader-wallet"})

    def test_legacy_manual_review_flag_keeps_held_and_leader_wallets_monitored(self):
        position = {"mint": "open-mint", "source_wallet": "held-leader"}
        bot = self._bot({
            "manual_review_halt": "1",
            "manual_review_reason": "Reconcile wallet before resuming",
        }, positions=[position])
        bot._refresh_feed_subscriptions()
        self.assertEqual(bot.active_wallets, {"held-leader", "leader-wallet"})


class StaleWalletSignalTests(unittest.TestCase):
    def test_old_watched_wallet_signature_advances_cursor_without_copying(self):
        import time
        from unittest.mock import patch

        now = int(time.time())
        rows = [
            {"signature": "stale-new-signature", "blockTime": now - 120},
            {"signature": "stored-cursor", "blockTime": now - 240},
        ]

        class FakeStore:
            def __init__(self):
                self.cursor = "stored-cursor"
                self.seen = []

            def get_cursor(self, wallet):
                return self.cursor

            def set_cursor(self, wallet, signature):
                self.cursor = signature

            def signature_seen(self, key):
                return any(item[0] == key for item in self.seen)

            def mark_signature_seen(self, key, wallet):
                self.seen.append((key, wallet))

        bot = object.__new__(PumpFunBot)
        bot.rpc = SimpleNamespace(signatures=lambda wallet, limit: rows)
        bot.store = FakeStore()
        detected = []
        bot.detect_pump_signal = lambda wallet, signature: detected.append(signature) or []
        bot.process_signal = lambda signal: None

        with patch.dict("os.environ", {"MAX_COPY_SIGNAL_AGE_SECONDS": "30"}):
            handled = bot.poll_wallet("leader-wallet")

        self.assertEqual(handled, 1)
        self.assertEqual(detected, [])
        self.assertEqual(bot.store.cursor, "stale-new-signature")
        self.assertEqual(
            bot.store.seen,
            [("leader-wallet|stale-new-signature", "leader-wallet")],
        )


    def test_stale_backlog_does_not_hide_fresh_signature_behind_poll_limit(self):
        import time
        from unittest.mock import patch

        now = int(time.time())
        stale_rows = [
            {"signature": f"stale-signature-{n}", "blockTime": now - 120 - n}
            for n in range(12)
        ]
        # RPC returns newest-first; the cursor is older than every unseen row.
        rows = [
            {"signature": "fresh-signature", "blockTime": now},
            *stale_rows,
            {"signature": "stored-cursor", "blockTime": now - 300},
        ]

        class FakeStore:
            def __init__(self):
                self.cursor = "stored-cursor"
                self.seen = []

            def get_cursor(self, wallet):
                return self.cursor

            def set_cursor(self, wallet, signature):
                self.cursor = signature

            def signature_seen(self, key):
                return any(item[0] == key for item in self.seen)

            def mark_signature_seen(self, key, wallet):
                self.seen.append((key, wallet))

        bot = object.__new__(PumpFunBot)
        bot.rpc = SimpleNamespace(signatures=lambda wallet, limit: rows)
        bot.store = FakeStore()
        detected = []
        bot.detect_pump_signal = lambda wallet, signature: detected.append(signature) or []
        bot.process_signal = lambda signal: None

        with patch.dict("os.environ", {
            "MAX_COPY_SIGNAL_AGE_SECONDS": "30",
            "SOLANA_MAX_SIGNATURES_PER_WALLET_POLL": "1",
        }):
            handled = bot.poll_wallet("leader-wallet")

        self.assertEqual(detected, ["fresh-signature"])
        self.assertEqual(bot.store.cursor, "fresh-signature")
        self.assertEqual(handled, 13)
        self.assertEqual(len(bot.store.seen), 13)


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.rpc = SolanaRPC("https://example.invalid", timeout=1)

    def test_detect_sol_funded_buy_balance_delta(self):
        wallet = "Wallet111111111111111111111111111111111111111"
        mint = "Mint1111111111111111111111111111111111111111"
        tx = {
            "transaction": {"message": {"accountKeys": [{"pubkey": wallet}, {"pubkey": "Other"}]}},
            "meta": {
                "err": None, "preBalances": [5_000_000_000, 0], "postBalances": [4_000_000_000, 0],
                "preTokenBalances": [],
                "postTokenBalances": [{
                    "owner": wallet, "mint": mint,
                    "uiTokenAmount": {"amount": "1250000", "decimals": 6, "uiAmount": 1.25},
                }],
            },
            "blockTime": 1,
        }
        deltas, lamports = self.rpc.wallet_token_deltas(wallet, tx)
        self.assertEqual(lamports, -1_000_000_000)
        self.assertEqual(deltas[mint]["delta_raw"], 1_250_000)
        self.assertEqual(deltas[mint]["decimals"], 6)


    def test_detect_usdc_funded_buy(self):
        wallet = "Wallet111111111111111111111111111111111111111"
        mint = "Mint1111111111111111111111111111111111111111"
        usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        tx = {
            "transaction": {"message": {"accountKeys": [{"pubkey": wallet}]}},
            "meta": {
                "err": None, "preBalances": [5_000_000_000], "postBalances": [4_999_995_000],
                "preTokenBalances": [
                    {"owner": wallet, "mint": usdc,
                     "uiTokenAmount": {"amount": "10000000", "decimals": 6}}
                ],
                "postTokenBalances": [
                    {"owner": wallet, "mint": usdc,
                     "uiTokenAmount": {"amount": "9000000", "decimals": 6}},
                    {"owner": wallet, "mint": mint,
                     "uiTokenAmount": {"amount": "1250000", "decimals": 6}},
                ],
            },
            "blockTime": 1,
        }
        self.rpc.transaction = lambda signature: tx
        signal = self.rpc.detect_wallet_signal(wallet, "signature")
        self.assertEqual(len(signal), 1)
        self.assertEqual(signal[0]["side"], "buy")
        self.assertEqual(signal[0]["mint"], mint)

    def test_detect_usdc_proceeds_as_sell(self):
        wallet = "Wallet111111111111111111111111111111111111111"
        mint = "Mint1111111111111111111111111111111111111111"
        usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        tx = {
            "transaction": {"message": {"accountKeys": [{"pubkey": wallet}]}},
            "meta": {
                "err": None, "preBalances": [5_000_000_000], "postBalances": [4_999_995_000],
                "preTokenBalances": [
                    {"owner": wallet, "mint": mint,
                     "uiTokenAmount": {"amount": "1250000", "decimals": 6}}
                ],
                "postTokenBalances": [
                    {"owner": wallet, "mint": usdc,
                     "uiTokenAmount": {"amount": "1000000", "decimals": 6}},
                ],
            },
            "blockTime": 1,
        }
        self.rpc.transaction = lambda signature: tx
        signal = self.rpc.detect_wallet_signal(wallet, "signature")
        self.assertEqual(len(signal), 1)
        self.assertEqual(signal[0]["side"], "sell")
        self.assertEqual(signal[0]["mint"], mint)

    def test_ignores_ambiguous_token_to_token_swap(self):
        wallet = "Wallet111111111111111111111111111111111111111"
        buy_mint = "Mint1111111111111111111111111111111111111111"
        sell_mint = "Mint2222222222222222222222222222222222222222"
        tx = {
            "transaction": {"message": {"accountKeys": [{"pubkey": wallet}]}},
            "meta": {
                "err": None, "preBalances": [5_000_000_000], "postBalances": [4_999_995_000],
                "preTokenBalances": [{"owner": wallet, "mint": sell_mint,
                    "uiTokenAmount": {"amount": "2000000", "decimals": 6}}],
                "postTokenBalances": [
                    {"owner": wallet, "mint": sell_mint,
                     "uiTokenAmount": {"amount": "1000000", "decimals": 6}},
                    {"owner": wallet, "mint": buy_mint,
                     "uiTokenAmount": {"amount": "1000000", "decimals": 6}},
                ],
            },
            "blockTime": 1,
        }
        self.rpc.transaction = lambda signature: tx
        self.assertEqual(self.rpc.detect_wallet_signal(wallet, "signature"), [])

    def test_failed_transaction_has_no_signal_deltas(self):
        wallet = "Wallet111111111111111111111111111111111111111"
        tx = {"transaction": {"message": {"accountKeys": [wallet]}},
              "meta": {"err": {"InstructionError": [0, "Custom"]}}}
        deltas, lamports = self.rpc.wallet_token_deltas(wallet, tx)
        self.assertEqual(deltas, {})
        self.assertEqual(lamports, 0)


class MarketTests(unittest.TestCase):
    def test_sol_price_uses_sol_as_base_token(self):
        class FakeMarket(MarketData):
            def token_pairs(self, mint):
                return [
                    {"baseToken": {"address": "meme", "symbol": "MEME"},
                     "quoteToken": {"address": WSOL_MINT, "symbol": "SOL"},
                     "priceUsd": "0.0002", "liquidity": {"usd": 100000}},
                    {"baseToken": {"address": WSOL_MINT, "symbol": "SOL"},
                     "quoteToken": {"address": "usdc", "symbol": "USDC"},
                     "priceUsd": "160", "liquidity": {"usd": 50000}},
                ]
        self.assertEqual(FakeMarket().sol_price_usd(), 160.0)

    def test_eligibility_rejects_low_liquidity(self):
        m = MarketData(min_liquidity_usd=5000)
        ok, reason = m.is_entry_eligible({"liquidity_usd": 100, "pair_age_seconds": 999})
        self.assertFalse(ok)
        self.assertIn("Liquidity", reason)


class VerifiedCurveFallbackTests(unittest.TestCase):
    def test_only_fresh_security_verified_pump_discovery_can_use_curve_fallback(self):
        signal = {
            "source": "token_discovery",
            "fresh_security_verified": True,
            "curve_state_verified": True,
        }
        self.assertTrue(verified_curve_fallback_allowed(
            signal, strategy="token_discovery", venue="pump",
            curve_liquidity_usd=6690, minimum_liquidity_usd=5000,
        ))

    def test_curve_fallback_does_not_bypass_missing_security_or_liquidity(self):
        valid = {
            "source": "token_discovery",
            "fresh_security_verified": True,
            "curve_state_verified": True,
        }
        self.assertFalse(verified_curve_fallback_allowed(
            valid, strategy="leader_copy", venue="pump",
            curve_liquidity_usd=6690, minimum_liquidity_usd=5000,
        ))
        self.assertFalse(verified_curve_fallback_allowed(
            {**valid, "fresh_security_verified": False},
            strategy="token_discovery", venue="pump",
            curve_liquidity_usd=6690, minimum_liquidity_usd=5000,
        ))
        self.assertFalse(verified_curve_fallback_allowed(
            valid, strategy="token_discovery", venue="pump",
            curve_liquidity_usd=4999, minimum_liquidity_usd=5000,
        ))
        self.assertFalse(verified_curve_fallback_allowed(
            valid, strategy="token_discovery", venue="pump-amm",
            curve_liquidity_usd=6690, minimum_liquidity_usd=5000,
        ))


class TrendingLiveRetryPolicyTests(unittest.TestCase):
    def test_retryable_terminal_status_waits_for_five_minute_cooldown(self):
        now = 10_000.0
        self.assertFalse(live_candidate_retry_allowed("blocked", now - 299, now=now))
        self.assertTrue(live_candidate_retry_allowed("blocked", now - 300, now=now))
        self.assertTrue(live_candidate_retry_allowed("failed", now - 600, now=now))
        self.assertTrue(live_candidate_retry_allowed("", 0, now=now))

    def test_unknown_or_submitted_outcomes_are_never_auto_replayed(self):
        now = 10_000.0
        for status in ("queued", "submitting", "uncertain", "opened", "closed", "unknown"):
            with self.subTest(status=status):
                self.assertFalse(live_candidate_retry_allowed(status, now - 3600, now=now))


class TrendingScannerRequeueIntegrationTests(unittest.TestCase):
    class FakeStore:
        def __init__(self, state=None):
            self.state = dict(state or {})

        def get_state(self, key, default=""):
            return self.state.get(key, default)

        def set_state(self, key, value):
            self.state[str(key)] = str(value)

        def list_positions(self):
            return []

        def list_state_prefix(self, prefix, limit=10000):
            return [(key, value) for key, value in self.state.items()
                    if key.startswith(prefix)][:limit]

    def _scanner(self, previous_status, age_seconds=600):
        import time
        from trending_scanner import TrendingTokenScanner

        now = time.time()
        mint = "test-trending-mint"
        store = self.FakeStore({
            f"trending_live_status:{mint}": previous_status,
            f"trending_live_reason:{mint}": "previous attempt",
            f"trending_live_status_at:{mint}": str(now - age_seconds),
        })
        settings = SimpleNamespace(
            mode="live",
            trending_discovery_enabled=False,
            trending_discovery_live_enabled=False,
            trending_discovery_poll_seconds=60,
            trending_discovery_max_candidates=8,
            trending_min_liquidity_usd=15000,
            trending_min_volume_1h_usd=10000,
            trending_min_score=75,
            trending_max_price_impact_pct=5.0,
        )
        queued = []
        scanner = TrendingTokenScanner(
            settings, object(), timeout=5, store=store, on_screened=queued.append
        )
        scanner.enabled = True
        scanner.live_enabled = True
        scanner._get_json = lambda *args, **kwargs: {}
        scanner._dexscreener_enrich = lambda rows: None
        scanner._evaluate = lambda row: {
            **row, "status": "SCREENED", "momentum_score": 90,
            "updated_at": time.time(),
        }
        return scanner, queued, mint

    def test_old_blocked_candidate_is_rechecked_and_queued(self):
        from unittest.mock import patch
        scanner, queued, mint = self._scanner("blocked")
        candidate = {
            "mint": mint, "liquidity_usd": 20000, "volume_1h_usd": 15000,
            "momentum_score": 90, "updated_at": 1,
        }
        with patch("trending_scanner.parse_gecko_trending", return_value=[candidate]):
            scanner.scan_once()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["mint"], mint)
        self.assertEqual(scanner.store.get_state(f"trending_live_status:{mint}"), "queued")

    def test_uncertain_candidate_is_never_queued_again(self):
        from unittest.mock import patch
        scanner, queued, mint = self._scanner("uncertain")
        candidate = {
            "mint": mint, "liquidity_usd": 20000, "volume_1h_usd": 15000,
            "momentum_score": 90, "updated_at": 1,
        }
        with patch("trending_scanner.parse_gecko_trending", return_value=[candidate]):
            scanner.scan_once()
        self.assertEqual(queued, [])
        self.assertEqual(scanner.store.get_state(f"trending_live_status:{mint}"), "uncertain")


class StorageTests(unittest.TestCase):
    def test_position_and_daily_pause_persist(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "test.db", 10)
            position = {
                "mint": "mint", "decimals": 6, "amount_raw": "1000000", "entry_price_usd": 1.0,
                "initial_amount_raw": "1000000", "peak_price_usd": 1.0, "tp1_done": 0,
                "tp2_done": 0, "opened_at": "now", "last_price_usd": 1.0, "invested_usd": 1.0,
                "source_wallet": "leader", "source_signature": "sig",
            }
            store.upsert_position(position)
            self.assertEqual(store.get_position("mint")["source_wallet"], "leader")
            store.set_state("daily_paused", 1)
            store.set_state("day_key", "2000-01-01")
            store.reset_day_if_needed(10)
            self.assertEqual(store.get_state("daily_paused"), "0")
            store.close()



class ConfirmedExitLedgerReconciliationTests(unittest.TestCase):
    def test_reconciliation_atomically_removes_position_without_inventing_pnl(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "reconcile.db", 10, mode="live")
            position = {
                "mint": "test-mint",
                "decimals": 6,
                "amount_raw": "110517218",
                "entry_price_usd": 0.0104,
                "initial_amount_raw": "110517218",
                "peak_price_usd": 0.0104,
                "tp1_done": 0,
                "tp2_done": 0,
                "opened_at": "2026-10-18T10:19:16+00:00",
                "last_price_usd": 0.0076,
                "invested_usd": 1.15,
                "source_wallet": "leader-wallet",
                "source_signature": "leader-signal",
                "strategy": "leader_copy",
            }
            store.upsert_position(position)
            store.set_state("exit_pending:test-mint", "manual exit requested from dashboard")
            store.set_state("exit_blocked_balance_zero:test-mint", "zero balance")
            ok = store.reconcile_confirmed_full_exit(
                position,
                mode="live",
                tx_signature="confirmed-full-sale-signature",
                ts="2026-10-18T13:58:14+00:00",
                reason="Verified full PumpSwap sale; USD proceeds/P&L not reconstructed.",
            )

            self.assertTrue(ok)
            self.assertIsNone(store.get_position("test-mint"))
            trades = store.recent_trades(5)
            self.assertEqual(len(trades), 1)
            self.assertEqual(trades[0]["action"], "RECONCILED_EXIT")
            self.assertEqual(trades[0]["tx_signature"], "confirmed-full-sale-signature")
            self.assertEqual(trades[0]["value_usd"], 0.0)
            self.assertEqual(trades[0]["pnl_usd"], 0.0)
            self.assertEqual(store.get_state("exit_pending:test-mint"), "")
            self.assertEqual(store.get_state("exit_blocked_balance_zero:test-mint"), "")
            self.assertEqual(
                store.get_state("exit_reconciled:test-mint"),
                "confirmed-full-sale-signature",
            )
            store.close()

    def test_reconciliation_refuses_to_delete_changed_position_amount(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "reconcile.db", 10, mode="live")
            position = {
                "mint": "test-mint", "decimals": 6, "amount_raw": "1000",
                "entry_price_usd": 1.0, "initial_amount_raw": "1000",
                "peak_price_usd": 1.0, "tp1_done": 0, "tp2_done": 0,
                "opened_at": "2026-10-18T10:19:16+00:00",
                "last_price_usd": 1.0, "invested_usd": 1.0,
                "source_wallet": "", "source_signature": "",
            }
            store.upsert_position(position)
            changed = dict(position, amount_raw="900")
            self.assertFalse(store.reconcile_confirmed_full_exit(
                changed, mode="live", tx_signature="sale-signature",
                ts="2026-10-18T13:58:14+00:00", reason="Test"
            ))
            self.assertEqual(store.get_position("test-mint")["amount_raw"], "1000")
            self.assertEqual(store.recent_trades(5), [])
            store.close()


class TransactionOutcomeHandlingTests(unittest.TestCase):
    class FakeStore:
        def __init__(self):
            self.state = {
                "auto_copy_enabled": "1",
                "copy_paused": "0",
                "manual_review_halt": "0",
            }
            self.positions = []
            self.seen = []

        def get_state(self, key, default=""):
            return self.state.get(key, default)

        def set_state(self, key, value):
            self.state[key] = str(value)

        def list_positions(self):
            return list(self.positions)

        def mark_signature_seen(self, signature, wallet):
            self.seen.append((signature, wallet))

        def get_position(self, mint):
            return next((p for p in self.positions if p.get("mint") == mint), None)

    def _bot(self):
        bot = object.__new__(PumpFunBot)
        bot.store = self.FakeStore()
        bot.s = SimpleNamespace(
            watch_wallets=(),
            top_traders_count=10,
            stream_token_prices=False,
        )
        bot.auto_copy_enabled = True
        bot.active_wallets = {"leader-wallet"}
        bot.leaderboards = {}
        bot.selected_window = "day"
        bot.feed = None
        bot.logs_feed = None
        return bot

    def test_followup_runtime_issue_does_not_overwrite_existing_manual_review_reason(self):
        bot = self._bot()
        reason = "SELL outcome unresolved for mint test-mint; signature important-signature"
        bot.store.set_state("manual_review_halt", "1")
        bot.store.set_state("manual_review_reason", reason)

        bot._activate_manual_review_halt(
            "Runtime exception; reconcile wallet and transactions before resuming"
        )

        self.assertEqual(bot.store.get_state("manual_review_halt"), "0")
        self.assertEqual(bot.store.get_state("manual_review_reason"), reason)

    def test_new_uncertain_trade_appends_signature_to_existing_manual_review_reason(self):
        bot = self._bot()
        bot.store.set_state("manual_review_halt", "1")
        bot.store.set_state("manual_review_reason", "RPC wallet monitoring error")
        bot._activate_manual_review_halt(
            "SELL outcome unresolved for mint test-mint; signature important-signature",
            append_reason=True,
        )

        reason = bot.store.get_state("manual_review_reason")
        self.assertIn("RPC wallet monitoring error", reason)
        self.assertIn("important-signature", reason)
        self.assertEqual(bot.store.get_state("manual_review_halt"), "0")

    def test_uncertain_buy_halts_copy_records_signature_and_consumes_source_signal(self):
        import json
        bot = self._bot()
        exc = TransactionOutcomeUncertain(
            "submitted-signature", "buy", "test-mint", "confirmation timeout"
        )

        bot._handle_uncertain_trade(
            exc,
            source_wallet="leader-wallet",
            source_signature="leader-signature",
        )

        self.assertEqual(bot.store.get_state("manual_review_halt"), "0")
        self.assertEqual(bot.store.get_state("auto_copy_enabled"), "1")
        self.assertEqual(bot.store.get_state("copy_paused"), "0")
        self.assertIn("submitted-signature", bot.store.get_state("manual_review_reason"))
        record = json.loads(bot.store.get_state("unresolved_trade:test-mint"))
        self.assertEqual(record["signature"], "submitted-signature")
        self.assertEqual(record["action"], "buy")
        self.assertEqual(
            bot.store.seen,
            [("leader-wallet|leader-signature", "leader-wallet")],
        )
        self.assertEqual(bot.active_wallets, set())

    def test_verified_not_landed_sell_consumes_source_and_clears_only_its_pending_exit(self):
        import json

        bot = self._bot()
        reason = "followed wallet leader-wallet partial/full sell on pump"
        bot.store.set_state("exit_pending:test-mint", reason)
        exc = TransactionExpiredNotLanded(
            "expired-signature", "sell", "test-mint",
            "blockhash expired; history absent; token balance unchanged",
        )

        bot._handle_expired_not_landed_trade(
            exc,
            source_wallet="leader-wallet",
            source_signature="leader-sell-signature",
            reason=reason,
        )

        self.assertEqual(bot.store.get_state("manual_review_halt"), "0")
        self.assertEqual(bot.store.get_state("auto_copy_enabled"), "1")
        self.assertEqual(bot.store.get_state("copy_paused"), "0")
        self.assertEqual(bot.store.get_state("exit_pending:test-mint"), "")
        self.assertEqual(
            bot.store.seen,
            [("leader-wallet|leader-sell-signature", "leader-wallet")],
        )
        record = json.loads(
            bot.store.get_state("reconciled_not_landed_trade:test-mint:expired-signature")
        )
        self.assertEqual(record["resolution"],
                         "blockhash_expired_status_history_absent_token_balance_unchanged")

    def test_not_landed_sell_does_not_clear_unrelated_pending_exit(self):
        bot = self._bot()
        bot.store.set_state("exit_pending:test-mint", "trailing stop")
        exc = TransactionExpiredNotLanded(
            "expired-signature", "sell", "test-mint", "verified not landed"
        )

        bot._handle_expired_not_landed_trade(
            exc, source_wallet="leader-wallet",
            source_signature="leader-sell-signature",
            reason="followed wallet leader-wallet partial/full sell on pump",
        )

        self.assertEqual(bot.store.get_state("exit_pending:test-mint"), "trailing stop")

    def test_buy_will_not_retry_mint_with_unresolved_outcome(self):
        bot = self._bot()
        bot.store.set_state(
            "unresolved_trade:test-mint", '{"signature":"submitted-signature"}'
        )
        self.assertIsNone(bot.buy({"mint": "test-mint"}))

    def test_sell_will_not_retry_mint_with_unresolved_outcome(self):
        bot = self._bot()
        bot.store.set_state(
            "unresolved_trade:test-mint", '{"signature":"submitted-signature"}'
        )
        self.assertFalse(bot.sell("test-mint", 1000, "pending exit: test"))


class NativeSendConfirmationTests(unittest.TestCase):
    def test_lost_send_response_raises_uncertain_outcome_with_local_signature(self):
        from native_pump import NativePumpExecutor

        signature = "test-deterministic-signature"

        class FakeTransaction:
            signatures = [signature]

            def __bytes__(self):
                return b"test-signed-transaction"

        class FakeRPC:
            def call(self, method, params):
                if method == "sendTransaction":
                    raise TimeoutError("network timed out")
                raise AssertionError("status polling should not begin")

        executor = object.__new__(NativePumpExecutor)
        executor.rpc = FakeRPC()

        with self.assertRaises(TransactionOutcomeUncertain) as raised:
            executor._send_confirm(
                FakeTransaction(), action="buy", mint="test-mint",
                owner="test-wallet", token_before_raw=0,
            )

        self.assertEqual(raised.exception.signature, signature)
        self.assertEqual(raised.exception.action, "buy")
        self.assertEqual(raised.exception.mint, "test-mint")

    def test_confirmed_on_chain_error_raises_typed_failed_outcome(self):
        from native_pump import NativePumpExecutor, TransactionFailedOnChain

        signature = "confirmed-failed-signature"

        class FakeTransaction:
            signatures = [signature]

            def __bytes__(self):
                return b"test-signed-transaction"

        class FakeRPC:
            def call(self, method, params):
                if method == "sendTransaction":
                    return signature
                if method == "getSignatureStatuses":
                    return {"value": [{
                        "err": {"InstructionError": [3, {"Custom": 6042}]},
                        "confirmationStatus": "confirmed",
                    }]}
                raise AssertionError(f"unexpected RPC method: {method}")

        executor = object.__new__(NativePumpExecutor)
        executor.rpc = FakeRPC()

        with self.assertRaises(TransactionFailedOnChain) as raised:
            executor._send_confirm(
                FakeTransaction(), action="buy", mint="test-mint",
                owner="test-wallet", token_before_raw=0,
            )

        self.assertEqual(raised.exception.signature, signature)
        self.assertEqual(raised.exception.action, "buy")
        self.assertEqual(raised.exception.mint, "test-mint")
        self.assertIn("6042", raised.exception.detail)

    def test_expired_transaction_is_reconciled_when_history_absent_and_balance_unchanged(self):
        from native_pump import NativePumpExecutor
        import time

        signature = "test-expired-signature"

        class FakeTransaction:
            signatures = [signature]

            def __bytes__(self):
                return b"test-signed-transaction"

        class FakeRPC:
            def call(self, method, params):
                if method == "sendTransaction":
                    return signature
                if method == "getSignatureStatuses":
                    return {"value": [None]}
                if method == "getBlockHeight":
                    return 1000
                raise AssertionError(f"unexpected RPC method: {method}")

            def transaction(self, requested_signature):
                self.assert_signature = requested_signature
                return None

        executor = object.__new__(NativePumpExecutor)
        executor.rpc = FakeRPC()
        executor.token_balance_raw = lambda owner, mint: 1234

        started = time.monotonic()
        with self.assertRaises(TransactionExpiredNotLanded) as raised:
            executor._send_confirm(
                FakeTransaction(), action="sell", mint="test-mint",
                owner="test-wallet", token_before_raw=1234,
                last_valid_block_height=10,
            )
        self.assertEqual(raised.exception.signature, signature)
        self.assertLess(time.monotonic() - started, 10)

    def test_expired_transaction_with_changed_balance_remains_uncertain(self):
        from native_pump import NativePumpExecutor

        signature = "test-changed-balance-signature"

        class FakeTransaction:
            signatures = [signature]

            def __bytes__(self):
                return b"test-signed-transaction"

        class FakeRPC:
            def call(self, method, params):
                if method == "sendTransaction":
                    return signature
                if method == "getSignatureStatuses":
                    return {"value": [None]}
                if method == "getBlockHeight":
                    return 1000
                raise AssertionError(f"unexpected RPC method: {method}")

            def transaction(self, requested_signature):
                return None

        executor = object.__new__(NativePumpExecutor)
        executor.rpc = FakeRPC()
        executor.token_balance_raw = lambda owner, mint: 999

        with self.assertRaises(TransactionOutcomeUncertain) as raised:
            executor._send_confirm(
                FakeTransaction(), action="sell", mint="test-mint",
                owner="test-wallet", token_before_raw=1234,
                last_valid_block_height=10,
            )
        self.assertIn("token balance changed", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()