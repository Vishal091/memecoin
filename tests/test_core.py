import json
import tempfile
import unittest
from pathlib import Path

from market_data import MarketData, WSOL_MINT
from pumpfun_bot import (
    PumpFunBot,
    normalize_pending_exit_reason,
    position_source_matches_wallet,
    resolve_pending_exit_raw_amount,
    native_build_retry_delay_seconds,
    wallet_is_transaction_signer,
)
from native_pump import TransactionBuildFailed, TransactionConfirmationUnknown
from types import SimpleNamespace
from solana_rpc import SolanaRPC
from storage import Store


class TransactionSignerDetectionTests(unittest.TestCase):
    def test_parsed_account_key_signer_flag_is_respected(self):
        message = {
            "accountKeys": [
                {"pubkey": "leader", "signer": True},
                {"pubkey": "other", "signer": False},
            ],
            "header": {"numRequiredSignatures": 1},
        }
        self.assertTrue(wallet_is_transaction_signer("leader", message))
        self.assertFalse(wallet_is_transaction_signer("other", message))

    def test_plain_string_account_keys_use_required_signature_count(self):
        message = {
            "accountKeys": ["leader", "other"],
            "header": {"numRequiredSignatures": 1},
        }
        self.assertTrue(wallet_is_transaction_signer("leader", message))
        self.assertFalse(wallet_is_transaction_signer("other", message))

    def test_non_signer_account_key_is_not_promoted_to_signer(self):
        message = {
            "accountKeys": [
                {"pubkey": "fee-payer", "signer": True},
                {"pubkey": "leader", "signer": False},
            ],
            "header": {"numRequiredSignatures": 1},
        }
        self.assertFalse(wallet_is_transaction_signer("leader", message))

    def test_malformed_header_fails_closed(self):
        message = {"accountKeys": ["leader"], "header": {"numRequiredSignatures": "bad"}}
        self.assertFalse(wallet_is_transaction_signer("leader", message))


class AmbiguousLiveTransactionStateTests(unittest.TestCase):
    def test_persists_signature_and_forces_manual_copy_halt(self):
        class FakeStore:
            def __init__(self):
                self.state = {}

            def set_state(self, key, value):
                self.state[key] = str(value)

        bot = object.__new__(PumpFunBot)
        bot.store = FakeStore()
        bot.wallet_address = "test-wallet"
        bot.message = ""

        exc = TransactionConfirmationUnknown(
            "test-signature", "confirmation timeout after 30 seconds"
        )
        bot._record_ambiguous_live_transaction(
            "buy", "test-mint", exc,
            requested_amount_sol=0.012,
            source_wallet="leader-wallet",
            source_signature="leader-signature",
        )

        record = json.loads(bot.store.state["ambiguous_live_tx:test-signature"])
        self.assertEqual(record["signature"], "test-signature")
        self.assertEqual(record["action"], "BUY")
        self.assertEqual(record["mint"], "test-mint")
        self.assertEqual(record["requested_amount_sol"], 0.012)
        self.assertEqual(bot.store.state["ambiguous_live_tx_mint:test-mint"], "test-signature")
        self.assertEqual(bot.store.state["manual_review_halt"], "1")
        self.assertEqual(bot.store.state["copy_paused"], "1")
        self.assertIn("test-signature", bot.store.state["manual_review_reason"])

    def test_sell_is_blocked_for_mint_with_unresolved_transaction(self):
        class FakeStore:
            def get_position(self, mint):
                return {"mint": mint, "amount_raw": "100"}

            def get_state(self, key, default=""):
                return "ambiguous-signature" if key == "ambiguous_live_tx_mint:test-mint" else default

        bot = object.__new__(PumpFunBot)
        bot.s = SimpleNamespace(mode="live")
        bot.store = FakeStore()

        self.assertFalse(bot.sell("test-mint", 100, "pending exit: trailing stop"))


class StaleMonitorHaltRecoveryTests(unittest.TestCase):
    class FakeRows:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class FakeDB:
        def __init__(self, state):
            self.state = state

        def execute(self, query):
            keys = [
                key for key in self.state
                if key.startswith("ambiguous_live_tx:") or key.startswith("ambiguous_live_tx_mint:")
            ]
            return StaleMonitorHaltRecoveryTests.FakeRows([{"key": key} for key in keys])

    class FakeStore:
        def __init__(self, reason="RPC wallet monitoring error for leader-wallet; reconcile transactions before resuming"):
            self.state = {
                "manual_review_halt": "1",
                "manual_review_reason": reason,
                "copy_paused": "0",
            }
            self.db = StaleMonitorHaltRecoveryTests.FakeDB(self.state)
            self.positions = []

        def get_state(self, key, default=""):
            return self.state.get(key, default)

        def set_state(self, key, value):
            self.state[key] = str(value)

        def list_positions(self):
            return list(self.positions)

    def test_clears_only_legacy_monitor_halt_and_keeps_copy_paused(self):
        bot = object.__new__(PumpFunBot)
        bot.store = self.FakeStore()
        bot.message = ""

        self.assertTrue(bot.resolve_stale_monitor_halt())
        self.assertEqual(bot.store.state["manual_review_halt"], "0")
        self.assertEqual(bot.store.state["manual_review_reason"], "")
        self.assertEqual(bot.store.state["copy_paused"], "1")
        self.assertIn("Copying remains paused", bot.message)

    def test_refuses_recovery_when_ambiguous_transaction_exists(self):
        bot = object.__new__(PumpFunBot)
        bot.store = self.FakeStore()
        bot.store.state["ambiguous_live_tx:test-signature"] = '{"signature":"test-signature"}'

        self.assertFalse(bot.resolve_stale_monitor_halt())
        self.assertEqual(bot.store.state["manual_review_halt"], "1")

    def test_refuses_recovery_when_a_position_remains(self):
        bot = object.__new__(PumpFunBot)
        bot.store = self.FakeStore()
        bot.store.positions = [{"mint": "test-mint"}]

        self.assertFalse(bot.resolve_stale_monitor_halt())
        self.assertEqual(bot.store.state["manual_review_halt"], "1")

    def test_refuses_to_clear_an_unrelated_halt_reason(self):
        bot = object.__new__(PumpFunBot)
        bot.store = self.FakeStore("Ambiguous live BUY transaction; signature=test-signature")

        self.assertFalse(bot.resolve_stale_monitor_halt())
        self.assertEqual(bot.store.state["manual_review_halt"], "1")


class NativeBuildRetryDelayTests(unittest.TestCase):
    def test_transient_missing_pool_gets_short_per_mint_cooldown(self):
        self.assertEqual(
            native_build_retry_delay_seconds("Native SDK transaction build failed: Pool account not found"),
            60,
        )

    def test_unsupported_non_sol_quote_gets_long_per_mint_cooldown(self):
        self.assertEqual(
            native_build_retry_delay_seconds(
                "Native SDK transaction build failed: Unsupported PumpSwap quote mint PUMP"
            ),
            6 * 60 * 60,
        )


class BuildFailureHandlingTests(unittest.TestCase):
    class FakeStore:
        def __init__(self, position=None):
            self.position = position
            self.state = {}

        def get_position(self, mint):
            return self.position if self.position and self.position.get("mint") == mint else None

        def get_state(self, key, default=""):
            return self.state.get(key, default)

        def set_state(self, key, value):
            self.state[key] = str(value)

    class FakeMarket:
        def sol_price_usd(self):
            return 150.0

        def token_market(self, mint):
            return {"price_usd": 1.0, "liquidity_usd": 10_000.0, "pair_age_seconds": 120}

    class FakeExecutor:
        def trade(self, **kwargs):
            raise TransactionBuildFailed("Native SDK transaction build failed: Error: Pool account not found")

        def token_balance_raw(self, wallet, mint):
            return 1_000

    def test_unbuildable_buy_skips_only_that_signal_without_raising_monitor_error(self):
        bot = object.__new__(PumpFunBot)
        bot.s = SimpleNamespace(
            mode="live", min_liquidity_usd=5_000, min_pair_age_seconds=60,
            allow_unindexed_pump_curve=False, max_entry_usd=1.0,
            max_total_exposure_usd=10.0, slippage_bps=100,
        )
        bot.store = self.FakeStore()
        bot.market = self.FakeMarket()
        bot.executor = self.FakeExecutor()
        bot.keypair = object()
        bot.wallet_address = "test-wallet"
        bot._estimate_market_cap_usd = lambda *args, **kwargs: (None, "")
        bot.equity = lambda: 10.0
        bot._refresh_risk_state = lambda equity: None
        bot._can_enter = lambda equity: (True, "allowed")
        bot._current_positions_value = lambda: 0.0
        bot._refresh_feed_subscriptions = lambda: None
        bot.price_cache = {}
        bot.price_cache_at = {}

        bot.buy({
            "mint": "test-mint", "observed_price_usd": 1.0,
            "wallet": "leader-wallet", "signature": "leader-signature",
            "venue": "pump-amm", "source": "account_trade", "decimals": 6,
        })

        self.assertEqual(bot.store.state.get("manual_review_halt"), None)
        self.assertGreater(
            float(bot.store.state["native_build_retry_after:test-mint"]), 0
        )
        self.assertIn("Pool account not found", bot.store.state["native_build_failure:test-mint"])
        self.assertFalse(bot.store.get_position("test-mint"))

    def test_unbuildable_sell_leaves_pending_exit_without_raising_monitor_error(self):
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

        result = bot.sell(
            "test-mint", 500,
            "followed wallet leader-wallet partial/full sell on pump",
            "leader-wallet", "leader-sell-signature",
        )

        self.assertFalse(result)
        self.assertEqual(
            bot.store.get_state("exit_pending:test-mint"),
            "followed wallet leader-wallet partial/full sell on pump",
        )
        self.assertEqual(bot.store.get_state("exit_pending_amount:test-mint"), "500")
        self.assertEqual(bot.store.get_state("manual_review_halt"), "")


class DuplicateSignalHandlingTests(unittest.TestCase):
    class FakeStore:
        def __init__(self):
            self.keys = set()

        def add_signal(self, wallet, signature, mint, side, amount_ui, decimals):
            key = (wallet, signature, mint, side)
            if key in self.keys:
                return False
            self.keys.add(key)
            return True

    def test_same_transaction_from_websocket_and_logs_is_copied_once(self):
        bot = object.__new__(PumpFunBot)
        bot.active_wallets = {"leader-wallet"}
        bot.wallet_address = None
        bot.store = self.FakeStore()
        bot.signal_stats = {"duplicate_events": 0, "buy_signals": 0}
        attempted = []
        bot.buy = lambda signal: attempted.append(signal["signature"])
        signal = {
            "wallet": "leader-wallet", "signature": "same-transaction",
            "mint": "test-mint", "side": "buy", "amount_ui": 1.0, "decimals": 6,
        }

        bot.process_signal(signal)
        bot.process_signal(signal)

        self.assertEqual(attempted, ["same-transaction"])
        self.assertEqual(bot.signal_stats["duplicate_events"], 1)
        self.assertEqual(bot.signal_stats["buy_signals"], 1)


class PendingExitReasonTests(unittest.TestCase):
    def test_normal_exit_reason_is_unchanged(self):
        self.assertEqual(normalize_pending_exit_reason("trailing stop"), "trailing stop")

    def test_repeated_pending_exit_prefixes_are_removed(self):
        self.assertEqual(
            normalize_pending_exit_reason(
                "pending exit: pending exit: pending exit: trailing stop"
            ),
            "trailing stop",
        )

    def test_wrapped_leader_exit_reason_is_preserved(self):
        self.assertEqual(
            normalize_pending_exit_reason(
                "pending exit: pending exit: followed wallet Leader123 partial/full sell on pump-amm"
            ),
            "followed wallet Leader123 partial/full sell on pump-amm",
        )


class PendingExitAmountTests(unittest.TestCase):
    def test_preserves_partial_sell_amount_on_retry(self):
        self.assertEqual(resolve_pending_exit_raw_amount(1000, "250"), 250)

    def test_pending_amount_is_capped_at_current_position_balance(self):
        self.assertEqual(resolve_pending_exit_raw_amount(200, "500"), 200)

    def test_legacy_pending_exit_without_amount_defaults_to_current_balance(self):
        self.assertEqual(resolve_pending_exit_raw_amount(400, None), 400)
        self.assertEqual(resolve_pending_exit_raw_amount(400, ""), 400)

    def test_invalid_stored_amount_fails_closed(self):
        with self.assertRaises(ValueError):
            resolve_pending_exit_raw_amount(400, "not-a-number")


class SourceWalletSellMatchingTests(unittest.TestCase):
    def test_matching_source_wallet_allows_copy_sell(self):
        position = {"mint": "same-mint", "source_wallet": "leader-A"}
        self.assertTrue(position_source_matches_wallet(position, "leader-A"))

    def test_different_wallet_cannot_trigger_copy_sell_for_same_mint(self):
        position = {"mint": "same-mint", "source_wallet": "leader-A"}
        self.assertFalse(position_source_matches_wallet(position, "leader-B"))

    def test_missing_position_or_source_wallet_fails_closed(self):
        self.assertFalse(position_source_matches_wallet(None, "leader-A"))
        self.assertFalse(position_source_matches_wallet({"mint": "same-mint"}, "leader-A"))
        self.assertFalse(position_source_matches_wallet({"source_wallet": "leader-A"}, None))


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

    def test_token_market_exposes_market_cap_or_fdv_estimate(self):
        class FakeMarket(MarketData):
            def token_pairs(self, mint):
                return [{
                    "chainId": "solana",
                    "baseToken": {"address": mint, "symbol": "MEME"},
                    "quoteToken": {"address": "quote", "symbol": "SOL"},
                    "priceUsd": "0.0001",
                    "liquidity": {"usd": 25000},
                    "pairCreatedAt": 1,
                    "fdv": 125000,
                }]

        market = FakeMarket().token_market("mint")
        self.assertEqual(market["market_cap_usd"], 125000)
        self.assertEqual(market["market_cap_source"], "DexScreener FDV estimate")


class StorageTests(unittest.TestCase):
    def test_duplicate_signals_are_reported_as_not_new(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "signals.db", 10)
            args = ("leader", "signature", "mint", "buy", 1.0, 6)
            self.assertTrue(store.add_signal(*args))
            self.assertFalse(store.add_signal(*args))
            store.close()

    def test_position_and_daily_pause_persist(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "test.db", 10)
            position = {
                "mint": "mint", "decimals": 6, "amount_raw": "1000000", "entry_price_usd": 1.0,
                "initial_amount_raw": "1000000", "peak_price_usd": 1.0, "tp1_done": 0,
                "tp2_done": 0, "opened_at": "now", "last_price_usd": 1.0, "invested_usd": 1.0,
                "source_wallet": "leader", "source_signature": "sig",
                "entry_market_cap_usd": 250000.0,
                "entry_market_cap_source": "PumpPortal leader-trade snapshot",
            }
            store.upsert_position(position)
            self.assertEqual(store.get_position("mint")["source_wallet"], "leader")
            self.assertEqual(store.get_position("mint")["entry_market_cap_usd"], 250000.0)
            self.assertEqual(
                store.get_position("mint")["entry_market_cap_source"],
                "PumpPortal leader-trade snapshot",
            )
            store.set_state("daily_paused", 1)
            store.set_state("day_key", "2000-01-01")
            store.reset_day_if_needed(10)
            self.assertEqual(store.get_state("daily_paused"), "0")
            store.close()


if __name__ == "__main__":
    unittest.main()
