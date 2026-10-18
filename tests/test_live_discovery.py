import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pumpfun_bot import PumpFunBot
from pumpfun_config import PumpFunSettings
from native_pump import TransactionFailedOnChain
from storage import Store


class FakeStore:
    def __init__(self, positions=None):
        self.positions = [dict(p) for p in (positions or [])]
        self.state = {}
        self.trades = []
        self.entries = 0

    def get_state(self, key, default=""):
        return self.state.get(key, default)

    def set_state(self, key, value):
        self.state[key] = str(value)

    def list_positions(self):
        return self.positions

    def get_position(self, mint):
        return next((p for p in self.positions if p["mint"] == mint), None)

    def upsert_position(self, position):
        old = self.get_position(position["mint"])
        if old:
            self.positions.remove(old)
        self.positions.append(dict(position))

    def increment_daily_entries(self):
        self.entries += 1

    def add_trade(self, **kwargs):
        self.trades.append(kwargs)

    def daily_entries(self):
        return self.entries


class FakeMarket:
    def sol_price_usd(self):
        return 150.0

    def token_market(self, mint):
        return None


class FakeExecutor:
    def __init__(self):
        self.calls = []

    def trade(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "signature": "confirmed-test-signature",
            "pool": "pump",
            "total_output_amount": 1_000_000,
            "sol_delta_lamports": -3_333_333,
        }


class LiveDiscoveryConfigTests(unittest.TestCase):
    def test_live_discovery_is_off_by_default_and_cap_cannot_exceed_50_cents(self):
        with patch.dict(os.environ, {
            "BOT_MODE": "paper",
            "TOKEN_DISCOVERY_ENABLED": "true",
            "TOKEN_DISCOVERY_LIVE_ENABLED": "false",
            "TOKEN_DISCOVERY_MAX_ENTRY_USD": "9.00",
        }):
            config = PumpFunSettings.load()
        self.assertFalse(config.token_discovery_live_enabled)
        self.assertEqual(config.token_discovery_max_entry_usd, 0.50)

    def test_discovery_liquidity_floor_cannot_be_lowered_below_1000(self):
        with patch.dict(os.environ, {
            "BOT_MODE": "paper",
            "TOKEN_DISCOVERY_ENABLED": "true",
            "TOKEN_DISCOVERY_LIVE_ENABLED": "false",
            "TOKEN_DISCOVERY_MIN_LIQUIDITY_USD": "500",
        }):
            config = PumpFunSettings.load()
        self.assertEqual(config.token_discovery_min_liquidity_usd, 1000.0)

    def test_live_discovery_requires_live_mode_and_scanner_enabled(self):
        with patch.dict(os.environ, {
            "BOT_MODE": "paper",
            "TOKEN_DISCOVERY_ENABLED": "true",
            "TOKEN_DISCOVERY_LIVE_ENABLED": "true",
            "TOKEN_DISCOVERY_MAX_ENTRY_USD": "0.50",
        }):
            with self.assertRaisesRegex(ValueError, "requires BOT_MODE=live"):
                PumpFunSettings.load()

        with patch.dict(os.environ, {
            "BOT_MODE": "live",
            "TOKEN_DISCOVERY_ENABLED": "false",
            "TOKEN_DISCOVERY_LIVE_ENABLED": "true",
            "LIVE_CONFIRMATION": "I_ACCEPT_THE_RISK_AND_UNDERSTAND_LIVE_TRADING",
        }):
            with self.assertRaisesRegex(ValueError, "requires TOKEN_DISCOVERY_ENABLED=true"):
                PumpFunSettings.load()


class LiveDiscoveryBuyTests(unittest.TestCase):
    def make_bot(self, *, live_enabled=True, max_entry=0.50, global_entry_max=2.0, positions=None):
        bot = object.__new__(PumpFunBot)
        bot.s = SimpleNamespace(
            mode="live",
            token_discovery_enabled=True,
            token_discovery_live_enabled=live_enabled,
            token_discovery_max_entry_usd=max_entry,
            token_discovery_min_liquidity_usd=1000.0,
            min_liquidity_usd=1000.0,
            allow_unindexed_pump_curve=True,
            min_pair_age_seconds=10,
            slippage_bps=300,
            max_entry_usd=global_entry_max,
            max_total_exposure_usd=3.0,
            max_open_positions=3,
            max_entries_per_day=3,
            min_sol_reserve=0.008,
            pumpportal_priority_fee_sol=0.00001,
        )
        bot.store = FakeStore(positions)
        bot.market = FakeMarket()
        bot.executor = FakeExecutor()
        bot.rpc = SimpleNamespace()
        bot.wallet_address = "test-wallet"
        bot.keypair = object()
        bot._native_build_retry_deferred = lambda mint: False
        bot.equity = lambda: 10.0
        bot._refresh_risk_state = lambda equity: None
        bot._can_enter = lambda equity, **kwargs: (True, "allowed")
        bot._current_positions_value = lambda: sum(float(p.get("invested_usd", 0)) for p in bot.store.list_positions())
        bot._refresh_feed_subscriptions = lambda: None
        bot.price_cache = {}
        bot.price_cache_at = {}
        return bot

    def signal(self):
        return {
            "mint": "discovery-mint",
            "wallet": "",
            "signature": "token-discovery-discovery-mint-123",
            "side": "buy",
            "amount_ui": 0.0,
            "decimals": 6,
            "observed_price_usd": 0.0001,
            "curve_liquidity_usd": 1500.0,
            "venue": "pump",
            "source": "token_discovery",
            "curve_state_verified": True,
            "fresh_security_verified": True,
        }

    def test_live_discovery_disabled_never_calls_executor(self):
        bot = self.make_bot(live_enabled=False)
        bot.buy(self.signal(), strategy="token_discovery", entry_cap_usd=0.50)
        self.assertEqual(bot.executor.calls, [])
        self.assertEqual(bot.store.positions, [])

    def test_buy_refuses_when_one_discovery_position_is_already_open(self):
        existing = {
            "mint": "already-open", "strategy": "token_discovery",
            "amount_raw": "1000000", "invested_usd": 0.50,
        }
        bot = self.make_bot(positions=[existing])
        bot.buy(self.signal(), strategy="token_discovery", entry_cap_usd=0.50)
        self.assertEqual(bot.executor.calls, [])
        self.assertEqual(len(bot.store.positions), 1)

    def test_respects_lower_global_max_entry_setting(self):
        bot = self.make_bot(max_entry=0.50, global_entry_max=0.25)
        bot.buy(self.signal(), strategy="token_discovery", entry_cap_usd=0.50)
        self.assertEqual(len(bot.executor.calls), 1)
        self.assertLessEqual(bot.executor.calls[0]["amount"] * 150.0, 0.25 + 1e-7)

    def test_live_discovery_executor_hard_caps_spend_at_50_cents(self):
        # Even a malformed in-memory setting or caller argument cannot raise the code cap.
        bot = self.make_bot(max_entry=9.0)
        passed_gates = {}
        bot._can_enter = lambda equity, **kwargs: (passed_gates.update(kwargs) is None and True, "allowed")
        bot.buy(self.signal(), strategy="token_discovery", entry_cap_usd=9.0)

        self.assertEqual(len(bot.executor.calls), 1)
        self.assertLessEqual(bot.executor.calls[0]["amount"] * 150.0, 0.50)
        self.assertEqual(passed_gates["strategy"], "token_discovery")
        self.assertEqual(passed_gates["entry_cap_usd"], 0.50)
        self.assertEqual(bot.store.positions[0]["strategy"], "token_discovery")
        self.assertEqual(bot.store.trades[0]["strategy"], "token_discovery")
        self.assertIsNone(bot.store.positions[0]["source_wallet"])


class LiveDiscoveryCandidateGatesTests(unittest.TestCase):
    class FakeDiscovery:
        def __init__(self, candidate, inspected=None):
            self.candidate = candidate
            self.inspected = inspected or {}
            self.saved = []
            self.updated_subscriptions = False

        def candidates(self, limit=300):
            return [self.candidate]

        def _save(self, candidate):
            self.candidate = dict(candidate)
            self.saved.append(dict(candidate))

        def _apply_score(self, candidate):
            candidate["would_buy"] = True
            candidate["gate_reasons"] = []
            return candidate

        def _inspect_new_token(self, mint, creator=""):
            return self.inspected

        def _update_feed_subscriptions(self):
            self.updated_subscriptions = True

    def candidate(self, entry_at):
        return {
            "mint": "candidate-mint",
            "creator": "creator",
            "status": "shadow_open",
            "would_buy": True,
            "security_passed": True,
            "shadow_entry_at": entry_at,
            "shadow_entry_price_usd": 0.0001,
            "unique_buyers": 5,
            "buy_trades": 5,
            "sell_trades": 0,
            "buy_volume_sol": 1.5,
            "sell_volume_sol": 0.0,
            "real_liquidity_usd": 1500.0,
            "score": 78,
            "gate_reasons": [],
        }

    def make_bot(self, candidate, discovery, positions=None):
        bot = object.__new__(PumpFunBot)
        bot.s = SimpleNamespace(
            mode="live", token_discovery_enabled=True,
            token_discovery_live_enabled=True, token_discovery_max_entry_usd=0.50,
            token_discovery_min_liquidity_usd=1000.0,
        )
        bot.run_started_at = time.time() - 10
        bot.store = FakeStore(positions)
        bot.token_discovery = discovery
        bot.equity = lambda: 10.0
        bot._refresh_risk_state = lambda equity: None
        bot._can_enter = lambda equity, **kwargs: (True, "allowed")
        bot.buy_calls = []
        def fake_buy(signal, *, strategy, entry_cap_usd):
            bot.buy_calls.append((signal, strategy, entry_cap_usd))
            bot.store.upsert_position({
                "mint": signal["mint"], "strategy": strategy, "amount_raw": "1000000",
                "invested_usd": entry_cap_usd,
            })
        bot.buy = fake_buy
        return bot

    def test_old_shadow_entry_is_never_bought_after_restart(self):
        c = self.candidate(time.time() - 60)
        scanner = self.FakeDiscovery(c)
        bot = self.make_bot(c, scanner)
        bot.run_started_at = time.time() - 1

        bot._process_discovery_live_entries()

        self.assertEqual(bot.buy_calls, [])
        self.assertEqual(scanner.candidate["live_entry_status"], "skipped_stale")

    def test_transient_low_liquidity_retries_while_original_entry_is_fresh(self):
        c = self.candidate(time.time() - 1)
        c["real_liquidity_usd"] = 3000.0
        scanner = self.FakeDiscovery(c, {
            "safe": True, "liquidity_usd": 2176.0, "price_usd": 0.00011,
            "decimals": 6, "checks": ["authorities absent"],
            "top5_holder_supply_pct": 20.0, "creator_supply_pct": 10.0,
        })
        bot = self.make_bot(c, scanner)
        bot.s.token_discovery_min_liquidity_usd = 2500.0

        bot._process_discovery_live_entries()

        self.assertEqual(bot.buy_calls, [])
        self.assertEqual(scanner.candidate["live_entry_status"], "retryable")
        self.assertIn("no transaction submitted", scanner.candidate["live_entry_reason"])
        self.assertEqual(scanner.candidate["live_entry_retry_count"], 1)

        scanner.candidate["live_entry_retry_at"] = time.time() - 1
        scanner.inspected = {
            "safe": True, "liquidity_usd": 3100.0, "price_usd": 0.00011,
            "decimals": 6, "checks": ["authorities absent"],
            "top5_holder_supply_pct": 20.0, "creator_supply_pct": 10.0,
        }
        bot._process_discovery_live_entries()

        self.assertEqual(len(bot.buy_calls), 1)
        self.assertEqual(scanner.candidate["live_entry_status"], "opened")

    def test_transient_preflight_retry_never_buys_after_freshness_window_expires(self):
        c = self.candidate(time.time() - 60)
        c["live_entry_status"] = "retryable"
        c["live_entry_retry_at"] = time.time() - 1
        scanner = self.FakeDiscovery(c)
        bot = self.make_bot(c, scanner)
        bot.run_started_at = time.time() - 1

        bot._process_discovery_live_entries()

        self.assertEqual(bot.buy_calls, [])
        self.assertEqual(scanner.candidate["live_entry_status"], "skipped_stale")
        self.assertIn("no catch-up", scanner.candidate["live_entry_reason"])

    def test_interrupted_prior_submission_needs_reconciliation_and_is_not_replayed(self):
        c = self.candidate(time.time() - 60)
        c["live_entry_status"] = "submitting"
        scanner = self.FakeDiscovery(c)
        bot = self.make_bot(c, scanner)
        bot.run_started_at = time.time() - 1

        bot._process_discovery_live_entries()

        self.assertEqual(bot.buy_calls, [])
        self.assertEqual(scanner.candidate["live_entry_status"], "needs_reconciliation")
        self.assertIn("No automatic retry", scanner.candidate["live_entry_reason"])

    def test_rechecks_on_chain_then_sends_only_capped_entry(self):
        c = self.candidate(time.time() - 1)
        scanner = self.FakeDiscovery(c, {
            "safe": True, "liquidity_usd": 1700.0, "price_usd": 0.00011,
            "decimals": 6, "checks": ["authorities absent"],
            "top5_holder_supply_pct": 20.0, "creator_supply_pct": 10.0,
        })
        bot = self.make_bot(c, scanner)

        bot._process_discovery_live_entries()

        self.assertEqual(len(bot.buy_calls), 1)
        signal, strategy, cap = bot.buy_calls[0]
        self.assertEqual(strategy, "token_discovery")
        self.assertEqual(cap, 0.50)
        self.assertEqual(signal["source"], "token_discovery")
        self.assertTrue(signal["fresh_security_verified"])
        self.assertEqual(scanner.candidate["live_entry_status"], "opened")
        self.assertTrue(scanner.updated_subscriptions)

    def test_confirmed_slippage_failure_is_logged_without_automatic_retry_or_global_halt(self):
        c = self.candidate(time.time() - 1)
        scanner = self.FakeDiscovery(c, {
            "safe": True, "liquidity_usd": 1700.0, "price_usd": 0.00011,
            "decimals": 6, "checks": ["authorities absent"],
            "top5_holder_supply_pct": 20.0, "creator_supply_pct": 10.0,
        })
        bot = self.make_bot(c, scanner)

        def fail_confirmed(signal, *, strategy, entry_cap_usd):
            raise TransactionFailedOnChain(
                "failed-signature",
                "buy",
                signal["mint"],
                "{'InstructionError': [3, {'Custom': 6042}]}",
            )

        bot.buy = fail_confirmed

        bot._process_discovery_live_entries()

        self.assertEqual(scanner.candidate["live_entry_status"], "failed")
        self.assertIn("6042", scanner.candidate["live_entry_reason"])
        self.assertIn("failed-signature", scanner.candidate["live_entry_reason"])
        self.assertEqual(bot.store.positions, [])
        self.assertEqual(bot.store.get_state("manual_review_halt", "0"), "0")

    def test_one_discovery_position_blocks_another_candidate(self):
        c = self.candidate(time.time() - 1)
        scanner = self.FakeDiscovery(c, {"safe": True})
        existing = {
            "mint": "existing-mint", "strategy": "token_discovery",
            "amount_raw": "1000000", "invested_usd": 0.50,
        }
        bot = self.make_bot(c, scanner, positions=[existing])

        bot._process_discovery_live_entries()

        self.assertEqual(bot.buy_calls, [])
        self.assertEqual(scanner.candidate["live_entry_status"], "blocked")
        self.assertIn("already open", scanner.candidate["live_entry_reason"])


class StrategyPersistenceTests(unittest.TestCase):
    def test_position_and_trade_strategy_survive_database_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "live-discovery-test.db", 10, mode="live")
            position = {
                "mint": "discovery-mint", "decimals": 6, "amount_raw": "1000000",
                "entry_price_usd": 0.5, "initial_amount_raw": "1000000",
                "peak_price_usd": 0.5, "tp1_done": 0, "tp2_done": 0,
                "opened_at": "now", "last_price_usd": 0.5, "invested_usd": 0.5,
                "source_wallet": None, "source_signature": "discovery-source",
                "strategy": "token_discovery",
                "entry_market_cap_usd": 500_000.0,
                "total_supply_tokens": 1_000_000_000.0,
                "entry_market_cap_source": "Estimated: executed average price × total supply (FDV-style)",
            }
            store.upsert_position(position)
            store.add_trade(
                mode="live", action="BUY", mint="discovery-mint",
                amount_raw="1000000", price_usd=0.5, value_usd=0.5,
                reason="independent token discovery pump buy",
                source_signature="discovery-source", strategy="token_discovery",
            )
            stored_position = store.get_position("discovery-mint")
            self.assertEqual(stored_position["strategy"], "token_discovery")
            self.assertEqual(stored_position["entry_market_cap_usd"], 500_000.0)
            self.assertEqual(stored_position["total_supply_tokens"], 1_000_000_000.0)
            self.assertEqual(stored_position["entry_market_cap_source"], "Estimated: executed average price × total supply (FDV-style)")
            self.assertEqual(store.recent_trades(1)[0]["strategy"], "token_discovery")
            store.close()


if __name__ == "__main__":
    unittest.main()
