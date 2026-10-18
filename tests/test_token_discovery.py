import base64
import json
import struct
import time
import unittest
from types import SimpleNamespace

from native_pump import TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID, WRAPPED_SOL_MINT
from pump_curve import BONDING_CURVE_DISCRIMINATOR, bonding_curve_address
from pumpportal import PUMP_PROGRAM_ID
from solders.pubkey import Pubkey
from token_discovery import TokenDiscoveryShadow, score_candidate_metrics


MINT = "So11111111111111111111111111111111111111112"


def curve_account_data(real_sol=5_000_000_000, complete=False, quote_mint=WRAPPED_SOL_MINT):
    raw = (
        BONDING_CURVE_DISCRIMINATOR
        + struct.pack(
            "<QQQQQ",
            1_000_000_000_000_000,
            30_000_000_000,
            793_100_000_000_000,
            real_sol,
            1_000_000_000_000_000,
        )
        + bytes([1 if complete else 0])
    )
    if quote_mint is not None:
        raw += bytes(Pubkey.from_string("11111111111111111111111111111111"))
        raw += bytes([0, 0])
        raw += bytes(Pubkey.from_string(quote_mint))
    return [base64.b64encode(raw).decode("ascii"), "base64"]


class FakeStore:
    def __init__(self):
        self.state = {}

    def get_state(self, key, default=""):
        return self.state.get(key, default)

    def set_state(self, key, value):
        self.state[key] = str(value)

    def list_state_prefix(self, prefix, limit=1000):
        rows = [(k, v) for k, v in self.state.items() if k.startswith(prefix)]
        return rows[-limit:]

    def delete_state(self, key):
        self.state.pop(key, None)


class FakeFeed:
    def __init__(self):
        self.discovery_enabled = False
        self.discovery_tokens = ()

    def set_discovery_subscriptions(self, enabled, tokens=()):
        self.discovery_enabled = bool(enabled)
        self.discovery_tokens = tuple(tokens)


class FakeMarket:
    def sol_price_usd(self):
        return 150.0

    def token_market(self, mint):
        return None


class FakeRPC:
    def __init__(self, mint_info=None, curve_data=None, holder_owner=None, holder_amount="900000000000000"):
        self.mint_info = mint_info or {
            "decimals": 6,
            "supply": "1000000000000000",
            "mintAuthority": None,
            "freezeAuthority": None,
        }
        self.curve_data = curve_data or curve_account_data()
        self.holder_owner = holder_owner or bonding_curve_address(MINT)
        self.holder_amount = str(holder_amount)
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        address = params[0]
        if method == "getAccountInfo" and address == MINT:
            return {
                "value": {
                    "owner": TOKEN_PROGRAM_ID,
                    "data": {"program": "spl-token", "parsed": {"info": self.mint_info}},
                }
            }
        if method == "getTokenLargestAccounts" and address == MINT:
            return {"value": [{"address": "LargestTokenAccount111111111111111111111111111111"}]}
        if method == "getMultipleAccounts":
            return {"value": [{
                "data": {"parsed": {"info": {
                    "mint": MINT,
                    "owner": self.holder_owner,
                    "tokenAmount": {"amount": self.holder_amount, "decimals": 6},
                }}}
            } for _ in params[0]]}
        if method == "getTokenAccountsByOwner":
            owner = params[0]
            if owner == self.holder_owner:
                return {"value": [{
                    "account": {"data": {"parsed": {"info": {
                        "mint": MINT,
                        "owner": owner,
                        "tokenAmount": {"amount": self.holder_amount, "decimals": 6},
                    }}}}
                }]}
            return {"value": []}
        if method == "getAccountInfo" and address == bonding_curve_address(MINT):
            return {
                "value": {
                    "owner": PUMP_PROGRAM_ID,
                    "data": self.curve_data,
                }
            }
        raise AssertionError(f"Unexpected RPC method/address: {method} {address}")


def settings(**overrides):
    values = {
        "token_discovery_enabled": True,
        "token_discovery_max_watched_tokens": 5,
        "token_discovery_watch_seconds": 300,
        "token_discovery_max_launches_per_minute": 12,
        "token_discovery_min_liquidity_usd": 5000.0,
        "token_discovery_min_score": 80,
        "token_discovery_min_unique_buyers": 8,
        "token_discovery_max_token_age_hours": 24,
        "token_discovery_shadow_window_seconds": 900,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class TokenDiscoveryScoringTests(unittest.TestCase):
    def test_score_rewards_diverse_buyers_net_flow_and_real_liquidity(self):
        score, parts = score_candidate_metrics(
            unique_buyers=10,
            buy_trades=10,
            sell_trades=1,
            buy_volume_sol=1.5,
            sell_volume_sol=0.1,
            liquidity_usd=6500,
            min_liquidity_usd=5000,
        )
        self.assertEqual(score, 100)
        self.assertEqual(sum(parts.values()), score)
        self.assertEqual(parts["real_curve_liquidity"], 20)

    def test_low_liquidity_caps_score_component(self):
        score, parts = score_candidate_metrics(
            unique_buyers=10,
            buy_trades=10,
            sell_trades=0,
            buy_volume_sol=2.0,
            sell_volume_sol=0,
            liquidity_usd=100,
            min_liquidity_usd=5000,
        )
        self.assertEqual(parts["real_curve_liquidity"], 0)
        self.assertLess(score, 100)

    def test_mint_authority_is_a_hard_reject(self):
        rpc = FakeRPC(mint_info={
            "decimals": 6,
            "supply": "1000000000",
            "mintAuthority": "ActiveAuthority111111111111111111111111111",
            "freezeAuthority": None,
        })
        scanner = TokenDiscoveryShadow(settings(), rpc, FakeMarket(), FakeStore())
        result = scanner._inspect_new_token(MINT)
        self.assertFalse(result["safe"])
        self.assertIn("Mint authority remains active", result["reason"])
        self.assertEqual(len(rpc.calls), 1, "Do not inspect or subscribe to the curve after a failed mint gate")

    def test_missing_quote_mint_is_a_hard_reject(self):
        rpc = FakeRPC(curve_data=curve_account_data(quote_mint=None))
        scanner = TokenDiscoveryShadow(settings(), rpc, FakeMarket(), FakeStore())
        result = scanner._inspect_new_token(MINT)
        self.assertFalse(result["safe"])
        self.assertIn("Quote mint is missing or unsupported", result["reason"])

    def test_non_sol_quote_mint_is_a_hard_reject(self):
        usdc_mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        rpc = FakeRPC(curve_data=curve_account_data(quote_mint=usdc_mint))
        scanner = TokenDiscoveryShadow(settings(), rpc, FakeMarket(), FakeStore())
        result = scanner._inspect_new_token(MINT)
        self.assertFalse(result["safe"])
        self.assertIn("Quote mint is missing or unsupported", result["reason"])

    def test_high_non_curve_holder_concentration_is_a_hard_reject(self):
        rpc = FakeRPC(
            holder_owner="Insider111111111111111111111111111111111111",
            holder_amount="400000000000000",
        )
        scanner = TokenDiscoveryShadow(settings(), rpc, FakeMarket(), FakeStore())
        result = scanner._inspect_new_token(MINT)
        self.assertFalse(result["safe"])
        self.assertIn("Top five non-curve holders control 40.0%", result["reason"])

    def test_creator_holding_above_limit_is_a_hard_reject(self):
        creator = "DevWallet1111111111111111111111111111111111"
        rpc = FakeRPC(holder_owner=creator, holder_amount="250000000000000")
        scanner = TokenDiscoveryShadow(settings(), rpc, FakeMarket(), FakeStore())
        result = scanner._inspect_new_token(MINT, creator=creator)
        self.assertFalse(result["safe"])
        self.assertIn("Creator directly holds 25.0%", result["reason"])

    def test_unreviewed_token_2022_extension_is_a_hard_reject(self):
        rpc = FakeRPC(mint_info={
            "decimals": 6,
            "supply": "1000000000",
            "mintAuthority": None,
            "freezeAuthority": None,
            "extensions": [{"extension": "transferHook"}],
        })
        scanner = TokenDiscoveryShadow(settings(), rpc, FakeMarket(), FakeStore())
        result = scanner._inspect_new_token(MINT)
        self.assertFalse(result["safe"])
        self.assertIn("Risky Token-2022 extension", result["reason"])


class TokenDiscoveryMarketCapTests(unittest.TestCase):
    def test_new_candidate_converts_pumpportal_market_cap_sol_to_usd(self):
        scanner = TokenDiscoveryShadow(settings(), FakeRPC(), FakeMarket(), FakeStore())
        candidate = scanner._new_candidate(
            MINT,
            {"mint": MINT, "name": "CapTest", "marketCapSol": 20},
            {"safe": True, "price_usd": 0.0001, "total_supply_tokens": 1_000_000_000},
        )
        self.assertEqual(candidate["market_cap_usd"], 3000.0)
        self.assertIn("marketCapSol", candidate["market_cap_source"])
        self.assertEqual(candidate["total_supply_tokens"], 1_000_000_000)
        self.assertEqual(candidate["entry_market_cap_usd"], 0.0)

    def test_market_cap_snapshot_prefers_live_entry_for_open_position(self):
        store = FakeStore()
        scanner = TokenDiscoveryShadow(settings(), FakeRPC(), FakeMarket(), store)
        scanner._save({
            "mint": MINT,
            "entry_market_cap_usd": 100_000,
            "entry_market_cap_source": "shadow estimate",
            "live_entry_market_cap_usd": 95_000,
            "live_entry_market_cap_source": "live entry estimate",
            "market_cap_usd": 110_000,
            "market_cap_source": "current estimate",
        })

        snapshot = scanner.market_cap_snapshot(MINT)

        self.assertEqual(snapshot["entry_market_cap_usd"], 95_000)
        self.assertEqual(snapshot["entry_market_cap_source"], "live entry estimate")
        self.assertEqual(snapshot["current_market_cap_usd"], 110_000)
        self.assertEqual(snapshot["current_market_cap_source"], "current estimate")

    def test_price_times_supply_is_labeled_as_estimate(self):
        scanner = TokenDiscoveryShadow(settings(), FakeRPC(), FakeMarket(), FakeStore())
        candidate = scanner._new_candidate(
            MINT,
            {"mint": MINT, "name": "EstimateTest"},
            {"safe": True, "price_usd": 0.0002, "total_supply_tokens": 1_000_000_000},
        )
        self.assertEqual(candidate["market_cap_usd"], 200_000.0)
        self.assertIn("Estimated:", candidate["market_cap_source"])


class TokenDiscoveryShadowTests(unittest.TestCase):
    def test_low_real_curve_liquidity_is_rejected_without_using_watch_slot(self):
        store = FakeStore()
        feed = FakeFeed()
        # 1 SOL real curve reserve => approximately $300 two-sided value at $150/SOL.
        rpc = FakeRPC(curve_data=curve_account_data(real_sol=1_000_000_000))
        scanner = TokenDiscoveryShadow(settings(), rpc, FakeMarket(), store, feed)

        scanner.on_new_token_event({
            "mint": MINT, "name": "TooThin", "symbol": "THIN",
            "traderPublicKey": "Creator111111111111111111111111111111111111",
            "txType": "create",
        })

        candidate = json.loads(store.get_state("token_discovery_candidate:" + MINT))
        self.assertEqual(candidate["status"], "rejected")
        self.assertFalse(candidate["subscribed"])
        self.assertIn("below required $5000", candidate["reason"])
        self.assertEqual(scanner.stats["liquidity_rejected"], 1)
        self.assertEqual(feed.discovery_tokens, ())

    def test_new_token_is_verified_and_only_subscribed_for_shadow_observation(self):
        store = FakeStore()
        feed = FakeFeed()
        scanner = TokenDiscoveryShadow(
            settings(), FakeRPC(curve_data=curve_account_data(real_sol=20_000_000_000)),
            FakeMarket(), store, feed
        )

        scanner.on_new_token_event({
            "mint": MINT, "name": "ShadowTest", "symbol": "SHT",
            "traderPublicKey": "Creator111111111111111111111111111111111111",
            "txType": "create",
        })

        candidate = json.loads(store.get_state("token_discovery_candidate:" + MINT))
        self.assertEqual(candidate["status"], "watching")
        self.assertTrue(candidate["security_passed"])
        self.assertEqual(candidate["real_liquidity_usd"], 1500)
        self.assertTrue(feed.discovery_enabled)
        self.assertEqual(feed.discovery_tokens, (MINT,))
        self.assertNotIn("position", candidate)

    def test_shadow_entry_uses_trade_metrics_and_never_calls_executor(self):
        store = FakeStore()
        feed = FakeFeed()
        scanner = TokenDiscoveryShadow(settings(), FakeRPC(), FakeMarket(), store, feed)
        now = time.time()
        candidate = {
            "mint": MINT, "name": "ShadowTest", "symbol": "SHT",
            "status": "watching", "reason": "observing",
            "security_passed": True, "subscribed": True,
            "subscription_expires_at": now + 300,
            "last_chain_check": now, "created_at": now,
            "real_liquidity_usd": 6000, "price_usd": 0.0001,
            "total_supply_tokens": 1_000_000_000,
            "market_cap_usd": 100_000.0, "market_cap_source": "Estimated: test price × supply",
            "entry_market_cap_usd": 0.0, "entry_market_cap_source": "",
            "score": 0, "score_components": {},
            "buy_trades": 0, "sell_trades": 0,
            "unique_buyers": 0, "buyer_wallets": [],
            "buy_volume_sol": 0.0, "sell_volume_sol": 0.0,
            "last_trade_signature": "", "last_trade_at": 0.0,
            "shadow_entry_price_usd": 0.0, "shadow_entry_at": 0.0,
            "current_return_pct": 0.0, "max_return_pct": 0.0,
            "min_return_pct": 0.0, "return_1m_pct": None,
            "return_5m_pct": None, "return_15m_pct": None,
        }
        store.set_state("token_discovery_candidate:" + MINT, json.dumps(candidate))

        for i in range(8):
            scanner.on_token_trade_event({
                "source": "token_trade",
                "mint": MINT,
                "traderPublicKey": f"Buyer{i}1111111111111111111111111111111111111",
                "txType": "buy",
                "signature": f"trade-signature-{i}",
                "solAmount": 0.3,
                "tokenAmount": 1_000_000,
                "marketCapSol": 20,
            })

        observed = json.loads(store.get_state("token_discovery_candidate:" + MINT))
        self.assertEqual(observed["status"], "shadow_open")
        self.assertTrue(observed["would_buy"])
        self.assertGreaterEqual(observed["score"], 80)
        self.assertEqual(observed["unique_buyers"], 8)
        self.assertAlmostEqual(observed["buy_volume_sol"], 2.4)
        self.assertGreater(observed["shadow_entry_price_usd"], 0)
        self.assertEqual(observed["entry_market_cap_usd"], 3000.0)
        self.assertEqual(observed["market_cap_usd"], 3000.0)
        self.assertIn("PumpPortal marketCapSol", observed["entry_market_cap_source"])
        self.assertIn("SHADOW ENTRY only", observed["reason"])
        self.assertEqual(scanner.stats["shadow_entries"], 1)
        self.assertNotIn("trades", store.state)

    def test_tick_can_create_shadow_entry_after_real_curve_liquidity_recovers(self):
        store = FakeStore()
        feed = FakeFeed()
        scanner = TokenDiscoveryShadow(
            settings(), FakeRPC(curve_data=curve_account_data(real_sol=20_000_000_000)),
            FakeMarket(), store, feed
        )
        old = time.time() - 20
        candidate = {
            "mint": MINT, "name": "ShadowTest", "symbol": "SHT",
            "status": "watching", "reason": "waiting for liquidity",
            "security_passed": True, "subscribed": True,
            "subscription_expires_at": time.time() + 300,
            "last_chain_check": old, "created_at": old,
            "real_liquidity_usd": 500, "price_usd": 0.0001,
            "score": 0, "score_components": {},
            "buy_trades": 8, "sell_trades": 0,
            "unique_buyers": 8,
            "buyer_wallets": [f"Buyer{i}" for i in range(8)],
            "buy_volume_sol": 2.4, "sell_volume_sol": 0.0,
            "last_trade_signature": "recent-trade", "last_trade_at": old,
            "shadow_entry_price_usd": 0.0, "shadow_entry_at": 0.0,
            "current_return_pct": 0.0, "max_return_pct": 0.0,
            "min_return_pct": 0.0, "return_1m_pct": None,
            "return_5m_pct": None, "return_15m_pct": None,
        }
        store.set_state("token_discovery_candidate:" + MINT, json.dumps(candidate))

        scanner.tick()

        observed = json.loads(store.get_state("token_discovery_candidate:" + MINT))
        self.assertEqual(observed["status"], "shadow_open")
        self.assertTrue(observed["would_buy"])
        self.assertGreater(observed["real_liquidity_usd"], 5000)
        self.assertGreater(observed["shadow_entry_price_usd"], 0)

    def test_disabled_discovery_ignores_new_tokens(self):
        store = FakeStore()
        feed = FakeFeed()
        rpc = FakeRPC()
        scanner = TokenDiscoveryShadow(
            settings(token_discovery_enabled=False), rpc, FakeMarket(), store, feed
        )
        scanner.on_new_token_event({"mint": MINT, "txType": "create"})
        self.assertEqual(scanner.stats["launch_events"], 0)
        self.assertEqual(rpc.calls, [])
        self.assertEqual(store.state, {})


if __name__ == "__main__":
    unittest.main()
