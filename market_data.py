"""Public market-data helpers (Dexscreener) and market sanity filters."""
from __future__ import annotations
import time
import requests

WSOL_MINT = "So11111111111111111111111111111111111111112"

class MarketData:
    BASE = "https://api.dexscreener.com"
    def __init__(self, timeout: int = 12, min_liquidity_usd: float = 5000, min_pair_age_seconds: int = 90):
        self.timeout, self.min_liquidity_usd, self.min_pair_age_seconds = timeout, min_liquidity_usd, min_pair_age_seconds
        self.session = requests.Session()
        self.sol_price_cache: tuple[float,float] | None = None

    def token_pairs(self, mint: str) -> list[dict]:
        r = self.session.get(f"{self.BASE}/latest/dex/tokens/{mint}", timeout=self.timeout)
        if r.status_code == 404: return []
        r.raise_for_status()
        return [p for p in (r.json().get("pairs") or []) if p.get("chainId") == "solana"]

    def best_pair(self, mint: str) -> dict | None:
        pairs = self.token_pairs(mint)
        if not pairs: return None
        # Dexscreener's priceUsd is the USD price of the pair's base token.
        # Prefer a pair where the queried mint is base, to avoid treating a quote-token price as this token's price.
        base_pairs = [p for p in pairs if (p.get("baseToken") or {}).get("address") == mint]
        chosen = base_pairs or pairs
        chosen.sort(key=lambda p: float(((p.get("liquidity") or {}).get("usd")) or 0), reverse=True)
        return chosen[0]

    def token_market(self, mint: str) -> dict | None:
        pair = self.best_pair(mint)
        if not pair: return None
        try:
            price = float(pair.get("priceUsd") or 0)
            liquidity = float(((pair.get("liquidity") or {}).get("usd")) or 0)
            created_ms = int(pair.get("pairCreatedAt") or 0)
            age = max(0, int(time.time()-created_ms/1000)) if created_ms else 0
        except (TypeError, ValueError):
            return None
        # Never treat a quote-side token's base price as its own price.
        if (pair.get("baseToken") or {}).get("address") != mint or price <= 0:
            return None
        return {"mint":mint,"price_usd":price,"liquidity_usd":liquidity,"pair_age_seconds":age,
                "pair_address":pair.get("pairAddress"),"dex_id":pair.get("dexId"),"url":pair.get("url"),
                "base_token":pair.get("baseToken") or {},"quote_token":pair.get("quoteToken") or {},"raw":pair}

    def is_entry_eligible(self, market: dict | None) -> tuple[bool,str]:
        if not market: return False,"No Solana base-token market pair found"
        if market["liquidity_usd"] < self.min_liquidity_usd:
            return False, f"Liquidity too low ({market['liquidity_usd']:.0f} USD)"
        if market["pair_age_seconds"] < self.min_pair_age_seconds:
            return False, f"Pair too new ({market['pair_age_seconds']}s)"
        return True,"market filters passed"

    def sol_price_usd(self) -> float:
        now = time.time()
        if self.sol_price_cache and now-self.sol_price_cache[1] < 30: return self.sol_price_cache[0]
        pairs = self.token_pairs(WSOL_MINT)
        # We need priceUsd for SOL itself, so use only pairs where wrapped SOL is the base token.
        valid=[]
        for p in pairs:
            if (p.get("baseToken") or {}).get("address") != WSOL_MINT:
                continue
            price=float(p.get("priceUsd") or 0)
            liquidity=float(((p.get("liquidity") or {}).get("usd")) or 0)
            if price > 0: valid.append((liquidity,price))
        if not valid: raise RuntimeError("Could not determine SOL/USD price from a SOL-base market pair")
        valid.sort(reverse=True)
        self.sol_price_cache=(valid[0][1],now)
        return valid[0][1]
