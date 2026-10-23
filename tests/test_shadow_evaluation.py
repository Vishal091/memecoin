import tempfile
import unittest
from pathlib import Path

from shadow_evaluation import (
    build_report,
    evaluate_observations,
    load_database,
    net_return_pct,
)
from storage import Store


class ShadowEvaluationMathTests(unittest.TestCase):
    @staticmethod
    def _row(strategy, mint, timestamp, status, price, *, eligible=False, payload=None, score=90):
        return {
            "observed_at": float(timestamp),
            "strategy": strategy,
            "mint": mint,
            "status": status,
            "price_usd": price,
            "liquidity_usd": 20000.0,
            "volume_1h_usd": 15000.0,
            "score": score,
            "shadow_eligible": eligible,
            "market_source": "unit-test fixture",
            "payload": dict(payload or {}),
        }

    def test_net_return_applies_both_entry_and_exit_cost_assumptions(self):
        self.assertAlmostEqual(net_return_pct(10.0, 10.0, 0.0), 0.0)
        self.assertLess(net_return_pct(10.0, 10.0, 5.0), 0.0)
        self.assertGreater(net_return_pct(10.0, 12.0, 5.0), 0.0)
        with self.assertRaises(ValueError):
            net_return_pct(0.0, 10.0, 5.0)
        with self.assertRaises(ValueError):
            net_return_pct(10.0, 10.0, 60.0)

    def test_first_eligible_entry_is_measured_at_future_chronological_horizons(self):
        start = 1_800_000_000.0
        token_mint = "TokenMintForShadowEvaluation000000000000001"
        trend_mint = "TrendMintForShadowEvaluation00000000000001"
        rows = []

        token_payload = {
            "status": "shadow_open",
            "shadow_entry_at": start,
            "shadow_entry_price_usd": 10.0,
            "price_usd": 10.0,
            "shadow_mark_source": "verified curve fixture",
        }
        rows.append(self._row(
            "token_discovery", token_mint, start, "shadow_open", 10.0,
            eligible=True, payload=token_payload,
        ))
        for age, price in ((60, 12.0), (300, 9.0), (900, 11.0)):
            rows.append(self._row(
                "token_discovery", token_mint, start + age, "shadow_open" if age < 900 else "shadow_closed_15m",
                price, eligible=True,
                payload={**token_payload, "price_usd": price, "current_return_pct": (price / 10 - 1) * 100},
            ))

        trend_start = start + 5000
        for age, price in ((0, 5.0), (60, 4.5), (300, 5.5), (900, 5.25)):
            rows.append(self._row(
                "trending_discovery", trend_mint, trend_start + age, "SCREENED", price,
                eligible=True, payload={"price_usd": price, "shadow_eligible": True},
            ))

        report, csv_rows = evaluate_observations(
            rows, round_trip_costs_pct=(5.0,), min_coverage_days=0, min_15m_samples=1
        )
        token = report["strategy_results"]["token_discovery"]
        self.assertEqual(token["eligible_shadow_entries"], 1)
        self.assertEqual(token["horizons"]["1m"]["evaluable_samples"], 1)
        self.assertAlmostEqual(token["horizons"]["1m"]["mean_pct"], 20.0, places=3)
        self.assertAlmostEqual(token["horizons"]["5m"]["mean_pct"], -10.0, places=3)
        self.assertAlmostEqual(token["horizons"]["15m"]["mean_pct"], 10.0, places=3)
        self.assertIn("round_trip_5_pct", token["horizons"]["15m"]["cost_scenarios"])
        trend = report["strategy_results"]["trending_discovery"]
        self.assertEqual(trend["eligible_shadow_entries"], 1)
        self.assertAlmostEqual(trend["horizons"]["1m"]["mean_pct"], -10.0, places=3)
        self.assertAlmostEqual(trend["horizons"]["5m"]["mean_pct"], 10.0, places=3)
        self.assertEqual(len(csv_rows), 6)
        self.assertEqual(report["status"], "READY_FOR_STRATEGY_REVIEW")

    def test_entry_without_future_mark_is_not_reported_as_a_completed_horizon(self):
        start = 1_800_000_000.0
        row = self._row(
            "trending_discovery", "NoFutureMarkMint000000000000000000000001",
            start, "SCREENED", 1.0, eligible=True,
            payload={"price_usd": 1.0, "shadow_eligible": True},
        )
        report, samples = evaluate_observations(
            [row], round_trip_costs_pct=(3.5,), min_coverage_days=0, min_15m_samples=1
        )
        strategy = report["strategy_results"]["trending_discovery"]
        self.assertEqual(strategy["eligible_shadow_entries"], 1)
        self.assertEqual(strategy["horizons"]["1m"]["evaluable_samples"], 0)
        self.assertEqual(samples, [])
        self.assertEqual(strategy["status"], "INSUFFICIENT_SHADOW_EVIDENCE")

    def test_missing_valid_usd_mark_cannot_create_trending_entry(self):
        start = 1_800_000_000.0
        row = self._row(
            "trending_discovery", "QuoteSidePriceMustNotBeUsed000000000000001",
            start, "SCREENED", None, eligible=False,
            payload={"price_usd": None, "shadow_eligible": False},
        )
        report, _ = evaluate_observations([row], min_coverage_days=0, min_15m_samples=1)
        self.assertEqual(
            report["strategy_results"]["trending_discovery"]["eligible_shadow_entries"], 0
        )
        self.assertEqual(
            report["strategy_results"]["trending_discovery"]["status"],
            "NO_ELIGIBLE_SHADOW_ENTRIES",
        )

    def test_30_day_and_100_sample_readiness_are_not_inferred_from_small_fixture(self):
        start = 1_800_000_000.0
        payload = {"shadow_entry_at": start, "shadow_entry_price_usd": 1.0, "price_usd": 1.0}
        rows = [
            self._row("token_discovery", "ShortWindowMint000000000000000000000001",
                      start, "shadow_open", 1.0, eligible=True, payload=payload),
            self._row("token_discovery", "ShortWindowMint000000000000000000000001",
                      start + 900, "shadow_closed_15m", 1.1, eligible=True,
                      payload={**payload, "price_usd": 1.1}),
        ]
        report, _ = evaluate_observations(rows)
        self.assertEqual(report["status"], "INSUFFICIENT_SHADOW_EVIDENCE")
        result = report["strategy_results"]["token_discovery"]
        self.assertFalse(result["coverage_target_met"])
        self.assertFalse(result["fifteen_minute_sample_target_met"])


class ShadowObservationStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "phase5-paper.db"
        self.store = Store(self.db_path, 10.0, mode="paper")
        self.addCleanup(self.store.close)

    def test_observations_coalesce_per_minute_and_status(self):
        stamp = 1_800_000_000.0
        common = {
            "strategy": "trending_discovery",
            "mint": "CoalescedObservationMint00000000000001",
            "status": "SCREENED",
            "liquidity_usd": 20000,
            "volume_1h_usd": 15000,
            "score": 90,
            "shadow_eligible": True,
            "market_source": "DEX Screener",
        }
        self.store.record_shadow_observation(
            **common, observed_at=stamp, price_usd=2.0, payload={"price_usd": 2.0}
        )
        self.store.record_shadow_observation(
            **common, observed_at=stamp + 20, price_usd=2.2, payload={"price_usd": 2.2}
        )
        observations = self.store.list_shadow_observations(strategies=("trending_discovery",))
        self.assertEqual(len(observations), 1)
        self.assertAlmostEqual(observations[0]["price_usd"], 2.2)
        self.assertEqual(observations[0]["payload"]["price_usd"], 2.2)
        self.assertTrue(observations[0]["shadow_eligible"])

    def test_non_finite_mark_is_persisted_as_missing_not_as_infinity(self):
        self.store.record_shadow_observation(
            strategy="trending_discovery",
            mint="InvalidPriceMint00000000000000000000001",
            observed_at=1_800_000_000.0,
            status="SCREENED",
            price_usd=float("inf"),
            shadow_eligible=True,
            payload={"price_usd": float("inf")},
        )
        row = self.store.list_shadow_observations()[0]
        self.assertIsNone(row["price_usd"])

    def test_report_reads_database_without_mutating_paper_ledger(self):
        stamp = 1_800_000_000.0
        payload = {
            "status": "shadow_open",
            "shadow_entry_at": stamp,
            "shadow_entry_price_usd": 1.0,
            "price_usd": 1.0,
        }
        for age, status, price in (
            (0, "shadow_open", 1.0),
            (60, "shadow_open", 1.1),
            (300, "shadow_open", 1.05),
            (900, "shadow_closed_15m", 1.2),
        ):
            self.store.record_shadow_observation(
                strategy="token_discovery",
                mint="ReadOnlyReportMint000000000000000001",
                observed_at=stamp + age,
                status=status,
                price_usd=price,
                liquidity_usd=6000,
                score=85,
                shadow_eligible=True,
                payload={**payload, "price_usd": price},
            )
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO trades(ts,mode,action,mint,amount_raw,price_usd,value_usd,pnl_usd,reason,strategy) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("2026-10-23T00:00:00+00:00", "paper", "SELL", "fixture", "1", 1.0, 1.0, 0.25, "fixture sell", "leader_copy"),
            )
        count_before = self.store.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

        report, csv_rows, ledger = build_report(
            self.db_path, round_trip_costs_pct=(5.0,), min_coverage_days=0, min_15m_samples=1
        )
        self.assertEqual(report["observation_count"], 4)
        self.assertEqual(report["strategy_results"]["token_discovery"]["horizons"]["15m"]["evaluable_samples"], 1)
        self.assertEqual(ledger["paper_sell_rows"], 1)
        self.assertAlmostEqual(ledger["realized_sell_pnl_usd"], 0.25)
        self.assertEqual(len(csv_rows), 3)
        count_after = self.store.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        self.assertEqual(count_before, count_after)

    def test_no_shadow_data_is_reported_as_no_data_not_zero_profit(self):
        report, csv_rows, ledger = build_report(
            self.db_path, round_trip_costs_pct=(5.0,)
        )
        self.assertEqual(report["status"], "NO_SHADOW_DATA")
        self.assertIsNone(report["strategy_results"]["token_discovery"]["horizons"]["15m"]["mean_pct"])
        self.assertEqual(csv_rows, [])
        self.assertTrue(ledger["available"])


if __name__ == "__main__":
    unittest.main()
