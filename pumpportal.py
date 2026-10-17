"""PumpPortal Local Transaction API adapter for Pump.fun/PumpSwap."""
import base64
import json
import logging
import time
from pathlib import Path
import requests
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PUMPPORTAL_TRADE_URL = "https://pumpportal.fun/api/trade-local"
VENUE_PROGRAMS = {"pump": PUMP_PROGRAM_ID, "pump-amm": PUMPSWAP_PROGRAM_ID}
logger = logging.getLogger(__name__)


class PumpPortalExecutor:
    """Build a Pump ecosystem transaction, locally sign it, submit via configured RPC."""

    def __init__(self, rpc, timeout=12, priority_fee_sol=0.00001):
        self.rpc = rpc
        self.timeout = timeout
        self.priority_fee_sol = priority_fee_sol
        self.http = requests.Session()

    @staticmethod
    def load_keypair(path: Path) -> Keypair:
        """Load a standard Solana CLI JSON keypair locally; never commit the file."""
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or len(raw) != 64:
            raise ValueError("Expected a standard Solana CLI keypair JSON array of 64 bytes")
        return Keypair.from_bytes(bytes(raw))

    def sol_balance_lamports(self, owner):
        result = self.rpc.call("getBalance", [owner, {"commitment":"confirmed"}]) or {}
        return int(result.get("value", 0))

    def token_balance_raw(self, owner, mint):
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

    def _build(self, owner, mint, action, amount, denominated_in_sol, slippage_bps, pool, keypair):
        if pool not in VENUE_PROGRAMS:
            raise ValueError("Only Pump.fun bonding curve and PumpSwap pools are allowed")
        body = {
            "publicKey": owner, "action": action, "mint": mint, "amount": amount,
            "denominatedInSol": "true" if denominated_in_sol else "false",
            "slippage": slippage_bps / 100.0, "priorityFee": self.priority_fee_sol, "pool": pool,
        }
        response = self.http.post(PUMPPORTAL_TRADE_URL, json=body, timeout=self.timeout)
        if response.status_code != 200:
            # Keep the response body visible for diagnosis, but bound its size so
            # an upstream HTML error page cannot flood the bot log.
            body = " ".join((response.text or "").split())[:500]
            raise RuntimeError(
                f"PumpPortal {pool} build failed HTTP {response.status_code}: "
                f"{body or '(empty response body)'}"
            )
        unsigned = VersionedTransaction.from_bytes(response.content)
        message = unsigned.message
        signers = list(message.account_keys[:message.header.num_required_signatures])
        if len(signers) != 1 or str(signers[0]) != owner:
            raise RuntimeError("Unexpected signer list; refusing to sign")
        if VENUE_PROGRAMS[pool] not in {str(k) for k in message.account_keys}:
            raise RuntimeError("Transaction does not contain the requested Pump venue program")
        return VersionedTransaction(message, [keypair])

    def _send_confirm(self, tx):
        encoded = base64.b64encode(bytes(tx)).decode("ascii")
        sig = self.rpc.call("sendTransaction", [
            encoded, {"encoding": "base64", "preflightCommitment": "confirmed", "maxRetries": 3}
        ])
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            result = self.rpc.call("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}]) or {}
            statuses = result.get("value") or []
            status = statuses[0] if statuses else None
            if status:
                if status.get("err") is not None:
                    raise RuntimeError(f"Transaction failed on-chain: {status['err']}")
                if status.get("confirmationStatus") in {"confirmed", "finalized"}:
                    return sig
            time.sleep(0.7)
        raise TimeoutError(f"Confirmation timeout for {sig}; do not retry before checking signature")

    def trade(self, owner, keypair, mint, action, amount, denominated_in_sol, slippage_bps, preferred_pool):
        build_errors = []
        pools = [preferred_pool] + [p for p in ("pump", "pump-amm") if p != preferred_pool]
        for pool in pools:
            token_before = self.token_balance_raw(owner, mint)
            sol_before = self.sol_balance_lamports(owner)
            try:
                tx = self._build(owner, mint, action, amount, denominated_in_sol, slippage_bps, pool, keypair)
            except Exception as exc:
                detail = f"{pool}: {type(exc).__name__}: {exc}"
                build_errors.append(detail)
                logger.warning("PumpPortal route build failed mint=%s pool=%s error=%s", mint, pool, exc)
                continue
            # Do not catch submission/confirmation exceptions here. Once a signed
            # transaction might have been sent, trying another route could duplicate
            # a live trade when the first transaction's status is ambiguous.
            signature = self._send_confirm(tx)
            deadline = time.monotonic() + 8
            token_after = token_before
            sol_after = sol_before
            while time.monotonic() < deadline:
                token_after = self.token_balance_raw(owner, mint)
                sol_after = self.sol_balance_lamports(owner)
                delta = token_after - token_before
                if (action == "buy" and delta > 0) or (action == "sell" and delta < 0):
                    break
                time.sleep(0.5)
            delta = token_after - token_before
            sol_delta = sol_after - sol_before
            if action == "buy" and delta <= 0:
                raise RuntimeError(f"Buy {signature} confirmed but token balance did not increase; reconcile manually")
            if action == "sell" and delta >= 0:
                raise RuntimeError(f"Sell {signature} confirmed but token balance did not decrease; reconcile manually")
            return {"signature": signature, "pool": pool, "token_before_raw": token_before,
                    "token_after_raw": token_after, "token_delta_raw": delta,
                    "sol_delta_lamports": sol_delta,
                    "total_input_amount": max(0, -sol_delta) if action == "buy" else abs(delta),
                    "total_output_amount": delta if action == "buy" else max(0, sol_delta)}
        details = " | ".join(build_errors) if build_errors else "no build attempts recorded"
        raise RuntimeError(
            f"No Pump ecosystem route for {mint}; no transaction was submitted. "
            f"Build attempts: {details}"
        )
