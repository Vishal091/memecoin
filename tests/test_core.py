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
)
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

    def test_backfill_queues_recent_candidates_without_scanning_them_inline(self):
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
        self.assertEqual(candidate["reason"], "Waiting for scan/watch capacity; authenticated recent-launch backfill")
        self.assertAlmostEqual(candidate["created_at"], created_at, delta=0.01)
        self.assertEqual(discovery.stats["backfill_polls"], 1)
        self.assertEqual(discovery.stats["backfill_candidates_seen"], 1)
        self.assertEqual(discovery.stats["safety_scans"], 0)

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
        self.assertEqual(discovery.stats["queue_count"] if "queue_count" in discovery.stats else 0, 0)

    def test_low_real_curve_liquidity_is_rejected_at_five_thousand_floor(self):
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
        self.assertEqual(candidate["status"], "rejected")
        self.assertIn("below required $5000", candidate["reason"])
        self.assertEqual(discovery.stats["liquidity_rejected"], 1)



class LossExitGracePeriodTests(unittest.TestCase):
    def test_losing_exit_waits_for_remaining_two_minute_window(self):
        now = datetime(2026, 10, 18, 12, 2, tzinfo=timezone.utc)
        position = {
            "entry_price_usd": 1.0,
            "opened_at": (now - timedelta(seconds=30)).isoformat(timespec="seconds"),
        }
        self.assertEqual(
            loss_exit_hold_remaining_seconds(
                position, 0.75, "marked loss stop", 120, now=now.timestamp()
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

        def get_state(self, key, default=""):
            return self.state.get(key, default)

        def set_state(self, key, value):
            self.state[key] = str(value)

        def get_position(self, mint):
            return self.position if self.position.get("mint") == mint else None

    class FakeMarket:
        def token_market(self, mint):
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
        return bot

    def test_automatic_stop_wait_does_not_persist_stale_pending_trigger(self):
        bot = self._bot_with_fresh_position()
        self.assertFalse(bot.sell("test-mint", 1000, "marked loss stop"))
        self.assertEqual(bot.store.get_state("exit_pending:test-mint", ""), "")
        self.assertEqual(bot.store.get_state("exit_pending_amount_raw:test-mint", ""), "")

    def test_losing_leader_sell_preserves_its_partial_amount_while_deferred(self):
        bot = self._bot_with_fresh_position()
        reason = "followed wallet leader-wallet partial/full sell on pump"
        self.assertFalse(
            bot.sell(
                "test-mint", 300, reason,
                "leader-wallet", "leader-sell-signature",
            )
        )
        self.assertEqual(bot.store.get_state("exit_pending:test-mint"), reason)
        self.assertEqual(bot.store.get_state("exit_pending_amount_raw:test-mint"), "300")
        self.assertEqual(
            bot.store.get_state("exit_pending_source_wallet:test-mint"), "leader-wallet"
        )
        self.assertEqual(
            bot.store.get_state("exit_pending_source_signature:test-mint"),
            "leader-sell-signature",
        )


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

    def test_overall_loss_halt_has_clear_copy_block_reason(self):
        bot = self._bot({"halted": "1", "halt_reason": "Total loss limit reached"})
        self.assertEqual(
            bot._copy_block_reason(),
            "Overall loss halt active: Total loss limit reached",
        )

    def test_manual_review_halt_has_clear_copy_block_reason(self):
        bot = self._bot({
            "manual_review_halt": "1",
            "manual_review_reason": "Reconcile wallet before resuming",
        })
        self.assertEqual(
            bot._copy_block_reason(),
            "Manual-review halt active: Reconcile wallet before resuming",
        )

    def test_blocked_copy_does_not_subscribe_to_new_leaders(self):
        bot = self._bot({
            "manual_review_halt": "1",
            "manual_review_reason": "Reconcile wallet before resuming",
        })
        bot._refresh_feed_subscriptions()
        self.assertEqual(bot.active_wallets, set())

    def test_held_position_leader_remains_monitored_when_copy_is_blocked(self):
        position = {"mint": "open-mint", "source_wallet": "held-leader"}
        bot = self._bot({
            "manual_review_halt": "1",
            "manual_review_reason": "Reconcile wallet before resuming",
        }, positions=[position])
        bot._refresh_feed_subscriptions()
        self.assertEqual(bot.active_wallets, {"held-leader"})


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

        self.assertEqual(bot.store.get_state("manual_review_halt"), "1")
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

        self.assertEqual(bot.store.get_state("manual_review_halt"), "1")
        self.assertEqual(bot.store.get_state("auto_copy_enabled"), "0")
        self.assertEqual(bot.store.get_state("copy_paused"), "1")
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