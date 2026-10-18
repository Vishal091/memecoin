import base64
import json
import struct
import time
import unittest
from types import SimpleNamespace

from native_pump import TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID
from pump_curve import BONDING_CURVE_DISCRIMINATOR, bonding_curve_address
from pumpportal import PUMP_PROGRAM_ID
from token_discovery import TokenDiscoveryShadow, score_candidate_metrics


MINT = "So11111111111111111111111111111111111111112"


def curve_account_data(real_sol=5_000_000_000, complete=False):
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
        "token_discovery_watch_seconds": 180,
        "token_discovery_max_launches_per_minute": 12,
        "token_discovery_min_liquidity_usd": 1000.0,
        "token_discovery_min_score": 75,
        "token_discovery_min_unique_buyers": 5,
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
            liquidity_usd=1500,
            min_liquidity_usd=1000,
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
            min_liquidity_usd=1000,
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


class TokenDiscoveryShadowTests(unittest.TestCase):
    def test_new_token_is_verified_and_only_subscribed_for_shadow_observation(self):
        store = FakeStore()
        feed = FakeFeed()
        scanner = TokenDiscoveryShadow(settings(), FakeRPC(), FakeMarket(), store, feed)

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
            "subscription_expires_at": now + 180,
            "last_chain_check": now, "created_at": now,
            "real_liquidity_usd": 1500, "price_usd": 0.0001,
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

        for i in range(5):
            scanner.on_token_trade_event({
                "source": "token_trade",
                "mint": MINT,
                "traderPublicKey": f"Buyer{i}1111111111111111111111111111111111111",
                "txType": "buy",
                "signature": f"trade-signature-{i}",
                "solAmount": 0.3,
                "tokenAmount": 1_000_000,
            })

        observed = json.loads(store.get_state("token_discovery_candidate:" + MINT))
        self.assertEqual(observed["status"], "shadow_open")
        self.assertTrue(observed["would_buy"])
        self.assertGreaterEqual(observed["score"], 75)
        self.assertEqual(observed["unique_buyers"], 5)
        self.assertAlmostEqual(observed["buy_volume_sol"], 1.5)
        self.assertGreater(observed["shadow_entry_price_usd"], 0)
        self.assertIn("SHADOW ENTRY only", observed["reason"])
        self.assertEqual(scanner.stats["shadow_entries"], 1)
        self.assertNotIn("trades", store.state)

    def test_tick_can_create_shadow_entry_after_real_curve_liquidity_recovers(self):
        store = FakeStore()
        feed = FakeFeed()
        scanner = TokenDiscoveryShadow(settings(), FakeRPC(), FakeMarket(), store, feed)
        old = time.time() - 20
        candidate = {
            "mint": MINT, "name": "ShadowTest", "symbol": "SHT",
            "status": "watching", "reason": "waiting for liquidity",
            "security_passed": True, "subscribed": True,
            "subscription_expires_at": time.time() + 180,
            "last_chain_check": old, "created_at": old,
            "real_liquidity_usd": 500, "price_usd": 0.0001,
            "score": 0, "score_components": {},
            "buy_trades": 5, "sell_trades": 0,
            "unique_buyers": 5,
            "buyer_wallets": [f"Buyer{i}" for i in range(5)],
            "buy_volume_sol": 1.5, "sell_volume_sol": 0.0,
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
        self.assertGreater(observed["real_liquidity_usd"], 1000)
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
