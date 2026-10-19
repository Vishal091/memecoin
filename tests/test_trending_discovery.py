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
        }, clear=False):
            settings = PumpFunSettings.load()
        self.assertFalse(settings.trending_discovery_enabled)


if __name__ == "__main__":
    unittest.main()
