import json
import tempfile
import unittest
from pathlib import Path

from research_journal import ResearchJournal, get_research_journal
from storage import Store


class ResearchJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "journal.db", 12.0, mode="paper")
        self.addCleanup(self.store.close)
        self.journal = get_research_journal(self.store, path_window_seconds=900)

    def test_event_payload_is_allowlisted_and_credentials_are_not_persisted(self):
        self.journal.record_event(
            mode="paper",
            strategy="leader_copy",
            event_type="leader_trade_observation",
            observed_at=1_800_000_000.0,
            event={
                "source": "unit-test",
                "mint": "MintSafe",
                "wallet": "PublicWallet",
                "signature": "PublicSignature",
                "side": "buy",
                "price_usd": 0.001,
                "api_key": "must-not-be-stored",
                "authorization": "must-not-be-stored",
                "headers": {"authorization": "must-not-be-stored"},
                "nested": {"api_key": "must-not-be-stored", "score": 7},
            },
        )
        row = self.store.db.execute(
            "SELECT payload_json FROM research_events"
        ).fetchone()
        payload = json.loads(row["payload_json"])
        text = json.dumps(payload)
        self.assertIn("MintSafe", text)
        self.assertIn("price_usd", text)
        self.assertNotIn("must-not-be-stored", text)
        self.assertNotIn("api_key", text)
        self.assertNotIn("authorization", text)
        self.assertNotIn("headers", payload)

    def test_verified_signal_records_future_price_path(self):
        signal_id = self.journal.record_signal(
            mode="paper", strategy="leader_copy", mint="MintA",
            signal_type="leader_trade", decision="would_copy",
            observed_at=1_800_000_000.0, price_usd=1.0,
            source_wallet="LeaderPublicWallet", source_signature="LeaderSignature",
            payload={"mint": "MintA", "price_usd": 1.0, "signer_verified": True,
                     "program_verified": True},
        )
        inserted = self.journal.record_event(
            mode="paper", strategy="leader_copy", event_type="trade_tick",
            observed_at=1_800_000_900.0,
            event={"source": "verified_tick", "mint": "MintA", "price_usd": 1.2},
        )
        marks = self.store.db.execute(
            "SELECT signal_id,price_usd FROM research_marks WHERE signal_id=?",
            (signal_id,),
        ).fetchall()
        self.assertTrue(inserted)
        self.assertGreaterEqual(len(marks), 1)
        self.assertAlmostEqual(float(marks[-1]["price_usd"]), 1.2)

    def test_signal_id_is_stable_for_the_same_source_signature_and_decision(self):
        common = dict(
            mode="paper", strategy="leader_copy", mint="MintA",
            signal_type="leader_trade", decision="would_copy",
            source_signature="SameSignature", source_wallet="Leader",
        )
        one = self.journal.record_signal(**common, observed_at=1_800_000_000.0, price_usd=1.0)
        two = self.journal.record_signal(**common, observed_at=1_800_000_005.0, price_usd=1.1)
        self.assertEqual(one, two)
        rows = self.store.db.execute(
            "SELECT COUNT(*) FROM research_signals WHERE signal_id=?", (one,)
        ).fetchone()[0]
        self.assertEqual(rows, 1)

    def test_unknown_cost_components_keep_all_in_total_unknown(self):
        result = self.journal.record_cost_estimate(
            strategy="trending_discovery", mint="MintB", notional_usd=0.50,
            protocol_creator_fee_usd=None, priority_fee_usd=0.0001,
            jito_tip_usd=0.0, slippage_pct=1.0, failed_tx_cost_usd=0.0,
            latency_ms=180.0, source="unit-test",
        )
        self.assertAlmostEqual(result["pumpportal_fee_usd"], 0.0025)
        self.assertIsNone(result["total_known_cost_usd"])
        self.assertIn("protocol_creator_fee_usd", result["missing_components"])

    def test_complete_cost_estimate_includes_fee_slippage_and_other_components(self):
        result = self.journal.record_cost_estimate(
            strategy="leader_copy", mint="MintC", notional_usd=1.0,
            protocol_creator_fee_usd=0.01, priority_fee_usd=0.001,
            jito_tip_usd=0.002, slippage_pct=1.5, failed_tx_cost_usd=0.004,
            latency_ms=250.0, source="measured-fixture",
        )
        self.assertAlmostEqual(result["total_known_cost_usd"], 0.0325)
        self.assertEqual(result["missing_components"], [])
        stored = self.store.db.execute("SELECT COUNT(*) FROM research_cost_estimates").fetchone()[0]
        self.assertEqual(stored, 2)

    def test_dashboard_snapshot_exposes_counts_not_raw_payloads(self):
        self.journal.record_candidate(
            mode="paper", strategy="trending_discovery",
            candidate={
                "mint": "MintRejected", "status": "REJECTED", "price_usd": 0.002,
                "reason": "bad holder distribution", "api_key": "secret",
            }, observed_at=1_800_000_000.0,
        )
        snap = self.journal.snapshot()
        self.assertGreaterEqual(snap["events"], 1)
        self.assertGreaterEqual(snap["signals"], 1)
        self.assertGreaterEqual(snap["rejected_signals"], 1)
        self.assertNotIn("payload", snap)


if __name__ == "__main__":
    unittest.main()
