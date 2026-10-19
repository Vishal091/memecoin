"""Jupiter Ultra swap executor for explicitly opted-in trending-token trades.

All trading calls are gated elsewhere by fresh token screening. This module adds
transaction-level checks, bounded SOL input, local signing, pre-submit simulation,
and no automatic resubmission when a submission outcome is ambiguous.
"""
from __future__ import annotations

import base64
import logging
import time
from typing import Any

import requests
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from native_pump import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    COMPUTE_BUDGET_ID,
    SYSTEM_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    TransactionBuildFailed,
    TransactionFailedOnChain,
    TransactionOutcomeUncertain,
    TransactionSimulationFailed,
)
from solders.pubkey import Pubkey as SoldersPubkey

logger = logging.getLogger("jupiter_ultra")

JUPITER_SWAP_V2_BASE = "https://api.jup.ag/swap/v2"
PROGRAM_LABELS_URL = "https://api.jup.ag/swap/v1/program-id-to-label"
WSOL_MINT = "So11111111111111111111111111111111111111112"
ADDRESS_LOOKUP_TABLE_PROGRAM_ID = "AddressLookupTab1e1111111111111111111111111"
JUPITER_AGGREGATOR_PROGRAM_ID = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
CORE_PROGRAMS = {
    SYSTEM_PROGRAM_ID,
    COMPUTE_BUDGET_ID,
    TOKEN_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
    ASSOCIATED_TOKEN_PROGRAM_ID,
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo",
    JUPITER_AGGREGATOR_PROGRAM_ID,
}


def _base58(value: Any) -> str:
    return str(value or "").strip()


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not (result == result and abs(result) != float("inf")):
        return None
    return result


class JupiterUltraExecutor:
    """Build/sign/execute swaps via Jupiter Ultra; never retries a submitted swap."""

    def __init__(self, rpc, api_key: str, timeout: int = 12):
        self.rpc = rpc
        self.api_key = str(api_key or "").strip()
        self.timeout = max(3, min(25, int(timeout)))
        self.session = requests.Session()
        self._program_labels: set[str] = set()
        self._program_labels_at = 0.0

    @staticmethod
    def _expected_wsol_ata(owner: str) -> str:
        owner_key = Pubkey.from_string(owner)
        token_program = Pubkey.from_string(TOKEN_PROGRAM_ID)
        mint = Pubkey.from_string(WSOL_MINT)
        ata_program = Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM_ID)
        ata, _ = Pubkey.find_program_address(
            [bytes(owner_key), bytes(token_program), bytes(mint)],
            ata_program,
        )
        return str(ata)

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise TransactionBuildFailed("JUPITER_API_KEY is required for trending live execution")
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "User-Agent": "MemecoinTrendingExecutor/1.0",
        }

    def _get_json(self, url: str, *, params: dict | None = None) -> dict:
        try:
            response = self.session.get(
                url, params=params, headers=self._headers(), timeout=self.timeout
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise TransactionBuildFailed(
                f"Jupiter order/metadata request failed before submission: {type(exc).__name__}"
            ) from exc
        except ValueError as exc:
            raise TransactionBuildFailed("Jupiter returned invalid JSON before submission") from exc
        if not isinstance(payload, dict):
            raise TransactionBuildFailed("Jupiter response was not a JSON object")
        return payload

    def _dex_program_labels(self) -> set[str]:
        now = time.time()
        if self._program_labels and now - self._program_labels_at < 3600:
            return set(self._program_labels)
        payload = self._get_json(PROGRAM_LABELS_URL)
        labels = set(payload.keys())
        if not labels:
            raise TransactionBuildFailed(
                "Jupiter DEX program allowlist was empty; refusing to sign an unreviewed route"
            )
        self._program_labels = labels
        self._program_labels_at = now
        return set(labels)

    @staticmethod
    def _validate_order_fields(
        order: dict,
        *,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        max_price_impact_pct: float | None,
        expected_output_value_usd: float | None,
        input_value_usd: float | None,
        output_decimals: int,
    ) -> int:
        if order.get("errorCode") not in (None, "", 0, "0"):
            detail = str(order.get("errorMessage") or order.get("errorCode") or "route unavailable")
            raise TransactionBuildFailed(f"Jupiter order rejected: {detail[:250]}")
        # Swap API V2's order schema may omit echoed input/output fields; when
        # present, they must match exactly. The transaction's own SOL funding
        # instruction is separately inspected before signing.
        if order.get("inputMint") not in (None, input_mint) or order.get("outputMint") not in (None, output_mint):
            raise TransactionBuildFailed("Jupiter order mint mismatch; refusing to sign")
        try:
            input_echo = order.get("inAmount")
            in_amount = int(input_echo) if input_echo not in (None, "") else amount_raw
            out_amount = int(order.get("outAmount") or 0)
            threshold_raw = order.get("otherAmountThreshold")
            threshold = int(threshold_raw) if threshold_raw not in (None, "") else None
        except (TypeError, ValueError, OverflowError) as exc:
            raise TransactionBuildFailed("Jupiter returned malformed order amounts") from exc
        if in_amount != amount_raw or in_amount <= 0:
            raise TransactionBuildFailed("Jupiter input amount differs from the requested hard cap")
        if out_amount <= 0:
            raise TransactionBuildFailed("Jupiter order has no valid positive output amount")
        # Swap V2's Order & Execute response need not expose otherAmountThreshold;
        # the route's RTSE-protected transaction enforces its on-chain minimum.
        if threshold is not None and (threshold <= 0 or threshold > out_amount):
            raise TransactionBuildFailed("Jupiter order returned an invalid minimum output")
        if not order.get("transaction") or not order.get("requestId"):
            raise TransactionBuildFailed("Jupiter order omitted the transaction or request ID")

        if max_price_impact_pct is not None:
            impact = _finite_float(order.get("priceImpactPct"))
            if impact is not None:
                # Jupiter expresses priceImpactPct as a fraction (0.01 == 1%).
                if impact < 0 or impact > 1:
                    raise TransactionBuildFailed("Jupiter returned an invalid price-impact value")
                impact_pct = impact * 100.0
            else:
                impact_pct = None
            if impact_pct is not None and impact_pct > max_price_impact_pct:
                raise TransactionBuildFailed(
                    f"Jupiter price impact {impact_pct:.2f}% exceeds the "
                    f"{max_price_impact_pct:.2f}% entry limit"
                )
            # Independently compare the quoted output to the current same-pool USD
            # mark. This check remains mandatory even when the API omits priceImpactPct.
            if (
                expected_output_value_usd is None
                or input_value_usd is None
                or input_value_usd <= 0
                or expected_output_value_usd < 0
            ):
                raise TransactionBuildFailed(
                    "No reliable independent USD mark is available for impact verification"
                )
            implied_loss_pct = max(
                0.0, (input_value_usd - expected_output_value_usd) / input_value_usd * 100.0
            )
            # The order's output-value proxy is intentionally a second hard check.
            # A small tolerance allows slight mark/quote timing differences.
            if implied_loss_pct > max_price_impact_pct + 2.0:
                raise TransactionBuildFailed(
                    f"Quoted output is {implied_loss_pct:.2f}% below the independent market mark"
                )
        if output_decimals < 0 or output_decimals > 18:
            raise TransactionBuildFailed("Output token decimals are invalid")
        return out_amount

    def _resolved_account_keys(self, message) -> list[str]:
        """Resolve v0 address lookup tables so all route programs can be inspected."""
        static_keys = [str(key) for key in list(getattr(message, "account_keys", []) or [])]
        lookups = list(getattr(message, "address_table_lookups", []) or [])
        if not lookups:
            return static_keys

        writable_keys: list[str] = []
        readonly_keys: list[str] = []
        for lookup in lookups:
            table_key = str(getattr(lookup, "account_key", ""))
            if not table_key:
                raise TransactionBuildFailed("Transaction lookup table has no address")
            result = self.rpc.call(
                "getAccountInfo",
                [table_key, {"encoding": "base64", "commitment": "confirmed"}],
            ) or {}
            account = result.get("value") or {}
            if str(account.get("owner") or "") != ADDRESS_LOOKUP_TABLE_PROGRAM_ID:
                raise TransactionBuildFailed("Transaction references an account that is not an address lookup table")
            raw_data = account.get("data")
            if not isinstance(raw_data, list) or not raw_data:
                raise TransactionBuildFailed("Address lookup table data is unavailable")
            try:
                table_data = base64.b64decode(raw_data[0], validate=True)
            except Exception as exc:
                raise TransactionBuildFailed("Address lookup table returned invalid base64") from exc
            # AddressLookupTableState has a fixed 56-byte metadata prefix followed
            # by packed 32-byte public keys.
            if len(table_data) < 56 or (len(table_data) - 56) % 32:
                raise TransactionBuildFailed("Address lookup table has a malformed byte layout")
            addresses = [
                str(Pubkey.from_bytes(table_data[offset:offset + 32]))
                for offset in range(56, len(table_data), 32)
            ]
            for attribute, target in (
                ("writable_indexes", writable_keys),
                ("readonly_indexes", readonly_keys),
            ):
                indexes = list(getattr(lookup, attribute, []) or [])
                for index in indexes:
                    index = int(index)
                    if index < 0 or index >= len(addresses):
                        raise TransactionBuildFailed("Address lookup table index is out of range")
                    target.append(addresses[index])
        return static_keys + writable_keys + readonly_keys

    def _validate_transaction_instructions(
        self,
        *,
        instructions: list,
        keys: list[str],
        owner: str,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        allowed_programs: set[str],
    ) -> None:
        """Enforce explicit top-level programs, bounded priority fee and owner-only ATA creation."""
        wsol_ata = self._expected_wsol_ata(owner)
        saw_system_input = False
        system_input_lamports = 0
        compute_unit_limit = 1_400_000
        compute_unit_price_micro_lamports = 0
        saw_compute_limit = False
        saw_compute_price = False
        ata_creations = 0
        owner_key = Pubkey.from_string(owner)
        associated_program = Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM_ID)

        for instruction in instructions:
            program_index = int(instruction.program_id_index)
            if program_index >= len(keys):
                raise TransactionBuildFailed(
                    "Jupiter transaction has an unresolved program address; refusing to sign"
                )
            program_id = str(keys[program_index])
            accounts = list(bytes(instruction.accounts))
            data = bytes(instruction.data)
            if program_id not in allowed_programs:
                raise TransactionBuildFailed(
                    f"Jupiter transaction contains unrecognized program {program_id}; no signature made"
                )

            if program_id == COMPUTE_BUDGET_ID:
                if len(data) != 5:
                    if len(data) == 9 and data[0] == 3:
                        if saw_compute_price:
                            raise TransactionBuildFailed("Jupiter transaction repeats compute-unit price instructions")
                        compute_unit_price_micro_lamports = int.from_bytes(data[1:9], "little")
                        saw_compute_price = True
                        continue
                    raise TransactionBuildFailed("Jupiter transaction has an unsupported compute-budget instruction")
                tag = data[0]
                value = int.from_bytes(data[1:5], "little")
                if tag == 1:  # RequestHeapFrame
                    if not 32_768 <= value <= 262_144:
                        raise TransactionBuildFailed("Jupiter transaction requests an invalid heap-frame size")
                elif tag == 2:  # SetComputeUnitLimit
                    if saw_compute_limit:
                        raise TransactionBuildFailed("Jupiter transaction repeats compute-unit limit instructions")
                    if not 1 <= value <= 1_400_000:
                        raise TransactionBuildFailed("Jupiter transaction compute-unit limit exceeds the hard cap")
                    compute_unit_limit = value
                    saw_compute_limit = True
                elif tag == 4:  # SetLoadedAccountsDataSizeLimit
                    if not 1 <= value <= 64_000_000:
                        raise TransactionBuildFailed("Jupiter transaction loaded-account data size is invalid")
                else:
                    raise TransactionBuildFailed("Jupiter transaction has an unknown compute-budget instruction")
            elif program_id == SYSTEM_PROGRAM_ID:
                if len(data) != 12 or int.from_bytes(data[:4], "little") != 2 or len(accounts) < 2:
                    raise TransactionBuildFailed(
                        "Jupiter transaction contains a non-transfer System Program instruction"
                    )
                src_idx, dst_idx = accounts[0], accounts[1]
                if src_idx >= len(keys) or dst_idx >= len(keys):
                    raise TransactionBuildFailed("System transfer uses unresolved account keys")
                src, dst = str(keys[src_idx]), str(keys[dst_idx])
                lamports = int.from_bytes(data[4:12], "little")
                if (
                    input_mint != WSOL_MINT
                    or src != owner
                    or dst != wsol_ata
                    or lamports <= 0
                    or lamports > amount_raw
                ):
                    raise TransactionBuildFailed(
                        "Jupiter transaction attempts an unexpected SOL transfer; no signature made"
                    )
                system_input_lamports += lamports
                saw_system_input = True
            elif program_id in {TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID}:
                # Top-level token instructions are restricted to SyncNative and
                # CloseAccount for the wallet's WSOL ATA.
                if not data or data[0] not in {9, 17}:
                    raise TransactionBuildFailed(
                        "Jupiter transaction contains an unexpected top-level token instruction"
                    )
                if not accounts or accounts[0] >= len(keys) or str(keys[accounts[0]]) != wsol_ata:
                    raise TransactionBuildFailed(
                        "Jupiter transaction token instruction is not scoped to the wallet's WSOL ATA"
                    )
                if data[0] == 9:
                    if len(accounts) < 2 or accounts[1] >= len(keys) or str(keys[accounts[1]]) != owner:
                        raise TransactionBuildFailed(
                            "WSOL account close would not return lamports to the bot wallet"
                        )
            elif program_id == ASSOCIATED_TOKEN_PROGRAM_ID:
                # Only create the bot wallet's ATA for one of the two swap mints.
                # ATA rent comes from the wallet; other owners or unexpected mints
                # are out of scope for a $0.50 trending swap.
                if data not in {b"", b"\\x01"} or len(accounts) < 6:
                    raise TransactionBuildFailed("Jupiter transaction contains an unsupported associated-token instruction")
                if any(index >= len(keys) for index in accounts[:6]):
                    raise TransactionBuildFailed("Associated-token instruction references unresolved account keys")
                payer = str(keys[accounts[0]])
                ata = str(keys[accounts[1]])
                ata_owner = str(keys[accounts[2]])
                mint = str(keys[accounts[3]])
                system_program = str(keys[accounts[4]])
                token_program = str(keys[accounts[5]])
                if (
                    payer != owner
                    or ata_owner != owner
                    or system_program != SYSTEM_PROGRAM_ID
                    or token_program not in {TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID}
                    or mint not in {input_mint, output_mint}
                ):
                    raise TransactionBuildFailed("Associated-token instruction is not limited to the bot's swap accounts")
                mint_key = Pubkey.from_string(mint)
                token_program_key = Pubkey.from_string(token_program)
                expected_ata, _ = Pubkey.find_program_address(
                    [bytes(owner_key), bytes(token_program_key), bytes(mint_key)],
                    associated_program,
                )
                if ata != str(expected_ata):
                    raise TransactionBuildFailed("Associated-token instruction targets a non-canonical wallet ATA")
                ata_creations += 1
                if ata_creations > 2:
                    raise TransactionBuildFailed("Jupiter route would create more than two token accounts; rent risk exceeds policy")

        # Jupiter's route may omit threshold fields in its V2 JSON response, but the
        # signed transaction must still contain an on-chain spend cap.
        if input_mint == WSOL_MINT and (
            not saw_system_input or system_input_lamports != amount_raw
        ):
            raise TransactionBuildFailed(
                "Jupiter buy transaction does not transfer exactly the bounded SOL input"
            )
        estimated_priority_fee = (
            compute_unit_price_micro_lamports * compute_unit_limit + 999_999
        ) // 1_000_000
        if estimated_priority_fee > 1_000_000:
            raise TransactionBuildFailed(
                "Jupiter compute-budget priority fee exceeds the 0.001 SOL hard cap"
            )

    def _validate_and_sign(
        self,
        transaction_b64: str,
        *,
        owner: str,
        keypair: Keypair,
        input_mint: str,
        amount_raw: int,
        allowed_dex_programs: set[str],
    ) -> tuple[VersionedTransaction, str]:
        try:
            raw = base64.b64decode(transaction_b64, validate=True)
            unsigned = VersionedTransaction.from_bytes(raw)
        except Exception as exc:
            raise TransactionBuildFailed("Jupiter order contained an invalid serialized transaction") from exc

        message = unsigned.message
        header = getattr(message, "header", None)
        required_signers = int(getattr(header, "num_required_signatures", 0) or 0)
        try:
            keys = self._resolved_account_keys(message)
        except TransactionBuildFailed:
            raise
        except Exception as exc:
            raise TransactionBuildFailed(
                f"Could not resolve transaction account lookup tables ({type(exc).__name__})"
            ) from exc
        if required_signers != 1 or not keys or str(keys[0]) != owner:
            raise TransactionBuildFailed(
                "Jupiter route must require only the configured bot wallet signer; "
                "RFQ/extra-signer transactions are not supported by this executor"
            )
        if str(keypair.pubkey()) != owner:
            raise TransactionBuildFailed("Configured keypair does not match the bot wallet")

        allowed_programs = CORE_PROGRAMS | allowed_dex_programs
        instructions = list(getattr(message, "instructions", []) or [])
        if not instructions:
            raise TransactionBuildFailed("Jupiter transaction has no instructions")
        self._validate_transaction_instructions(
            instructions=instructions,
            keys=keys,
            owner=owner,
            input_mint=input_mint,
            output_mint=output_mint,
            amount_raw=amount_raw,
            allowed_programs=allowed_programs,
        )

        try:
            signed = VersionedTransaction(message, [keypair])
            signed.verify()
        except Exception as exc:
            raise TransactionBuildFailed("Could not sign/verify the Jupiter transaction") from exc
        if len(signed.signatures) != 1:
            raise TransactionBuildFailed("Signed Jupiter transaction has an unexpected signer count")
        signature = str(signed.signatures[0])
        return signed, signature

    def swap(
        self,
        *,
        owner: str,
        keypair: Keypair,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        action: str,
        output_decimals: int,
        expected_price_usd: float | None = None,
        expected_output_value_usd: float | None = None,
        input_value_usd: float | None = None,
        max_price_impact_pct: float | None = None,
    ) -> dict:
        if not self.api_key:
            raise TransactionBuildFailed("Set JUPITER_API_KEY locally before enabling trending live trades")
        try:
            Pubkey.from_string(owner)
            Pubkey.from_string(input_mint)
            Pubkey.from_string(output_mint)
        except Exception as exc:
            raise TransactionBuildFailed("Invalid wallet or mint address") from exc
        if input_mint == output_mint or amount_raw <= 0:
            raise TransactionBuildFailed("Invalid swap input/output mint or amount")
        if (input_mint == WSOL_MINT) != (action == "buy"):
            raise TransactionBuildFailed("Swap direction does not match the requested trade action")
        if input_mint == WSOL_MINT and amount_raw > 50_000_000:
            raise TransactionBuildFailed("Trending live buy exceeds the hard 0.05 SOL transaction input ceiling")
        if output_mint != WSOL_MINT and action != "buy":
            raise TransactionBuildFailed("Trending live sell must return to wrapped SOL")
        if output_mint == WSOL_MINT and action != "sell":
            raise TransactionBuildFailed("Only trending live sells may output wrapped SOL")

        order = self._get_json(
            f"{JUPITER_SWAP_V2_BASE}/order",
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(int(amount_raw)),
                "taker": owner,
                # RFQ routes may require a second market-maker signature which
                # this one-keypair executor deliberately does not support.
                "excludeRouters": "jupiterz",
            },
        )
        if expected_output_value_usd is None and action == "buy" and expected_price_usd is not None:
            try:
                quoted_out = int(order.get("outAmount") or 0)
            except (TypeError, ValueError, OverflowError):
                quoted_out = 0
            if quoted_out > 0 and output_decimals >= 0:
                expected_output_value_usd = (quoted_out / (10 ** output_decimals)) * float(expected_price_usd)
        if str(order.get("router") or "").strip().lower() == "jupiterz":
            raise TransactionBuildFailed(
                "Jupiter selected an RFQ route requiring an extra signer; no transaction submitted"
            )
        out_amount = self._validate_order_fields(
            order,
            input_mint=input_mint,
            output_mint=output_mint,
            amount_raw=int(amount_raw),
            max_price_impact_pct=max_price_impact_pct,
            expected_output_value_usd=expected_output_value_usd,
            input_value_usd=input_value_usd,
            output_decimals=output_decimals,
        )
        allowed_dex_programs = self._dex_program_labels()
        signed, signature = self._validate_and_sign(
            str(order["transaction"]),
            owner=owner,
            keypair=keypair,
            input_mint=input_mint,
            amount_raw=int(amount_raw),
            allowed_dex_programs=allowed_dex_programs,
        )

        try:
            simulation = self.rpc.call(
                "simulateTransaction",
                [
                    base64.b64encode(bytes(signed)).decode("ascii"),
                    {"encoding": "base64", "sigVerify": True, "commitment": "processed"},
                ],
            )
        except Exception as exc:
            raise TransactionSimulationFailed(
                f"Jupiter transaction simulation unavailable before submission: {type(exc).__name__}",
                retryable=False,
            ) from exc
        sim_value = (simulation or {}).get("value") or {}
        if sim_value.get("err") is not None:
            detail = f"Jupiter route simulation failed: {sim_value.get('err')}; logs={sim_value.get('logs') or []}"
            raise TransactionSimulationFailed(detail[:1000], retryable=False)

        try:
            response = self.session.post(
                f"{JUPITER_SWAP_V2_BASE}/execute",
                json={
                    "signedTransaction": base64.b64encode(bytes(signed)).decode("ascii"),
                    "requestId": str(order["requestId"]),
                },
                headers=self._headers(),
                timeout=self.timeout,
            )
            response.raise_for_status()
            result = response.json()
        except Exception as exc:
            # A request failure after signed bytes may have reached Jupiter is
            # ambiguous. Never issue a second order until the signature is checked.
            raise TransactionOutcomeUncertain(
                signature, action, output_mint if action == "buy" else input_mint,
                f"Jupiter execute response unavailable; inspect signature before retry ({type(exc).__name__})",
            ) from exc
        if not isinstance(result, dict):
            raise TransactionOutcomeUncertain(
                signature, action, output_mint if action == "buy" else input_mint,
                "Jupiter execute returned a non-object response after submission",
            )
        status = str(result.get("status") or "").strip().lower()
        result_signature = str(result.get("signature") or "")
        mint_for_error = output_mint if action == "buy" else input_mint
        if status == "failed":
            raise TransactionFailedOnChain(
                result_signature or signature, action, mint_for_error,
                str(result.get("error") or result.get("errorMessage") or "Jupiter reports failed execution")[:500],
            )
        if status != "success" or (result_signature and result_signature != signature):
            raise TransactionOutcomeUncertain(
                signature, action, mint_for_error,
                f"Jupiter execute status/signature could not be verified: status={status or 'missing'}",
            )

        try:
            confirmed_tx = self.rpc.transaction(signature)
        except Exception as exc:
            raise TransactionOutcomeUncertain(
                signature, action, mint_for_error,
                f"Jupiter reported success but RPC confirmation check failed ({type(exc).__name__})",
            ) from exc
        if not confirmed_tx:
            raise TransactionOutcomeUncertain(
                signature, action, mint_for_error,
                "Jupiter reported success but transaction is not yet visible in configured RPC",
            )
        meta = confirmed_tx.get("meta") or {}
        if meta.get("err") is not None:
            raise TransactionFailedOnChain(
                signature, action, mint_for_error, str(meta.get("err"))[:500]
            )
        return {
            "signature": signature,
            "input_amount_raw": int(amount_raw),
            "quoted_output_raw": int(out_amount),
            "execute_result": result,
            "order": order,
        }
