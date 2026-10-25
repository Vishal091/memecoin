import tempfile
import unittest
from pathlib import Path

from storage import Store


class StrategyAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "strategy-accounts.db", 12.0, mode="paper")
        self.addCleanup(self.store.close)

    def test_paper_budget_is_split_evenly_across_three_strategy_ledgers(self):
        accounts = self.store.strategy_accounts_snapshot(mode="paper")
        self.assertEqual(set(accounts), {"leader_copy", "trending_discovery", "token_discovery"})
        for account in accounts.values():
            self.assertAlmostEqual(account["starting_cash_usd"], 4.0)
            self.assertAlmostEqual(account["cash_usd"], 4.0)
            self.assertAlmostEqual(account["realized_pnl_usd"], 0.0)

    def test_fill_updates_only_the_matching_strategy_paper_ledger(self):
        self.store.add_trade(
            mode="paper", action="BUY", mint="TrendingMint", amount_raw="100",
            price_usd=0.005, value_usd=0.52, reason="unit test buy",
            tx_signature="PAPER-TEST-BUY", strategy="trending_discovery",
        )
        self.store.add_trade(
            mode="paper", action="SELL", mint="TrendingMint", amount_raw="100",
            price_usd=0.006, value_usd=0.58, pnl_usd=0.06, reason="unit test sell",
            tx_signature="PAPER-TEST-SELL", strategy="trending_discovery",
        )
        accounts = self.store.strategy_accounts_snapshot(mode="paper")
        self.assertAlmostEqual(accounts["trending_discovery"]["cash_usd"], 4.06)
        self.assertAlmostEqual(accounts["trending_discovery"]["realized_pnl_usd"], 0.06)
        self.assertAlmostEqual(accounts["leader_copy"]["cash_usd"], 4.0)
        self.assertAlmostEqual(accounts["token_discovery"]["cash_usd"], 4.0)

    def test_strategy_ledger_refuses_negative_virtual_cash(self):
        with self.assertRaises(ValueError):
            self.store.add_trade(
                mode="paper", action="BUY", mint="TooLarge", amount_raw="1",
                price_usd=1.0, value_usd=4.01, reason="budget should block",
                tx_signature="PAPER-OVER-BUDGET", strategy="leader_copy",
            )
        count = self.store.db.execute(
            "SELECT COUNT(*) FROM trades WHERE tx_signature='PAPER-OVER-BUDGET'"
        ).fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
