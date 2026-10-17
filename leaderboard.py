"""Pump.fun leaderboard discovery.

Uses the current frontend PnL leaderboard endpoint observed from the Pump.fun
UI. Authentication uses the value of the auth_token cookie (not the full
Cookie header). The frontend endpoint is not a stable public API.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import requests
from solders.pubkey import Pubkey

logger = logging.getLogger(__name__)

WINDOWS = ("day", "week", "month")
DEFAULT_VALUES = {"day": "daily", "week": "weekly", "month": "monthly"}
WALLET_FIELDS = (
    "wallet", "walletAddress", "wallet_address", "publicKey", "public_key",
    "address", "userAddress", "user_address", "traderPublicKey",
    "trader_public_key", "ownerAddress", "owner", "account", "userId",
)


class LeaderboardError(RuntimeError):
    pass


def _walk_items(value: Any):
    """Find a list of leaderboard row objects in common nested API shapes."""
    if isinstance(value, list):
        if value and all(isinstance(item, dict) for item in value):
            yield value
            return
        for item in value:
            yield from _walk_items(item)
    elif isinstance(value, dict):
        preferred = (
            "traders", "leaderboard", "entries", "items", "results", "data",
            "users", "rows", "topTraders", "top_traders",
        )
        for key in preferred:
            if key in value:
                yield from _walk_items(value[key])
        # Some APIs wrap the list in one more object without a known property name.
        if not any(k in value for k in preferred):
            for child in value.values():
                yield from _walk_items(child)


def _find_wallet(row: dict) -> str | None:
    for key in WALLET_FIELDS:
        val = row.get(key)
        if isinstance(val, str):
            try:
                return str(Pubkey.from_string(val))
            except Exception:
                pass
        elif isinstance(val, dict):
            for nested in ("address", "wallet", "publicKey", "public_key", "id"):
                candidate = val.get(nested)
                if isinstance(candidate, str):
                    try:
                        return str(Pubkey.from_string(candidate))
                    except Exception:
                        pass
    for nested_key in ("user", "trader", "walletInfo", "profile", "accountInfo"):
        val = row.get(nested_key)
        if isinstance(val, dict):
            result = _find_wallet(val)
            if result:
                return result
    return None


def _find_profit(row: dict) -> float | None:
    keys = (
        "profit", "pnl", "netProfit", "net_profit", "realizedProfit",
        "realized_pnl", "realizedPnl", "totalProfit", "total_profit",
        "profitUsd", "profit_usd", "profitSol", "profit_sol", "score",
    )
    for key in keys:
        value = row.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.replace(",", "").replace("$", "").strip())
            except ValueError:
                continue
    return None


class PumpFunLeaderboard:
    def __init__(self, timeout: int = 12):
        self.timeout = timeout
        self.endpoint = os.getenv(
            "PUMPFUN_LEADERBOARD_URL",
            "https://frontend-api-v3.pump.fun/pnl-leaderboard",
        ).strip()
        # The browser request uses Cookie: auth_token=<JWT>. Keep backwards
        # compatibility with the former bearer-token configuration.
        self.auth_token = os.getenv("PUMPFUN_AUTH_TOKEN", "").strip()
        self.bearer_token = os.getenv("PUMPFUN_BEARER_TOKEN", "").strip()
        self.period_param = os.getenv("PUMPFUN_LEADERBOARD_PERIOD_PARAM", "period").strip()
        self.period_values = {
            "day": os.getenv("PUMPFUN_LEADERBOARD_DAY_VALUE", "daily").strip(),
            "week": os.getenv("PUMPFUN_LEADERBOARD_WEEK_VALUE", "weekly").strip(),
            "month": os.getenv("PUMPFUN_LEADERBOARD_MONTH_VALUE", "monthly").strip(),
        }
        self.sort_param = os.getenv("PUMPFUN_LEADERBOARD_SORT_PARAM", "sort").strip()
        self.sort_value = os.getenv("PUMPFUN_LEADERBOARD_SORT_VALUE", "combined").strip()
        self.fetch_limit = max(10, min(100, int(os.getenv("PUMPFUN_LEADERBOARD_LIMIT", "100"))))
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Origin": "https://pump.fun",
            "Referer": "https://pump.fun/leaderboard",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/155.0.0.0 Safari/537.36",
        })
        if self.auth_token:
            self.session.headers["Cookie"] = "auth_token=" + self.auth_token
        elif self.bearer_token:
            self.session.headers["Authorization"] = "Bearer " + self.bearer_token

    @property
    def ready(self) -> bool:
        return bool(self.endpoint and (self.auth_token or self.bearer_token))

    def _fetch(self, window: str) -> list[dict]:
        if window not in WINDOWS:
            raise ValueError(f"Unsupported Pump.fun leaderboard window: {window}")
        if not self.ready:
            raise LeaderboardError(
                "Automatic trader discovery needs PUMPFUN_AUTH_TOKEN in your local .env. "
                "Copy only the auth_token cookie value, not the full Cookie header. "
                "No wallet addresses are required."
            )
        params = {"limit": str(self.fetch_limit)}
        if self.period_param:
            params[self.period_param] = self.period_values[window]
        if self.sort_param:
            params[self.sort_param] = self.sort_value
        try:
            response = self.session.get(self.endpoint, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise LeaderboardError(f"Pump.fun leaderboard request failed: {exc}") from exc
        if response.status_code in (401, 403):
            raise LeaderboardError(
                "Pump.fun rejected the leaderboard request. Refresh PUMPFUN_AUTH_TOKEN locally "
                "from a fresh logged-in session or verify the endpoint/query settings."
            )
        if response.status_code == 429:
            raise LeaderboardError("Pump.fun leaderboard rate-limited the request; retry later.")
        if not response.ok:
            raise LeaderboardError(f"Pump.fun leaderboard returned HTTP {response.status_code}.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise LeaderboardError(
                "Leaderboard response was not JSON. The endpoint may have changed; update "
                "PUMPFUN_LEADERBOARD_URL using a currently supported Pump.fun endpoint."
            ) from exc

        candidates = []
        seen = set()
        for rows in _walk_items(payload):
            for rank, row in enumerate(rows, start=1):
                address = _find_wallet(row)
                if not address or address in seen:
                    continue
                seen.add(address)
                profit = _find_profit(row)
                candidates.append({
                    "rank": rank, "wallet": address,
                    "profit": profit, "period": window,
                    "label": str(row.get("username") or row.get("displayName") or row.get("name") or ""),
                    "raw": row,
                })
            if candidates:
                break
        if not candidates:
            raise LeaderboardError(
                "Pump.fun returned no recognizable trader addresses. This can mean the API schema "
                "changed, the selected period parameter is not accepted, or the leaderboard is empty."
            )
        # Trust explicit API order when profit fields are absent; otherwise use numeric profit.
        if any(item["profit"] is not None for item in candidates):
            candidates.sort(key=lambda item: item["profit"] if item["profit"] is not None else float("-inf"), reverse=True)
        return candidates[:10]

    def fetch_all(self) -> dict[str, list[dict]]:
        boards = {window: self._fetch(window) for window in WINDOWS}
        address_lists = [tuple(row["wallet"] for row in boards[w]) for w in WINDOWS]
        if address_lists[0] == address_lists[1] == address_lists[2]:
            logger.warning(
                "Pump.fun returned identical trader order for 1D/1W/1M. "
                "Period parameter may be ignored; auto-copy activation is blocked until verified."
            )
            raise LeaderboardError(
                "The API returned identical rankings for day, week and month. "
                "Verify PUMPFUN_LEADERBOARD_PERIOD_PARAM and period values before auto-copying."
            )
        # The official leaderboard exposes 1D/1W/1M, not a verified all-time view.
        # Our 'overall' is explicitly a composite rank across those three windows.
        scores: dict[str, dict] = {}
        for window, board in boards.items():
            for rank, row in enumerate(board, start=1):
                entry = scores.setdefault(row["wallet"], {
                    "wallet": row["wallet"], "rank_score": 0, "day_rank": None,
                    "week_rank": None, "month_rank": None, "label": row["label"],
                })
                entry["rank_score"] += 11 - rank
                entry[window + "_rank"] = rank
        overall = sorted(scores.values(), key=lambda row: row["rank_score"], reverse=True)[:10]
        boards["overall"] = [
            {"rank": rank, "wallet": row["wallet"], "label": row["label"],
             "profit": None, "period": "overall", "rank_score": row["rank_score"],
             "day_rank": row["day_rank"], "week_rank": row["week_rank"], "month_rank": row["month_rank"]}
            for rank, row in enumerate(overall, start=1)
        ]
        return boards
