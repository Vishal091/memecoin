"""Read-only chronological evaluation of saved paper/shadow observations.

This module never imports an executor, signs a transaction, or modifies the trading
database. Outputs are retrospective estimates, not backtest guarantees or live-fill data.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DEFAULT_COST_SCENARIOS_PCT = (3.5, 5.0, 8.0)
HORIZONS = {
    "1m": {"seconds": 60, "max_delay_seconds": 120},
    "5m": {"seconds": 300, "max_delay_seconds": 180},
    "15m": {"seconds": 900, "max_delay_seconds": 300},
}
STRATEGIES = ("token_discovery", "trending_discovery")


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _utc_string(timestamp: float | None) -> str | None:
    if timestamp is None or not math.isfinite(timestamp) or timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * min(1.0, max(0.0, percentile))
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def net_return_pct(entry_price: float, exit_price: float, round_trip_cost_pct: float) -> float:
    """Apply a symmetric per-side cost assumption to an entry-to-exit price ratio."""
    if entry_price <= 0 or exit_price <= 0:
        raise ValueError("Entry and exit prices must be positive.")
    if not math.isfinite(round_trip_cost_pct) or not 0 <= round_trip_cost_pct <= 50:
        raise ValueError("Round-trip cost must be between 0 and 50 percent.")
    side_cost = round_trip_cost_pct / 200.0
    return ((exit_price / entry_price) * (1.0 - side_cost) / (1.0 + side_cost) - 1.0) * 100.0


def load_database(path: str | Path) -> tuple[list[dict], dict]:
    """Read observations and simulated paper ledger from a SQLite database in read-only mode."""
    db_path = Path(path).expanduser().resolve()
    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found: {db_path}")
    uri = db_path.as_uri() + "?mode=ro"
    observations: list[dict] = []
    ledger = {
        "available": False,
        "paper_trade_rows": 0,
        "paper_buy_rows": 0,
        "paper_sell_rows": 0,
        "realized_sell_pnl_usd": 0.0,
        "profitable_sell_rows": 0,
        "losing_sell_rows": 0,
        "pnl_observations": 0,
        "note": "Paper ledger metrics reflect the application's simulator accounting, not live executions.",
    }
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        tables = {
            str(row["name"])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "shadow_observations" in tables:
            rows = connection.execute(
                "SELECT observed_at, strategy, mint, status, price_usd, liquidity_usd, "
                "volume_1h_usd, score, shadow_eligible, market_source, payload_json "
                "FROM shadow_observations ORDER BY observed_at ASC, strategy ASC, mint ASC"
            ).fetchall()
            for row in rows:
                item = dict(row)
                try:
                    payload = json.loads(item.get("payload_json") or "{}")
                    if not isinstance(payload, dict):
                        payload = {}
                except (TypeError, ValueError):
                    payload = {}
                item["payload"] = payload
                item["shadow_eligible"] = bool(item.get("shadow_eligible"))
                item["observed_at"] = _number(item.get("observed_at"))
                observations.append(item)

        if "trades" in tables:
            rows = connection.execute(
                "SELECT action, pnl_usd FROM trades WHERE lower(mode)='paper' ORDER BY id ASC"
            ).fetchall()
            ledger["available"] = True
            for row in rows:
                action = str(row["action"] or "").strip().upper()
                pnl = _number(row["pnl_usd"])
                ledger["paper_trade_rows"] += 1
                if action == "BUY":
                    ledger["paper_buy_rows"] += 1
                if action.startswith("SELL"):
                    ledger["paper_sell_rows"] += 1
                    if pnl is not None:
                        ledger["pnl_observations"] += 1
                        ledger["realized_sell_pnl_usd"] += pnl
                        if pnl > 0:
                            ledger["profitable_sell_rows"] += 1
                        elif pnl < 0:
                            ledger["losing_sell_rows"] += 1

    ledger["realized_sell_pnl_usd"] = round(ledger["realized_sell_pnl_usd"], 6)
    return observations, ledger


def _price_for_observation(row: dict) -> float | None:
    payload = row.get("payload") or {}
    # Prefer the persisted top-level normalized mark, then source payload fields.
    for value in (
        payload.get("price_usd"),
        row.get("price_usd"),
    ):
        result = _positive(value)
        if result is not None:
            return result
    return None


def _entry_from_group(strategy: str, group: list[dict]) -> dict | None:
    candidates = []
    for row in group:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        observed_at = _number(row.get("observed_at"))
        if observed_at is None:
            continue
        if strategy == "token_discovery":
            entry_at = _number(payload.get("shadow_entry_at"))
            entry_price = _positive(payload.get("shadow_entry_price_usd"))
            eligible = entry_at is not None and entry_at > 0 and entry_price is not None
        elif strategy == "trending_discovery":
            entry_at = observed_at
            entry_price = _price_for_observation(row)
            eligible = bool(row.get("shadow_eligible") or payload.get("shadow_eligible")) and entry_price is not None
        else:
            continue
        if eligible:
            candidates.append({
                "strategy": strategy,
                "mint": str(row.get("mint") or ""),
                "entry_at": float(entry_at),
                "entry_price": float(entry_price),
                "entry_status": str(row.get("status") or ""),
                "entry_score": _number(row.get("score")),
                "entry_liquidity_usd": _number(row.get("liquidity_usd")),
                "entry_market_source": str(row.get("market_source") or ""),
            })
    if not candidates:
        return None
    candidates.sort(key=lambda item: item["entry_at"])
    return candidates[0]


def _summarize_returns(values: list[float]) -> dict:
    if not values:
        return {
            "count": 0,
            "mean_pct": None,
            "median_pct": None,
            "win_rate_pct": None,
            "p10_pct": None,
            "p90_pct": None,
        }
    return {
        "count": len(values),
        "mean_pct": round(statistics.fmean(values), 4),
        "median_pct": round(statistics.median(values), 4),
        "win_rate_pct": round(100.0 * sum(value > 0 for value in values) / len(values), 2),
        "p10_pct": round(_percentile(values, 0.10), 4),
        "p90_pct": round(_percentile(values, 0.90), 4),
    }


def evaluate_observations(
    observations: Iterable[dict],
    *,
    round_trip_costs_pct: Iterable[float] = DEFAULT_COST_SCENARIOS_PCT,
    min_coverage_days: float = 30.0,
    min_15m_samples: int = 100,
) -> tuple[dict, list[dict]]:
    costs = sorted({float(cost) for cost in round_trip_costs_pct})
    if not costs or any(not math.isfinite(cost) or cost < 0 or cost > 50 for cost in costs):
        raise ValueError("Provide one or more round-trip cost scenarios between 0 and 50 percent.")
    if min_coverage_days < 0 or min_15m_samples < 1:
        raise ValueError("Coverage target must be non-negative and sample target must be positive.")

    rows = []
    for item in observations:
        row = dict(item)
        row["observed_at"] = _number(row.get("observed_at"))
        row["payload"] = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if row["observed_at"] is not None:
            rows.append(row)
    rows.sort(key=lambda row: row["observed_at"])
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    by_strategy_rows: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        strategy = str(row.get("strategy") or "")
        mint = str(row.get("mint") or "")
        if strategy not in STRATEGIES or not mint:
            continue
        grouped[(strategy, mint)].append(row)
        by_strategy_rows[strategy].append(row)

    report_candidates = {}
    csv_rows: list[dict] = []
    overall_start = min((row["observed_at"] for row in rows), default=None)
    overall_end = max((row["observed_at"] for row in rows), default=None)
    coverage_days = max(0.0, (overall_end - overall_start) / 86400.0) if overall_start and overall_end else 0.0

    for strategy in STRATEGIES:
        strategy_keys = [key for key in grouped if key[0] == strategy]
        trajectories = {
            key: sorted(grouped[key], key=lambda row: row["observed_at"])
            for key in strategy_keys
        }
        entries = []
        for key, trajectory in trajectories.items():
            entry = _entry_from_group(strategy, trajectory)
            if entry is not None:
                entry["trajectory"] = trajectory
                entries.append(entry)
        entries.sort(key=lambda entry: (entry["entry_at"], entry["mint"]))

        first_strategy_ts = min((row["observed_at"] for row in by_strategy_rows.get(strategy, [])), default=None)
        last_strategy_ts = max((row["observed_at"] for row in by_strategy_rows.get(strategy, [])), default=None)
        strategy_coverage = (
            max(0.0, (last_strategy_ts - first_strategy_ts) / 86400.0)
            if first_strategy_ts is not None and last_strategy_ts is not None else 0.0
        )
        horizon_result = {}
        for horizon_name, horizon in HORIZONS.items():
            samples = []
            for entry in entries:
                target = entry["entry_at"] + horizon["seconds"]
                deadline = target + horizon["max_delay_seconds"]
                future = []
                for observation in entry["trajectory"]:
                    ts = observation["observed_at"]
                    if ts is None or ts < target or ts > deadline:
                        continue
                    price = _price_for_observation(observation)
                    if price is not None:
                        future.append((ts, price, observation))
                if not future:
                    continue
                exit_at, exit_price, exit_row = min(future, key=lambda value: value[0])
                gross_return = (exit_price / entry["entry_price"] - 1.0) * 100.0
                observed_returns = []
                for observation in entry["trajectory"]:
                    ts = observation["observed_at"]
                    if ts is None or not entry["entry_at"] < ts <= exit_at:
                        continue
                    price = _price_for_observation(observation)
                    if price is not None:
                        observed_returns.append((price / entry["entry_price"] - 1.0) * 100.0)
                adverse = min([0.0] + observed_returns)
                favorable = max([0.0] + observed_returns)
                result = {
                    "strategy": strategy,
                    "mint": entry["mint"],
                    "entry_at": entry["entry_at"],
                    "entry_at_utc": _utc_string(entry["entry_at"]),
                    "entry_price_usd": entry["entry_price"],
                    "entry_status": entry["entry_status"],
                    "entry_score": entry["entry_score"],
                    "entry_liquidity_usd": entry["entry_liquidity_usd"],
                    "entry_market_source": entry["entry_market_source"],
                    "horizon": horizon_name,
                    "target_seconds": horizon["seconds"],
                    "actual_age_seconds": round(exit_at - entry["entry_at"], 2),
                    "exit_at_utc": _utc_string(exit_at),
                    "exit_price_usd": exit_price,
                    "gross_return_pct": round(gross_return, 4),
                    "adverse_excursion_pct": round(adverse, 4),
                    "favorable_excursion_pct": round(favorable, 4),
                    "exit_market_source": str(exit_row.get("market_source") or ""),
                }
                for cost in costs:
                    label = f"net_return_cost_{cost:g}_pct"
                    result[label] = round(net_return_pct(entry["entry_price"], exit_price, cost), 4)
                samples.append(result)
                csv_rows.append(result)

            gross_values = [row["gross_return_pct"] for row in samples]
            horizon_summary = {
                **_summarize_returns(gross_values),
                "evaluable_samples": len(samples),
                "entry_candidates": len(entries),
                "coverage_pct": round(100.0 * len(samples) / len(entries), 2) if entries else 0.0,
                "mean_adverse_excursion_pct": round(
                    statistics.fmean([row["adverse_excursion_pct"] for row in samples]), 4
                ) if samples else None,
                "cost_scenarios": {},
            }
            for cost in costs:
                net_values = [row[f"net_return_cost_{cost:g}_pct"] for row in samples]
                horizon_summary["cost_scenarios"][f"round_trip_{cost:g}_pct"] = _summarize_returns(net_values)
            horizon_result[horizon_name] = horizon_summary

        fifteen_minute_samples = horizon_result["15m"]["evaluable_samples"]
        has_coverage = strategy_coverage >= min_coverage_days
        has_sample = fifteen_minute_samples >= min_15m_samples
        if not entries:
            readiness = "NO_ELIGIBLE_SHADOW_ENTRIES"
        elif has_coverage and has_sample:
            readiness = "READY_FOR_STRATEGY_REVIEW"
        else:
            readiness = "INSUFFICIENT_SHADOW_EVIDENCE"
        priced_screened = sum(
            1 for row in by_strategy_rows.get(strategy, [])
            if str(row.get("status") or "").upper() == "SCREENED"
        )
        missing_price_screened = sum(
            1 for row in by_strategy_rows.get(strategy, [])
            if str(row.get("status") or "").upper() == "SCREENED"
            and _price_for_observation(row) is None
        )
        report_candidates[strategy] = {
            "status": readiness,
            "observations": len(by_strategy_rows.get(strategy, [])),
            "unique_mints_observed": len(strategy_keys),
            "eligible_shadow_entries": len(entries),
            "coverage_days": round(strategy_coverage, 4),
            "coverage_target_days": min_coverage_days,
            "coverage_target_met": has_coverage,
            "fifteen_minute_sample_target": min_15m_samples,
            "fifteen_minute_sample_target_met": has_sample,
            "screened_observations": priced_screened,
            "screened_observations_missing_valid_usd_mark": missing_price_screened,
            "horizons": horizon_result,
        }

    overall_ready = bool(report_candidates) and all(
        report_candidates[strategy]["status"] == "READY_FOR_STRATEGY_REVIEW"
        for strategy in STRATEGIES
    )
    report = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "read_only_evaluation": True,
        "status": (
            "READY_FOR_STRATEGY_REVIEW"
            if overall_ready
            else ("NO_SHADOW_DATA" if not rows else "INSUFFICIENT_SHADOW_EVIDENCE")
        ),
        "observation_count": len(rows),
        "first_observation_utc": _utc_string(overall_start),
        "last_observation_utc": _utc_string(overall_end),
        "overall_observation_span_days": round(coverage_days, 4),
        "required_coverage_days": min_coverage_days,
        "required_evaluable_15m_samples_per_strategy": min_15m_samples,
        "round_trip_cost_scenarios_pct": costs,
        "cost_model_note": (
            "Each scenario applies half its round-trip percentage as a symmetric entry-side and exit-side "
            "cost. This is a sensitivity analysis, not a route-specific fee quote, price-impact forecast, "
            "or simulation of executable fills. Replace scenarios with measured route/provider cost estimates."
        ),
        "strategy_results": report_candidates,
        "next_step": (
            "Review the strategy, data quality, and failure modes; this status is not live-trading authorization."
            if overall_ready
            else "Continue paper/shadow collection. Do not infer edge or authorize live execution from an incomplete sample."
        ),
    }
    return report, csv_rows


def build_report(
    db_path: str | Path,
    *,
    round_trip_costs_pct: Iterable[float] = DEFAULT_COST_SCENARIOS_PCT,
    min_coverage_days: float = 30.0,
    min_15m_samples: int = 100,
) -> tuple[dict, list[dict], dict]:
    observations, ledger = load_database(db_path)
    report, csv_rows = evaluate_observations(
        observations,
        round_trip_costs_pct=round_trip_costs_pct,
        min_coverage_days=min_coverage_days,
        min_15m_samples=min_15m_samples,
    )
    report["database_path"] = str(Path(db_path).expanduser().resolve())
    report["paper_ledger"] = ledger
    return report, csv_rows, ledger


def _parse_costs(value: str) -> list[float]:
    try:
        costs = [float(part.strip()) for part in value.split(",") if part.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Costs must be a comma-separated list of numbers.") from exc
    if not costs or any(not math.isfinite(cost) or not 0 <= cost <= 50 for cost in costs):
        raise argparse.ArgumentTypeError("Provide one or more round-trip costs between 0 and 50 percent.")
    return costs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only paper/shadow evaluation from a local bot SQLite database."
    )
    parser.add_argument("--db", default="memecoin-pumpfun.db", help="SQLite database path (read-only)")
    parser.add_argument("--round-trip-costs", type=_parse_costs, default=list(DEFAULT_COST_SCENARIOS_PCT),
                        help="Comma-separated scenario costs in percent (default: 3.5,5,8)")
    parser.add_argument("--min-coverage-days", type=float, default=30.0,
                        help="Minimum observed span required for review readiness (default: 30)")
    parser.add_argument("--min-15m-samples", type=int, default=100,
                        help="Minimum evaluable 15-minute marks per strategy (default: 100)")
    parser.add_argument("--json-out", help="Optional JSON report output path")
    parser.add_argument("--csv-out", help="Optional CSV completed-horizon observations output path")
    args = parser.parse_args(argv)
    try:
        report, csv_rows, _ = build_report(
            args.db,
            round_trip_costs_pct=args.round_trip_costs,
            min_coverage_days=args.min_coverage_days,
            min_15m_samples=args.min_15m_samples,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"Shadow evaluation failed: {exc}", file=sys.stderr)
        return 2

    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.json_out:
        out = Path(args.json_out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered + "\n", encoding="utf-8")
    if args.csv_out:
        out = Path(args.csv_out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        fields = [
            "strategy", "mint", "entry_at_utc", "entry_price_usd", "entry_status",
            "entry_score", "entry_liquidity_usd", "entry_market_source", "horizon",
            "target_seconds", "actual_age_seconds", "exit_at_utc", "exit_price_usd",
            "gross_return_pct", "adverse_excursion_pct", "favorable_excursion_pct",
            "exit_market_source",
        ] + [f"net_return_cost_{cost:g}_pct" for cost in args.round_trip_costs]
        with out.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(csv_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
