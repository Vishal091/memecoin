import os
import unittest
from unittest.mock import patch

from solana_rpc import SolanaRPC, SolanaRPCRateLimited


class SolanaRPCTests(unittest.TestCase):
    def test_transaction_uses_configured_v1_support(self):
        with patch.dict(os.environ, {"SOLANA_MAX_SUPPORTED_TRANSACTION_VERSION": "1"}):
            rpc = SolanaRPC("https://example.invalid", timeout=1)
        with patch.object(rpc, "call", return_value={"ok": True}) as call:
            result = rpc.transaction("signature")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(call.call_args.args[0], "getTransaction")
        self.assertEqual(
            call.call_args.args[1][1]["maxSupportedTransactionVersion"], 1
        )

    def test_http_429_starts_cooldown_and_suppresses_more_requests(self):
        with patch.dict(os.environ, {
            "SOLANA_RPC_MIN_INTERVAL_MS": "0",
            "SOLANA_RPC_429_COOLDOWN_SECONDS": "10",
        }):
            rpc = SolanaRPC("https://example.invalid", timeout=1)

        class Response:
            status_code = 429
            headers = {"Retry-After": "1"}

            def raise_for_status(self):
                raise AssertionError("HTTP 429 should be handled before raise_for_status")

        with patch.object(rpc.session, "post", return_value=Response()) as post:
            with self.assertRaises(SolanaRPCRateLimited):
                rpc.call("getTransaction", ["signature", {}])
            with self.assertRaises(SolanaRPCRateLimited):
                rpc.call("getBalance", ["address", {}])
            self.assertEqual(post.call_count, 1)
        self.assertGreater(rpc.backoff_remaining, 0)

    def test_zero_retry_after_uses_safe_default_cooldown(self):
        with patch.dict(os.environ, {
            "SOLANA_RPC_MIN_INTERVAL_MS": "0",
            "SOLANA_RPC_429_COOLDOWN_SECONDS": "10",
        }):
            rpc = SolanaRPC("https://example.invalid", timeout=1)

        class Response:
            status_code = 429
            headers = {}

        with patch.object(rpc.session, "post", return_value=Response()):
            with self.assertRaises(SolanaRPCRateLimited):
                rpc.call("getSlot", [])
        self.assertGreaterEqual(rpc.backoff_remaining, 9)


if __name__ == "__main__":
    unittest.main()
