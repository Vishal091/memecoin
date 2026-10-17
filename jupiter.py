"""Jupiter Swap API V2 adapter. Private keys stay in a local keypair file."""
from __future__ import annotations
import base64
import json
import logging
from pathlib import Path
from typing import Any

import requests
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

logger = logging.getLogger(__name__)
WSOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_BASE = "https://api.jup.ag/swap/v2"


class JupiterExecutor:
    def __init__(self, api_key: str, timeout: int = 12):
        self.api_key = api_key
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"x-api-key": api_key, "accept": "application/json"})

    def load_keypair(self, path: Path) -> Keypair:
        # Solana CLI keypair JSON file is a JSON array of 64 bytes.
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, list) or len(raw) not in (32, 64):
                raise ValueError("Expected a Solana CLI keypair JSON array of 32 or 64 bytes")
            return Keypair.from_bytes(bytes(raw)) if len(raw) == 64 else Keypair.from_seed(bytes(raw))
        except Exception as exc:
            raise ValueError(f"Could not load local keypair file {path}: {exc}") from exc

    def order(self, *, input_mint: str, output_mint: str, amount_raw: int,
              taker: str, slippage_bps: int) -> dict[str, Any]:
        params = {
            "inputMint": input_mint, "outputMint": output_mint, "amount": str(amount_raw),
            "taker": taker, "slippageBps": str(slippage_bps),
        }
        r = self.session.get(f"{JUPITER_BASE}/order", params=params, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        if not data.get("transaction") or not data.get("requestId"):
            raise RuntimeError(f"Jupiter order response missing transaction/requestId: {data}")
        return data

    def execute(self, order: dict[str, Any], keypair: Keypair) -> dict[str, Any]:
        try:
            raw_tx = base64.b64decode(order["transaction"])
            unsigned = VersionedTransaction.from_bytes(raw_tx)
            message = unsigned.message
            header = message.header
            signer_keys = list(message.account_keys[:header.num_required_signatures])
            if str(keypair.pubkey()) not in {str(k) for k in signer_keys}:
                raise RuntimeError("Jupiter transaction does not request a signature from the configured keypair")
            # This refuses transactions requiring unknown additional signer(s).
            if len(signer_keys) != 1:
                raise RuntimeError(f"Refusing transaction requiring {len(signer_keys)} signers; expected one wallet signer")
            signed = VersionedTransaction(message, [keypair])
            encoded = base64.b64encode(bytes(signed)).decode("ascii")
        except Exception as exc:
            raise RuntimeError(f"Could not safely validate/sign Jupiter transaction: {exc}") from exc

        payload = {"signedTransaction": encoded, "requestId": order["requestId"]}
        r = self.session.post(f"{JUPITER_BASE}/execute", json=payload, timeout=self.timeout)
        r.raise_for_status()
        result = r.json()
        status = str(result.get("status", "")).lower()
        if status and status not in {"success", "confirmed"}:
            raise RuntimeError(f"Jupiter execution did not confirm success: {result}")
        signature = result.get("signature") or result.get("txid")
        if not signature:
            raise RuntimeError(f"Jupiter execution response has no transaction signature: {result}")
        return {"signature": signature, "response": result}

    def swap(self, *, input_mint: str, output_mint: str, amount_raw: int,
             taker: str, keypair: Keypair, slippage_bps: int) -> dict[str, Any]:
        order = self.order(input_mint=input_mint, output_mint=output_mint, amount_raw=amount_raw,
                           taker=taker, slippage_bps=slippage_bps)
        result = self.execute(order, keypair)
        return {
            "signature": result["signature"],
            "in_amount_raw": int(order.get("inAmount") or amount_raw),
            "out_amount_raw": int(order.get("outAmount") or 0),
            "price_impact_pct": float(order.get("priceImpactPct") or 0),
            "request_id": order["requestId"],
        }
