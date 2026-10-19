"""Shadow-only Solana trending-token scanner; never signs or submits transactions."""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timezone
from typing import Any

import requests

from native_pump import TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID
from token_discovery import BLOCKED_EXTENSION_NAMES, TOKEN_METADATA_SAFE_EXTENSIONS

logger = logging.getLogger("trending_discovery")
GECKO = "https://api.geckoterminal.com/api/v2"
DEX = "https://api.dexscreener.com"
RUGCHECK = "https://api.rugcheck.xyz/v1"
QUOTE_MINTS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}
QUOTE_SYMBOLS = {"SOL", "WSOL", "USDC", "USDT"}
BAD_LEVELS = {"danger", "critical", "high", "risky", "malicious", "severe"}
GOOD_LEVELS = {"good", "low", "safe"}
REVIEW_LEVELS = {"medium", "moderate", "warning", "caution", "unknown", "unverified"}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value) if value not in (None, "") else default
        return result if math.isfinite(result) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _epoch(value: Any) -> float:
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return 0.0


def _address(resource_id: Any) -> str:
    value = str(resource_id or "")
    return value[len("solana_"):] if value.startswith("solana_") else value


def _token_ref(resource_id: Any, included: dict[str, dict]) -> dict:
    resource_id = str(resource_id or "")
    attrs = (included.get(resource_id) or {}).get("attributes") or {}
    return {
        "address": str(attrs.get("address") or _address(resource_id)),
        "name": str(attrs.get("name") or ""),
        "symbol": str(attrs.get("symbol") or "").upper(),
    }


def parse_gecko_trending(payload: Any) -> list[dict]:
    """Normalize GeckoTerminal's trending pools into one row per Solana token."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return []
    included = {
        str(row.get("id")): row for row in payload.get("included", [])
        if isinstance(row, dict) and row.get("id")
    }
    best: dict[str, dict] = {}
    for pool in payload["data"]:
        if not isinstance(pool, dict):
            continue
        attrs = pool.get("attributes") or {}
        rel = pool.get("relationships") or {}
        br = (rel.get("base_token") or {}).get("data") or {}
        qr = (rel.get("quote_token") or {}).get("data") or {}
        base, quote = _token_ref(br.get("id"), included), _token_ref(qr.get("id"), included)
        token = quote if (base["address"] in QUOTE_MINTS or base["symbol"] in QUOTE_SYMBOLS) else base
        mint = token["address"]
        if not mint or mint in QUOTE_MINTS or token["symbol"] in QUOTE_SYMBOLS or not (32 <= len(mint) <= 48):
            continue
        volume = attrs.get("volume_usd") or {}
        change = attrs.get("price_change_percentage") or {}
        txns = attrs.get("transactions") or {}
        h1, m5 = txns.get("h1") or {}, txns.get("m5") or {}
        row = {
            "mint": mint,
            "name": token["name"] or str(attrs.get("name") or "Unknown token"),
            "symbol": token["symbol"] or "—",
            "pool_address": _address(pool.get("id")),
            "pool_name": str(attrs.get("name") or ""),
            "source": "GeckoTerminal trending pools",
            "liquidity_usd": _num(attrs.get("reserve_in_usd")),
            "volume_5m_usd": _num(volume.get("m5")),
            "volume_1h_usd": _num(volume.get("h1")),
            "volume_6h_usd": _num(volume.get("h6")),
            "volume_24h_usd": _num(volume.get("h24")),
            "change_5m_pct": _num(change.get("m5")),
            "change_1h_pct": _num(change.get("h1")),
            "change_6h_pct": _num(change.get("h6")),
            "change_24h_pct": _num(change.get("h24")),
            "buys_5m": int(_num(m5.get("buys"))),
            "sells_5m": int(_num(m5.get("sells"))),
            "buys_1h": int(_num(h1.get("buys"))),
            "sells_1h": int(_num(h1.get("sells"))),
            "market_cap_usd": _num(attrs.get("market_cap_usd")),
            "fdv_usd": _num(attrs.get("fdv_usd")),
            "pool_created_at": _epoch(attrs.get("pool_created_at")),
            "updated_at": time.time(),
            "market_data_sources": ["GeckoTerminal"],
            "risk_flags": [],
            "execution_mode": "analysis_only",
            "trade_eligible": False,
        }
        row["momentum_score"] = score_trending_candidate(row)
        previous = best.get(mint)
        if previous is None or row["liquidity_usd"] > previous["liquidity_usd"]:
            best[mint] = row
    return list(best.values())


def score_trending_candidate(c: dict) -> int:
    """Relative research ranking, not a rug verdict or profit prediction."""
    liq, vol1, vol24 = (_num(c.get(k)) for k in ("liquidity_usd", "volume_1h_usd", "volume_24h_usd"))
    buys, sells = max(0, int(_num(c.get("buys_1h")))), max(0, int(_num(c.get("sells_1h"))))
    pressure = buys / (buys + sells) if buys + sells else 0.5
    chg5, chg1, chg6 = (_num(c.get(k)) for k in ("change_5m_pct", "change_1h_pct", "change_6h_pct"))
    score = (
        min(20, 5 * math.log10(1 + max(0, liq) / 1000))
        + min(20, 5 * math.log10(1 + max(0, vol1) / 1000))
        + min(10, 2.5 * math.log10(1 + max(0, vol24) / 10000))
        + 15 * pressure
        + max(0, min(10, 5 + max(-20, min(20, chg5)) * 0.25))
        + max(0, min(15, 7.5 + max(-30, min(30, chg1)) * 0.25))
        + max(0, min(10, 5 + max(-40, min(40, chg6)) * 0.125))
    )
    score -= min(18, max(0, chg1 - 60) * 0.18)
    if chg5 > 20:
        score -= min(8, (chg5 - 20) * 0.25)
    return max(0, min(100, int(round(score))))


def evaluate_rugcheck_report(report: Any) -> tuple[str, list[str]]:
    """Fail closed on missing risk fields; return SAFE_CANDIDATE, REVIEW, REJECTED, UNKNOWN."""
    if not isinstance(report, dict):
        return "UNKNOWN", ["RugCheck response was not a JSON object"]
    risks = report.get("risks")
    level = str(report.get("riskLevel") or report.get("risk_level") or "").strip().lower()
    if not level and not isinstance(risks, list):
        return "UNKNOWN", ["RugCheck response lacks a risk level and risk list"]
    flags: list[str] = []
    if level in BAD_LEVELS:
        return "REJECTED", [f"RugCheck risk level: {level}"]
    if report.get("mintAuthority") not in (None, ""):
        flags.append("RugCheck reports active mint authority")
    if report.get("freezeAuthority") not in (None, ""):
        flags.append("RugCheck reports active freeze authority")
    top = report.get("topHoldersPct")
    lp = report.get("lpLockedPct")
    if top is not None and _num(top, 100.0) > 35:
        return "REJECTED", [f"Top holders concentration {_num(top):.1f}% exceeds 35%"]
    if lp is not None and _num(lp, -1) < 80:
        flags.append(f"LP lock/burn coverage is only {_num(lp):.1f}% (review threshold 80%)")
    elif lp is None and report.get("lpLocked") is False:
        flags.append("RugCheck reports LP is not locked")
    if isinstance(risks, list):
        for risk in risks:
            if not isinstance(risk, dict):
                flags.append("RugCheck returned an unparsed risk item")
                continue
            risk_level = str(risk.get("level") or risk.get("riskLevel") or "").strip().lower()
            label = str(risk.get("name") or risk.get("description") or "Unlabelled risk").strip()
            if risk_level in BAD_LEVELS:
                flags.append(f"{risk_level.upper()}: {label}")
            elif risk_level in REVIEW_LEVELS or risk_level not in GOOD_LEVELS | {"info", "informational", ""}:
                flags.append(f"Review: {label}")
    if report.get("mintAuthority") not in (None, "") or report.get("freezeAuthority") not in (None, ""):
        return "REJECTED", flags
    if any(x.startswith(("DANGER:", "CRITICAL:", "HIGH:", "SEVERE:", "MALICIOUS:")) for x in flags):
        return "REJECTED", flags
    if top is None:
        flags.append("Top-holder concentration field is missing")
    if lp is None and report.get("lpLocked") is not True:
        flags.append("LP lock coverage field is missing")
    if level not in GOOD_LEVELS:
        flags.append(f"RugCheck level is not clearly low risk: {level or 'missing'}")
    if flags:
        return "REVIEW", flags
    return "SAFE_CANDIDATE", ["RugCheck summary passed configured thresholds"]


class TrendingTokenScanner:
    """Paced background scanner. Trending results are research-only, never live orders."""

    def __init__(self, settings, rpc, timeout: int = 8):
        self.settings = settings
        self.rpc = rpc
        self.enabled = bool(getattr(settings, "trending_discovery_enabled", False))
        self.poll_seconds = max(60, min(900, int(getattr(settings, "trending_discovery_poll_seconds", 60))))
        self.max_candidates = max(1, min(20, int(getattr(settings, "trending_discovery_max_candidates", 8))))
        self.min_liquidity_usd = max(15000.0, float(getattr(settings, "trending_min_liquidity_usd", 15000.0)))
        self.min_volume_1h_usd = max(10000.0, float(getattr(settings, "trending_min_volume_1h_usd", 10000.0)))
        self.timeout = max(3, min(15, int(timeout)))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._candidates: dict[str, dict] = {}
        self._last_scan_at = 0.0
        self._last_error = ""
        self._pool_count = self._checked_count = self._screened_count = self._rejected_count = self._scan_count = 0

    def start(self) -> None:
        if self.enabled and not (self._thread and self._thread.is_alive()):
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="trending-token-scanner", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def _get_json(self, url: str, gecko: bool = False) -> Any:
        headers = {"Accept": "application/json", "User-Agent": "MemecoinResearchBot/1.0"}
        if gecko:
            headers["Accept"] = "application/json;version=20230203"
        response = requests.get(url, headers=headers, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception as exc:
                error = f"{type(exc).__name__}: {str(exc)[:200]}"
                with self._lock:
                    self._last_error = error
                logger.warning("Trending scanner refresh failed: %s", error)
            self._stop.wait(self.poll_seconds)

    def _dexscreener_enrich(self, candidates: list[dict]) -> None:
        mints = [str(c.get("mint") or "") for c in candidates[:30] if c.get("mint")]
        if not mints:
            return
        try:
            payload = self._get_json(f"{DEX}/tokens/v1/solana/{','.join(mints)}")
            if not isinstance(payload, list):
                return
            best: dict[str, dict] = {}
            for pair in payload:
                if not isinstance(pair, dict) or pair.get("chainId") != "solana":
                    continue
                base, quote = pair.get("baseToken") or {}, pair.get("quoteToken") or {}
                mint = str(base.get("address") or "")
                if mint not in mints:
                    mint = str(quote.get("address") or "")
                liq = _num((pair.get("liquidity") or {}).get("usd"))
                if mint in mints and (mint not in best or liq > _num((best[mint].get("liquidity") or {}).get("usd"))):
                    best[mint] = pair
            for c in candidates:
                pair = best.get(c.get("mint"))
                if not pair:
                    c.setdefault("risk_flags", []).append("No matching DEX Screener pair; market cross-check incomplete")
                    c["dexscreener_confirmed"] = False
                    continue
                base, quote = pair.get("baseToken") or {}, pair.get("quoteToken") or {}
                chosen = base if str(base.get("address") or "") == str(c.get("mint") or "") else quote
                if chosen.get("name"):
                    c["name"] = str(chosen["name"])
                if chosen.get("symbol"):
                    c["symbol"] = str(chosen["symbol"]).upper()
                c["dexscreener_pair_address"] = str(pair.get("pairAddress") or "")
                c["dexscreener_url"] = str(pair.get("url") or "")
                dex_liq = _num((pair.get("liquidity") or {}).get("usd"))
                gecko_liq = _num(c.get("liquidity_usd"))
                if dex_liq > 0:
                    c["gecko_liquidity_usd"] = gecko_liq
                    c["dexscreener_liquidity_usd"] = dex_liq
                    c["liquidity_usd"] = min(gecko_liq, dex_liq) if gecko_liq > 0 else dex_liq
                    c["market_data_sources"] = ["GeckoTerminal", "DEX Screener"]
                    c["dexscreener_confirmed"] = True
                    if gecko_liq > 0 and max(gecko_liq, dex_liq) / min(gecko_liq, dex_liq) > 3:
                        c["market_data_discrepancy"] = True
                        c.setdefault("risk_flags", []).append("Market sources disagree on liquidity by more than 3x")
                volume, change = pair.get("volume") or {}, pair.get("priceChange") or {}
                c["dexscreener_volume_1h_usd"] = _num(volume.get("h1"))
                c["dexscreener_volume_24h_usd"] = _num(volume.get("h24"))
                c["dexscreener_change_1h_pct"] = _num(change.get("h1"))
                c["dexscreener_change_24h_pct"] = _num(change.get("h24"))
        except Exception as exc:
            logger.info("DEX Screener cross-check unavailable: %s", type(exc).__name__)
            for c in candidates:
                c.setdefault("risk_flags", []).append("DEX Screener cross-check unavailable")

    def _check_mint(self, mint: str) -> tuple[str, list[str]]:
        result = self.rpc.call("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        account = (result or {}).get("value")
        if not isinstance(account, dict):
            return "UNKNOWN", ["Mint account is unavailable from RPC"]
        owner = str(account.get("owner") or "")
        if owner not in {TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID}:
            return "REJECTED", ["Mint is not owned by a supported SPL token program"]
        info = ((((account.get("data") or {}).get("parsed") or {}).get("info")) or {})
        if not info:
            return "UNKNOWN", ["RPC did not return parsed mint data"]
        try:
            decimals, supply = int(info.get("decimals", -1)), int(info.get("supply") or 0)
        except (TypeError, ValueError):
            return "UNKNOWN", ["Mint decimals or supply could not be parsed"]
        if supply <= 0 or not 0 <= decimals <= 12:
            return "REJECTED", [f"Invalid mint shape: decimals={decimals}, supply={supply}"]
        if info.get("mintAuthority") not in (None, ""):
            return "REJECTED", ["On-chain mint authority is still active"]
        if info.get("freezeAuthority") not in (None, ""):
            return "REJECTED", ["On-chain freeze authority is still active"]
        for ext in info.get("extensions") or []:
            if not isinstance(ext, dict):
                return "UNKNOWN", ["Token extension metadata is malformed"]
            name = str(ext.get("extension") or ext.get("type") or "").replace("_", "").lower()
            if name in BLOCKED_EXTENSION_NAMES:
                return "REJECTED", [f"Risky Token-2022 extension: {name}"]
            if name not in TOKEN_METADATA_SAFE_EXTENSIONS:
                return "REVIEW", [f"Unreviewed Token-2022 extension: {name or 'unknown'}"]
        return "PASS", [f"Mint verified; decimals={decimals}; mint/freeze authorities revoked"]

    def _evaluate(self, candidate: dict) -> dict:
        item = dict(candidate)
        item["updated_at"] = time.time()
        item["execution_mode"] = "analysis_only"
        item["trade_eligible"] = False
        liquidity, vol1 = _num(item.get("liquidity_usd")), _num(item.get("volume_1h_usd"))
        if liquidity < self.min_liquidity_usd:
            item.update({"status": "FILTERED", "decision": f"Liquidity ${{liquidity:,.0f}} below ${{self.min_liquidity_usd:,.0f}} minimum"})
            return item
        if vol1 < self.min_volume_1h_usd:
            item.update({"status": "FILTERED", "decision": f"1h volume ${{vol1:,.0f}} below ${{self.min_volume_1h_usd:,.0f}} minimum"})
            return item
        if not item.get("dexscreener_confirmed"):
            item.update({"status": "REVIEW", "decision": "Independent DEX market-data cross-check unavailable"})
            return item
        if item.get("market_data_discrepancy"):
            item.update({"status": "REVIEW", "decision": "Market sources disagree; manual review required"})
            return item
        onchain, flags = self._check_mint(item["mint"])
        item["onchain_status"] = onchain
        item.setdefault("risk_flags", []).extend(flags)
        if onchain == "REJECTED":
            item.update({"status": "REJECTED", "decision": "; ".join(flags)})
            return item
        if onchain != "PASS":
            item.update({"status": "UNKNOWN", "decision": "; ".join(flags)})
            return item
        try:
            report = self._get_json(f"{RUGCHECK}/tokens/{item['mint']}/report/summary")
        except Exception as exc:
            item["rugcheck_status"] = "UNAVAILABLE"
            item.setdefault("risk_flags", []).append(f"RugCheck unavailable ({type(exc).__name__})")
            item.update({"status": "UNKNOWN", "decision": "RugCheck unavailable; no safety approval"})
            return item
        status, risk_flags = evaluate_rugcheck_report(report)
        item["rugcheck_status"] = status
        if isinstance(report, dict):
            item["rugcheck_level"] = str(report.get("riskLevel") or report.get("risk_level") or "")
            item["rugcheck_score"] = report.get("score")
            item["top_holders_pct"] = _num(report.get("topHoldersPct")) if report.get("topHoldersPct") is not None else None
            item["lp_locked_pct"] = _num(report.get("lpLockedPct")) if report.get("lpLockedPct") is not None else None
        item.setdefault("risk_flags", []).extend(risk_flags)
        if status == "REJECTED":
            item.update({"status": "REJECTED", "decision": "; ".join(risk_flags) or "RugCheck rejected candidate"})
        elif status == "SAFE_CANDIDATE":
            item.update({"status": "SCREENED", "decision": "Passed market, on-chain mint and RugCheck screens; analysis-only"})
        else:
            item.update({"status": "REVIEW", "decision": "; ".join(risk_flags) or "Risk report incomplete; manual review required"})
        return item

    def scan_once(self) -> None:
        # Request token resources explicitly; without include=base_token,quote_token
        # GeckoTerminal can omit name/symbol attributes and leave every row unnamed.
        payload = self._get_json(
            f"{GECKO}/networks/solana/trending_pools?include=base_token,quote_token&page=1",
            gecko=True,
        )
        raw = parse_gecko_trending(payload)
        raw.sort(key=lambda x: (score_trending_candidate(x), _num(x.get("liquidity_usd"))), reverse=True)
        self._dexscreener_enrich(raw)
        raw.sort(key=lambda x: (score_trending_candidate(x), _num(x.get("liquidity_usd"))), reverse=True)
        selected = raw[:self.max_candidates]
        checked: list[dict] = []
        for candidate in selected:
            if self._stop.is_set():
                break
            try:
                candidate["momentum_score"] = score_trending_candidate(candidate)
                checked.append(self._evaluate(candidate))
            except Exception as exc:
                item = dict(candidate)
                item.update({
                    "updated_at": time.time(), "status": "UNKNOWN", "execution_mode": "analysis_only",
                    "trade_eligible": False, "decision": f"Risk check failed ({type(exc).__name__}); no approval",
                })
                item.setdefault("risk_flags", []).append("Risk check exception; fail closed")
                checked.append(item)
        with self._lock:
            self._last_scan_at = time.time()
            self._last_error = ""
            self._pool_count, self._checked_count = len(raw), len(checked)
            self._screened_count = sum(c.get("status") == "SCREENED" for c in checked)
            self._rejected_count = sum(c.get("status") in {"REJECTED", "FILTERED"} for c in checked)
            self._scan_count += 1
            for item in checked:
                self._candidates[item["mint"]] = item
            cutoff = time.time() - 6 * 3600
            self._candidates = {m: c for m, c in self._candidates.items() if _num(c.get("updated_at")) >= cutoff}
        logger.info("Trending scan complete: pools=%d checked=%d screened=%d rejected/filtered=%d",
                    len(raw), len(checked), self._screened_count, self._rejected_count)

    def dashboard_snapshot(self) -> dict:
        with self._lock:
            candidates = sorted(
                (dict(c) for c in self._candidates.values()),
                key=lambda c: (_num(c.get("momentum_score")), _num(c.get("liquidity_usd"))),
                reverse=True,
            )[:30]
            return {
                "enabled": self.enabled, "analysis_only": True, "last_scan_at": self._last_scan_at,
                "last_error": self._last_error, "poll_seconds": self.poll_seconds,
                "min_liquidity_usd": self.min_liquidity_usd, "min_volume_1h_usd": self.min_volume_1h_usd,
                "pool_count": self._pool_count, "checked_count": self._checked_count,
                "screened_count": self._screened_count, "rejected_count": self._rejected_count,
                "scan_count": self._scan_count, "candidates": candidates,
                "source": "GeckoTerminal + DEX Screener + RugCheck + direct mint RPC",
            }
