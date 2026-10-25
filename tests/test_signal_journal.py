import unittest
from unittest.mock import Mock

from pumpfun_bot import PumpFunBot


class FakeStore:
    def __init__(self):
        self.events = []
        self.signals = []

    def record_event(self, **kwargs):
        self.events.append(kwargs)
        return True

    def add_signal(self, wallet, signature, mint, side, amount_ui, decimals):
        self.signals.append({
            "wallet": wallet, "signature": signature, "mint": mint,
            "side": side, "amount_ui": amount_ui, "decimals": decimals,
        })

    def get_position(self, mint):
        return None


def make_bot(active_wallets, wallet_address="bot-wallet"):
    bot = object.__new__(PumpFunBot)
    bot.store = FakeStore()
    bot.active_wallets = set(active_wallets)
    bot.wallet_address = wallet_address
    bot.signal_stats = {
        "buy_signals": 0, "sell_signals": 0, "sell_without_position": 0,
    }
    bot.buy = Mock()
    bot.sell = Mock()
    return bot


def make_signal(wallet, side="sell"):
    return {
        "wallet": wallet,
        "signature": "fixture-signature",
        "mint": "fixture-mint",
        "side": side,
        "amount_ui": 1.0,
        "decimals": 6,
        "venue": "pump",
        "source": "fixture",
        "observed_price_usd": 1.0,
    }


class SignalJournalCoverageTests(unittest.TestCase):
    def test_own_wallet_signal_is_recorded_as_ignored_and_never_copied(self):
        bot = make_bot({"bot-wallet", "leader-wallet"})
        bot.process_signal(make_signal("bot-wallet", side="buy"))

        self.assertTrue(any(
            event.get("decision") == "ignored_own_wallet"
            and event.get("reason", "").startswith("signal originated from the configured bot wallet")
            for event in bot.store.events
        ))
        bot.buy.assert_not_called()
        bot.sell.assert_not_called()

    def test_leader_sell_without_follower_position_records_explicit_rejection_reason(self):
        bot = make_bot({"leader-wallet"})
        bot.process_signal(make_signal("leader-wallet", side="sell"))

        self.assertEqual(bot.signal_stats["sell_without_position"], 1)
        self.assertTrue(any(
            event.get("event_type") == "signal_decision"
            and event.get("decision") == "rejected"
            and event.get("reason") == "leader sell observed but no matching follower position is open"
            for event in bot.store.events
        ))
        bot.sell.assert_not_called()


if __name__ == "__main__":
    unittest.main()
