"""Read-only per-mode counterfactual and paper-ledger evidence report.

This report never reads credentials or keypairs, imports no signing/executor module,
and opens SQLite with mode=ro. Outcomes are reference-price counterfactuals, not fills.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

MODES = ("leader_copy", "trending_discovery", "token_discovery")
HORIZONS = (60, 300, 900)
ROUND_TRIP_COST_SCENARIOS_PCT = (3.5, 5.0, 8.0)
DEFAULT_MAX_DELAY_SECONDS = 90.0


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def load_events_read_only(db_path: Path, limit: int = 500_000) -> tuple[list[dict], list[dict]]:
    """Read event_journal and trades without schema creation or DB mutation."""
    resolved = Path(db_path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Database not found: {resolved}")
    uri = resolved.as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        tables = {row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "event_journal" not in tables:
            return [], []
        events = []
        for row in connection.execute(
            "SELECT * FROM event_journal ORDER BY observed_at ASC,id ASC LIMIT ?",
            (max(1, min(500_000, int(limit))),),
        ).fetchall():
            item = dict(row)
            for column, name, default in (
                ("payload_json", "payload", {}),
                ("cost_inputs_json", "cost_inputs", {}),
                ("sources_json", "sources", []),
            ):
                try:
                    item[name] = json.loads(item.pop(column))
                except (KeyError, TypeError, ValueError):
                    item[name] = default
            events.append(item)
        trades = []
        if "trades" in tables:
            trades = [dict(row) for row in connection.execute(
                "SELECT ts,mode,action,mint,value_usd,pnl_usd,reason,source_wallet,"
                "source_signature,tx_signature,strategy FROM trades ORDER BY ts ASC,id ASC"
            ).fetchall()]
        return events, trades
    finally:
        connection.close()


def net_return_pct(gross_pct: float, round_trip_cost_pct: float) -> float:
    """Return-minus-cost sensitivity (cost percentage is applied to entry notional)."""
    gross = _finite(gross_pct)
    cost = _finite(round_trip_cost_pct)
    if gross is None or cost is None:
        raise ValueError("returns and cost must be finite")
    if cost < 0 or cost >= 100:
        raise ValueError("round-trip cost must be in [0,100)")
    return gross - cost


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lo, hi = int(math.floor(position)), int(math.ceil(position))
    return ordered[lo] if lo == hi else (
        ordered[lo] * (hi - position) + ordered[hi] * (position - lo)
    )


def bootstrap_ci(values: list[float], *, seed: int, resamples: int = 2000) -> dict:
    clean = [x for item in values if (x := _finite(item)) is not None]
    if len(clean) < 2:
        return {"lower_95": None, "upper_95": None, "resamples": 0,
                "method": "deterministic percentile bootstrap of sample mean"}
    rng = random.Random(int(seed))
    n = len(clean)
    means = [
        sum(clean[rng.randrange(n)] for _ in range(n)) / n
        for _ in range(max(500, min(10000, int(resamples))))
    ]
    return {
        "lower_95": _percentile(means, 0.025),
        "upper_95": _percentile(means, 0.975),
        "resamples": len(means),
        "method": "deterministic percentile bootstrap of sample mean",
    }


def summarize_returns(values: list[float], *, seed: int = 1) -> dict:
    vals = [x for item in values if (x := _finite(item)) is not None]
    wins, losses = [x for x in vals if x > 0], [x for x in vals if x < 0]
    win_sum, loss_sum = sum(wins), abs(sum(losses))
    running_sum, peak_sum, max_dd_sum = 0.0, 0.0, 0.0
    loss_streak = worst_streak = 0
    for value in vals:
        running_sum += value
        peak_sum = max(peak_sum, running_sum)
        max_dd_sum = max(max_dd_sum, peak_sum - running_sum)
        if value < 0:
            loss_streak += 1
            worst_streak = max(worst_streak, loss_streak)
        else:
            loss_streak = 0
    return {
        "samples": len(vals),
        "expectancy_mean_pct": statistics.fmean(vals) if vals else None,
        "median_pct": statistics.median(vals) if vals else None,
        "hit_rate_pct": 100.0 * len(wins) / len(vals) if vals else None,
        "avg_win_pct": statistics.fmean(wins) if wins else None,
        "avg_loss_pct": statistics.fmean(losses) if losses else None,
        "profit_factor": (win_sum / loss_sum) if loss_sum > 0 else None,
        "max_peak_to_trough_sum_of_trade_returns_pct": max_dd_sum if vals else None,
        "worst_consecutive_loss_streak": worst_streak,
        "percentiles_pct": {
            "p05": _percentile(vals, 0.05), "p25": _percentile(vals, 0.25),
            "p50": _percentile(vals, 0.50), "p75": _percentile(vals, 0.75),
            "p95": _percentile(vals, 0.95),
        },
        "expectancy_ci_95_pct": bootstrap_ci(vals, seed=seed),
    }


def _decision_text(event: dict) -> str:
    return str(event.get("decision") or event.get("status") or "").strip().lower()


def _is_rejected(event: dict) -> bool:
    value = _decision_text(event) + " " + str(event.get("status") or "").lower()
    return any(word in value for word in (
        "reject", "block", "skip", "filter", "fail", "stale", "unparsed", "duplicate"
    ))


def _decision_events(events: list[dict], mode: str) -> list[dict]:
    allowed = {"candidate_decision", "signal_decision", "entry_decision"}
    rows = [
        e for e in events
        if str(e.get("strategy") or "") == mode
        and str(e.get("event_type") or "") in allowed
        and _finite(e.get("observed_at")) is not None
    ]
    rows.sort(key=lambda e: float(e["observed_at"]))
    return rows


def _build_signal_cohort(events: list[dict], mode: str) -> list[dict]:
    """Choose one decision per leader signature or token mint without lookahead."""
    rows = _decision_events(events, mode)
    chosen: dict[str, dict] = {}
    if mode in {"trending_discovery", "token_discovery"}:
        # The earliest persisted decision is the counterfactual decision point. If its
        # price is missing, do not move the entry anchor forward to a later better mark.
        for item in rows:
            mint = str(item.get("mint") or "")
            if mint:
                chosen.setdefault(mint, item)
        return sorted(chosen.values(), key=lambda x: float(x["observed_at"]))

    # A leader event may first be seen as a parsed signal and subsequently receive
    # a terminal entry decision. Prefer that terminal entry decision for the cohort.
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in rows:
        signature = str(item.get("signature") or "")
        identity = signature or "|".join((
            str(item.get("wallet") or ""), str(item.get("mint") or ""),
            str(item.get("side") or ""), str(item.get("observed_at") or ""),
        ))
        groups[identity].append(item)
    for identity, candidates in groups.items():
        entry_events = [x for x in candidates if x.get("event_type") == "entry_decision"]
        signal_events = [x for x in candidates if x.get("event_type") == "signal_decision"]
        selected = entry_events[-1] if entry_events else (signal_events[0] if signal_events else candidates[0])
        chosen[identity] = selected
    return sorted(chosen.values(), key=lambda x: float(x["observed_at"]))


def _build_marks(events: list[dict]) -> dict[str, list[dict]]:
    marks: dict[str, list[dict]] = defaultdict(list)
    eligible_types = {"price_mark", "trade_tick", "leader_trade", "candidate_decision",
                      "signal_decision", "entry_decision"}
    for event in events:
        mint = str(event.get("mint") or "")
        timestamp, price = _finite(event.get("observed_at")), _finite(event.get("price_usd"))
        if not mint or timestamp is None or price is None or price <= 0:
            continue
        if str(event.get("event_type") or "") not in eligible_types:
            continue
        marks[mint].append({
            "observed_at": timestamp, "price_usd": price,
            "liquidity_usd": _finite(event.get("liquidity_usd")),
            "event_type": str(event.get("event_type") or ""),
            "signature": str(event.get("signature") or ""),
            "source": str(event.get("source") or ""),
        })
    for mint in marks:
        marks[mint].sort(key=lambda x: (x["observed_at"], x["event_type"] != "price_mark"))
    return marks


def evaluate_mode_events(
    events: list[dict], mode: str, *,
    round_trip_costs_pct: tuple[float, ...] = ROUND_TRIP_COST_SCENARIOS_PCT,
    max_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS,
) -> dict:
    mode_events = [e for e in events if e.get("strategy") == mode]
    cohort = _build_signal_cohort(events, mode)
    valid_signals, missing_reference = [], 0
    for event in cohort:
        price = _finite(event.get("price_usd"))
        if price is None or price <= 0:
            missing_reference += 1
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        costs = event.get("cost_inputs") if isinstance(event.get("cost_inputs"), dict) else {}
        valid_signals.append({
            "observed_at": float(event["observed_at"]),
            "event_type": str(event.get("event_type") or ""),
            "strategy": mode,
            "mint": str(event.get("mint") or ""),
            "wallet": str(event.get("wallet") or ""),
            "signature": str(event.get("signature") or ""),
            "side": str(event.get("side") or ""),
            "status": str(event.get("status") or ""),
            "decision": _decision_text(event),
            "reason": str(event.get("reason") or ""),
            "entry_price_usd": price,
            "liquidity_usd": _finite(event.get("liquidity_usd")),
            "latency_ms": _finite(event.get("latency_ms")),
            "cost_inputs": costs,
            "payload": payload,
            "rejected": _is_rejected(event),
        })
    marks = _build_marks(events)
    result: dict[str, Any] = {
        "mode": mode,
        "observed_events": len(mode_events),
        "decision_events": len(_decision_events(events, mode)),
        "signal_cohort": len(cohort),
        "rejected_decisions": sum(1 for e in _decision_events(events, mode) if _is_rejected(e)),
        "signals_with_reference_price": len(valid_signals),
        "signals_missing_reference_price": missing_reference,
        "rejection_reasons": {},
        "horizons": {},
        "observed_span_days": (
            max(0.0, (max(s["observed_at"] for s in valid_signals) -
                      min(s["observed_at"] for s in valid_signals)) / 86400.0)
            if valid_signals else 0.0
        ),
        "status": "INSUFFICIENT_EVIDENCE",
    }
    rejection_reasons = Counter(
        str(e.get("reason") or "unspecified")
        for e in _decision_events(events, mode) if _is_rejected(e)
    )
    result["rejection_reasons"] = dict(rejection_reasons.most_common(30))

    for horizon in HORIZONS:
        gross, outcomes, missing = [], [], 0
        for signal in valid_signals:
            target = signal["observed_at"] + horizon
            future = next((
                mark for mark in marks.get(signal["mint"], [])
                if mark["observed_at"] >= target
                and mark["observed_at"] <= target + max(0.0, max_delay_seconds)
            ), None)
            if future is None:
                missing += 1
                continue
            value = (future["price_usd"] / signal["entry_price_usd"] - 1.0) * 100.0
            if not math.isfinite(value):
                missing += 1
                continue
            gross.append(value)
            outcomes.append({
                "mint": signal["mint"], "strategy": mode,
                "signal_at": signal["observed_at"], "signal_price_usd": signal["entry_price_usd"],
                "status": signal["status"], "decision": signal["decision"],
                "rejected": signal["rejected"], "rejection_reason": signal["reason"],
                "horizon_seconds": horizon, "mark_at": future["observed_at"],
                "mark_price_usd": future["price_usd"], "gross_return_pct": value,
                "mark_latency_after_horizon_seconds": future["observed_at"] - target,
                "signal_latency_ms": signal["latency_ms"],
                "liquidity_usd_at_signal": signal["liquidity_usd"],
                "cost_inputs": signal["cost_inputs"],
            })
        summary = {
            "horizon_seconds": horizon, "signals": len(valid_signals),
            "evaluable_samples": len(gross), "missing_or_stale_future_mark": missing,
            "gross": summarize_returns(gross, seed=20261025 + horizon),
            "cost_scenarios": {}, "outcomes": outcomes,
        }
        for cost in round_trip_costs_pct:
            net = [net_return_pct(value, cost) for value in gross]
            summary["cost_scenarios"][f"round_trip_{cost:g}_pct"] = summarize_returns(
                net, seed=20261025 + horizon + int(cost * 100)
            )
        result["horizons"][f"{horizon // 60}m"] = summary
    return result


def _paper_ledger_by_mode(trades: list[dict]) -> dict:
    result = {}
    for mode in MODES:
        rows = [
            t for t in trades
            if str(t.get("strategy") or "leader_copy") == mode
            and str(t.get("mode") or "").lower() == "paper"
        ]
        sells = [t for t in rows if str(t.get("action") or "").upper() == "SELL"]
        result[mode] = {
            "trade_rows": len(rows), "sell_rows": len(sells),
            "realized_pnl_usd": sum(_finite(t.get("pnl_usd")) or 0.0 for t in sells),
            "pnl_is_paper_ledger_only": True,
        }
    return result


def build_report(
    db_path: Path, *,
    round_trip_costs_pct: tuple[float, ...] = ROUND_TRIP_COST_SCENARIOS_PCT,
    min_samples_for_conclusion: int = 300,
    min_coverage_days_for_conclusion: float = 14.0,
    max_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS,
) -> dict:
    events, trades = load_events_read_only(db_path)
    event_times = [_finite(e.get("observed_at")) for e in events]
    times = [x for x in event_times if x is not None]
    global_span_days = max(0.0, (max(times) - min(times)) / 86400.0) if times else 0.0
    mode_results = {}
    for mode_index, mode in enumerate(MODES):
        item = evaluate_mode_events(
            events, mode, round_trip_costs_pct=round_trip_costs_pct,
            max_delay_seconds=max_delay_seconds,
        )
        horizon = item["horizons"]["15m"]
        outcomes = horizon["outcomes"]
        split = int(len(outcomes) * 0.70)
        train, test = outcomes[:split], outcomes[split:]
        sample_target = (
            len(outcomes) >= max(2, int(min_samples_for_conclusion))
            or item["observed_span_days"] >= float(min_coverage_days_for_conclusion)
        )
        costs_complete = bool(outcomes) and all(
            isinstance(row.get("cost_inputs"), dict)
            and row["cost_inputs"].get("complete_for_after_cost_verdict") is True
            and _finite(row["cost_inputs"].get("round_trip_cost_pct_of_notional")) is not None
            for row in outcomes
        )
        latencies_complete = bool(outcomes) and all(
            row.get("signal_latency_ms") is not None for row in outcomes
        )
        real_liquidity_complete = bool(outcomes) and all(
            row.get("liquidity_usd_at_signal") is not None
            and row["liquidity_usd_at_signal"] > 0 for row in outcomes
        )
        net_rows = []
        if costs_complete:
            net_rows = [
                {**row, "net_return_pct": row["gross_return_pct"] -
                 float(row["cost_inputs"]["round_trip_cost_pct_of_notional"])}
                for row in outcomes
            ]
        net_test = [r["net_return_pct"] for r in net_rows[split:]] if net_rows else []
        net_test_summary = summarize_returns(net_test, seed=6126 + mode_index)
        sensitivity_test = {
            f"round_trip_{cost:g}_pct": summarize_returns(
                [net_return_pct(row["gross_return_pct"], cost) for row in test],
                seed=6200 + int(cost * 100) + mode_index,
            )
            for cost in round_trip_costs_pct
        }
        item["walk_forward"] = {
            "split": "chronological first 70% / final 30% holdout; no parameter fitting is performed",
            "train_samples": len(train), "untouched_test_samples": len(test),
            "train_gross": summarize_returns([r["gross_return_pct"] for r in train], seed=4101 + mode_index),
            "test_gross": summarize_returns([r["gross_return_pct"] for r in test], seed=4102 + mode_index),
            "test_cost_sensitivity": sensitivity_test,
            "test_measured_after_cost": net_test_summary if costs_complete else None,
        }
        item["cost_model_readiness"] = {
            "complete_measured_cost_inputs_for_all_15m_outcomes": costs_complete,
            "observed_latency_for_all_outcomes": latencies_complete,
            "verified_real_liquidity_for_all_outcomes": real_liquidity_complete,
            "measured_after_cost_expectancy_ci_verified": costs_complete and len(net_test) >= 2,
            "note": "TVL/price-mark sensitivity is not an executable quote. Measured fee, failure, latency and size-aware exit inputs are required for an after-cost verdict.",
        }
        item["conclusion_threshold"] = {
            "minimum_samples": int(min_samples_for_conclusion),
            "alternative_coverage_days": float(min_coverage_days_for_conclusion),
            "coverage_or_sample_target_met": sample_target,
            "untouched_test_samples": len(test),
            "cost_inputs_complete": costs_complete,
        }

        lower_ci = net_test_summary["expectancy_ci_95_pct"]["lower_95"] if costs_complete else None
        if not outcomes:
            item["status"], item["verdict"] = "NO_COUNTERFACTUAL_OUTCOMES", "KEEP IN PAPER — NO OUTCOME DATA"
        elif not sample_target or len(outcomes) < 2 or len(test) < 2:
            item["status"], item["verdict"] = "KEEP_IN_PAPER_INSUFFICIENT_EVIDENCE", "KEEP IN PAPER — INSUFFICIENT EVIDENCE"
        elif not costs_complete:
            item["status"], item["verdict"] = "KEEP_IN_PAPER_COST_MODEL_INCOMPLETE", "KEEP IN PAPER — COST DATA INCOMPLETE"
        elif lower_ci is None or lower_ci <= 0:
            item["status"], item["verdict"] = "DISABLE_NO_POSITIVE_AFTER_COST_CI", "DISABLE"
        else:
            item["status"], item["verdict"] = "ELIGIBLE_FOR_SEPARATE_REVIEW_NOT_AUTO_ENABLED", "ELIGIBLE FOR REVIEW — NOT LIVE AUTHORIZATION"

        item["15m_summary"] = {
            "signals": horizon["signals"],
            "evaluable_samples": horizon["evaluable_samples"],
            "missing_or_stale_future_mark": horizon["missing_or_stale_future_mark"],
            "gross_metrics": horizon["gross"],
            "net_sensitivity_3_5_pct": horizon["cost_scenarios"].get("round_trip_3.5_pct"),
            "net_sensitivity_5_pct": horizon["cost_scenarios"].get("round_trip_5_pct"),
            "net_sensitivity_8_pct": horizon["cost_scenarios"].get("round_trip_8_pct"),
        }
        item.pop("horizons", None)
        mode_results[mode] = item

    return {
        "report_version": 2,
        "database_path": str(Path(db_path).expanduser().resolve()),
        "read_only": True,
        "generated_at_utc_epoch": time.time(),
        "event_count": len(events),
        "event_span_days": global_span_days,
        "paper_ledger_by_mode": _paper_ledger_by_mode(trades),
        "mode_results": mode_results,
        "global_verdict": "NO MODE IS LIVE-AUTHORIZED BY THIS REPORT",
        "readiness_policy": {
            "minimum_samples_per_mode": int(min_samples_for_conclusion),
            "or_observation_span_days": float(min_coverage_days_for_conclusion),
            "live_eligibility_requires": [
                "positive lower 95% bootstrap CI for measured after-cost expectancy on untouched holdout",
                "complete measured venue/router fees, network/priority/tip and failed-transaction costs",
                "measured signal/fill latency and size-aware buy/sell output data",
                "all feed-health, risk and reconciliation gates passing",
                "separate explicit rollout review",
            ],
        },
        "limitations": [
            "Observed price marks are not guaranteed executable fills.",
            "Fixed 3.5%, 5% and 8% round-trip costs are sensitivity assumptions, not verified route costs.",
            "Candidates without reference/future marks are counted as missing instead of receiving invented returns.",
            "The 70/30 chronology split is a reporting holdout only; this report does not fit parameters.",
            "Bootstrap intervals can overstate certainty where tokens, creators or event windows are correlated.",
            "Live performance, real wallet debits and failed execution are not inferred from paper records.",
        ],
        "WHAT_COULD_I_BE_WRONG_ABOUT": [
            "Reference prices may not be executable at intended size and liquidity can disappear before exit.",
            "Missing marks, candidate selection and duplicate launches may bias apparent outcomes.",
            "The effective independent sample may be smaller than the number of token rows.",
            "Fixed sensitivity costs may understate fees, failed transactions, priority/tip costs, MEV and delayed exits.",
            "Two weeks of data with a small sample is not sufficient evidence of edge.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only per-mode Pump.fun strategy evidence report")
    parser.add_argument("--db", type=Path, required=True, help="Path to the SQLite database")
    parser.add_argument("--json-out", type=Path, help="Optional JSON report output path")
    parser.add_argument("--min-samples", type=int, default=300)
    parser.add_argument("--min-coverage-days", type=float, default=14.0)
    parser.add_argument("--max-delay-seconds", type=float, default=DEFAULT_MAX_DELAY_SECONDS)
    parser.add_argument("--round-trip-costs-pct", default="3.5,5,8")
    args = parser.parse_args()
    try:
        costs = tuple(float(x.strip()) for x in args.round_trip_costs_pct.split(",") if x.strip())
        if not costs or any(not math.isfinite(x) or x < 0 or x >= 100 for x in costs):
            raise ValueError("cost scenarios must be finite percentages in [0,100)")
        report = build_report(
            args.db, round_trip_costs_pct=costs,
            min_samples_for_conclusion=args.min_samples,
            min_coverage_days_for_conclusion=args.min_coverage_days,
            max_delay_seconds=args.max_delay_seconds,
        )
    except Exception as exc:
        parser.error(f"could not build report ({type(exc).__name__}): {exc}")
    output = json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
