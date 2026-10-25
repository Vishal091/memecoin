"""Transparent execution-cost assumptions; estimates are not confirmed transaction fees.

Callers must provide actual route/venue assumptions and measured latency/slippage when
available. Missing liquidity or SOL/USD inputs are reported as missing, never guessed.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any


def _num(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


@dataclass(frozen=True)
class CostAssumptions:
    pumpportal_fee_pct_per_side: float = 0.5
    pump_bonding_curve_fee_pct_per_side: float = 1.25
    pumpswap_conservative_fee_pct_per_side: float = 1.25
    base_network_fee_sol_per_side: float = 0.000005
    priority_fee_sol_per_side: float = 0.00001
    jito_tip_sol_per_side: float = 0.0
    failed_tx_probability: float | None = None
    failed_tx_cost_sol: float = 0.000015
    latency_slippage_pct_per_side: float | None = None
    pumpportal_stream_fee_sol_per_10k: float = 0.01
    pumpportal_stream_events_per_10k: int = 10000
    stream_events_attributed_per_round_trip: float | None = None
    assumptions_version: str = "2026-10-25-v1"


def assumptions_from_settings(settings: Any, *, stream_events_attributed_per_round_trip: float | None = None) -> CostAssumptions:
    """Load bounded model assumptions from a settings-like object, without reading files."""
    def get(name: str, default: Any) -> Any:
        return getattr(settings, name, default)

    return CostAssumptions(
        pumpportal_fee_pct_per_side=float(get("pumpportal_fee_pct_per_side", 0.5)),
        pump_bonding_curve_fee_pct_per_side=float(get("pump_bonding_curve_fee_pct_per_side", 1.25)),
        pumpswap_conservative_fee_pct_per_side=float(get("pumpswap_conservative_fee_pct_per_side", 1.25)),
        base_network_fee_sol_per_side=float(get("base_network_fee_sol_per_side", 0.000005)),
        priority_fee_sol_per_side=float(get("pumpportal_priority_fee_sol", 0.00001)),
        jito_tip_sol_per_side=float(get("jito_tip_sol_per_side", 0.0)),
        failed_tx_probability=get("failed_tx_probability", None),
        failed_tx_cost_sol=float(get("failed_tx_cost_sol", 0.000015)),
        latency_slippage_pct_per_side=get("latency_slippage_pct_per_side", None),
        stream_events_attributed_per_round_trip=stream_events_attributed_per_round_trip,
        assumptions_version=str(get("cost_assumptions_version", "2026-10-25-v1")),
    )


def estimate_round_trip_cost(
    *,
    notional_usd: float,
    route: str,
    venue: str,
    liquidity_usd: float | None,
    sol_usd: float | None,
    assumptions: CostAssumptions = CostAssumptions(),
) -> dict:
    """Estimate round-trip cost USD, returning incomplete when required data is absent.

    Liquidity slippage uses a declared conservative first-order proxy, not an
    executable quote: 2 * notional / observed pool/real curve TVL per side, capped
    at 100%. This must be replaced with curve math or a sell/buy simulation before
    live authorization. Virtual reserves are intentionally not accepted as TVL.
    """
    amount = _num(notional_usd)
    if amount is None or amount <= 0:
        raise ValueError("notional_usd must be positive and finite")
    route_key = str(route or "").strip().lower()
    venue_key = str(venue or "").strip().lower()
    missing: list[str] = []
    breakdown: dict[str, Any] = {
        "notional_usd": amount,
        "route": route_key or "unknown",
        "venue": venue_key or "unknown",
        "assumptions_version": assumptions.assumptions_version,
        "status": "ESTIMATE_ONLY",
    }

    portal_fee = assumptions.pumpportal_fee_pct_per_side if route_key == "pumpportal_local" else 0.0
    if route_key in {"", "unknown"}:
        missing.append("execution_route")
    if venue_key in {"pump", "pump_bonding_curve", "bonding_curve"}:
        venue_fee = assumptions.pump_bonding_curve_fee_pct_per_side
    elif venue_key in {"pumpswap", "pump_amm", "pump-amm"}:
        venue_fee = assumptions.pumpswap_conservative_fee_pct_per_side
    else:
        venue_fee = None
        missing.append("venue_fee_schedule")

    liquidity = _num(liquidity_usd)
    if liquidity is None or liquidity <= 0:
        slippage_per_side = None
        missing.append("verified_real_liquidity_or_pool_tvl")
    else:
        slippage_per_side = min(100.0, 200.0 * amount / liquidity)

    latency_slippage = _num(assumptions.latency_slippage_pct_per_side)
    if latency_slippage is None or latency_slippage < 0:
        missing.append("measured_latency_slippage")
    if assumptions.failed_tx_probability is None:
        failure_probability = None
        missing.append("measured_failed_tx_probability")
    else:
        failure_probability = _num(assumptions.failed_tx_probability)
        if failure_probability is None or not 0 <= failure_probability <= 1:
            raise ValueError("failed_tx_probability must be between 0 and 1")

    sol_price = _num(sol_usd)
    if sol_price is None or sol_price <= 0:
        missing.append("SOL_USD_for_fixed_fees")

    stream_events = _num(assumptions.stream_events_attributed_per_round_trip)
    if stream_events is None or stream_events < 0:
        stream_cost_usd = None
        missing.append("pumpportal_stream_cost_allocation")
    elif sol_price is None or sol_price <= 0 or assumptions.pumpportal_stream_events_per_10k <= 0:
        stream_cost_usd = None
        missing.append("pumpportal_stream_cost_conversion")
    else:
        stream_cost_usd = (
            stream_events * assumptions.pumpportal_stream_fee_sol_per_10k
            / assumptions.pumpportal_stream_events_per_10k * sol_price
        )

    if venue_fee is not None and slippage_per_side is not None and latency_slippage is not None:
        variable_pct = 2 * (portal_fee + venue_fee + slippage_per_side + latency_slippage)
        variable_cost_usd = amount * variable_pct / 100.0
    else:
        variable_pct = None
        variable_cost_usd = None

    fixed_cost_usd = None
    expected_failed_cost_usd = None
    if sol_price is not None and sol_price > 0:
        fixed_sol = 2 * (
            assumptions.base_network_fee_sol_per_side
            + assumptions.priority_fee_sol_per_side
            + assumptions.jito_tip_sol_per_side
        )
        fixed_cost_usd = fixed_sol * sol_price
        if failure_probability is not None:
            expected_failed_cost_usd = (
                failure_probability * assumptions.failed_tx_cost_sol * sol_price
            )
        else:
            missing.append("failed_tx_cost_conversion")
    total_cost = None
    total_pct = None
    if (variable_cost_usd is not None and fixed_cost_usd is not None
            and expected_failed_cost_usd is not None and stream_cost_usd is not None):
        total_cost = variable_cost_usd + fixed_cost_usd + expected_failed_cost_usd + stream_cost_usd
        total_pct = 100.0 * total_cost / amount

    breakdown.update({
        "pumpportal_fee_pct_per_side": portal_fee,
        "venue_fee_pct_per_side": venue_fee,
        "estimated_slippage_pct_per_side": slippage_per_side,
        "latency_slippage_pct_per_side": latency_slippage,
        "variable_cost_usd": variable_cost_usd,
        "network_priority_tip_cost_usd": fixed_cost_usd,
        "pumpportal_stream_events_attributed_per_round_trip": stream_events,
        "pumpportal_stream_cost_usd": stream_cost_usd,
        "pumpportal_stream_fee_sol_per_10k": assumptions.pumpportal_stream_fee_sol_per_10k,
        "pumpportal_stream_events_per_10k": assumptions.pumpportal_stream_events_per_10k,
        "failed_tx_probability": failure_probability,
        "expected_failed_tx_cost_usd": expected_failed_cost_usd,
        "round_trip_cost_usd": total_cost,
        "round_trip_cost_pct_of_notional": total_pct,
        "missing_inputs": sorted(set(missing)),
        "complete_for_after_cost_verdict": not missing and total_cost is not None,
        "warning": (
            "TVL-based slippage is a screening proxy, not a quote; confirm actual buy and sell outputs. "
            "Rates are configurable assumptions, and actual on-chain fees may differ."
        ),
    })
    return breakdown
