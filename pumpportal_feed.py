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
        self._stop_event = threading.Event()
        self.connected = threading.Event()
        self.last_error = ""
        self.last_message_at = 0.0

    def set_subscriptions(self, wallets: Iterable[str], tokens: Iterable[str] = ()) -> None:
        account_list = tuple(dict.fromkeys(w for w in wallets if w))
        token_list = tuple(dict.fromkeys(t for t in tokens if t))
        with self._lock:
            self._desired_wallets = account_list
            self._desired_tokens = token_list

    def get_subscriptions(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        with self._lock:
            return self._desired_wallets, self._desired_tokens

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
                    self.connected.set()
                    self.last_error = ""
                    logger.info("Connected to PumpPortal data feed")
                    desired_wallets, desired_tokens = self.get_subscriptions()
                    self._send_diff(ws, "subscribeAccountTrade", "unsubscribeAccountTrade",
                                    subscribed_wallets, set(desired_wallets))
                    self._send_diff(ws, "subscribeTokenTrade", "unsubscribeTokenTrade",
                                    subscribed_tokens, set(desired_tokens))
                    subscribed_wallets, subscribed_tokens = set(desired_wallets), set(desired_tokens)
                    while not self._stop_event.is_set():
                        desired_wallets, desired_tokens = self.get_subscriptions()
                        desired_wallets, desired_tokens = set(desired_wallets), set(desired_tokens)
                        if desired_wallets != subscribed_wallets:
                            self._send_diff(ws, "subscribeAccountTrade", "unsubscribeAccountTrade",
                                            subscribed_wallets, desired_wallets)
                            subscribed_wallets = desired_wallets
                            logger.info("PumpPortal account subscriptions updated: %d wallets", len(subscribed_wallets))
                        if desired_tokens != subscribed_tokens:
                            self._send_diff(ws, "subscribeTokenTrade", "unsubscribeTokenTrade",
                                            subscribed_tokens, desired_tokens)
                            subscribed_tokens = desired_tokens
                            logger.info("PumpPortal token-price subscriptions updated: %d mints", len(subscribed_tokens))
                        try:
                            raw = ws.recv(timeout=1)
                        except TimeoutError:
                            continue
                        if not isinstance(raw, str):
                            continue
                        self.last_message_at = time.time()
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
                        if tx_type not in {"buy", "sell"} or not mint:
                            continue
                        if trader and trader in subscribed_wallets:
                            event = dict(message)
                            event["wallet"] = trader
                            event["side"] = tx_type
                            event["source"] = "account_trade"
                            self.output_queue.put(event)
                        elif mint in subscribed_tokens:
                            event = dict(message)
                            event["wallet"] = str(trader or "")
                            event["side"] = tx_type
                            event["source"] = "token_trade"
                            self.output_queue.put(event)
            except Exception as exc:
                self.connected.clear()
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("PumpPortal feed disconnected: %s; reconnecting in 3 seconds", self.last_error)
                self._stop_event.wait(3)
        self.connected.clear()
