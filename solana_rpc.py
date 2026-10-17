"""Small Solana JSON-RPC client and watched-wallet swap heuristics."""
from __future__ import annotations
import logging, time
from typing import Any
import requests

logger = logging.getLogger(__name__)

class SolanaRPC:
    def __init__(self, url: str, timeout: int = 12):
        self.url, self.timeout = url, timeout
        self.session = requests.Session()
        self._request_id = 0

    def call(self, method: str, params: list[Any]) -> Any:
        self._request_id += 1
        r = self.session.post(self.url, json={"jsonrpc":"2.0","id":self._request_id,"method":method,"params":params}, timeout=self.timeout)
        r.raise_for_status()
        payload = r.json()
        if payload.get("error"):
            raise RuntimeError(f"Solana RPC {method} error: {payload['error']}")
        return payload.get("result")

    def signatures(self, address: str, limit: int = 20) -> list[dict]:
        return self.call("getSignaturesForAddress", [address, {"limit":limit,"commitment":"confirmed"}]) or []

    def transaction(self, signature: str) -> dict | None:
        for attempt in range(3):
            tx = self.call("getTransaction", [signature, {"encoding":"jsonParsed","maxSupportedTransactionVersion":0,"commitment":"confirmed"}])
            if tx is not None: return tx
            if attempt < 2: time.sleep(0.5 * (attempt + 1))
        return None

    def sol_balance_lamports(self, address: str) -> int:
        result = self.call("getBalance", [address, {"commitment":"confirmed"}])
        return int((result or {}).get("value", 0))

    @staticmethod
    def _key_string(key: Any) -> str:
        if isinstance(key, str): return key
        if isinstance(key, dict): return str(key.get("pubkey", ""))
        return str(key)

    def wallet_token_deltas(self, wallet: str, tx: dict) -> tuple[dict[str, dict], int]:
        meta = tx.get("meta") or {}
        if meta.get("err") is not None: return {}, 0
        message = ((tx.get("transaction") or {}).get("message") or {})
        keys = [self._key_string(k) for k in message.get("accountKeys", [])]
        try: wallet_index = keys.index(wallet)
        except ValueError: return {}, 0
        pre_sol, post_sol = meta.get("preBalances") or [], meta.get("postBalances") or []
        native_delta = int(post_sol[wallet_index])-int(pre_sol[wallet_index]) if wallet_index < len(pre_sol) and wallet_index < len(post_sol) else 0
        pre, post = {}, {}
        for row in meta.get("preTokenBalances") or []:
            if row.get("owner") == wallet: pre[row["mint"]] = row
        for row in meta.get("postTokenBalances") or []:
            if row.get("owner") == wallet: post[row["mint"]] = row
        deltas = {}
        for mint in set(pre) | set(post):
            arow, brow = pre.get(mint, {}), post.get(mint, {})
            a = int((arow.get("uiTokenAmount") or {}).get("amount", "0"))
            b = int((brow.get("uiTokenAmount") or {}).get("amount", "0"))
            amount_info = brow.get("uiTokenAmount") or arow.get("uiTokenAmount") or {}
            decimals = int(amount_info.get("decimals", 0))
            delta = b-a
            if delta:
                deltas[mint] = {"delta_raw":delta,"decimals":decimals,"delta_ui":delta/(10**decimals)}
        return deltas, native_delta

    def detect_wallet_signal(self, wallet: str, signature: str) -> list[dict]:
        tx = self.transaction(signature)
        if not tx: return []
        deltas, native_delta = self.wallet_token_deltas(wallet, tx)
        sol_delta = native_delta / 1_000_000_000
        # Recognize simple SOL or canonical USDC/USDT-funded swaps. Token-to-token
        # trades between volatile assets are ambiguous and deliberately ignored.
        wsol_mint = "So11111111111111111111111111111111111111112"
        stablecoin_mints = {
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # native USDC
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # native USDT
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
            mint, d = added[0]
            side = "buy"
        elif len(removed) == 1 and not added and (sol_delta > 0.001 or stablecoin_received):
            mint, d = removed[0]
            side = "sell"
        else:
            return []
        return [{"wallet":wallet,"signature":signature,"mint":mint,"side":side,
                 "amount_ui":abs(float(d["delta_ui"])),"amount_raw":abs(int(d["delta_raw"])),
                 "decimals":int(d["decimals"]),"native_delta_sol":sol_delta,"block_time":tx.get("blockTime")}]
