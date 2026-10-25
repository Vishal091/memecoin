import unittest

from execution_costs import CostAssumptions, estimate_round_trip_cost


class ExecutionCostTests(unittest.TestCase):
    def test_complete_assumption_fixture_includes_two_sides_and_fixed_costs(self):
        assumptions = CostAssumptions(
            failed_tx_probability=0.0,
            latency_slippage_pct_per_side=0.0,
            stream_events_attributed_per_round_trip=0.0,
            pump_protocol_fee_pct_per_side=1.0,
            pump_creator_fee_pct_per_side=0.25,
            pumpswap_protocol_fee_pct_per_side=1.0,
            pumpswap_creator_fee_pct_per_side=0.25,
        )
        report = estimate_round_trip_cost(
            notional_usd=0.50,
            route="pumpportal_local",
            venue="pump",
            liquidity_usd=10_000.0,
            sol_usd=150.0,
            assumptions=assumptions,
        )
        self.assertEqual(report["pumpportal_fee_pct_per_side"], 0.5)
        self.assertEqual(report["venue_fee_pct_per_side"], 1.25)
        self.assertEqual(report["protocol_fee_pct_per_side"], 1.0)
        self.assertEqual(report["creator_fee_pct_per_side"], 0.25)
        self.assertAlmostEqual(report["estimated_slippage_pct_per_side"], 0.01, places=6)
        self.assertTrue(report["complete_for_after_cost_verdict"])
        self.assertAlmostEqual(report["round_trip_cost_usd"], 0.0221, places=6)
        self.assertEqual(report["status"], "ESTIMATE_ONLY")
        self.assertIn("not a quote", report["warning"].lower())

    def test_unknown_latency_failure_rate_and_stream_allocation_keep_cost_model_incomplete(self):
        report = estimate_round_trip_cost(
            notional_usd=0.5,
            route="native_pump_sdk",
            venue="pump",
            liquidity_usd=5000.0,
            sol_usd=150.0,
            assumptions=CostAssumptions(),
        )
        self.assertFalse(report["complete_for_after_cost_verdict"])
        self.assertIsNone(report["round_trip_cost_usd"])
        self.assertIn("pump_protocol_fee_schedule", report["missing_inputs"])
        self.assertIn("pump_creator_fee_schedule", report["missing_inputs"])
        self.assertIn("measured_latency_slippage", report["missing_inputs"])
        self.assertIn("measured_failed_tx_probability", report["missing_inputs"])
        self.assertIn("pumpportal_stream_cost_allocation", report["missing_inputs"])


if __name__ == "__main__":
    unittest.main()
