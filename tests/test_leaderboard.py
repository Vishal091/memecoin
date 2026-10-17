import os
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from leaderboard import LeaderboardError, PumpFunLeaderboard


class PumpFunLeaderboardTests(unittest.TestCase):
    def test_uses_auth_token_cookie_and_observed_query_parameters(self):
        env = {
            "PUMPFUN_AUTH_TOKEN": "test-session-token",
            "PUMPFUN_BEARER_TOKEN": "",
            "PUMPFUN_LEADERBOARD_URL": "https://frontend-api-v3.pump.fun/pnl-leaderboard",
            "PUMPFUN_LEADERBOARD_WEEK_VALUE": "weekly",
            "PUMPFUN_LEADERBOARD_SORT_VALUE": "combined",
            "PUMPFUN_LEADERBOARD_LIMIT": "100",
        }
        with patch.dict(os.environ, env, clear=False):
            client = PumpFunLeaderboard()
            payload = [{"walletAddress": "11111111111111111111111111111111", "pnl": 12.5}]
            response = SimpleNamespace(status_code=200, ok=True, json=lambda: payload)
            with patch.object(client.session, "get", return_value=response) as get:
                rows = client._fetch("week")

        self.assertEqual(rows[0]["wallet"], "11111111111111111111111111111111")
        self.assertEqual(client.session.headers["Cookie"], "auth_token=test-session-token")
        self.assertNotIn("Authorization", client.session.headers)
        self.assertEqual(get.call_args.kwargs["params"], {
            "limit": "100", "period": "weekly", "sort": "combined"
        })

    def test_requires_local_session_token_or_legacy_bearer(self):
        with patch.dict(os.environ, {
            "PUMPFUN_AUTH_TOKEN": "",
            "PUMPFUN_BEARER_TOKEN": "",
        }, clear=False):
            client = PumpFunLeaderboard()
        with self.assertRaises(LeaderboardError):
            client._fetch("week")


if __name__ == "__main__":
    unittest.main()
