import unittest

from pumpportal_feed import (
    INITIAL_RECONNECT_DELAY_SECONDS,
    MAX_RECONNECT_DELAY_SECONDS,
    _reconnect_backoff,
)


class PumpPortalReconnectBackoffTests(unittest.TestCase):
    def test_first_failures_back_off_exponentially(self):
        delay, next_delay = _reconnect_backoff(INITIAL_RECONNECT_DELAY_SECONDS, None)
        self.assertEqual(delay, 3.0)
        self.assertEqual(next_delay, 6.0)

        delay, next_delay = _reconnect_backoff(next_delay, None)
        self.assertEqual(delay, 6.0)
        self.assertEqual(next_delay, 12.0)

    def test_backoff_is_capped(self):
        delay, next_delay = _reconnect_backoff(MAX_RECONNECT_DELAY_SECONDS, None)
        self.assertEqual(delay, 300.0)
        self.assertEqual(next_delay, 300.0)

    def test_stable_connection_resets_backoff(self):
        delay, next_delay = _reconnect_backoff(96.0, 60.0)
        self.assertEqual(delay, 3.0)
        self.assertEqual(next_delay, 6.0)

    def test_short_connection_does_not_reset_backoff(self):
        delay, next_delay = _reconnect_backoff(24.0, 12.0)
        self.assertEqual(delay, 24.0)
        self.assertEqual(next_delay, 48.0)


if __name__ == "__main__":
    unittest.main()
