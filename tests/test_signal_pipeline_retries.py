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


    def test_detector_retries_when_transaction_details_are_missing(self):
        bot = PumpFunBot.__new__(PumpFunBot)
        bot.rpc = Mock()
        bot.rpc.transaction.return_value = None

        with self.assertRaises(SolanaTransactionNotReady):
            bot.detect_pump_signal(WALLET, SIGNATURE)

    def test_signer_requirement_rejects_fee_recipient_mention(self):
        from pumpportal import PUMP_PROGRAM_ID
        from unittest.mock import patch

        mint = "7VzWURCfosjxvT7KKM9gib5CzqnCg4vKU2UobGp3pump"
        for signer, expected_count in ((False, 0), (True, 1)):
            bot = PumpFunBot.__new__(PumpFunBot)
            bot.rpc = Mock()
            bot.rpc.transaction.return_value = {
                "transaction": {"message": {"accountKeys": [
                    {"pubkey": WALLET, "signer": signer}
                ]}},
                "meta": {
                    "err": None,
                    "preTokenBalances": [],
                    "postTokenBalances": [],
                },
                "blockTime": 1,
            }
            bot.rpc.wallet_token_deltas.return_value = (
                {mint: {"delta_raw": 1_000_000, "delta_ui": 1.0, "decimals": 6}},
                -1_000_000_000,
            )
            bot._tx_programs = Mock(return_value=({PUMP_PROGRAM_ID}, {PUMP_PROGRAM_ID}))
            bot.market = Mock()
            with patch("pumpfun_bot.fetch_pump_curve", return_value=None):
                result = bot.detect_pump_signal(WALLET, SIGNATURE)
            self.assertEqual(len(result), expected_count)
            if not signer:
                bot.rpc.wallet_token_deltas.assert_not_called()
            else:
                self.assertEqual(result[0]["side"], "buy")
                self.assertEqual(result[0]["mint"], mint)


if __name__ == "__main__":
    unittest.main()
