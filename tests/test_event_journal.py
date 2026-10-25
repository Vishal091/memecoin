import math
import tempfile
import unittest
from pathlib import Path

from storage import Store


class EventJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "journal-test.db"
        self.store = Store(self.db_path, 10.0, mode="paper")

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_duplicate_transaction_from_two_feeds_coalesces_sources(self):
        timestamp = 1_800_000_000.0
        first = self.store.record_event(
            event_type="leader_trade",
            strategy="leader_copy",
            observed_at=timestamp,
            mint="mint-alpha",
            wallet="leader-alpha",
            side="buy",
            signature="signature-alpha",
            source="account_trade",
            status="observed",
            decision="observed",
            price_usd=0.00001,
            payload={"venue": "pump"},
        )
        second = self.store.record_event(
            event_type="leader_trade",
            strategy="leader_copy",
            observed_at=timestamp + 1,
            mint="mint-alpha",
            wallet="leader-alpha",
            side="buy",
            signature="signature-alpha",
            source="solana_logs",
            status="observed",
            decision="observed",
            price_usd=0.00001,
            payload={"venue": "pump"},
        )
        self.assertTrue(first)
        self.assertFalse(second)
        rows = self.store.list_events(event_types=["leader_trade"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "multi-source")
        self.assertEqual(rows[0]["sources"], ["account_trade", "solana_logs"])

    def test_lifecycle_decisions_keep_reject_and_later_status_separate(self):
        common = dict(
            event_type="entry_decision",
            strategy="leader_copy",
            mint="mint-alpha",
            wallet="leader-alpha",
            side="buy",
            signature="signature-alpha",
            source="entry_pipeline",
            price_usd=0.001,
        )
        self.store.record_event(
            **common, observed_at=1_800_000_000,
            status="rejected", decision="rejected", reason="liquidity below floor",
        )
        self.store.record_event(
            **common, observed_at=1_800_000_010,
            status="position_opened", decision="opened", reason="fresh checks passed",
        )
        rows = self.store.list_events(event_types=["entry_decision"])
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["decision"] for row in rows], ["rejected", "opened"])

    def test_secret_fields_and_nonfinite_prices_are_removed_or_sanitized(self):
        self.store.record_event(
            event_type="candidate_decision",
            strategy="token_discovery",
            observed_at=1_800_000_000,
            mint="mint-alpha",
            status="rejected",
            decision="rejected",
            reason="missing quote mint",
            price_usd=float("inf"),
            payload={
                "price_usd": float("nan"),
                "PUMPPORTAL_API_KEY": "DO_NOT_STORE_THIS",
                "nested": {"password": "DO_NOT_STORE_THIS_EITHER", "safe_metric": 12},
            },
        )
        row = self.store.list_events(event_types=["candidate_decision"])[0]
        self.assertIsNone(row["price_usd"])
        serialized = str(row["payload"])
        self.assertNotIn("DO_NOT_STORE_THIS", serialized)
        self.assertNotIn("DO_NOT_STORE_THIS_EITHER", serialized)
        self.assertEqual(row["payload"]["nested"]["safe_metric"], 12)

    def test_dashboard_ledger_summary_only_counts_paper_pnl(self):
        self.store.add_trade(
            mode="paper", action="SELL", mint="paper-mint", amount_raw="10",
            price_usd=1.0, value_usd=2.0, pnl_usd=0.25, reason="paper exit",
            source_wallet="leader-alpha", strategy="leader_copy",
        )
        self.store.add_trade(
            mode="live", action="SELL", mint="live-mint", amount_raw="10",
            price_usd=1.0, value_usd=2.0, pnl_usd=100.0, reason="live fixture",
            source_wallet="leader-alpha", strategy="leader_copy",
        )
        summary = self.store.event_summary()
        leader = summary["by_strategy"]["leader_copy"]
        self.assertEqual(leader["paper_sell_rows"], 1)
        self.assertAlmostEqual(leader["paper_realized_pnl_usd"], 0.25)
        self.assertEqual(summary["total_events"], 2)


if __name__ == "__main__":
    unittest.main()
