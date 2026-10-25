import tempfile
import unittest
from pathlib import Path

from mode_evaluation import (
    auto_disable_unprofitable_modes,
    bootstrap_mean_ci,
    build_mode_report,
)
from storage import Store


class ModeEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "mode-paper.db"
        self.store = Store(self.db_path, 12.0, mode="paper")
        self.addCleanup(self.store.close)

    def _add_token_shadow_outcome(self, mint, start, entry, exit_price):
        base_payload = {
            "mint": mint, "status": "shadow_open",
            "shadow_entry_at": start, "shadow_entry_price_usd": entry,
            "price_usd": entry, "shadow_eligible": True,
            "shadow_mark_source": "unit-test",
        }
        self.store.record_shadow_observation(
            strategy="token_discovery", mint=mint, observed_at=start,
            status="shadow_open", price_usd=entry, liquidity_usd=6000,
            score=90, shadow_eligible=True, market_source="unit-test",
            payload=base_payload,
        )
        self.store.record_shadow_observation(
            strategy="token_discovery", mint=mint, observed_at=start + 900,
            status="shadow_closed_15m", price_usd=exit_price, liquidity_usd=6000,
            score=90, shadow_eligible=True, market_source="unit-test",
            payload={**base_payload, "status": "shadow_closed_15m", "price_usd": exit_price},
        )

    def test_bootstrap_is_deterministic_for_a_fixed_seed(self):
        values = [-3.0, -1.0, 0.5, 1.0, 2.0]
        one = bootstrap_mean_ci(values, seed=17, iterations=1000)
        two = bootstrap_mean_ci(values, seed=17, iterations=1000)
        self.assertEqual(one, two)
        self.assertLessEqual(one["lower_95"], one["upper_95"])

    def test_empty_database_reports_insufficient_evidence_and_keeps_modes_in_paper(self):
        report = build_mode_report(
            self.db_path, mode="paper", min_counterfactual_trades=1,
            min_active_days=1, min_samples_for_day_gate=1,
        )
        self.assertEqual(report["status"], "INSUFFICIENT_EVIDENCE")
        for strategy in ("leader_copy", "trending_discovery", "token_discovery"):
            self.assertEqual(report["strategies"][strategy]["verdict"], "KEEP_PAPER")
            self.assertIsNone(report["strategies"][strategy]["counterfactual_15m_outcomes"]["expectancy_pct"])

    def test_mature_negative_counterfactual_strategy_is_auto_disabled(self):
        self._add_token_shadow_outcome(
            "TokenWithNegative15mReturn000000000001",
            1_800_000_000.0, 1.0, 0.80,
        )
        report = build_mode_report(
            self.db_path, mode="paper", round_trip_cost_pct=1.0,
            min_counterfactual_trades=1, min_active_days=100, min_samples_for_day_gate=100,
            bootstrap_iterations=1000,
        )
        token = report["strategies"]["token_discovery"]
        self.assertEqual(token["verdict"], "AUTO_DISABLE")
        self.assertLess(token["counterfactual_15m_outcomes"]["expectancy_ci_95_pct"]["lower_95"], 0)

        result = auto_disable_unprofitable_modes(
            self.store, mode="paper", round_trip_cost_pct=1.0,
            min_counterfactual_trades=1, min_active_days=100,
            min_samples_for_day_gate=100,
        )
        self.assertIn("token_discovery", result["disabled"])
        self.assertEqual(self.store.get_state("mode_policy_disabled:paper:token_discovery"), "1")
        self.assertEqual(self.store.get_state("mode_policy_disabled:paper:leader_copy", "0"), "0")
        self.assertEqual(self.store.get_state("LIVE_ENTRIES_APPROVED", "false"), "false")

    def test_positive_mature_interval_is_review_only_and_never_auto_enables(self):
        for i in range(5):
            self._add_token_shadow_outcome(
                f"PositiveToken{i:02d}000000000000000000000001",
                1_800_000_000.0 + i * 1800, 1.0, 1.3,
            )
        report = build_mode_report(
            self.db_path, mode="paper", round_trip_cost_pct=1.0,
            min_counterfactual_trades=5, min_active_days=100, min_samples_for_day_gate=100,
            bootstrap_iterations=1000,
        )
        token = report["strategies"]["token_discovery"]
        self.assertEqual(token["verdict"], "CANDIDATE_FOR_HUMAN_REVIEW_NOT_LIVE_ENABLEMENT")
        self.assertEqual(self.store.get_state("mode_policy_disabled:paper:token_discovery", "0"), "0")
        self.assertEqual(self.store.get_state("LIVE_ENTRIES_APPROVED", "false"), "false")


if __name__ == "__main__":
    unittest.main()
