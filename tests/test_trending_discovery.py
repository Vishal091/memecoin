import os
import unittest
from unittest.mock import patch

from pumpfun_config import PumpFunSettings
from trending_scanner import (
    evaluate_rugcheck_report,
    parse_gecko_trending,
    score_trending_candidate,
)


class TrendingTokenParsingTests(unittest.TestCase):
    def test_gecko_trending_pool_extracts_token_and_metrics(self):
        mint = "TokenMint11111111111111111111111111111111"
        wrapped_sol = "So11111111111111111111111111111111111111112"
        payload = {
            "data": [{
                "id": "solana_poolAddress111111111111111111111111",
                "attributes": {
                    "name": "MOON / SOL",
                    "reserve_in_usd": "24000",
                    "market_cap_usd": "500000",
                    "fdv_usd": "520000",
                    "pool_created_at": "2026-10-18T20:00:00Z",
                    "volume_usd": {"m5": "500", "h1": "18000", "h6": "70000", "h24": "250000"},
                    "price_change_percentage": {"m5": "2.5", "h1": "12", "h6": "28", "h24": "53"},
                    "transactions": {
                        "m5": {"buys": 8, "sells": 3},
                        "h1": {"buys": 90, "sells": 40},
                    },
                },
                "relationships": {
                    "base_token": {"data": {"id": "solana_" + mint}},
                    "quote_token": {"data": {"id": "solana_" + wrapped_sol}},
                },
            }],
            "included": [
                {"id": "solana_" + mint, "attributes": {"address": mint, "name": "Moon", "symbol": "MOON"}},
                {"id": "solana_" + wrapped_sol, "attributes": {"address": wrapped_sol, "name": "Wrapped SOL", "symbol": "SOL"}},
            ],
        }

        rows = parse_gecko_trending(payload)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["mint"], mint)
        self.assertEqual(row["symbol"], "MOON")
        self.assertEqual(row["liquidity_usd"], 24000)
        self.assertEqual(row["volume_1h_usd"], 18000)
        self.assertEqual(row["buys_1h"], 90)
        self.assertEqual(row["sells_1h"], 40)
        self.assertGreater(row["momentum_score"], 0)
        self.assertFalse(row["trade_eligible"])
        self.assertEqual(row["execution_mode"], "analysis_only")

    def test_quote_asset_is_not_returned_as_the_candidate(self):
        mint = "TokenMint22222222222222222222222222222222"
        wrapped_sol = "So11111111111111111111111111111111111111112"
        payload = {
            "data": [{
                "id": "solana_poolAddress222222222222222222222222",
                "attributes": {"reserve_in_usd": 30000, "volume_usd": {"h1": 12000}},
                "relationships": {
                    "base_token": {"data": {"id": "solana_" + wrapped_sol}},
                    "quote_token": {"data": {"id": "solana_" + mint}},
                },
            }],
            "included": [
                {"id": "solana_" + wrapped_sol, "attributes": {"address": wrapped_sol, "symbol": "SOL"}},
                {"id": "solana_" + mint, "attributes": {"address": mint, "symbol": "MOON"}},
            ],
        }
        rows = parse_gecko_trending(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mint"], mint)

    def test_invalid_or_empty_gecko_payload_returns_no_candidates(self):
        self.assertEqual(parse_gecko_trending(None), [])
        self.assertEqual(parse_gecko_trending({"data": "not-a-list"}), [])


class TrendingTokenRankingTests(unittest.TestCase):
    def test_higher_liquidity_volume_and_buy_pressure_rank_higher(self):
        weak = {
            "liquidity_usd": 4000,
            "volume_1h_usd": 1000,
            "volume_24h_usd": 5000,
            "buys_1h": 5,
            "sells_1h": 9,
            "change_5m_pct": -4,
            "change_1h_pct": -10,
            "change_6h_pct": -15,
        }
        strong = {
            "liquidity_usd": 250000,
            "volume_1h_usd": 100000,
            "volume_24h_usd": 900000,
            "buys_1h": 180,
            "sells_1h": 70,
            "change_5m_pct": 2,
            "change_1h_pct": 12,
            "change_6h_pct": 18,
        }
        self.assertGreater(score_trending_candidate(strong), score_trending_candidate(weak))
        self.assertLessEqual(score_trending_candidate(strong), 100)
        self.assertGreaterEqual(score_trending_candidate(weak), 0)

    def test_extreme_short_term_spike_is_not_rewarded_without_limit(self):
        normal = {
            "liquidity_usd": 50000, "volume_1h_usd": 30000, "volume_24h_usd": 100000,
            "buys_1h": 60, "sells_1h": 30, "change_5m_pct": 5, "change_1h_pct": 30, "change_6h_pct": 15,
        }
        vertical = dict(normal, change_5m_pct=80, change_1h_pct=400)
        self.assertLess(score_trending_candidate(vertical), score_trending_candidate(normal))


class RugCheckDecisionTests(unittest.TestCase):
    def test_low_risk_complete_report_is_only_a_screened_candidate(self):
        status, flags = evaluate_rugcheck_report({
            "riskLevel": "Good",
            "risks": [],
            "mintAuthority": None,
            "freezeAuthority": None,
            "topHoldersPct": 12.5,
            "lpLockedPct": 98.0,
        })
        self.assertEqual(status, "SAFE_CANDIDATE")
        self.assertTrue(flags)

    def test_active_mint_authority_is_rejected(self):
        status, flags = evaluate_rugcheck_report({
            "riskLevel": "Good",
            "risks": [],
            "mintAuthority": "CreatorWallet",
            "freezeAuthority": None,
            "topHoldersPct": 10,
            "lpLockedPct": 99,
        })
        self.assertEqual(status, "REJECTED")
        self.assertIn("active mint authority", " ".join(flags).lower())

    def test_danger_report_is_rejected(self):
        status, flags = evaluate_rugcheck_report({"riskLevel": "Danger", "risks": []})
        self.assertEqual(status, "REJECTED")
        self.assertTrue(flags)

    def test_high_holder_concentration_is_rejected(self):
        status, flags = evaluate_rugcheck_report({
            "riskLevel": "Good",
            "risks": [],
            "mintAuthority": None,
            "freezeAuthority": None,
            "topHoldersPct": 42,
            "lpLockedPct": 99,
        })
        self.assertEqual(status, "REJECTED")
        self.assertIn("42.0%", " ".join(flags))

    def test_missing_report_fails_closed_as_unknown(self):
        status, flags = evaluate_rugcheck_report({})
        self.assertEqual(status, "UNKNOWN")
        self.assertTrue(flags)


class TrendingScannerSafetyTests(unittest.TestCase):
    def test_missing_token_2022_extension_list_stays_review(self):
        from trending_scanner import TrendingTokenScanner
        from native_pump import TOKEN_2022_PROGRAM_ID

        mint = "TokenMint33333333333333333333333333333333"

        class FakeRPC:
            def call(self, method, params):
                return {"value": {
                    "owner": TOKEN_2022_PROGRAM_ID,
                    "data": {"parsed": {"info": {
                        "decimals": 6,
                        "supply": "1000",
                        "mintAuthority": None,
                        "freezeAuthority": None,
                    }}},
                }}

        scanner = object.__new__(TrendingTokenScanner)
        scanner.rpc = FakeRPC()
        status, flags = scanner._check_mint(mint)
        self.assertEqual(status, "REVIEW")
        self.assertIn("extension list is missing", " ".join(flags))


class TrendingScannerIntegrationTests(unittest.TestCase):
    def _settings(self):
        from types import SimpleNamespace
        return SimpleNamespace(
            trending_discovery_enabled=True,
            trending_discovery_poll_seconds=60,
            trending_discovery_max_candidates=8,
            trending_min_liquidity_usd=15000.0,
            trending_min_volume_1h_usd=10000.0,
        )

    def test_scan_requests_gecko_token_metadata_and_publishes_candidates(self):
        from trending_scanner import TrendingTokenScanner

        mint = "TrendMint11111111111111111111111111111111"
        wsol = "So11111111111111111111111111111111111111112"
        payload = {
            "data": [{
                "id": "solana_poolTrend111111111111111111111111111",
                "attributes": {
                    "name": "TREND / SOL",
                    "reserve_in_usd": "24000",
                    "volume_usd": {"h1": "18000", "h24": "90000"},
                    "price_change_percentage": {"m5": "2", "h1": "8", "h6": "12", "h24": "25"},
                    "transactions": {"h1": {"buys": 80, "sells": 40}},
                },
                "relationships": {
                    "base_token": {"data": {"id": "solana_" + mint}},
                    "quote_token": {"data": {"id": "solana_" + wsol}},
                },
            }],
            "included": [
                {"id": "solana_" + mint, "attributes": {"address": mint, "name": "Trend", "symbol": "TRND"}},
                {"id": "solana_" + wsol, "attributes": {"address": wsol, "name": "Wrapped SOL", "symbol": "SOL"}},
            ],
        }
        scanner = TrendingTokenScanner(self._settings(), rpc=object())
        requests_made = []

        def fake_get(url, gecko=False):
            requests_made.append((url, gecko))
            return payload

        scanner._get_json = fake_get
        scanner._dexscreener_enrich = lambda candidates: None
        scanner._evaluate = lambda candidate: dict(candidate, status="REVIEW", decision="fixture only")
        scanner.scan_once()

        self.assertEqual(len(requests_made), 1)
        self.assertTrue(requests_made[0][1])
        self.assertIn("include=base_token,quote_token", requests_made[0][0])
        snapshot = scanner.dashboard_snapshot()
        self.assertEqual(snapshot["scan_count"], 1)
        self.assertEqual(snapshot["checked_count"], 1)
        self.assertEqual(snapshot["candidates"][0]["mint"], mint)
        self.assertFalse(snapshot["candidates"][0]["trade_eligible"])
        self.assertTrue(snapshot["analysis_only"])

    def test_dexscreener_cross_check_adds_name_liquidity_and_discrepancy_flags(self):
        from trending_scanner import TrendingTokenScanner

        mint = "TrendMint44444444444444444444444444444444"
        scanner = TrendingTokenScanner(self._settings(), rpc=object())
        pair = {
            "chainId": "solana",
            "pairAddress": "VerifiedPair111111111111111111111111111",
            "url": "https://dexscreener.com/solana/VerifiedPair111111111111111111111111111",
            "baseToken": {"address": mint, "name": "Crosschecked Trend", "symbol": "CTREND"},
            "quoteToken": {"address": "So11111111111111111111111111111111111111112", "name": "Wrapped SOL", "symbol": "SOL"},
            "liquidity": {"usd": 5000},
            "volume": {"h1": 90000, "h24": 150000},
            "priceChange": {"h1": -50, "h24": -30},
        }
        scanner._get_json = lambda url: [pair]
        candidate = {
            "mint": mint,
            "pool_address": pair["pairAddress"],
            "name": "Unknown token",
            "symbol": "—",
            "liquidity_usd": 20000,
            "volume_1h_usd": 12000,
            "change_1h_pct": 5,
            "risk_flags": [],
        }

        scanner._dexscreener_enrich([candidate])

        self.assertEqual(candidate["name"], "Crosschecked Trend")
        self.assertEqual(candidate["symbol"], "CTREND")
        self.assertEqual(candidate["liquidity_usd"], 5000)
        self.assertEqual(candidate["dexscreener_pair_address"], pair["pairAddress"])
        self.assertTrue(candidate["dexscreener_confirmed"])
        self.assertTrue(candidate["market_data_discrepancy"])
        self.assertTrue(candidate["same_pool_crosscheck"])
        self.assertGreaterEqual(len(candidate["risk_flags"]), 3)

        different_pool = {
            "mint": mint,
            "pool_address": "OtherPool11111111111111111111111111111",
            "liquidity_usd": 20000,
            "volume_1h_usd": 12000,
            "change_1h_pct": 5,
            "risk_flags": [],
        }
        scanner._dexscreener_enrich([different_pool])
        self.assertFalse(different_pool["dexscreener_confirmed"])
        self.assertFalse(different_pool["same_pool_crosscheck"])
        self.assertIn(
            "not directly comparable",
            " ".join(different_pool["risk_flags"]),
        )


class TrendingLiveQueueTests(unittest.TestCase):
    @staticmethod
    def _settings():
        from types import SimpleNamespace
        return SimpleNamespace(
            mode="live",
            trending_discovery_enabled=True,
            trending_discovery_live_enabled=True,
            trending_discovery_poll_seconds=60,
            trending_discovery_max_candidates=8,
            trending_min_liquidity_usd=15000.0,
            trending_min_volume_1h_usd=10000.0,
            trending_min_score=75,
        )

    @staticmethod
    def _gecko_payload(mint="TrendMint11111111111111111111111111111111",
                       pool="TrendPool11111111111111111111111111111"):
        wsol = "So11111111111111111111111111111111111111112"
        return {
            "data": [{
                "id": "solana_" + pool,
                "attributes": {
                    "reserve_in_usd": "1000000",
                    "volume_usd": {"h1": "1000000", "h24": "1000000000"},
                    "price_change_percentage": {"m5": "20", "h1": "30", "h6": "40", "h24": "35"},
                    "transactions": {"h1": {"buys": 200, "sells": 5}},
                },
                "relationships": {
                    "base_token": {"data": {"id": "solana_" + mint}},
                    "quote_token": {"data": {"id": "solana_" + wsol}},
                },
            }],
            "included": [
                {"id": "solana_" + mint, "attributes": {"address": mint, "name": "Trend", "symbol": "TRND"}},
                {"id": "solana_" + wsol, "attributes": {"address": wsol, "name": "Wrapped SOL", "symbol": "SOL"}},
            ],
        }

    def test_live_candidates_are_queued_once_after_screening(self):
        from trending_scanner import TrendingTokenScanner

        class MemoryStore:
            def __init__(self):
                self.state = {}
            def get_state(self, key, default=""):
                return self.state.get(key, default)
            def set_state(self, key, value):
                self.state[key] = str(value)

        store, queued = MemoryStore(), []
        scanner = TrendingTokenScanner(
            self._settings(), rpc=object(), store=store, on_screened=queued.append
        )
        scanner._get_json = lambda url, gecko=False: self._gecko_payload()
        scanner._dexscreener_enrich = lambda rows: None
        scanner._evaluate = lambda row: dict(row, status="SCREENED", momentum_score=95)
        scanner.scan_once()
        scanner.scan_once()
        self.assertEqual(len(queued), 1)
        self.assertEqual(store.get_state("trending_live_status:TrendMint11111111111111111111111111111111"), "queued")

    def test_live_recheck_fails_closed_when_exact_pool_disappears(self):
        from trending_scanner import TrendingTokenScanner

        scanner = TrendingTokenScanner(self._settings(), rpc=object())
        scanner._get_json = lambda url, gecko=False: {"data": []}
        with self.assertRaisesRegex(ValueError, "Exact pool"):
            scanner.verify_live_candidate({
                "mint": "TrendMint11111111111111111111111111111111",
                "pool_address": "TrendPool11111111111111111111111111111",
                "dexscreener_pair_address": "TrendPool11111111111111111111111111111",
            })


class TrendingScannerConfigTests(unittest.TestCase):
    def test_scanner_is_opt_in_and_thresholds_cannot_be_lowered(self):
        env = {
            "BOT_MODE": "paper",
            "TOKEN_DISCOVERY_ENABLED": "false",
            "TOKEN_DISCOVERY_LIVE_ENABLED": "false",
            "TRENDING_DISCOVERY_ENABLED": "true",
            "TRENDING_DISCOVERY_POLL_SECONDS": "5",
            "TRENDING_DISCOVERY_MAX_CANDIDATES": "40",
            "TRENDING_MIN_LIQUIDITY_USD": "100",
            "TRENDING_MIN_VOLUME_1H_USD": "10",
        }
        with patch.dict(os.environ, env, clear=False):
            settings = PumpFunSettings.load()
        self.assertTrue(settings.trending_discovery_enabled)
        self.assertEqual(settings.trending_discovery_poll_seconds, 60)
        self.assertEqual(settings.trending_discovery_max_candidates, 20)
        self.assertEqual(settings.trending_min_liquidity_usd, 15000.0)
        self.assertEqual(settings.trending_min_volume_1h_usd, 10000.0)

    def test_scanner_defaults_off(self):
        with patch.dict(os.environ, {
            "BOT_MODE": "paper",
            "TOKEN_DISCOVERY_ENABLED": "false",
            "TOKEN_DISCOVERY_LIVE_ENABLED": "false",
            "TRENDING_DISCOVERY_ENABLED": "false",
            "TRENDING_DISCOVERY_LIVE_ENABLED": "false",
        }, clear=False):
            settings = PumpFunSettings.load()
        self.assertFalse(settings.trending_discovery_enabled)
        self.assertFalse(settings.trending_discovery_live_enabled)

    def test_trending_live_cap_cannot_exceed_fifty_cents(self):
        with patch.dict(os.environ, {
            "BOT_MODE": "paper",
            "TOKEN_DISCOVERY_ENABLED": "false",
            "TOKEN_DISCOVERY_LIVE_ENABLED": "false",
            "TRENDING_DISCOVERY_ENABLED": "true",
            "TRENDING_DISCOVERY_LIVE_ENABLED": "false",
            "TRENDING_DISCOVERY_MAX_ENTRY_USD": "20",
        }, clear=False):
            settings = PumpFunSettings.load()
        self.assertEqual(settings.trending_discovery_max_entry_usd, 0.50)

    def test_trending_live_mode_requires_jupiter_api_key(self):
        with patch.dict(os.environ, {
            "BOT_MODE": "live",
            "TOKEN_DISCOVERY_ENABLED": "false",
            "TOKEN_DISCOVERY_LIVE_ENABLED": "false",
            "TRENDING_DISCOVERY_ENABLED": "true",
            "TRENDING_DISCOVERY_LIVE_ENABLED": "true",
            "JUPITER_API_KEY": "",
        }, clear=False):
            with self.assertRaisesRegex(ValueError, "JUPITER_API_KEY"):
                PumpFunSettings.load()

    def test_trending_live_mode_requires_scanner_opt_in(self):
        with patch.dict(os.environ, {
            "BOT_MODE": "live",
            "TOKEN_DISCOVERY_ENABLED": "false",
            "TOKEN_DISCOVERY_LIVE_ENABLED": "false",
            "TRENDING_DISCOVERY_ENABLED": "false",
            "TRENDING_DISCOVERY_LIVE_ENABLED": "true",
            "JUPITER_API_KEY": "test-api-key",
        }, clear=False):
            with self.assertRaisesRegex(ValueError, "requires TRENDING_DISCOVERY_ENABLED=true"):
                PumpFunSettings.load()


if __name__ == "__main__":
    unittest.main()
