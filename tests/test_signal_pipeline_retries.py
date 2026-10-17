import queue
import time
import unittest
from unittest.mock import Mock

from pumpfun_bot import PumpFunBot
from solana_rpc import SolanaTransactionNotReady


WALLET = "2M2vLX34LXMg24dMEnjWHvRXS1tshpEDRWzmXgV8ENNZ"
SIGNATURE = "a" * 80


def make_bot(side_effect):
    bot = PumpFunBot.__new__(PumpFunBot)
    bot.solana_log_events = queue.Queue()
    bot.signal_stats = {
        "rpc_log_notifications": 0,
        "unwatched_events": 0,
        "duplicate_events": 0,
        "parsed_signals": 0,
        "rpc_log_signals": 0,
    }
    bot.active_wallets = {WALLET}
    bot.store = Mock()
    bot.store.signature_seen.return_value = False
    bot.rpc = Mock()
    bot.rpc.backoff_remaining = 0
    bot.detect_pump_signal = Mock(side_effect=side_effect)
    bot.process_signal = Mock()
    return bot


class TransactionRetryTests(unittest.TestCase):
    def test_unindexed_transaction_does_not_get_marked_seen(self):
        bot = make_bot(SolanaTransactionNotReady("not indexed yet"))
        event = {"wallet": WALLET, "signature": SIGNATURE, "source": "solana_logs"}
        bot.solana_log_events.put(event)

        bot._consume_solana_log_events()

        self.assertEqual(bot.solana_log_events.qsize(), 1)
        retried = bot.solana_log_events.get_nowait()
        self.assertEqual(retried["_retry_count"], 1)
        self.assertIn("_first_seen_at", retried)
        bot.store.mark_signature_seen.assert_not_called()
        bot.process_signal.assert_not_called()

    def test_valid_non_trade_transaction_is_marked_seen_once(self):
        bot = make_bot([])
        bot.solana_log_events.put({"wallet": WALLET, "signature": SIGNATURE})

        bot._consume_solana_log_events()

        self.assertEqual(bot.solana_log_events.qsize(), 0)
        bot.store.mark_signature_seen.assert_called_once_with(
            f"{WALLET}|{SIGNATURE}", WALLET
        )

    def test_unindexed_transaction_after_retry_window_is_not_marked_seen(self):
        bot = make_bot(SolanaTransactionNotReady("not indexed yet"))
        bot.solana_log_events.put({
            "wallet": WALLET,
            "signature": SIGNATURE,
            "_retry_count": 8,
            "_first_seen_at": time.time() - 60,
        })

        bot._consume_solana_log_events()

        self.assertEqual(bot.solana_log_events.qsize(), 0)
        bot.store.mark_signature_seen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
