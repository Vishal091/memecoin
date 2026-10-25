"""Read-only mode evaluation and fail-closed auto-disable policy.

Performance is evaluated per strategy from chronological counterfactual returns
with explicit cost assumptions. The realized ledger is reported separately.
This module never submits/signs transactions and never auto-enables a strategy.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shadow_evaluation import evaluate_observations, load_database, net_return_pct
from research_journal import STRATEGIES

DEFAULT_ROUND_TRIP_COST_PCT = 5.0
DEFAULT_MIN_COUNTERFACTUAL_TRADES = 300
DEFAULT_MIN_ACTIVE_DAYS = 14.0
MIN_SAMPLES_FOR_DAY_GATE = 30

def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None

def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * p
    lo, hi = math.floor(position), math.ceil(position)
    return ordered[lo] if lo == hi else ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)

def bootstrap_mean_ci(values: list[float], *, seed: int = 20261025,
                      iterations: int = 5000) -> dict:
    clean = [float(v) for v in values if math.isfinite(float(v))]
    if not clean:
        return {"lower_95": None, "upper_95": None, "iterations": 0, "method": "percentile bootstrap"}
    if len(clean) == 1:
        return {"lower_95": clean[0], "upper_95": clean[0], "iterations": 1,
                "method": "single-observation degenerate interval; not inferential"}
    rng = random.Random(seed)
    n = len(clean)
    means = [
        statistics.fmean(clean[rng.randrange(n)] for _ in range(n))
        for _ in range(max(500, min(20000, int(iterations))))
    ]
    return {
        "lower_95": round(float(_percentile(means, 0.025)), 6),
        "upper_95": round(float(_percentile(means, 0.975)), 6),
        "iterations": len(means),
        "method": "deterministic percentile bootstrap over observed counterfactual outcomes",
    }

def _return_metrics(values: list[float], *, seed: int, iterations: int) -> dict:
    vals = [float(v) for v in values if math.isfinite(float(v))]
    wins = [v for v in vals if v > 0]
    losses = [v for v in vals if v < 0]
    pf_denom = abs(sum(losses))
    equity = peak = 1.0
    max_dd = 0.0
    worst_streak = streak = 0
    for value in vals:
        equity *= max(0.0, 1.0 + value / 100.0)
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100.0)
        if value < 0:
            streak += 1
            worst_streak = max(worst_streak, streak)
        else:
            streak = 0
    ci = bootstrap_mean_ci(vals, seed=seed, iterations=iterations)
    return {
        "sample_count": len(vals),
        "expectancy_pct": round(statistics.fmean(vals), 6) if vals else None,
        "median_pct": round(statistics.median(vals), 6) if vals else None,
        "hit_rate_pct": round(100.0 * len(wins) / len(vals), 3) if vals else None,
        "avg_win_pct": round(statistics.fmean(wins), 6) if wins else None,
        "avg_loss_pct": round(statistics.fmean(losses), 6) if losses else None,
        "profit_factor": round(sum(wins) / pf_denom, 6) if pf_denom > 0 else (None if not wins else "infinite_no_losses"),
        "max_drawdown_pct": round(max_dd, 6) if vals else None,
        "worst_loss_streak": worst_streak,
        "return_p10_pct": round(float(_percentile(vals, 0.10)), 6) if vals else None,
        "return_p90_pct": round(float(_percentile(vals, 0.90)), 6) if vals else None,
        "expectancy_ci_95_pct": ci,
    }

def _leader_shadow_returns(connection, *, mode: str, cost_pct: float) -> tuple[list[tuple[float, float]], tuple[float | None, float | None]]:
    tables = {str(r[0]) for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"research_signals", "research_marks"}.issubset(tables):
        return [], (None, None)
    signals = connection.execute(
        """SELECT signal_id, observed_at, mint, price_usd, source_signature
           FROM research_signals
           WHERE mode=? AND strategy='leader_copy' AND decision='would_copy'
           ORDER BY observed_at ASC""", (mode,),
    ).fetchall()
    outcomes: list[tuple[float, float]] = []
    observed_times = []
    for signal in signals:
        entry_at = _finite(signal["observed_at"])
        entry_price = _finite(signal["price_usd"])
        if entry_at is None:
            continue
        observed_times.append(entry_at)
        if entry_price is None or entry_price <= 0:
            continue
        exit_row = connection.execute(
            """SELECT observed_at, price_usd FROM research_marks
               WHERE signal_id=? AND observed_at>=? AND observed_at<=?
               AND price_usd>0 ORDER BY observed_at ASC LIMIT 1""",
            (str(signal["signal_id"]), entry_at + 900, entry_at + 1200),
        ).fetchone()
        if not exit_row:
            continue
        exit_price = _finite(exit_row["price_usd"])
        exit_at = _finite(exit_row["observed_at"])
        if exit_price is None or exit_price <= 0 or exit_at is None:
            continue
        try:
            ret = net_return_pct(entry_price, exit_price, cost_pct)
        except ValueError:
            continue
        outcomes.append((exit_at, ret))
    span = (min(observed_times), max(observed_times)) if len(observed_times) > 1 else (
        (observed_times[0], observed_times[0]) if observed_times else (None, None)
    )
    return outcomes, span

def _shadow_strategy_returns(db_path: Path, strategy: str, cost_pct: float) -> tuple[list[tuple[float, float]], tuple[float | None, float | None]]:
    if strategy not in {"token_discovery", "trending_discovery"}:
        return [], (None, None)
    observations, _ledger = load_database(db_path)
    report, rows = evaluate_observations(
        observations, round_trip_costs_pct=(cost_pct,), min_coverage_days=0, min_15m_samples=1,
    )
    results = []
    for row in rows:
        if row.get("strategy") == strategy and row.get("horizon") == "15m":
            ret = _finite(row.get(f"net_return_cost_{cost_pct:g}_pct"))
            stamp = _finite(row.get("exit_at_utc") and datetime.fromisoformat(
                str(row["exit_at_utc"]).replace("Z", "+00:00")
            ).timestamp())
            if ret is not None and stamp is not None:
                results.append((stamp, ret))
    info = report.get("strategy_results", {}).get(strategy, {})
    # Use actual strategy observation span (not the global DB span) for readiness.
    first = last = None
    if observations:
        stamps = [
            _finite(row.get("observed_at")) for row in observations
            if row.get("strategy") == strategy and _finite(row.get("observed_at")) is not None
        ]
        if stamps:
            first, last = min(stamps), max(stamps)
    return results, (first, last)

def _ledger_metrics(connection, mode: str, strategy: str) -> dict:
    rows = connection.execute(
        """SELECT id,ts,action,pnl_usd,value_usd,reason FROM trades
           WHERE lower(mode)=lower(?) AND strategy=? AND upper(action) LIKE 'SELL%'
           ORDER BY id ASC""", (mode, strategy),
    ).fetchall()
    pnls = [_finite(r["pnl_usd"]) for r in rows]
    pnls = [v for v in pnls if v is not None]
    winners, losers = [v for v in pnls if v > 0], [v for v in pnls if v < 0]
    cumulative = peak = max_dd = 0.0
    streak = worst_streak = 0
    for v in pnls:
        cumulative += v
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
        if v < 0:
            streak += 1
            worst_streak = max(worst_streak, streak)
        else:
            streak = 0
    return {
        "exit_fill_rows": len(rows),
        "realized_pnl_usd": round(sum(pnls), 6) if pnls else None,
        "expectancy_usd_per_exit_fill": round(statistics.fmean(pnls), 6) if pnls else None,
        "hit_rate_pct": round(100.0 * len(winners) / len(pnls), 3) if pnls else None,
        "avg_win_usd": round(statistics.fmean(winners), 6) if winners else None,
        "avg_loss_usd": round(statistics.fmean(losers), 6) if losers else None,
        "profit_factor": round(sum(winners) / abs(sum(losers)), 6) if losers and sum(losers) else (None if not winners else "infinite_no_losses"),
        "max_drawdown_usd_on_exit_fill_curve": round(max_dd, 6) if pnls else None,
        "worst_loss_streak": worst_streak,
        "note": "Sell rows are exit fill legs, not necessarily unique round-trip positions; simulator/live accounting depends on stored ledger fields.",
    }

def build_mode_report(db_path: str | Path, *, mode: str = "paper",
                      round_trip_cost_pct: float = DEFAULT_ROUND_TRIP_COST_PCT,
                      min_counterfactual_trades: int = DEFAULT_MIN_COUNTERFACTUAL_TRADES,
                      min_active_days: float = DEFAULT_MIN_ACTIVE_DAYS,
                      min_samples_for_day_gate: int = MIN_SAMPLES_FOR_DAY_GATE,
                      bootstrap_iterations: int = 5000) -> dict:
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Database not found: {path}")
    if not math.isfinite(round_trip_cost_pct) or not 0 <= round_trip_cost_pct <= 50:
        raise ValueError("Round-trip cost must be between 0 and 50 percent.")
    uri = path.as_uri() + "?mode=ro"
    modes = {}
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        state = {str(r["key"]): str(r["value"]) for r in connection.execute("SELECT key,value FROM state")}
        saved_mode = state.get("database_mode", "")
        if saved_mode and saved_mode.lower() != mode.lower():
            raise ValueError(f"Database is locked to {saved_mode!r} mode, not requested {mode!r}.")
        for index, strategy in enumerate(STRATEGIES):
            if strategy == "leader_copy":
                outcomes, span = _leader_shadow_returns(connection, mode=mode, cost_pct=round_trip_cost_pct)
            else:
                outcomes, span = _shadow_strategy_returns(path, strategy, round_trip_cost_pct)
            outcomes.sort(key=lambda x: x[0])
            values = [item[1] for item in outcomes]
            first, last = span
            observed_days = max(0.0, (last - first) / 86400.0) if first is not None and last is not None else 0.0
            enough = len(values) >= min_counterfactual_trades or (
                observed_days >= min_active_days and len(values) >= min_samples_for_day_gate
            )
            metrics = _return_metrics(values, seed=20261025 + index, iterations=bootstrap_iterations)
            lower = metrics["expectancy_ci_95_pct"]["lower_95"]
            if not values:
                status = "INSUFFICIENT_EVIDENCE"
                verdict = "KEEP_PAPER"
            elif not enough:
                status = "INSUFFICIENT_EVIDENCE"
                verdict = "KEEP_PAPER"
            elif lower is None or lower <= 0:
                status = "AUTO_DISABLE_NONPOSITIVE_CI"
                verdict = "AUTO_DISABLE"
            else:
                status = "POSITIVE_LOWER_CI_REVIEW_ONLY"
                verdict = "CANDIDATE_FOR_HUMAN_REVIEW_NOT_LIVE_ENABLEMENT"
            modes[strategy] = {
                "status": status, "verdict": verdict,
                "observed_span_days": round(observed_days, 6),
                "counterfactual_15m_outcomes": metrics,
                "maturity_gate": {
                    "minimum_counterfactual_trades": min_counterfactual_trades,
                    "minimum_active_days_alternative": min_active_days,
                    "minimum_samples_for_day_alternative": min_samples_for_day_gate,
                    "passed": enough,
                },
                "round_trip_cost_assumption_pct": round_trip_cost_pct,
                "paper_or_live_ledger": _ledger_metrics(connection, mode, strategy),
                "mode_auto_disabled_state": state.get(f"mode_policy_disabled:{mode}:{strategy}", "0") == "1",
            }
    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "database_mode": mode,
        "database_path": str(path),
        "read_only_unless_apply_auto_disable": True,
        "status": "INSUFFICIENT_EVIDENCE" if all(v["status"] == "INSUFFICIENT_EVIDENCE" for v in modes.values()) else "EVALUATED",
        "strategies": modes,
        "cost_model": {
            "round_trip_sensitivity_pct": round_trip_cost_pct,
            "description": (
                "Observed 15-minute mark-to-mark returns apply a symmetric assumed round-trip cost. "
                "This is a sensitivity scenario, not a verified route quote. PumpPortal's 0.5% fee is "
                "tracked separately by research_journal; protocol/creator fees, slippage, priority fees, "
                "tips, failed transactions and latency stay unknown until observed or explicitly supplied."
            ),
        },
        "limitations": [
            "Counterfactual marks are not executable fills and may omit price impact, failed transactions and MEV.",
            "The bootstrap interval is conditional on observed signals and does not correct selection bias or regime shifts.",
            "The current live-entry approval remains separate and is never changed by this evaluator.",
            "A positive interval is a review candidate, not authorization to trade live.",
        ],
        "what_could_be_wrong": [
            "Observations may be sparse, stale, selected by existing filters or missing rugs that disappeared before a mark.",
            "The assumed cost may understate venue/protocol fees, impact, tips and latency; measured live PnL can differ.",
            "Correlated token returns reduce the effective sample size; an ordinary bootstrap may overstate certainty.",
        ],
    }

def auto_disable_unprofitable_modes(
    store, *, mode: str = "paper",
    round_trip_cost_pct: float = DEFAULT_ROUND_TRIP_COST_PCT,
    min_counterfactual_trades: int = DEFAULT_MIN_COUNTERFACTUAL_TRADES,
    min_active_days: float = DEFAULT_MIN_ACTIVE_DAYS,
    min_samples_for_day_gate: int = MIN_SAMPLES_FOR_DAY_GATE,
) -> dict:
    """Persist a per-mode disable only when mature counterfactual evidence has CI lower <= 0.

    This function never re-enables a disabled strategy and never changes the global live
    authorization flag. It is intended to be called periodically by the running bot.
    """
    report = build_mode_report(
        store.path, mode=mode, round_trip_cost_pct=round_trip_cost_pct,
        min_counterfactual_trades=min_counterfactual_trades,
        min_active_days=min_active_days,
        min_samples_for_day_gate=min_samples_for_day_gate,
        bootstrap_iterations=1000,
    )
    changes = []
    for strategy, result in report["strategies"].items():
        if result["verdict"] != "AUTO_DISABLE":
            continue
        key = f"mode_policy_disabled:{mode}:{strategy}"
        reason_key = f"mode_policy_reason:{mode}:{strategy}"
        with store.db:
            store.db.execute(
                "INSERT INTO state(key,value) VALUES(?, '1') ON CONFLICT(key) DO UPDATE SET value='1'",
                (key,),
            )
            store.db.execute(
                "INSERT INTO state(key,value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (reason_key, "Evidence maturity passed and 95% bootstrap expectancy lower bound was not positive after configured cost assumption."),
            )
        changes.append(strategy)
    return {"disabled": changes, "report": report}

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only per-mode counterfactual and ledger evaluation.")
    parser.add_argument("--db", default="memecoin-pumpfun.db", help="Mode-specific SQLite database path.")
    parser.add_argument("--mode", choices=("paper", "live"), default="paper")
    parser.add_argument("--round-trip-cost-pct", type=float, default=DEFAULT_ROUND_TRIP_COST_PCT)
    parser.add_argument("--json-out", help="Optional JSON report path.")
    parser.add_argument("--apply-auto-disable", action="store_true",
                        help="Persist disable flags for mature modes whose 95% expectancy CI lower bound is <= 0; never enables a mode.")
    args = parser.parse_args(argv)
    try:
        report = build_mode_report(args.db, mode=args.mode, round_trip_cost_pct=args.round_trip_cost_pct)
        if args.apply_auto_disable:
            if args.mode != "paper":
                raise ValueError("Automatic policy application is allowed only to a paper-mode database.")
            uri = Path(args.db).expanduser().resolve()
            with sqlite3.connect(uri) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                for strategy, result in report["strategies"].items():
                    if result["verdict"] != "AUTO_DISABLE":
                        continue
                    connection.execute(
                        "INSERT INTO state(key,value) VALUES(?, '1') ON CONFLICT(key) DO UPDATE SET value='1'",
                        (f"mode_policy_disabled:paper:{strategy}",),
                    )
                    connection.execute(
                        "INSERT INTO state(key,value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (f"mode_policy_reason:paper:{strategy}", "Mature counterfactual sample with non-positive lower 95% expectancy confidence bound."),
                    )
                connection.commit()
            report["auto_disable_applied"] = True
        text = json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
        if args.json_out:
            dest = Path(args.json_out)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text + "\n", encoding="utf-8")
        print(text)
        return 0
    except Exception as exc:
        print(f"Evaluation failed: {type(exc).__name__}: {exc}")
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
