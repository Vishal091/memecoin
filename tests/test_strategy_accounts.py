import tempfile
import unittest
from pathlib import Path

from storage import Store


class StrategyAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "strategy-accounts.db", 12.0, mode="paper")
        self.addCleanup(self.store.db.close)

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

    def test_mode_mismatch_is_rejected_before_database_migration(self):
        import sqlite3

        bad_path = Path(self.tmp.name) / "live-locked.db"
        connection = sqlite3.connect(str(bad_path))
        connection.executescript("""
            CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO state(key,value) VALUES('database_mode','live');
            CREATE TABLE trades (id INTEGER PRIMARY KEY, action TEXT NOT NULL);
            INSERT INTO trades(action) VALUES('BUY');
            CREATE TABLE positions (mint TEXT PRIMARY KEY);
        """)
        connection.commit()
        before_tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        connection.close()

        with self.assertRaisesRegex(RuntimeError, "locked to 'live' mode"):
            Store(bad_path, 10.0, mode="paper")

        check = sqlite3.connect(str(bad_path))
        after_tables = {
            row[0] for row in check.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertEqual(after_tables, before_tables)
        self.assertNotIn("strategy_accounts", after_tables)
        self.assertEqual(
            check.execute("SELECT value FROM state WHERE key='database_mode'").fetchone()[0],
            "live",
        )
        self.assertEqual(check.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 1)
        check.close()

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
