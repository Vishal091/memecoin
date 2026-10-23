"""Read-only reader for Pump.fun bonding-curve reserves.

This is used to estimate a live curve spot price and quote-reserve-backed
liquidity when a new Pump token is not yet indexed by external market APIs.
The estimates are not execution guarantees.
"""
from __future__ import annotations

import base64
import struct
from dataclasses import dataclass
from typing import Any

from solders.pubkey import Pubkey

from pumpportal import PUMP_PROGRAM_ID

# Anchor account discriminator for account:BondingCurve.
BONDING_CURVE_DISCRIMINATOR = bytes((23, 183, 248, 55, 96, 216, 172, 96))
PUMP_PROGRAM = Pubkey.from_string(PUMP_PROGRAM_ID)


@dataclass(frozen=True)
class PumpCurveSnapshot:
    virtual_token_reserves: int
    virtual_sol_reserves: int
    real_token_reserves: int
    real_sol_reserves: int
    token_total_supply: int
    complete: bool
    # None means the legacy account layout ended before quoteMint was appended.
    quote_mint: str | None = None

    def price_usd(self, sol_price_usd: float, decimals: int = 6) -> float | None:
        if self.complete or sol_price_usd <= 0 or self.virtual_token_reserves <= 0 or self.virtual_sol_reserves <= 0:
            return None
        token_reserve_ui = self.virtual_token_reserves / (10 ** decimals)
        sol_reserve_ui = self.virtual_sol_reserves / 1_000_000_000
        price = (sol_reserve_ui / token_reserve_ui) * sol_price_usd
        return price if price > 0 else None

    def liquidity_usd(self, sol_price_usd: float) -> float:
        # Conservative, approximate two-sided value of the SOL reserve. This is
        # an eligibility estimate, not a promise that a swap can exit at this value.
        if self.complete or sol_price_usd <= 0:
            return 0.0
        return (self.real_sol_reserves / 1_000_000_000) * sol_price_usd * 2


def bonding_curve_address(mint: str) -> str:
    mint_key = Pubkey.from_string(mint)
    address, _bump = Pubkey.find_program_address(
        [b"bonding-curve", bytes(mint_key)],
        PUMP_PROGRAM,
    )
    return str(address)


def parse_bonding_curve_data(data: Any) -> PumpCurveSnapshot | None:
    """Parse JSON-RPC base64 account data or already-decoded bytes."""
    try:
        if isinstance(data, bytes):
            raw = data
        elif isinstance(data, (list, tuple)) and data and isinstance(data[0], str):
            raw = base64.b64decode(data[0], validate=True)
        elif isinstance(data, str):
            raw = base64.b64decode(data, validate=True)
        else:
            return None
        # discriminator + five u64 fields + one bool
        if len(raw) < 49 or raw[:8] != BONDING_CURVE_DISCRIMINATOR:
            return None
        v_token, v_sol, r_token, r_sol, total_supply = struct.unpack_from("<QQQQQ", raw, 8)
        complete_byte = raw[48]
        if complete_byte not in (0, 1):
            return None
        if v_token <= 0 or v_sol <= 0 or total_supply <= 0:
            return None
        # Current Pump bonding-curve accounts append creator (32 bytes),
        # mayhem/cashback booleans, then quoteMint after the legacy 49-byte layout.
        # Legacy accounts remain valid and default to the SOL quote path.
        quote_mint = None
        if len(raw) >= 115:
            quote_mint = str(Pubkey.from_bytes(raw[83:115]))
        return PumpCurveSnapshot(
            virtual_token_reserves=v_token,
            virtual_sol_reserves=v_sol,
            real_token_reserves=r_token,
            real_sol_reserves=r_sol,
            token_total_supply=total_supply,
            complete=bool(complete_byte),
            quote_mint=quote_mint,
        )
    except (ValueError, TypeError, struct.error):
        return None


def fetch_pump_curve(
    rpc, mint: str, timeout: float | None = None
) -> PumpCurveSnapshot | None:
    """Fetch and parse the mint's canonical Pump.fun bonding-curve account."""
    address = bonding_curve_address(mint)
    params = [address, {"encoding": "base64", "commitment": "confirmed"}]
    response = (
        rpc.call("getAccountInfo", params)
        if timeout is None
        else rpc.call("getAccountInfo", params, timeout=timeout)
    ) or {}
    account = response.get("value")
    if not isinstance(account, dict):
        return None
    return parse_bonding_curve_data((account.get("data") or [None])[0:2])
