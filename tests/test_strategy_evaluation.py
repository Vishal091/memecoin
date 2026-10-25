import tempfile
import unittest
from pathlib import Path

from storage import Store
from strategy_evaluation import build_report, evaluate_mode_events, load_events_read_only, net_return_pct


class StrategyEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "evaluation.db"
        self.store = Store(self.db_path, 10.0, mode="paper")

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def _candidate(self, *, mint, timestamp, price, status, decision, reason=""):
        self.store.record_event(
            event_type="candidate_decision", strategy="trending_discovery",
            observed_at=timestamp, mint=mint, side="screen",
            source="fixture", status=status, decision=decision,
            reason=reason, price_usd=price, liquidity_usd=25000.0,
            cost_inputs={"measurement_basis": "estimate_only", "complete_for_after_cost_verdict": False},
        )

    def _mark(self, *, mint, timestamp, price):
        self.store.record_event(
            event_type="price_mark", strategy="market_data", observed_at=timestamp,
            mint=mint, source="fixture", status="observed", decision="price_mark",
            price_usd=price, liquidity_usd=25000.0,
        )

    def test_rejected_candidate_counterfactual_and_future_horizons_are_reported(self):
        start = 1_800_000_000.0
        self._candidate(mint="mint-good", timestamp=start, price=1.0,
                        status="SCREENED", decision="shadow_eligible")
        self._candidate(mint="mint-rejected", timestamp=start + 1, price=2.0,
                        status="REJECTED", decision="rejected", reason="holder concentration")
        for mint, initial, multipliers in (
            ("mint-good", 1.0, (1.10, 1.20, 0.95)),
            ("mint-rejected", 2.0, (0.50, 0.40, 0.25)),
        ):
            signal_at = start if mint == "mint-good" else start + 1
            for horizon, multiple in zip((60, 300, 900), multipliers):
                self._mark(mint=mint, timestamp=signal_at + horizon, price=initial * multiple)
        events, _ = load_events_read_only(self.db_path)
        result = evaluate_mode_events(events, "trending_discovery")
        self.assertEqual(result["horizons"]["1m"]["evaluable_samples"], 2)
        self.assertEqual(result["horizons"]["5m"]["evaluable_samples"], 2)
        self.assertEqual(result["horizons"]["15m"]["evaluable_samples"], 2)
        outcomes = result["horizons"]["1m"]["outcomes"]
        rejected = next(row for row in outcomes if row["mint"] == "mint-rejected")
        self.assertTrue(rejected["rejected"])
        self.assertAlmostEqual(rejected["gross_return_pct"], -50.0)
        self.assertIn("holder concentration", rejected["rejection_reason"])

    def test_missing_first_reference_price_does_not_shift_entry_to_a_later_mark(self):
        start = 1_800_000_000.0
        self._candidate(mint="mint-missing", timestamp=start, price=None,
                        status="REJECTED", decision="rejected", reason="price unavailable")
        self._candidate(mint="mint-missing", timestamp=start + 30, price=10.0,
                        status="SCREENED", decision="shadow_eligible")
        self._mark(mint="mint-missing", timestamp=start + 60, price=12.0)
        events, _ = load_events_read_only(self.db_path)
        result = evaluate_mode_events(events, "trending_discovery")
        self.assertEqual(result["signal_cohort"], 1)
        self.assertEqual(result["signals_missing_reference_price"], 1)
        self.assertEqual(result["horizons"]["1m"]["evaluable_samples"], 0)

    def test_report_is_read_only_and_keeps_all_modes_in_paper_without_complete_cost_data(self):
        start = 1_800_000_000.0
        self._candidate(mint="mint-a", timestamp=start, price=1.0,
                        status="SCREENED", decision="shadow_eligible")
        for horizon in (60, 300, 900):
            self._mark(mint="mint-a", timestamp=start + horizon, price=1.1)
        before = self.store.event_summary()["total_events"]
        report = build_report(self.db_path, min_samples_for_conclusion=1, min_coverage_days_for_conclusion=0)
        after = self.store.event_summary()["total_events"]
        self.assertEqual(before, after)
        self.assertTrue(report["read_only"])
        self.assertEqual(report["global_verdict"], "NO MODE IS LIVE-AUTHORIZED BY THIS REPORT")
        self.assertIn(report["mode_results"]["trending_discovery"]["verdict"],
                      {"KEEP IN PAPER — INSUFFICIENT EVIDENCE", "KEEP IN PAPER — COST DATA INCOMPLETE"})
        self.assertEqual(report["mode_results"]["leader_copy"]["verdict"], "KEEP IN PAPER — NO OUTCOME DATA")

    def test_signal_decision_latency_is_distinct_from_provider_event_age(self):
        start = 1_800_000_000.0
        self.store.record_event(
            event_type="signal_decision", strategy="leader_copy",
            observed_at=start, mint="leader-mint", wallet="leader",
            side="buy", signature="leader-signature", source="fixture",
            status="observed", decision="accepted_for_processing",
            price_usd=1.0, payload={"provider_event_age_ms": 300.0},
        )
        self.store.record_event(
            event_type="entry_decision", strategy="leader_copy",
            observed_at=start + 2, mint="leader-mint", wallet="leader",
            side="buy", signature="leader-signature", source="fixture",
            status="rejected", decision="rejected", reason="fixture risk gate",
            price_usd=1.0, latency_ms=250.0,
            payload={
                "signal_to_decision_latency_ms": 250.0,
                "provider_event_age_ms": 300.0,
            },
        )
        self._mark(mint="leader-mint", timestamp=start + 902, price=1.05)
        events, _ = load_events_read_only(self.db_path)
        result = evaluate_mode_events(events, "leader_copy")
        outcome = result["horizons"]["15m"]["outcomes"][0]
        self.assertEqual(outcome["signal_to_decision_latency_ms"], 250.0)
        self.assertEqual(outcome["provider_event_age_ms"], 300.0)
        self.assertNotIn("signal_latency_ms", outcome)


    def test_round_trip_cost_sensitivity_is_explicit(self):
        self.assertAlmostEqual(net_return_pct(10.0, 3.5), 6.5)


if __name__ == "__main__":
    unittest.main()
