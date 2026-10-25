import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import Settings


LIVE_CONFIRMATION = "I_ACCEPT_THE_RISK_AND_UNDERSTAND_LIVE_TRADING"


class LiveCanaryConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.keypair = Path(self.temp.name) / "test-keypair.json"
        # Config validation checks path existence only; this fixture is never loaded as a wallet.
        self.keypair.write_text("[]", encoding="utf-8")

    def live_env(self):
        return {
            "BOT_MODE": "live",
            "LIVE_ENTRIES_APPROVED": "true",
            "LIVE_CONFIRMATION": LIVE_CONFIRMATION,
            "JUPITER_API_KEY": "test-placeholder-not-a-real-key",
            "KEYPAIR_PATH": str(self.keypair),
            "MAX_ENTRY_USD": "0.50",
            "MAX_TOTAL_EXPOSURE_USD": "0.50",
            "MAX_OPEN_POSITIONS": "1",
            "MAX_ENTRIES_PER_DAY": "1",
            "DAILY_LOSS_LIMIT_USD": "0.50",
            "TOTAL_LOSS_LIMIT_USD": "0.50",
            "MIN_SOL_RESERVE": "0.025",
            "TOKEN_DISCOVERY_LIVE_ENABLED": "false",
            "TRENDING_DISCOVERY_LIVE_ENABLED": "false",
        }

    def test_paper_mode_keeps_live_approval_false_by_default(self):
        with patch.dict(os.environ, {"BOT_MODE": "paper"}, clear=True):
            settings = Settings.load()
        self.assertEqual(settings.mode, "paper")
        self.assertFalse(settings.live_entries_approved)

    def test_live_mode_requires_separate_explicit_approval(self):
        env = self.live_env()
        env["LIVE_ENTRIES_APPROVED"] = "false"
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "LIVE_ENTRIES_APPROVED=true"):
                Settings.load()

    def test_valid_live_canary_profile_passes_configuration_validation(self):
        with patch.dict(os.environ, self.live_env(), clear=True):
            settings = Settings.load()
        self.assertEqual(settings.mode, "live")
        self.assertTrue(settings.live_entries_approved)
        self.assertEqual(settings.max_entry_usd, 0.50)
        self.assertEqual(settings.max_total_exposure_usd, 0.50)
        self.assertEqual(settings.max_open_positions, 1)
        self.assertEqual(settings.max_entries_per_day, 1)
        self.assertGreaterEqual(settings.min_sol_reserve, 0.025)

    def test_live_canary_rejects_limits_above_the_hard_caps(self):
        invalid_values = (
            ("MAX_ENTRY_USD", "0.51", "MAX_ENTRY_USD"),
            ("MAX_TOTAL_EXPOSURE_USD", "0.51", "MAX_TOTAL_EXPOSURE_USD"),
            ("MAX_OPEN_POSITIONS", "2", "MAX_OPEN_POSITIONS"),
            ("MAX_ENTRIES_PER_DAY", "2", "MAX_ENTRIES_PER_DAY"),
            ("DAILY_LOSS_LIMIT_USD", "0.51", "DAILY_LOSS_LIMIT_USD"),
            ("TOTAL_LOSS_LIMIT_USD", "0.51", "TOTAL_LOSS_LIMIT_USD"),
            ("MIN_SOL_RESERVE", "0.024", "MIN_SOL_RESERVE"),
        )
        for key, value, expected_message in invalid_values:
            with self.subTest(key=key):
                env = self.live_env()
                env[key] = value
                with patch.dict(os.environ, env, clear=True):
                    with self.assertRaisesRegex(ValueError, expected_message):
                        Settings.load()

    def test_live_canary_rejects_discovery_live_buys(self):
        for key in ("TOKEN_DISCOVERY_LIVE_ENABLED", "TRENDING_DISCOVERY_LIVE_ENABLED"):
            with self.subTest(key=key):
                env = self.live_env()
                env[key] = "true"
                with patch.dict(os.environ, env, clear=True):
                    with self.assertRaisesRegex(ValueError, "leader-copy only"):
                        Settings.load()

    def test_live_approval_boolean_must_be_unambiguous(self):
        env = self.live_env()
        env["LIVE_ENTRIES_APPROVED"] = "yes"
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "must be 'true' or 'false'"):
                Settings.load()


if __name__ == "__main__":
    unittest.main()
