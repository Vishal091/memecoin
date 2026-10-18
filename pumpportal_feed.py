"""Single-connection PumpPortal account-trade feed for near-real-time leader events.

PumpPortal data subscriptions are metered. Keep this connection single, and do
not add more addresses than the selected top-ten list unless you intend to pay
for the additional event stream.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Iterable
from urllib.parse import quote

from websockets.sync.client import connect

logger = logging.getLogger(__name__)


class PumpPortalAccountFeed(threading.Thread):
    def __init__(self, api_key: str, output_queue: queue.Queue, timeout: int = 12):
        super().__init__(name="pumpportal-account-feed", daemon=True)
        self.api_key = api_key.strip()
        self.output_queue = output_queue
        self.timeout = timeout
        self._lock = threading.Lock()
        self._desired_wallets: tuple[str, ...] = ()
        self._desired_tokens: tuple[str, ...] = ()
        self._discovery_enabled = False
        self._discovery_tokens: tuple[str, ...] = ()
        self._stop_event = threading.Event()
        self.connected = threading.Event()
        self.last_error = ""
        self.last_message_at = 0.0
        self.connected_at = 0.0
        self.messages_received = 0
        self.trade_events_received = 0
        self.account_trade_events_received = 0
        self.last_account_trade_at = 0.0
        self.last_event_summary = ""
        self.last_notice = ""
        self.subscribed_wallet_count = 0
        self.subscription_updates = 0

    def set_subscriptions(self, wallets: Iterable[str], tokens: Iterable[str] = ()) -> None:
        account_list = tuple(dict.fromkeys(w for w in wallets if w))
        token_list = tuple(dict.fromkeys(t for t in tokens if t))
        with self._lock:
            self._desired_wallets = account_list
            self._desired_tokens = token_list

    def get_subscriptions(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        with self._lock:
            return self._desired_wallets, self._desired_tokens

    def set_discovery_subscriptions(self, enabled: bool, tokens: Iterable[str] = ()) -> None:
        """Enable the free new-token stream and cap metered token-trade subscriptions."""
        token_list = tuple(dict.fromkeys(t for t in tokens if t))
        with self._lock:
            self._discovery_enabled = bool(enabled)
            self._discovery_tokens = token_list

    def get_discovery_subscriptions(self) -> tuple[bool, tuple[str, ...]]:
        with self._lock:
            return self._discovery_enabled, self._discovery_tokens

    def stop(self) -> None:
        self._stop_event.set()

    def _send_diff(self, ws, method_add: str, method_remove: str, old: set[str], new: set[str]) -> None:
        add = sorted(new - old)
        remove = sorted(old - new)
        for start in range(0, len(remove), 100):
            ws.send(json.dumps({"method": method_remove, "keys": remove[start:start + 100]}))
        for start in range(0, len(add), 100):
            ws.send(json.dumps({"method": method_add, "keys": add[start:start + 100]}))

    def run(self) -> None:
        if not self.api_key:
            self.last_error = "PumpPortal API key is not configured"
            return
        uri = "wss://pumpportal.fun/api/data?api-key=" + quote(self.api_key, safe="")
        while not self._stop_event.is_set():
            try:
                with connect(uri, open_timeout=self.timeout, close_timeout=3, ping_interval=20, max_size=2**20) as ws:
                    subscribed_wallets: set[str] = set()
                    subscribed_tokens: set[str] = set()
                    self.connected_at = time.time()
                    self.connected.set()
                    self.last_error = ""
                    logger.info("Connected to PumpPortal data feed")
                    desired_wallets, desired_tokens = self.get_subscriptions()
                    discovery_enabled, discovery_tokens = self.get_discovery_subscriptions()
                    self._send_diff(ws, "subscribeAccountTrade", "unsubscribeAccountTrade",
                                    subscribed_wallets, set(desired_wallets))
                    combined_tokens = set(desired_tokens) | set(discovery_tokens)
                    self._send_diff(ws, "subscribeTokenTrade", "unsubscribeTokenTrade",
                                    subscribed_tokens, combined_tokens)
                    if discovery_enabled:
                        ws.send(json.dumps({"method": "subscribeNewToken"}))
                        logger.info("PumpPortal new-token stream subscribed (free; independent shadow scanner)")
                    subscribed_wallets, subscribed_tokens = set(desired_wallets), combined_tokens
                    self.subscribed_wallet_count = len(subscribed_wallets)
                    self.subscription_updates += 1
                    logger.info("PumpPortal account-trade subscription requested for %d wallet(s)", self.subscribed_wallet_count)
                    while not self._stop_event.is_set():
                        desired_wallets, desired_tokens = self.get_subscriptions()
                        desired_wallets, desired_tokens = set(desired_wallets), set(desired_tokens)
                        discovery_enabled, discovery_tokens = self.get_discovery_subscriptions()
                        if desired_wallets != subscribed_wallets:
                            self._send_diff(ws, "subscribeAccountTrade", "unsubscribeAccountTrade",
                                            subscribed_wallets, desired_wallets)
                            subscribed_wallets = desired_wallets
                            self.subscribed_wallet_count = len(subscribed_wallets)
                            self.subscription_updates += 1
                            logger.info("PumpPortal account-trade subscription updated for %d wallet(s)", len(subscribed_wallets))
                        combined_tokens = desired_tokens | set(discovery_tokens)
                        if combined_tokens != subscribed_tokens:
                            self._send_diff(ws, "subscribeTokenTrade", "unsubscribeTokenTrade",
                                            subscribed_tokens, combined_tokens)
                            subscribed_tokens = combined_tokens
                            logger.info("PumpPortal token-trade subscriptions updated: %d total mints", len(subscribed_tokens))
                        try:
                            raw = ws.recv(timeout=1)
                        except TimeoutError:
                            continue
                        if not isinstance(raw, str):
                            continue
                        self.last_message_at = time.time()
                        self.messages_received += 1
                        try:
                            message = json.loads(raw)
                        except json.JSONDecodeError:
                            logger.debug("Ignoring non-JSON PumpPortal message")
                            continue
                        if not isinstance(message, dict):
                            continue
                        trader = (message.get("traderPublicKey") or message.get("userPublicKey")
                                  or message.get("wallet") or message.get("trader"))
                        mint = message.get("mint")
                        tx_type = str(message.get("txType") or message.get("side") or "").lower()
                        if mint and (
                            tx_type in {"create", "newtoken", "new_token"}
                            or ("bondingCurveKey" in message and tx_type not in {"buy", "sell"})
                        ):
                            event = dict(message)
                            event["source"] = "token_discovery_new"
                            self.output_queue.put(event)
                            continue
                        if tx_type not in {"buy", "sell"} or not mint:
                            # PumpPortal can send subscription/error notices as JSON too.
                            # Keep a safe, short status line rather than logging whole payloads.
                            notice = str(message.get("error") or message.get("message") or message.get("status") or "")
                            if notice:
                                notice = " ".join(notice.split())[:200]
                                if notice != self.last_notice:
                                    self.last_notice = notice
                                    if message.get("error") or any(word in notice.lower() for word in ("error", "insufficient", "limit", "invalid", "unauthorized", "denied", "only available", "funded with", "0.02 sol")):
                                        logger.warning("PumpPortal provider notice: %s", notice)
                                    else:
                                        logger.info("PumpPortal provider notice: %s", notice)
                            continue
                        self.trade_events_received += 1
                        matched_account = bool(trader and trader in subscribed_wallets)
                        matched_token = mint in subscribed_tokens
                        if matched_account:
                            event = dict(message)
                            event["wallet"] = trader
                            event["side"] = tx_type
                            event["source"] = "account_trade"
                            self.account_trade_events_received += 1
                            self.last_account_trade_at = time.time()
                            self.last_event_summary = f"{tx_type} {str(mint)[:8]}… wallet {str(trader)[:6]}…"
                            logger.info("PumpPortal account trade received: %s wallet=%s mint=%s",
                                        tx_type, str(trader)[:6], str(mint)[:8])
                            self.output_queue.put(event)
                        if matched_token:
                            # A single trade may feed both strategies: leader-copy consumes
                            # the account event; the shadow strategy consumes this token event.
                            event = dict(message)
                            event["wallet"] = str(trader or "")
                            event["side"] = tx_type
                            event["source"] = "token_trade"
                            self.output_queue.put(event)
                        if not matched_account and not matched_token:
                            self.last_notice = f"Trade event did not match active account/token subscriptions (side={tx_type})."
            except Exception as exc:
                self.connected.clear()
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("PumpPortal feed disconnected: %s; reconnecting in 3 seconds", self.last_error)
                self._stop_event.wait(3)
        self.connected.clear()
