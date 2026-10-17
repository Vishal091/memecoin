"""Small, rate-aware Solana JSON-RPC client and watched-wallet swap heuristics."""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)


class SolanaRPCRateLimited(RuntimeError):
    """Raised while the shared RPC endpoint is rate-limiting requests."""


class SolanaTransactionNotReady(RuntimeError):
    """Raised when a confirmed signature is visible but its transaction is not indexed yet."""


class SolanaRPC:
    def __init__(self, url: str, timeout: int = 12):
        self.url, self.timeout = url, timeout
        self.session = requests.Session()
        self._request_id = 0
        self.max_supported_transaction_version = max(
            0, int(os.getenv("SOLANA_MAX_SUPPORTED_TRANSACTION_VERSION", "1"))
        )
        self.min_request_interval = max(
            0.0, min(2.0, int(os.getenv("SOLANA_RPC_MIN_INTERVAL_MS", "300")) / 1000.0)
        )
        self.rate_limit_cooldown = max(
            10, min(180, int(os.getenv("SOLANA_RPC_429_COOLDOWN_SECONDS", "30")))
        )
        self._rate_limited_until = 0.0
        self._last_request_at = 0.0
        self._lock = threading.Lock()

    @property
    def backoff_remaining(self) -> float:
        return max(0.0, self._rate_limited_until - time.monotonic())

    def call(self, method: str, params: list[Any]) -> Any:
        with self._lock:
            remaining = self.backoff_remaining
            if remaining > 0:
                raise SolanaRPCRateLimited(
                    f"Solana RPC is cooling down for {remaining:.0f}s after HTTP 429; "
                    "use a dedicated RPC endpoint or wait for the cooldown."
                )
            wait_for_spacing = self.min_request_interval - (time.monotonic() - self._last_request_at)
            if wait_for_spacing > 0:
                time.sleep(wait_for_spacing)
            self._request_id += 1
            request_id = self._request_id
            self._last_request_at = time.monotonic()

        try:
            response = self.session.post(
                self.url,
                json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                timeout=self.timeout,
            )
        except requests.RequestException:
            raise

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "")
            try:
                wait_seconds = float(retry_after)
            except (TypeError, ValueError):
                wait_seconds = 0.0
            cooldown = max(self.rate_limit_cooldown, min(180.0, wait_seconds))
            with self._lock:
                self._rate_limited_until = time.monotonic() + cooldown
            logger.warning(
                "Solana RPC HTTP 429 on %s; pausing RPC calls for %.0fs. "
                "The public mainnet RPC is rate-limited; configure a dedicated RPC URL in .env.",
                method, cooldown,
            )
            raise SolanaRPCRateLimited(
                f"Solana RPC rate-limited {method} (HTTP 429); paused for {cooldown:.0f}s. "
                "Configure a dedicated RPC endpoint in .env."
            )

        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError(f"Solana RPC {method} error: {payload['error']}")
        return payload.get("result")

    def signatures(self, address: str, limit: int = 20) -> list[dict]:
        return self.call(
            "getSignaturesForAddress",
            [address, {"limit": limit, "commitment": "confirmed"}],
        ) or []

    def transaction(self, signature: str) -> dict | None:
        # Mainnet has activated transaction version 1; requests sent with version 0
        # fail for those transactions. Keep it configurable for future upgrades.
        config = {
            "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": self.max_supported_transaction_version,
            "commitment": "confirmed",
        }
        for attempt in range(2):
            tx = self.call("getTransaction", [signature, config])
            if tx is not None:
                return tx
            if attempt == 0:
                time.sleep(0.5)
        return None

    def sol_balance_lamports(self, address: str) -> int:
        result = self.call("getBalance", [address, {"commitment": "confirmed"}])
        return int((result or {}).get("value", 0))

    @staticmethod
    def _key_string(key: Any) -> str:
        if isinstance(key, str):
            return key
        if isinstance(key, dict):
            return str(key.get("pubkey", ""))
        return str(key)

    def wallet_token_deltas(self, wallet: str, tx: dict) -> tuple[dict[str, dict], int]:
        meta = tx.get("meta") or {}
        if meta.get("err") is not None:
            return {}, 0
        message = ((tx.get("transaction") or {}).get("message") or {})
        keys = [self._key_string(k) for k in message.get("accountKeys", [])]
        try:
            wallet_index = keys.index(wallet)
        except ValueError:
            return {}, 0
        pre_sol, post_sol = meta.get("preBalances") or [], meta.get("postBalances") or []
        native_delta = (
            int(post_sol[wallet_index]) - int(pre_sol[wallet_index])
            if wallet_index < len(pre_sol) and wallet_index < len(post_sol) else 0
        )
        pre, post = {}, {}
        for row in meta.get("preTokenBalances") or []:
            if row.get("owner") == wallet:
                pre[row["mint"]] = row
        for row in meta.get("postTokenBalances") or []:
            if row.get("owner") == wallet:
                post[row["mint"]] = row
        deltas = {}
        for mint in set(pre) | set(post):
            arow, brow = pre.get(mint, {}), post.get(mint, {})
            a = int((arow.get("uiTokenAmount") or {}).get("amount", "0"))
            b = int((brow.get("uiTokenAmount") or {}).get("amount", "0"))
            amount_info = brow.get("uiTokenAmount") or arow.get("uiTokenAmount") or {}
            decimals = int(amount_info.get("decimals", 0))
            delta = b - a
            if delta:
                deltas[mint] = {
                    "delta_raw": delta,
                    "decimals": decimals,
                    "delta_ui": delta / (10 ** decimals),
                }
        return deltas, native_delta

    def detect_wallet_signal(self, wallet: str, signature: str) -> list[dict]:
        tx = self.transaction(signature)
        if not tx:
            return []
        deltas, native_delta = self.wallet_token_deltas(wallet, tx)
        sol_delta = native_delta / 1_000_000_000
        wsol_mint = "So11111111111111111111111111111111111111112"
        stablecoin_mints = {
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
        }
        non_wsol = {m: d for m, d in deltas.items() if m != wsol_mint}
        quote_deltas = {m: d for m, d in non_wsol.items() if m in stablecoin_mints}
        assets = {m: d for m, d in non_wsol.items() if m not in stablecoin_mints}
        if not assets:
            return []
        added = [(m, d) for m, d in assets.items() if d["delta_raw"] > 0]
        removed = [(m, d) for m, d in assets.items() if d["delta_raw"] < 0]
        stablecoin_spent = any(d["delta_raw"] < 0 for d in quote_deltas.values())
        stablecoin_received = any(d["delta_raw"] > 0 for d in quote_deltas.values())

        if len(added) == 1 and not removed and (sol_delta < -0.001 or stablecoin_spent):
            mint, data = added[0]
            side = "buy"
        elif len(removed) == 1 and not added and (sol_delta > 0.001 or stablecoin_received):
            mint, data = removed[0]
            side = "sell"
        else:
            return []
        return [{
            "wallet": wallet,
            "signature": signature,
            "mint": mint,
            "side": side,
            "amount_ui": abs(float(data["delta_ui"])),
            "amount_raw": abs(int(data["delta_raw"])),
            "decimals": int(data["decimals"]),
            "native_delta_sol": sol_delta,
            "block_time": tx.get("blockTime"),
        }]
