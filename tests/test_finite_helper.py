import math
import unittest

from pumpfun_bot import _finite


class FiniteNumericHelperTests(unittest.TestCase):
    def test_converts_finite_numeric_values(self):
        self.assertEqual(_finite(1), 1.0)
        self.assertEqual(_finite("2.5"), 2.5)
        self.assertEqual(_finite(-3.25), -3.25)

    def test_rejects_non_finite_values(self):
        for value in (float("nan"), float("inf"), float("-inf"), "NaN", "Infinity"):
            with self.subTest(value=value):
                self.assertIsNone(_finite(value))

    def test_rejects_missing_or_invalid_values(self):
        for value in (None, "", "not-a-number", object()):
            with self.subTest(value=repr(value)):
                self.assertIsNone(_finite(value))


if __name__ == "__main__":
    unittest.main()
