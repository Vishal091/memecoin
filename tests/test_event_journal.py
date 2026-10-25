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

    def test_embedded_credential_values_are_redacted_inside_safe_string_fields(self):
        self.store.record_event(
            event_type="candidate_decision",
            strategy="token_discovery",
            observed_at=1_800_000_000,
            mint="mint-alpha",
            status="observed",
            decision="screened",
            payload={
                "metadata_uri": "https://example.invalid/item.json?access_token=QUERY_SECRET&safe=1",
                "signed_url": "https://example.invalid/blob?X-Amz-Credential=CREDENTIAL_SECRET&X-Amz-Signature=SIG_SECRET",
                "diagnostic": "Authorization: Bearer BEARER_SECRET",
                "debug": "api_key=INLINE_SECRET",
                "cookie_note": "Cookie: session=COOKIE_SECRET",
                "safe_metric": "ordinary diagnostic text",
            },
        )
        row = self.store.list_events(event_types=["candidate_decision"])[0]
        serialized = str(row["payload"])
        for secret in ("QUERY_SECRET", "CREDENTIAL_SECRET", "SIG_SECRET", "BEARER_SECRET", "INLINE_SECRET", "COOKIE_SECRET"):
            self.assertNotIn(secret, serialized)
        self.assertIn("[REDACTED]", serialized)
        self.assertEqual(row["payload"]["safe_metric"], "ordinary diagnostic text")

    def test_retention_limit_is_enforced_on_every_insert_not_only_hourly(self):
        self.store.journal.RETENTION_ROWS = 3
        for index in range(5):
            self.store.record_event(
                event_type="price_mark",
                strategy="market_data",
                observed_at=1_800_000_000 + index,
                mint=f"mint-{index}",
                source="retention-fixture",
                status="observed",
                decision="price_mark",
                price_usd=1.0 + index,
            )

        rows = self.store.list_events(limit=10)
        self.assertEqual(len(rows), 3)
        self.assertEqual([row["mint"] for row in rows], ["mint-2", "mint-3", "mint-4"])
        actual_count = self.store.db.execute(
            "SELECT COUNT(*) FROM event_journal"
        ).fetchone()[0]
        self.assertEqual(actual_count, 3)
        self.assertEqual(self.store.journal._row_count, 3)

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
