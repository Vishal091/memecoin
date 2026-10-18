import base64
import struct
import unittest

from pump_curve import (
    BONDING_CURVE_DISCRIMINATOR,
    PumpCurveSnapshot,
    bonding_curve_address,
    parse_bonding_curve_data,
)


class PumpCurveTests(unittest.TestCase):
    def fixture_data(self, complete=False):
        return (
            BONDING_CURVE_DISCRIMINATOR
            + struct.pack("<QQQQQ", 1_000_000_000_000_000, 30_000_000_000,
                          793_100_000_000_000, 5_000_000_000, 1_000_000_000_000_000)
            + bytes([1 if complete else 0])
        )

    def test_parses_base64_account_data_and_estimates_price(self):
        data = self.fixture_data()
        state = parse_bonding_curve_data([base64.b64encode(data).decode(), "base64"])
        self.assertIsInstance(state, PumpCurveSnapshot)
        self.assertAlmostEqual(state.price_usd(150.0), 0.0000045)
        self.assertAlmostEqual(state.liquidity_usd(150.0), 1500.0)

    def test_rejects_wrong_discriminator_and_truncated_data(self):
        self.assertIsNone(parse_bonding_curve_data(b"not-a-curve-account"))
        self.assertIsNone(parse_bonding_curve_data(BONDING_CURVE_DISCRIMINATOR + b"\0" * 10))

    def test_completed_curve_is_not_used_for_entry_price_or_liquidity(self):
        state = parse_bonding_curve_data(self.fixture_data(complete=True))
        self.assertIsNotNone(state)
        self.assertIsNone(state.price_usd(150.0))
        self.assertEqual(state.liquidity_usd(150.0), 0.0)

    def test_parses_extended_quote_mint_for_sol_quote_validation(self):
        from pump_curve import PumpCurveSnapshot
        from native_pump import WRAPPED_SOL_MINT
        from solders.pubkey import Pubkey

        raw = (
            self.fixture_data()
            + bytes(32)  # creator
            + bytes([0, 0])  # mayhem and cashback flags
            + bytes(Pubkey.from_string(WRAPPED_SOL_MINT))
        )
        state = parse_bonding_curve_data(raw)
        self.assertIsInstance(state, PumpCurveSnapshot)
        self.assertEqual(state.quote_mint, WRAPPED_SOL_MINT)

    def test_derives_a_deterministic_curve_address(self):
        mint = "7VzWURCfosjxvT7KKM9gib5CzqnCg4vKU2UobGp3pump"
        self.assertEqual(bonding_curve_address(mint), bonding_curve_address(mint))


if __name__ == "__main__":
    unittest.main()
