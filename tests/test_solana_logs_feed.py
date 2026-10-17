import unittest

from solana_logs_feed import SolanaLogsFeed


class SolanaLogsFeedTests(unittest.TestCase):
    def test_https_rpc_url_becomes_wss_preserving_api_key_query(self):
        self.assertEqual(
            SolanaLogsFeed.to_websocket_url(
                "https://mainnet.helius-rpc.com/?api-key=test-key"
            ),
            "wss://mainnet.helius-rpc.com/?api-key=test-key",
        )

    def test_http_rpc_url_becomes_ws(self):
        self.assertEqual(
            SolanaLogsFeed.to_websocket_url("http://localhost:8899"),
            "ws://localhost:8899",
        )

    def test_existing_websocket_url_is_preserved(self):
        self.assertEqual(
            SolanaLogsFeed.to_websocket_url("wss://node.example/rpc?x=1"),
            "wss://node.example/rpc?x=1",
        )

    def test_rejects_invalid_url(self):
        with self.assertRaises(ValueError):
            SolanaLogsFeed.to_websocket_url("not-a-url")


if __name__ == "__main__":
    unittest.main()
