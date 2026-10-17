"""Direct native Pump.fun/PumpSwap executor; never delegates transaction building to PumpPortal."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import shutil
import subprocess
import time
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

logger = logging.getLogger("native_pump")

ROOT = Path(__file__).resolve().parent
BUILDER = ROOT / "native_pump_builder.mjs"

PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PUMPPORTAL_ROUTER_ID = "FAdo9NCw1ssek6Z6yeWzWjhLVsr8uiCwcWNUnKgzTnHe"
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
COMPUTE_BUDGET_ID = "ComputeBudget111111111111111111111111111111"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ASSOCIATED_TOKEN_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"

ALLOWED_PROGRAMS = {
    SYSTEM_PROGRAM_ID,
    COMPUTE_BUDGET_ID,
    TOKEN_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
    ASSOCIATED_TOKEN_PROGRAM_ID,
    PUMP_PROGRAM_ID,
    PUMPSWAP_PROGRAM_ID,
}
EXPECTED_ROUTE_PROGRAM = {"pump": PUMP_PROGRAM_ID, "pump-amm": PUMPSWAP_PROGRAM_ID}
# Accounts for new ATAs and Pump volume-account initialization are separate from
# swap notional. Reserve this buffer as well as the configured minimum SOL reserve.
ENTRY_RENT_BUFFER_LAMPORTS = 8_000_000  # 0.008 SOL


def anchor_discriminator(name: str) -> bytes:
    return hashlib.sha256(f"global:{name}".encode("utf-8")).digest()[:8]


PUMP_BUY_EXACT_QUOTE_IN_V3 = anchor_discriminator("buy_exact_quote_in_v3")
PUMP_SELL_V3 = anchor_discriminator("sell_v3")
PUMPSWAP_BUY_V2 = anchor_discriminator("buy_v2")
PUMPSWAP_SELL_V2 = anchor_discriminator("sell_v2")
PUMPSWAP_EXTEND_ACCOUNT = anchor_discriminator("extend_account")


def _text(value: Any) -> str:
    return str(value)


def _ix_accounts(ix: Any) -> list[int]:
    try:
        return [int(index) for index in bytes(ix.accounts)]
    except Exception as exc:
        raise RuntimeError("Unable to read compiled instruction account indexes") from exc


def _ix_data(ix: Any) -> bytes:
    try:
        return bytes(ix.data)
    except Exception as exc:
        raise RuntimeError("Unable to read compiled instruction data") from exc


def _key_at(keys: list[Any], index: int) -> str:
    if index < 0 or index >= len(keys):
        raise RuntimeError(f"Instruction references invalid account index {index}")
    return str(keys[index])


def _associated_token_address(owner: Pubkey, mint: Pubkey, token_program: Pubkey) -> Pubkey:
    address, _ = Pubkey.find_program_address(
        [bytes(owner), bytes(token_program), bytes(mint)],
        Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM_ID),
    )
    return address


def validate_native_transaction(
    transaction: Any,
    *,
    wallet: str,
    mint: str,
    action: str,
    route: str,
    amount_lamports: int,
    amount_raw: int,
    priority_fee_limit_lamports: int,
) -> None:
    """Fail closed unless an unsigned SDK transaction matches the requested trade intent."""
    if route not in EXPECTED_ROUTE_PROGRAM:
        raise RuntimeError(f"Native builder returned unsupported route: {route}")
    if action not in {"buy", "sell"}:
        raise RuntimeError(f"Unsupported trade action: {action}")

    message = transaction.message
    keys = list(message.account_keys)
    if not keys or str(keys[0]) != wallet:
        raise RuntimeError("Unsigned transaction fee payer is not the configured wallet")
    if int(message.header.num_required_signatures) != 1:
        raise RuntimeError("Native transaction must require exactly the configured wallet signature")

    lookups = getattr(message, "address_table_lookups", ())
    if lookups:
        raise RuntimeError("Native transaction unexpectedly uses address lookup tables; refusing to sign")

    wallet_key = Pubkey.from_string(wallet)
    mint_key = Pubkey.from_string(mint)
    wsol_key = Pubkey.from_string(WRAPPED_SOL_MINT)
    token_keys = {
        TOKEN_PROGRAM_ID: Pubkey.from_string(TOKEN_PROGRAM_ID),
        TOKEN_2022_PROGRAM_ID: Pubkey.from_string(TOKEN_2022_PROGRAM_ID),
    }
    user_wsol_atas = {
        str(_associated_token_address(wallet_key, wsol_key, token_program))
        for token_program in [token_keys[TOKEN_PROGRAM_ID]]
    }

    program_ids: list[str] = []
    transfer_total = 0
    observed_unit_limit: int | None = None
    observed_unit_price: int | None = None
    venue_trade_count = 0
    route_program = EXPECTED_ROUTE_PROGRAM[route]

    for ix in message.instructions:
        program = _key_at(keys, int(ix.program_id_index))
        program_ids.append(program)
        data = _ix_data(ix)
        account_indexes = _ix_accounts(ix)
        account_keys = [_key_at(keys, index) for index in account_indexes]

        if program == PUMPPORTAL_ROUTER_ID:
            raise RuntimeError("PumpPortal router detected in a supposed native transaction")
        if program not in ALLOWED_PROGRAMS:
            raise RuntimeError(f"Native transaction invokes unapproved top-level program: {program}")

        if program == COMPUTE_BUDGET_ID:
            if not data:
                raise RuntimeError("Empty ComputeBudget instruction")
            if data[0] == 2 and len(data) == 5:
                observed_unit_limit = int.from_bytes(data[1:5], "little")
            elif data[0] == 3 and len(data) == 9:
                observed_unit_price = int.from_bytes(data[1:9], "little")
            else:
                raise RuntimeError("Unexpected ComputeBudget instruction; refusing to sign")
            continue

        if program == SYSTEM_PROGRAM_ID:
            # Only permit wrapping SOL from our own wallet into our own WSOL ATA.
            if len(data) < 12 or int.from_bytes(data[:4], "little") != 2:
                raise RuntimeError("Unexpected System instruction; only a bounded WSOL transfer is allowed")
            if len(account_keys) < 2 or account_keys[0] != wallet:
                raise RuntimeError("System transfer source is not the configured wallet")
            if account_keys[1] not in user_wsol_atas:
                raise RuntimeError("System transfer destination is not the wallet's WSOL ATA")
            transfer = int.from_bytes(data[4:12], "little")
            if action != "buy":
                raise RuntimeError("Unexpected outgoing SOL transfer in a sell transaction")
            transfer_total += transfer
            if transfer_total > amount_lamports:
                raise RuntimeError("WSOL funding transfer exceeds the configured buy notional")
            continue

        if program == ASSOCIATED_TOKEN_PROGRAM_ID:
            # create / createIdempotent: payer, ATA, owner, mint, system, token program.
            if len(account_keys) < 6:
                raise RuntimeError("Malformed Associated Token Account instruction")
            payer, ata, ata_owner, ata_mint, system_program, token_program = account_keys[:6]
            if payer != wallet or ata_owner != wallet:
                raise RuntimeError("ATA setup attempts to pay for or create an account for another wallet")
            if system_program != SYSTEM_PROGRAM_ID or token_program not in token_keys:
                raise RuntimeError("ATA setup uses an unexpected system or token program")
            if ata_mint not in {mint, WRAPPED_SOL_MINT}:
                raise RuntimeError("ATA setup references an unexpected mint")
            expected_ata = str(
                _associated_token_address(wallet_key, Pubkey.from_string(ata_mint), token_keys[token_program])
            )
            if ata != expected_ata:
                raise RuntimeError("ATA instruction does not derive to the configured wallet's associated account")
            if not data or data[0] not in {0, 1}:
                raise RuntimeError("Unexpected Associated Token Account instruction type")
            continue

        if program in {TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID}:
            # Only WSOL SyncNative and CloseAccount-to-self are needed for native
            # wrap/swap/unwrap. Transfers, approvals and authority changes are refused.
            if not data:
                raise RuntimeError("Empty token-program instruction")
            if data[0] == 17:  # SyncNative
                if not account_keys or account_keys[0] not in user_wsol_atas:
                    raise RuntimeError("SyncNative is not for the wallet's WSOL ATA")
            elif data[0] == 9:  # CloseAccount
                if len(account_keys) < 3 or account_keys[0] not in user_wsol_atas:
                    raise RuntimeError("Only the wallet's WSOL ATA may be closed by a swap")
                if account_keys[1] != wallet or account_keys[2] != wallet:
                    raise RuntimeError("WSOL account close must refund SOL to the configured wallet")
            else:
                raise RuntimeError(f"Unexpected top-level token instruction opcode {data[0]}")
            continue

        if program != route_program:
            if program in {PUMP_PROGRAM_ID, PUMPSWAP_PROGRAM_ID}:
                raise RuntimeError(f"Transaction mixes incompatible trade venues: {program}")
            raise RuntimeError(f"Unexpected program {program}")

        if len(data) < 8:
            raise RuntimeError("Venue instruction has no readable Anchor discriminator")

        discriminator = data[:8]
        if route == "pump":
            if action == "buy" and discriminator == PUMP_BUY_EXACT_QUOTE_IN_V3:
                if len(data) < 24:
                    raise RuntimeError("Malformed Pump buy_exact_quote_in_v3 instruction")
                spendable_quote = int.from_bytes(data[8:16], "little")
                if spendable_quote <= 0 or spendable_quote > amount_lamports:
                    raise RuntimeError("Pump curve instruction exceeds the requested SOL buy budget")
                if len(account_keys) < 9 or account_keys[1] != mint or account_keys[8] != wallet:
                    raise RuntimeError("Pump curve instruction mint or user does not match the requested trade")
                venue_trade_count += 1
            elif action == "sell" and discriminator == PUMP_SELL_V3:
                if len(data) < 24:
                    raise RuntimeError("Malformed Pump sell_v3 instruction")
                tokens_in = int.from_bytes(data[8:16], "little")
                if tokens_in != amount_raw:
                    raise RuntimeError("Pump sell instruction amount differs from the requested token quantity")
                if len(account_keys) < 9 or account_keys[1] != mint or account_keys[8] != wallet:
                    raise RuntimeError("Pump sell instruction mint or user does not match the requested trade")
                venue_trade_count += 1
            else:
                raise RuntimeError("Native Pump route contains an unsupported instruction instead of the expected v3 trade")
        else:
            if discriminator == PUMPSWAP_EXTEND_ACCOUNT:
                # Official PumpSwap SDK may extend an older pool account before swapping.
                if wallet not in account_keys:
                    raise RuntimeError("PumpSwap extend_account instruction does not include the configured payer")
                continue
            if action == "buy" and discriminator == PUMPSWAP_BUY_V2:
                if len(data) < 24:
                    raise RuntimeError("Malformed PumpSwap buy_v2 instruction")
                max_quote = int.from_bytes(data[16:24], "little")
                if max_quote <= 0 or max_quote > amount_lamports:
                    raise RuntimeError("PumpSwap worst-case quote exceeds the configured buy notional")
                if len(account_keys) < 4 or account_keys[1] != wallet or account_keys[3] != mint:
                    raise RuntimeError("PumpSwap buy instruction mint or user does not match the requested trade")
                venue_trade_count += 1
            elif action == "sell" and discriminator == PUMPSWAP_SELL_V2:
                if len(data) < 24:
                    raise RuntimeError("Malformed PumpSwap sell_v2 instruction")
                tokens_in = int.from_bytes(data[8:16], "little")
                if tokens_in != amount_raw:
                    raise RuntimeError("PumpSwap sell amount differs from the requested token quantity")
                if len(account_keys) < 4 or account_keys[1] != wallet or account_keys[3] != mint:
                    raise RuntimeError("PumpSwap sell instruction mint or user does not match the requested trade")
                venue_trade_count += 1

    if program_ids.count(route_program) < 1 or venue_trade_count != 1:
        raise RuntimeError("Native transaction must contain exactly one recognized trade on the expected venue")
    if observed_unit_limit is None or observed_unit_price is None:
        raise RuntimeError("Native transaction is missing the expected compute-budget instructions")
    actual_priority_fee = (
        observed_unit_limit * observed_unit_price + 999_999
    ) // 1_000_000
    if actual_priority_fee > priority_fee_limit_lamports + 2:
        raise RuntimeError("Native transaction priority fee exceeds the configured limit")


class NativePumpExecutor:
    """Build direct official-SDK transactions, validate, simulate, sign locally and confirm."""

    def __init__(self, rpc, rpc_url: str, timeout: int = 12,
                 priority_fee_sol: float = 0.00001, min_sol_reserve: float = 0.025):
        self.rpc = rpc
        self.rpc_url = rpc_url
        self.timeout = max(3, int(timeout))
        self.priority_fee_sol = float(priority_fee_sol)
        self.min_sol_reserve_lamports = int(Decimal(str(min_sol_reserve)) * Decimal(1_000_000_000))
        self.priority_fee_lamports = int(Decimal(str(priority_fee_sol)) * Decimal(1_000_000_000))
        self.node = shutil.which("node")
        if not self.node:
            raise RuntimeError("Node.js is required for native Pump SDK execution. Install Node.js LTS and run npm install.")
        if not BUILDER.is_file():
            raise RuntimeError(f"Native Pump SDK builder is missing: {BUILDER}")
        check = subprocess.run(
            [self.node, str(BUILDER), "--check"],
            cwd=str(ROOT), text=True, capture_output=True,
            timeout=max(20, self.timeout * 2), check=False,
        )
        if check.returncode != 0:
            detail = (check.stderr or check.stdout or "SDK check failed").strip()[-1200:]
            raise RuntimeError("Native Pump SDK dependencies are not ready: " + detail)
        logger.info("Native Pump SDK execution is available; PumpPortal is data-feed only.")

    @staticmethod
    def load_keypair(path: Path) -> Keypair:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or len(raw) != 64 or any(
            not isinstance(value, int) or value < 0 or value > 255 for value in raw
        ):
            raise ValueError("Expected a standard Solana CLI keypair JSON array of 64 bytes")
        return Keypair.from_bytes(bytes(raw))

    def sol_balance_lamports(self, owner: str) -> int:
        result = self.rpc.call("getBalance", [owner, {"commitment": "confirmed"}]) or {}
        return int(result.get("value", 0))

    def token_balance_raw(self, owner: str, mint: str) -> int:
        result = self.rpc.call("getTokenAccountsByOwner", [
            owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}
        ]) or {}
        total = 0
        for row in result.get("value", []):
            try:
                total += int(row["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
            except (KeyError, TypeError, ValueError):
                continue
        return total

    def _build_unsigned(self, payload: dict[str, Any]) -> tuple[VersionedTransaction, dict[str, Any]]:
        result = subprocess.run(
            [self.node, str(BUILDER)],
            input=json.dumps(payload), cwd=str(ROOT),
            text=True, capture_output=True, timeout=max(30, self.timeout * 4), check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "Native SDK build failed").strip()[-1800:]
            raise RuntimeError("Native Pump SDK transaction build failed: " + detail)
        try:
            built = json.loads(result.stdout)
            raw = base64.b64decode(built["transaction"], validate=True)
            transaction = VersionedTransaction.from_bytes(raw)
        except Exception as exc:
            raise RuntimeError("Native Pump SDK returned an invalid unsigned transaction response") from exc
        return transaction, built

    def _simulate(self, transaction: VersionedTransaction, expected_program: str) -> None:
        encoded = base64.b64encode(bytes(transaction)).decode("ascii")
        result = self.rpc.call("simulateTransaction", [
            encoded,
            {
                "encoding": "base64",
                "sigVerify": False,
                "replaceRecentBlockhash": True,
                "commitment": "confirmed",
                "innerInstructions": True,
            },
        ]) or {}
        value = result.get("value") or {}
        logs = value.get("logs") or []
        if value.get("err") is not None:
            excerpt = " | ".join(str(line) for line in logs[-8:])[:1000]
            raise RuntimeError(f"Native transaction simulation failed: {value.get('err')}; {excerpt}")
        if any(PUMPPORTAL_ROUTER_ID in str(line) for line in logs):
            raise RuntimeError("Simulation invoked the PumpPortal router; refusing to sign")
        if not any(f"Program {expected_program} invoke" in str(line) for line in logs):
            raise RuntimeError("Simulation did not invoke the expected official Pump venue")

    def _send_confirm(self, transaction: VersionedTransaction) -> str:
        encoded = base64.b64encode(bytes(transaction)).decode("ascii")
        signature = self.rpc.call("sendTransaction", [
            encoded,
            {"encoding": "base64", "preflightCommitment": "confirmed", "maxRetries": 3},
        ])
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = self.rpc.call(
                "getSignatureStatuses",
                [[signature], {"searchTransactionHistory": True}],
            ) or {}
            statuses = result.get("value") or []
            status = statuses[0] if statuses else None
            if status:
                if status.get("err") is not None:
                    raise RuntimeError(f"Transaction failed on-chain: {status['err']}; signature={signature}")
                if status.get("confirmationStatus") in {"confirmed", "finalized"}:
                    return str(signature)
            time.sleep(0.7)
        raise TimeoutError(f"Confirmation timeout for {signature}; do not retry before checking this signature")

    def trade(self, owner: str, keypair: Keypair, mint: str, action: str, amount: float,
              denominated_in_sol: bool, slippage_bps: int, preferred_pool: str | None = None,
              amount_raw: int | None = None) -> dict[str, Any]:
        del preferred_pool  # Route is chosen from the current on-chain curve/pool state, not a feed hint.
        if action not in {"buy", "sell"}:
            raise ValueError("action must be buy or sell")
        if owner != str(keypair.pubkey()):
            raise RuntimeError("Configured wallet does not match the local signing keypair")
        if action == "buy":
            if not denominated_in_sol:
                raise ValueError("Native executor supports SOL-denominated buys only")
            amount_lamports = int(
                (Decimal(str(amount)) * Decimal(1_000_000_000)).to_integral_value(rounding=ROUND_DOWN)
            )
            amount_raw_value = 0
            if amount_lamports <= 0:
                raise ValueError("Buy size rounds to zero lamports")
        else:
            if denominated_in_sol:
                raise ValueError("Sell amount must be denominated in token units")
            if amount_raw is None or int(amount_raw) <= 0:
                raise ValueError("Native sells require the exact raw token amount")
            amount_lamports = 0
            amount_raw_value = int(amount_raw)

        sol_before = self.sol_balance_lamports(owner)
        token_before = self.token_balance_raw(owner, mint)
        if action == "buy":
            required = (
                amount_lamports
                + self.min_sol_reserve_lamports
                + ENTRY_RENT_BUFFER_LAMPORTS
                + self.priority_fee_lamports
                + 20_000  # network signature fee safety margin
            )
            if sol_before < required:
                raise RuntimeError(
                    "Insufficient SOL for entry, rent/ATA buffer, network fee and configured reserve; "
                    "no transaction was submitted"
                )
        elif token_before < amount_raw_value:
            raise RuntimeError("Wallet token balance is below the requested sell amount; no transaction was submitted")

        payload = {
            "rpcUrl": self.rpc_url,
            "wallet": owner,
            "mint": mint,
            "action": action,
            "amountLamports": str(amount_lamports),
            "amountRaw": str(amount_raw_value),
            "slippageBps": int(slippage_bps),
            "priorityFeeSol": self.priority_fee_sol,
        }
        unsigned, built = self._build_unsigned(payload)
        route = str(built.get("pool") or "")
        expected_program = EXPECTED_ROUTE_PROGRAM.get(route)
        if expected_program is None or built.get("expectedProgram") != expected_program:
            raise RuntimeError("Native SDK reported an unsupported or inconsistent execution route")

        validate_native_transaction(
            unsigned,
            wallet=owner,
            mint=mint,
            action=action,
            route=route,
            amount_lamports=amount_lamports,
            amount_raw=amount_raw_value,
            priority_fee_limit_lamports=self.priority_fee_lamports,
        )
        # Simulate before signing. This is a non-committing RPC call; any route,
        # account, liquidity, or instruction failure aborts before private-key use.
        self._simulate(unsigned, expected_program)

        signed = VersionedTransaction(unsigned.message, [keypair])
        signature = self._send_confirm(signed)

        deadline = time.monotonic() + 10
        token_after = token_before
        sol_after = sol_before
        while time.monotonic() < deadline:
            token_after = self.token_balance_raw(owner, mint)
            sol_after = self.sol_balance_lamports(owner)
            delta = token_after - token_before
            if (action == "buy" and delta > 0) or (action == "sell" and delta < 0):
                break
            time.sleep(0.5)
        token_delta = token_after - token_before
        sol_delta = sol_after - sol_before
        if action == "buy" and token_delta <= 0:
            raise RuntimeError(
                f"Transaction {signature} confirmed but token balance did not increase; "
                f"reconcile manually before retrying"
            )
        if action == "sell" and token_delta >= 0:
            raise RuntimeError(
                f"Transaction {signature} confirmed but token balance did not decrease; "
                f"reconcile manually before retrying"
            )

        logger.warning("Native %s transaction confirmed route=%s signature=%s", action, route, signature)
        return {
            "signature": signature,
            "pool": route,
            "token_before_raw": token_before,
            "token_after_raw": token_after,
            "token_delta_raw": token_delta,
            "sol_delta_lamports": sol_delta,
            "total_input_amount": max(0, -sol_delta) if action == "buy" else abs(token_delta),
            "total_output_amount": token_delta if action == "buy" else max(0, sol_delta),
        }
