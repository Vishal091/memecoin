"""Build/validate a native Pump transaction without signing or broadcasting it.

Example:
    .\.venv\Scripts\python.exe diagnose_native_build.py --mint <MINT>
    .\.venv\Scripts\python.exe diagnose_native_build.py --mint <MINT> --simulate
"""
from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
import sys

from market_data import MarketData
from native_pump import EXPECTED_ROUTE_PROGRAM, NativePumpExecutor, validate_native_transaction
from pumpfun_config import PumpFunSettings
from solana_rpc import SolanaRPC


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mint", required=True, help="Solana token mint to inspect")
    parser.add_argument(
        "--usd", type=Decimal, default=None,
        help="Notional for building a diagnostic transaction (default: MAX_ENTRY_USD)",
    )
    parser.add_argument(
        "--simulate", action="store_true",
        help="Also call Solana simulateTransaction; this does not sign or submit",
    )
    args = parser.parse_args()

    settings = PumpFunSettings.load()
    if settings.mode != "live":
        raise RuntimeError("This diagnostic expects the existing live .env configuration.")
    notional = args.usd if args.usd is not None else Decimal(str(settings.max_entry_usd))
    if notional <= 0:
        raise ValueError("--usd must be positive.")
    if notional > Decimal(str(settings.max_entry_usd)):
        raise ValueError("--usd exceeds MAX_ENTRY_USD; refusing to exceed the configured cap.")
    if notional > Decimal(str(settings.max_total_exposure_usd)):
        raise ValueError("--usd exceeds MAX_TOTAL_EXPOSURE_USD.")

    keypair = NativePumpExecutor.load_keypair(settings.keypair_path)
    wallet = str(keypair.pubkey())
    rpc = SolanaRPC(settings.rpc_url, settings.request_timeout_seconds)
    sol_price = Decimal(str(MarketData(settings.request_timeout_seconds).sol_price_usd()))
    amount_lamports = int(
        (notional / sol_price * Decimal(1_000_000_000)).to_integral_value(rounding=ROUND_DOWN)
    )
    if amount_lamports <= 0:
        raise ValueError("Requested notional rounds to zero lamports.")

    executor = NativePumpExecutor(
        rpc,
        settings.rpc_url,
        settings.request_timeout_seconds,
        settings.pumpportal_priority_fee_sol,
        settings.min_sol_reserve,
    )
    payload = {
        "rpcUrl": settings.rpc_url,
        "wallet": wallet,
        "mint": args.mint,
        "action": "buy",
        "amountLamports": str(amount_lamports),
        "amountRaw": "0",
        "slippageBps": settings.slippage_bps,
        "priorityFeeSol": settings.pumpportal_priority_fee_sol,
    }

    print("Native Pump SDK build-only diagnostic")
    print("Wallet:", wallet)
    print("Mint:", args.mint)
    print(f"Requested notional: ~${notional:.2f}")
    transaction, result = executor._build_unsigned(payload)
    route = str(result.get("pool") or "")
    expected_program = EXPECTED_ROUTE_PROGRAM.get(route)
    if expected_program is None or result.get("expectedProgram") != expected_program:
        raise RuntimeError("Builder returned an unsupported or inconsistent venue.")
    validate_native_transaction(
        transaction,
        wallet=wallet,
        mint=args.mint,
        action="buy",
        route=route,
        amount_lamports=amount_lamports,
        amount_raw=0,
        priority_fee_limit_lamports=executor.priority_fee_lamports,
    )

    print("Route:", route)
    print("Expected official program:", expected_program)
    print("Top-level programs:")
    for program in result.get("topLevelPrograms", []):
        print(" -", program)
    if args.simulate:
        executor._simulate(transaction, expected_program)
        print("Simulation: SUCCESS")
    else:
        print("Simulation: skipped (use --simulate to run a non-committing simulation)")
    print("Transaction was not signed or submitted; no trade was placed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Diagnostic failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("No transaction was signed or submitted by this script.", file=sys.stderr)
        raise SystemExit(2)
