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
        base_is_quote = base["address"] in QUOTE_MINTS
        quote_is_quote = quote["address"] in QUOTE_MINTS
        # Skip quote/quote and token/token pools. Only known SOL/stablecoin mint
        # addresses may qualify a market; token symbols alone are spoofable.
        if base_is_quote == quote_is_quote:
            continue
        token = quote if base_is_quote else base
        mint = token["address"]
        if not mint or mint in QUOTE_MINTS or not (32 <= len(mint) <= 48):
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


def _rugcheck_metrics(report: Any, pool_address: str = "") -> dict:
    """Normalize the documented full RugCheck response without assuming summary-only fields."""
    if not isinstance(report, dict):
        return {}

    token = report.get("token") if isinstance(report.get("token"), dict) else {}
    explicit_level = report.get("riskLevel") or report.get("risk_level") or ""
    raw_score = report.get("score_normalised", report.get("score_normalized"))
    score = _num(raw_score, -1.0) if raw_score not in (None, "") else -1.0
    if not 0 <= score <= 100:
        score = None

    level = str(explicit_level or "").strip().lower()
    if not level and score is not None:
        # Project policy, not an official RugCheck verdict: <=15 is a low-risk
        # ceiling; 16-35 stays review-only; >35 is rejected below.
        level = "low" if score <= 15 else ("medium" if score <= 35 else "high")

    def authority_field(name: str) -> tuple[bool, Any]:
        if name in report:
            return True, report.get(name)
        if name in token:
            return True, token.get(name)
        return False, None

    mint_present, mint_authority = authority_field("mintAuthority")
    freeze_present, freeze_authority = authority_field("freezeAuthority")

    top = None
    for key in ("topHoldersPct", "top10HoldersPct", "top10_holders_pct"):
        value = report.get(key)
        if value not in (None, ""):
            parsed = _num(value, -1.0)
            if parsed >= 0:
                top = parsed
            break
    if top is None:
        holders = report.get("topHolders")
        if isinstance(holders, list) and holders:
            percentages = []
            for holder in holders[:10]:
                if not isinstance(holder, dict) or holder.get("pct") in (None, ""):
                    percentages = []
                    break
                pct = _num(holder.get("pct"), -1.0)
                if pct < 0 or pct > 100:
                    percentages = []
                    break
                percentages.append(pct)
            if percentages:
                top = sum(percentages)

    raw_lp = report.get("lpLockedPct", report.get("lp_locked_pct"))
    markets = report.get("markets")
    if pool_address and isinstance(markets, list) and markets:
        matching_markets = [
            market for market in markets
            if isinstance(market, dict)
            and str(market.get("pubkey") or market.get("address") or "") == pool_address
        ]
        # Full reports can describe several markets. Do not borrow LP-lock data
        # from a different pool and pretend it describes the candidate pool.
        if matching_markets:
            lp_info = matching_markets[0].get("lp") or {}
            raw_lp = lp_info.get("lpLockedPct", matching_markets[0].get("lpLockedPct"))
        else:
            raw_lp = None
    lp = _num(raw_lp, -1.0) if raw_lp not in (None, "") else -1.0
    if not 0 <= lp <= 100:
        lp = None

    return {
        "risk_level": level,
        "score_normalised": score,
        "mint_authority_present": mint_present,
        "mint_authority": mint_authority,
        "freeze_authority_present": freeze_present,
        "freeze_authority": freeze_authority,
        "top_holders_pct": top,
        "lp_locked_pct": lp,
    }


def evaluate_rugcheck_report(report: Any, pool_address: str = "") -> tuple[str, list[str]]:
    """Fail closed when RugCheck authority, holder, LP, or risk evidence is missing."""
    if not isinstance(report, dict):
        return "UNKNOWN", ["RugCheck response was not a JSON object"]
    risks = report.get("risks")
    if not isinstance(risks, list):
        return "UNKNOWN", ["RugCheck response lacks a usable risks list"]

    metrics = _rugcheck_metrics(report, pool_address)
    level = str(metrics.get("risk_level") or "").strip().lower()
    score = metrics.get("score_normalised")
    top = metrics.get("top_holders_pct")
    lp = metrics.get("lp_locked_pct")

    if level in BAD_LEVELS:
        return "REJECTED", [f"RugCheck risk level: {level}"]
    if score is not None and score > 35:
        return "REJECTED", [f"RugCheck normalized risk score {score:.1f} exceeds 35"]

    flags: list[str] = []
    fatal_risk = False

    if not metrics.get("mint_authority_present"):
        flags.append("Mint-authority field is missing from RugCheck report")
    elif metrics.get("mint_authority") not in (None, ""):
        return "REJECTED", ["RugCheck reports active mint authority"]

    if not metrics.get("freeze_authority_present"):
        flags.append("Freeze-authority field is missing from RugCheck report")
    elif metrics.get("freeze_authority") not in (None, ""):
        return "REJECTED", ["RugCheck reports active freeze authority"]

    if top is None:
        flags.append("Top-holder concentration data is missing or malformed")
    elif top > 35:
        return "REJECTED", [f"Top-holder concentration {top:.1f}% exceeds 35%"]

    if lp is None:
        flags.append("LP lock coverage for the candidate pool is missing or malformed")
    elif lp < 80:
        flags.append(f"LP lock/burn coverage is only {lp:.1f}% (review threshold 80%)")

    for risk in risks:
        if not isinstance(risk, dict):
            flags.append("RugCheck returned an unparsed risk item")
            continue
        risk_level = str(risk.get("level") or risk.get("riskLevel") or "").strip().lower()
        label = str(risk.get("name") or risk.get("description") or "Unlabelled risk").strip()
        if risk_level in BAD_LEVELS:
            fatal_risk = True
            flags.append(f"{risk_level.upper()}: {label}")
        elif risk_level in REVIEW_LEVELS or risk_level not in GOOD_LEVELS | {"info", "informational", ""}:
            flags.append(f"Review: {label}")

    if fatal_risk:
        return "REJECTED", flags

    if score is not None and score > 15:
        flags.append(f"Normalized RugCheck risk score {score:.1f} exceeds the 15-point live-entry ceiling")
    if not level:
        flags.append("RugCheck risk level and normalized score are unavailable")
    elif level not in GOOD_LEVELS:
        flags.append(f"RugCheck level is not clearly low risk: {level}")

    if flags:
        return "REVIEW", flags
    return "SAFE_CANDIDATE", ["RugCheck full report passed configured authority, holder, pool LP-lock, and risk-score thresholds"]

class TrendingTokenScanner:
    """Paced scanner; live candidate callbacks require separate explicit opt-ins."""

    def __init__(self, settings, rpc, timeout: int = 8, store=None, on_screened=None):
        self.settings = settings
        self.rpc = rpc
        self.store = store
        self.on_screened = on_screened
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
            pools_by_mint: dict[str, list[dict]] = {}
            for pair in payload:
                if not isinstance(pair, dict) or pair.get("chainId") != "solana":
                    continue
                base, quote = pair.get("baseToken") or {}, pair.get("quoteToken") or {}
                mint = str(base.get("address") or "")
                if mint not in mints:
                    mint = str(quote.get("address") or "")
                if mint in mints:
                    pools_by_mint.setdefault(mint, []).append(pair)
            for c in candidates:
                pairs = pools_by_mint.get(str(c.get("mint") or ""), [])
                if not pairs:
                    c.setdefault("risk_flags", []).append("No matching DEX Screener pair; market cross-check incomplete")
                    c["dexscreener_confirmed"] = False
                    continue
                pool_address = str(c.get("pool_address") or "")
                pair = next(
                    (row for row in pairs if str(row.get("pairAddress") or "") == pool_address),
                    None,
                )
                same_pool = pair is not None
                if pair is None:
                    # Do not compare liquidity/volume figures from different pools as
                    # if they were the same market; surface the best alternate pair for review.
                    pair = max(
                        pairs,
                        key=lambda row: _num((row.get("liquidity") or {}).get("usd")),
                    )
                base, quote = pair.get("baseToken") or {}, pair.get("quoteToken") or {}
                chosen = base if str(base.get("address") or "") == str(c.get("mint") or "") else quote
                if chosen.get("name"):
                    c["name"] = str(chosen["name"])
                if chosen.get("symbol"):
                    c["symbol"] = str(chosen["symbol"]).upper()
                c["dexscreener_pair_address"] = str(pair.get("pairAddress") or "")
                c["dexscreener_url"] = str(pair.get("url") or "")
                dex_liq = _num((pair.get("liquidity") or {}).get("usd"))
                volume, change = pair.get("volume") or {}, pair.get("priceChange") or {}
                c["dexscreener_liquidity_usd"] = dex_liq
                c["dexscreener_volume_1h_usd"] = _num(volume.get("h1"))
                c["dexscreener_volume_24h_usd"] = _num(volume.get("h24"))
                c["dexscreener_change_5m_pct"] = _num(change.get("m5"))
                c["dexscreener_change_1h_pct"] = _num(change.get("h1"))
                c["dexscreener_change_24h_pct"] = _num(change.get("h24"))
                txns = pair.get("txns") or {}
                h1_txns = txns.get("h1") or {}
                c["dexscreener_buys_1h"] = int(_num(h1_txns.get("buys")))
                c["dexscreener_sells_1h"] = int(_num(h1_txns.get("sells")))
                c["same_pool_crosscheck"] = same_pool
                c["dexscreener_confirmed"] = bool(same_pool and dex_liq > 0)
                if not same_pool:
                    c.setdefault("risk_flags", []).append(
                        "DEX Screener's best pair differs from GeckoTerminal's trending pool; pool-level metrics are not directly comparable"
                    )
                    continue
                gecko_liq = _num(c.get("liquidity_usd"))
                if dex_liq > 0:
                    c["gecko_liquidity_usd"] = gecko_liq
                    c["liquidity_usd"] = min(gecko_liq, dex_liq) if gecko_liq > 0 else dex_liq
                    c["market_data_sources"] = ["GeckoTerminal", "DEX Screener"]
                    if gecko_liq > 0 and max(gecko_liq, dex_liq) / min(gecko_liq, dex_liq) > 3:
                        c["market_data_discrepancy"] = True
                        c.setdefault("risk_flags", []).append("Market sources disagree on liquidity by more than 3x")
                gecko_vol1 = _num(c.get("volume_1h_usd"))
                dex_vol1 = _num(volume.get("h1"))
                if gecko_vol1 > 0 and dex_vol1 > 0 and max(gecko_vol1, dex_vol1) / min(gecko_vol1, dex_vol1) > 5:
                    c["market_data_discrepancy"] = True
                    c.setdefault("risk_flags", []).append("GeckoTerminal and DEX Screener 1h volume disagree by more than 5x")
                gecko_change1 = _num(c.get("change_1h_pct"))
                dex_change1 = change.get("h1")
                if dex_change1 is not None and abs(gecko_change1 - _num(dex_change1)) > 40:
                    c["market_data_discrepancy"] = True
                    c.setdefault("risk_flags", []).append("GeckoTerminal and DEX Screener 1h price changes disagree by more than 40 percentage points")
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
        extensions = info.get("extensions")
        if owner == TOKEN_2022_PROGRAM_ID and not isinstance(extensions, list):
            return "REVIEW", ["Token-2022 extension list is missing; extension safety cannot be verified"]
        if extensions is not None and not isinstance(extensions, list):
            return "UNKNOWN", ["Token extension data is malformed"]
        for ext in extensions or []:
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
            item.update({"status": "FILTERED", "decision": f"Liquidity ${liquidity:,.0f} below ${self.min_liquidity_usd:,.0f} minimum"})
            return item
        if vol1 < self.min_volume_1h_usd:
            item.update({"status": "FILTERED", "decision": f"1h volume ${vol1:,.0f} below ${self.min_volume_1h_usd:,.0f} minimum"})
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
            report = self._get_json(f"{RUGCHECK}/tokens/{item['mint']}/report")
        except Exception as exc:
            item["rugcheck_status"] = "UNAVAILABLE"
            item.setdefault("risk_flags", []).append(f"RugCheck unavailable ({type(exc).__name__})")
            item.update({"status": "UNKNOWN", "decision": "RugCheck unavailable; no safety approval"})
            return item
        status, risk_flags = evaluate_rugcheck_report(report, str(item.get("pool_address") or ""))
        item["rugcheck_status"] = status
        if isinstance(report, dict):
            metrics = _rugcheck_metrics(report, str(item.get("pool_address") or ""))
            item["rugcheck_level"] = str(metrics.get("risk_level") or report.get("riskLevel") or report.get("risk_level") or "")
            item["rugcheck_score"] = metrics.get("score_normalised", report.get("score"))
            item["top_holders_pct"] = metrics.get("top_holders_pct")
            item["lp_locked_pct"] = metrics.get("lp_locked_pct")
        item.setdefault("risk_flags", []).extend(risk_flags)
        if status == "REJECTED":
            item.update({"status": "REJECTED", "decision": "; ".join(risk_flags) or "RugCheck rejected candidate"})
        elif status == "SAFE_CANDIDATE":
            live_active = (
                str(getattr(self.settings, "mode", "paper")).lower() == "live"
                and bool(getattr(self.settings, "trending_discovery_enabled", False))
                and bool(getattr(self.settings, "trending_discovery_live_enabled", False))
            )
            score_eligible = int(item.get("momentum_score") or 0) >= int(
                getattr(self.settings, "trending_min_score", 75)
            )
            item.update({
                "status": "SCREENED",
                "execution_mode": "live_opt_in" if live_active else "analysis_only",
                "trade_eligible": bool(live_active and score_eligible),
                "decision": (
                    "Passed initial market, on-chain mint and RugCheck screens; "
                    "live entry still requires fresh pre-trade checks"
                    if live_active and score_eligible
                    else "Passed analysis screens; live execution is not authorized or score is below threshold"
                ),
            })
        else:
            item.update({"status": "REVIEW", "decision": "; ".join(risk_flags) or "Risk report incomplete; manual review required"})
        return item

    def verify_live_candidate(self, candidate: dict) -> dict:
        """Fail closed if the exact pool or fresh on-chain/risk checks no longer pass."""
        if str(getattr(self.settings, "mode", "paper")).lower() != "live" or not self.enabled or not bool(
            getattr(self.settings, "trending_discovery_live_enabled", False)
        ):
            raise ValueError("Trending live opt-in is not active")
        mint = str(candidate.get("mint") or "").strip()
        pool = str(candidate.get("pool_address") or "").strip()
        pair_address = str(candidate.get("dexscreener_pair_address") or "").strip()
        if not mint or not pool or not pair_address:
            raise ValueError("Mint or cross-checked pool address is missing")

        payload = self._get_json(
            f"{GECKO}/networks/solana/trending_pools?include=base_token,quote_token&page=1",
            gecko=True,
        )
        row = next((r for r in parse_gecko_trending(payload)
                    if r.get("mint") == mint and str(r.get("pool_address") or "") == pool), None)
        if row is None:
            raise ValueError("Exact pool is no longer in the fresh trending feed")
        self._dexscreener_enrich([row])
        if (not row.get("dexscreener_confirmed") or row.get("same_pool_crosscheck") is not True
                or str(row.get("dexscreener_pair_address") or "") != pair_address):
            raise ValueError("Fresh DEX Screener response did not confirm the same pool")
        if row.get("market_data_discrepancy"):
            raise ValueError("Fresh market sources disagree")
        liquidity = _num(row.get("liquidity_usd"))
        volume = _num(row.get("dexscreener_volume_1h_usd"))
        if liquidity < self.min_liquidity_usd or volume < self.min_volume_1h_usd:
            raise ValueError("Fresh pool liquidity or volume is below the configured floor")

        onchain, flags = self._check_mint(mint)
        if onchain != "PASS":
            raise ValueError("Fresh on-chain check failed: " + "; ".join(flags)[:250])
        acct_result = self.rpc.call(
            "getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}]
        ) or {}
        info = (((((acct_result.get("value") or {}).get("data") or {}).get("parsed") or {}).get("info")) or {})
        try:
            decimals, supply_raw = int(info.get("decimals", -1)), int(info.get("supply") or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Fresh token supply/decimals are invalid") from exc
        if supply_raw <= 0 or not 0 <= decimals <= 12:
            raise ValueError("Fresh token supply/decimals are outside the accepted range")

        report = self._get_json(f"{RUGCHECK}/tokens/{mint}/report")
        rug_status, rug_flags = evaluate_rugcheck_report(report, pool)
        if rug_status != "SAFE_CANDIDATE":
            raise ValueError("Fresh RugCheck did not pass: " + "; ".join(rug_flags)[:250])

        row["decimals"] = decimals
        row["total_supply_tokens"] = supply_raw / (10 ** decimals)
        row["onchain_status"] = onchain
        row["rugcheck_status"] = rug_status
        metrics = _rugcheck_metrics(report, pool)
        row["rugcheck_level"] = str(metrics.get("risk_level") or report.get("riskLevel") or report.get("risk_level") or "")
        row["rugcheck_score"] = metrics.get("score_normalised", report.get("score"))
        row["top_holders_pct"] = metrics.get("top_holders_pct")
        row["lp_locked_pct"] = metrics.get("lp_locked_pct")
        row["volume_1h_usd"] = volume
        row["volume_24h_usd"] = _num(row.get("dexscreener_volume_24h_usd"))
        row["buys_1h"] = int(_num(row.get("dexscreener_buys_1h")))
        row["sells_1h"] = int(_num(row.get("dexscreener_sells_1h")))
        row["change_5m_pct"] = _num(row.get("dexscreener_change_5m_pct"), _num(row.get("change_5m_pct")))
        row["change_1h_pct"] = _num(row.get("dexscreener_change_1h_pct"), _num(row.get("change_1h_pct")))
        row["change_24h_pct"] = _num(row.get("dexscreener_change_24h_pct"), _num(row.get("change_24h_pct")))
        row["momentum_score"] = score_trending_candidate(row)
        if row["momentum_score"] < int(getattr(self.settings, "trending_min_score", 75)):
            raise ValueError("Fresh momentum score is below the configured floor")
        if row["buys_1h"] <= row["sells_1h"]:
            raise ValueError("Fresh one-hour trading flow no longer shows net buying")
        row["fresh_security_verified"] = True
        row["updated_at"] = time.time()
        row["risk_flags"] = list(row.get("risk_flags") or []) + flags + rug_flags
        return row

    def set_live_status(self, mint: str, status: str, reason: str) -> None:
        """Persist one-shot live-attempt state so restarts cannot replay a buy."""
        normalized = " ".join(str(reason or "").split())[:500]
        if self.store is not None:
            self.store.set_state(f"trending_live_status:{mint}", str(status))
            self.store.set_state(f"trending_live_reason:{mint}", normalized)
            self.store.set_state(f"trending_live_status_at:{mint}", str(time.time()))
        with self._lock:
            if mint in self._candidates:
                self._candidates[mint]["live_entry_status"] = str(status)
                self._candidates[mint]["live_entry_reason"] = normalized
                self._candidates[mint]["live_entry_status_updated_at"] = time.time()

    def _stored_live_status(self, mint: str) -> tuple[str, str, float]:
        if self.store is None:
            return "", "", 0.0
        status = self.store.get_state(f"trending_live_status:{mint}", "")
        reason = self.store.get_state(f"trending_live_reason:{mint}", "")
        try:
            updated_at = float(self.store.get_state(f"trending_live_status_at:{mint}", "0") or 0)
        except (TypeError, ValueError):
            updated_at = 0.0
        return status, reason, updated_at

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
        queue_for_live: list[dict] = []
        live_active = (
            self.on_screened is not None
            and str(getattr(self.settings, "mode", "paper")).lower() == "live"
            and self.enabled
            and bool(getattr(self.settings, "trending_discovery_live_enabled", False))
        )
        # Only one live discovery candidate may be in flight at a time. Persisted
        # "submitting"/"uncertain" states intentionally block new entries after a
        # restart until the possible transaction has been reconciled.
        live_reserved = False
        if live_active and self.store is not None:
            try:
                if hasattr(self.store, "list_positions"):
                    live_reserved = any(
                        p.get("strategy", "leader_copy") in {"token_discovery", "trending_discovery"}
                        for p in self.store.list_positions()
                    )
                if hasattr(self.store, "list_state_prefix"):
                    for key, value in self.store.list_state_prefix("trending_live_status:", limit=10000):
                        status = str(value or "").strip().lower()
                        mint = key[len("trending_live_status:"):]
                        if status == "queued":
                            try:
                                queued_at = float(self.store.get_state(f"trending_live_status_at:{mint}", "0") or 0)
                            except (TypeError, ValueError):
                                queued_at = 0.0
                            if queued_at and time.time() - queued_at > 180:
                                # Safe to move on to another candidate after a stale queue.
                                # The candidate is not retried automatically across restart.
                                self.set_live_status(mint, "stale_queue", "Queued candidate expired before the main loop processed it")
                                continue
                            live_reserved = True
                        elif status in {"submitting", "uncertain"}:
                            live_reserved = True
            except Exception as exc:
                # State inspection failure means live entry must remain blocked.
                live_reserved = True
                logger.warning("Trending live single-flight state check failed; blocking new entries: %s", type(exc).__name__)
        with self._lock:
            self._last_scan_at = time.time()
            self._last_error = ""
            self._pool_count, self._checked_count = len(raw), len(checked)
            self._screened_count = sum(c.get("status") == "SCREENED" for c in checked)
            self._rejected_count = sum(c.get("status") in {"REJECTED", "FILTERED"} for c in checked)
            self._scan_count += 1
            for item in checked:
                live_status, live_reason, live_updated_at = self._stored_live_status(item["mint"])
                if live_status:
                    item["live_entry_status"] = live_status
                    item["live_entry_reason"] = live_reason
                    item["live_entry_status_updated_at"] = live_updated_at
                self._candidates[item["mint"]] = item
                if (
                    live_active
                    and not live_reserved
                    and item.get("status") == "SCREENED"
                    and int(item.get("momentum_score") or 0) >= int(getattr(self.settings, "trending_min_score", 75))
                    and not live_status
                ):
                    self.set_live_status(
                        item["mint"], "queued",
                        "Passed first-pass screens; checking fresh pool, on-chain and RugCheck data",
                    )
                    queue_for_live.append(dict(item))
                    live_reserved = True
            cutoff = time.time() - 6 * 3600
            self._candidates = {m: c for m, c in self._candidates.items() if _num(c.get("updated_at")) >= cutoff}
        for candidate in queue_for_live:
            try:
                self.on_screened(candidate)
            except Exception as exc:
                self.set_live_status(
                    str(candidate.get("mint") or ""), "failed",
                    f"Could not enqueue candidate ({type(exc).__name__})",
                )
        logger.info("Trending scan complete: pools=%d checked=%d screened=%d queued=%d rejected/filtered=%d",
                    len(raw), len(checked), self._screened_count, len(queue_for_live), self._rejected_count)

    def dashboard_snapshot(self) -> dict:
        with self._lock:
            candidates = sorted(
                (dict(c) for c in self._candidates.values()),
                key=lambda c: (_num(c.get("momentum_score")), _num(c.get("liquidity_usd"))),
                reverse=True,
            )[:30]
            live_active = (
                str(getattr(self.settings, "mode", "paper")).lower() == "live"
                and self.enabled
                and bool(getattr(self.settings, "trending_discovery_live_enabled", False))
            )
            return {
                "enabled": self.enabled, "analysis_only": not live_active, "last_scan_at": self._last_scan_at,
                "last_error": self._last_error, "poll_seconds": self.poll_seconds,
                "min_liquidity_usd": self.min_liquidity_usd, "min_volume_1h_usd": self.min_volume_1h_usd,
                "pool_count": self._pool_count, "checked_count": self._checked_count,
                "screened_count": self._screened_count, "rejected_count": self._rejected_count,
                "scan_count": self._scan_count, "candidates": candidates,
                "live_enabled": live_active,
                "live_entry_cap_usd": min(0.50, float(getattr(self.settings, "trending_discovery_max_entry_usd", 0.50))),
                "min_score": int(getattr(self.settings, "trending_min_score", 75)),
                "max_price_impact_pct": float(getattr(self.settings, "trending_max_price_impact_pct", 5.0)),
                "source": "GeckoTerminal + DEX Screener + RugCheck + direct mint RPC",
            }
