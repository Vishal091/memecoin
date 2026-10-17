import tempfile
import unittest
from pathlib import Path

from market_data import MarketData, WSOL_MINT
from pumpfun_bot import (
    normalize_pending_exit_reason,
    position_source_matches_wallet,
    resolve_pending_exit_raw_amount,
)
from solana_rpc import SolanaRPC
from storage import Store


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
