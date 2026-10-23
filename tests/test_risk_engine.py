import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from risk_engine import RECONCILIATION_CONFIRMATION, RiskEngine
from storage import Store


class RiskEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "risk-test.db", 10.0, mode="paper")
        self.addCleanup(self.store.db.close)
        self.store.reset_day_if_needed(10.0)
        self.settings = SimpleNamespace(
            mode="paper",
            live_entries_approved=False,
            max_entry_usd=0.50,
            max_total_exposure_usd=1.00,
            max_open_positions=1,
            max_entries_per_day=4,
            token_discovery_max_entry_usd=0.50,
            trending_discovery_max_entry_usd=0.50,
            max_portfolio_drawdown_pct=0.15,
        )
        self.risk = RiskEngine(self.store, self.settings)
        self.risk.engage_on_startup()

    def test_startup_engages_global_entry_switch(self):
        self.assertTrue(self.risk.kill_switch_active())
        reservation, reason = self.risk.reserve_entry("MintA", "leader_copy", 0.25)
        self.assertIsNone(reservation)
        self.assertIn("kill switch", reason.lower())

    def test_explicit_resume_allows_one_reservation_and_blocks_second(self):
        ok, reason = self.risk.resume_entries()
        self.assertTrue(ok, reason)
        first, reason = self.risk.reserve_entry(
            "MintA", "leader_copy", 0.50, exposure_usd=0.55
        )
        self.assertIsNotNone(first, reason)
        second, reason = self.risk.reserve_entry(
            "MintB", "trending_discovery", 0.40, exposure_usd=0.45
        )
        self.assertIsNone(second)
        self.assertIn("reservation", reason.lower())
        self.assertEqual(self.store.get_entry_reservation(first)["status"], "reserved")

    def test_switch_is_rechecked_at_submission_boundary(self):
        self.assertTrue(self.risk.resume_entries()[0])
        reservation, reason = self.risk.reserve_entry("MintA", "leader_copy", 0.40)
        self.assertIsNotNone(reservation, reason)
        self.risk.activate_kill_switch("Operator emergency stop")
        permitted, reason = self.risk.mark_submitting(reservation)
        self.assertFalse(permitted)
        self.assertIn("kill switch", reason.lower())
        self.risk.release_entry(reservation, status="released", detail="Test cleanup")
        self.assertTrue(self.risk.kill_switch_active())

    def test_live_restart_marks_interrupted_submission_uncertain(self):
        self.assertTrue(self.risk.resume_entries()[0])
        reservation, reason = self.risk.reserve_entry("MintA", "leader_copy", 0.40)
        self.assertIsNotNone(reservation, reason)
        self.assertTrue(self.risk.mark_submitting(reservation)[0])

        # Simulate the next process booting in live mode, where an interrupted
        # submission must remain uncertain instead of being automatically released.
        self.settings.mode = "live"
        self.settings.live_entries_approved = True  # exercise restart recovery in an explicitly approved test profile
        self.risk.engage_on_startup()
        row = self.store.get_entry_reservation(reservation)
        self.assertEqual(row["status"], "uncertain")
        self.assertTrue(self.risk.kill_switch_active())
        allowed, reason = self.risk.resume_entries()
        self.assertFalse(allowed)
        self.assertIn("reservation", reason.lower())

    def test_live_entries_are_blocked_without_separate_approval(self):
        self.settings.mode = "live"
        self.settings.live_entries_approved = False
        self.risk.engage_on_startup()

        allowed, reason = self.risk.resume_entries()
        self.assertFalse(allowed)
        self.assertIn("LIVE_ENTRIES_APPROVED=false", reason)
        self.assertIn("LIVE_ENTRIES_APPROVED=false", self.risk.gate_reason())
        reservation, reason = self.risk.reserve_entry("MintLive", "leader_copy", 0.25)
        self.assertIsNone(reservation)
        self.assertIn("LIVE_ENTRIES_APPROVED=false", reason)
        self.assertTrue(self.risk.kill_switch_active())

    def test_uncertain_transaction_requires_exact_signature_and_phrase(self):
        self.assertTrue(self.risk.resume_entries()[0])
        reservation, reason = self.risk.reserve_entry("MintA", "leader_copy", 0.40)
        self.assertIsNotNone(reservation, reason)
        self.risk.mark_uncertain("MintA", "Unknown submission result")
        self.store.set_state(
            "unresolved_trade:MintA",
            json.dumps({
                "signature": "SigA",
                "action": "buy",
                "detail": "Ambiguous transaction",
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }),
        )

        self.assertFalse(self.risk.acknowledge_reconciled_trade(
            "MintA", "WrongSig", RECONCILIATION_CONFIRMATION
        )[0])
        self.assertFalse(self.risk.acknowledge_reconciled_trade(
            "MintA", "SigA", "wrong phrase"
        )[0])
        ok, reason = self.risk.acknowledge_reconciled_trade(
            "MintA", "SigA", RECONCILIATION_CONFIRMATION
        )
        self.assertTrue(ok, reason)
        self.assertTrue(self.risk.kill_switch_active())
        self.assertTrue(self.risk.resume_entries()[0])

    def test_unused_pre_submit_reservation_can_be_released_without_chain_ack(self):
        self.assertTrue(self.risk.resume_entries()[0])
        reservation, reason = self.risk.reserve_entry("MintUnused", "leader_copy", 0.25)
        self.assertIsNotNone(reservation, reason)

        ok, detail = self.risk.acknowledge_uncertain_reservation(reservation, "")
        self.assertTrue(ok, detail)
        self.assertEqual(self.store.get_entry_reservation(reservation)["status"], "released")
        self.assertFalse(self.risk.kill_switch_active())
        another, reason = self.risk.reserve_entry("MintNext", "leader_copy", 0.25)
        self.assertIsNotNone(another, reason)

    def test_startup_preserves_existing_kill_switch_reason(self):
        self.risk.activate_kill_switch("Drawdown ceiling reached")
        self.risk.engage_on_startup()
        self.assertTrue(self.risk.kill_switch_active())
        self.assertIn("Drawdown ceiling reached", self.risk.kill_switch_reason())
        self.assertIn("process restarted", self.risk.kill_switch_reason())

    def test_store_reservation_transaction_enforces_exposure_atomically(self):
        self.assertTrue(self.risk.resume_entries()[0])
        first = self.store.try_reserve_entry_atomic(
            reservation_id="first",
            mint="MintA",
            strategy="leader_copy",
            amount_usd=0.55,
            max_total_exposure_usd=1.00,
            max_open_positions=3,
            max_entries_per_day=4,
        )
        self.assertEqual(first, "")
        second = self.store.try_reserve_entry_atomic(
            reservation_id="second",
            mint="MintB",
            strategy="leader_copy",
            amount_usd=0.55,
            max_total_exposure_usd=1.00,
            max_open_positions=3,
            max_entries_per_day=4,
        )
        self.assertIn("exposure", second.lower())
        active = self.store.active_entry_reservations()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["reservation_id"], "first")


if __name__ == "__main__":
    unittest.main()
