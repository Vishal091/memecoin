import os
import unittest
from unittest.mock import patch

from pumpfun_config import PumpFunSettings


class PumpFunDailyEntryCapTests(unittest.TestCase):
    def load_paper_settings(self, value):
        with patch.dict(
            os.environ,
            {"BOT_MODE": "paper", "MAX_ENTRIES_PER_DAY": str(value)},
            clear=True,
        ):
            return PumpFunSettings.load()

    def test_configured_daily_entry_cap_can_be_ten(self):
        settings = self.load_paper_settings(10)
        self.assertEqual(settings.max_entries_per_day, 10)
        self.assertEqual(settings.max_entry_usd, 0.50)

    def test_daily_entry_cap_is_clamped_at_ten(self):
        settings = self.load_paper_settings(99)
        self.assertEqual(settings.max_entries_per_day, 10)
        self.assertEqual(settings.max_entry_usd, 0.50)

    def test_ten_positions_and_exposure_budget_are_configurable(self):
        with patch.dict(
            os.environ,
            {
                "BOT_MODE": "paper",
                "MAX_ENTRIES_PER_DAY": "10",
                "MAX_OPEN_POSITIONS": "10",
                "MAX_TOTAL_EXPOSURE_USD": "5.50",
            },
            clear=True,
        ):
            settings = PumpFunSettings.load()
        self.assertEqual(settings.max_open_positions, 10)
        self.assertEqual(settings.max_entries_per_day, 10)
        self.assertEqual(settings.max_total_exposure_usd, 5.50)
        self.assertEqual(settings.max_entry_usd, 0.50)

    def test_position_count_and_exposure_are_clamped(self):
        with patch.dict(
            os.environ,
            {
                "BOT_MODE": "paper",
                "MAX_OPEN_POSITIONS": "99",
                "MAX_TOTAL_EXPOSURE_USD": "99",
            },
            clear=True,
        ):
            settings = PumpFunSettings.load()
        self.assertEqual(settings.max_open_positions, 10)
        self.assertEqual(settings.max_total_exposure_usd, 5.50)

    def test_daily_entry_cap_has_minimum_one(self):
        settings = self.load_paper_settings(0)
        self.assertEqual(settings.max_entries_per_day, 1)


if __name__ == "__main__":
    unittest.main()
