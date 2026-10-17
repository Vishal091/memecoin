import tempfile
import unittest
from pathlib import Path

from market_data import MarketData, WSOL_MINT
from pumpfun_bot import PumpFunBot, native_build_retry_delay_seconds
from native_pump import TransactionBuildFailed
from types import SimpleNamespace
from solana_rpc import SolanaRPC
from storage import Store




class BuildFailureHandlingTests(unittest.TestCase):
    class FakeStore:
        def __init__(self):
            self.state = {}

        def get_position(self, mint):
            return None

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
            raise TransactionBuildFailed(
                "Native Pump SDK transaction build failed: Error: "
                "Native executor only supports SOL-quoted Pump tokens; refusing a token-quoted coin."
            )

    def test_token_quoted_buy_is_skipped_without_global_halt(self):
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

        bot.buy({
            "mint": "test-mint", "observed_price_usd": 1.0,
            "wallet": "leader-wallet", "signature": "leader-signature",
            "venue": "pump-amm", "source": "solana_logs", "decimals": 6,
        })

        self.assertNotIn("manual_review_halt", bot.store.state)
        self.assertGreater(float(bot.store.state["native_build_retry_after:test-mint"]), 0)
        self.assertIn("token-quoted coin", bot.store.state["native_build_failure:test-mint"])
        self.assertIsNone(bot.store.get_position("test-mint"))

    def test_quote_token_failure_gets_long_cooldown(self):
        self.assertEqual(
            native_build_retry_delay_seconds("token-quoted coin is not supported"),
            6 * 60 * 60,
        )

    def test_other_builder_failures_get_short_cooldown(self):
        self.assertEqual(native_build_retry_delay_seconds("Pool account not found"), 60)

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
