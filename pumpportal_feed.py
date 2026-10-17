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
        self._desired: tuple[str, ...] = ()
        self._stop_event = threading.Event()
        self.connected = threading.Event()
        self.last_error = ""
        self.last_message_at = 0.0

    def set_wallets(self, wallets: Iterable[str]) -> None:
        unique = tuple(dict.fromkeys(w for w in wallets if w))
        with self._lock:
            self._desired = unique

    def get_wallets(self) -> tuple[str, ...]:
        with self._lock:
            return self._desired

    def stop(self) -> None:
        self._stop_event.set()

    def _send_subscriptions(self, ws, old: set[str], new: set[str]) -> None:
        add = sorted(new - old)
        remove = sorted(old - new)
        # Use one websocket and incrementally adjust its subscriptions.
        for start in range(0, len(remove), 100):
            ws.send(json.dumps({"method": "unsubscribeAccountTrade", "keys": remove[start:start + 100]}))
        for start in range(0, len(add), 100):
            ws.send(json.dumps({"method": "subscribeAccountTrade", "keys": add[start:start + 100]}))

    def run(self) -> None:
        if not self.api_key:
            self.last_error = "PumpPortal API key is not configured"
            return
        uri = "wss://pumpportal.fun/api/data?api-key=" + quote(self.api_key, safe="")
        while not self._stop_event.is_set():
            try:
                with connect(uri, open_timeout=self.timeout, close_timeout=3, ping_interval=20, max_size=2**20) as ws:
                    subscribed: set[str] = set()
                    self.connected.set()
                    self.last_error = ""
                    logger.info("Connected to PumpPortal account-trade feed")
                    # Establish current subscriptions immediately.
                    desired = set(self.get_wallets())
                    self._send_subscriptions(ws, subscribed, desired)
                    subscribed = desired
                    while not self._stop_event.is_set():
                        desired = set(self.get_wallets())
                        if desired != subscribed:
                            self._send_subscriptions(ws, subscribed, desired)
                            subscribed = desired
                            logger.info("PumpPortal account subscriptions updated: %d wallets", len(subscribed))
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
                        if not trader or trader not in subscribed:
                            # Ignore subscribe acknowledgements and unrelated events.
                            continue
                        tx_type = str(message.get("txType") or message.get("side") or "").lower()
                        if tx_type not in {"buy", "sell"}:
                            continue
                        mint = message.get("mint")
                        if not mint:
                            continue
                        event = dict(message)
                        event["wallet"] = trader
                        event["side"] = tx_type
                        event["source"] = "pumpportal_ws"
                        self.output_queue.put(event)
            except Exception as exc:
                self.connected.clear()
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("PumpPortal feed disconnected: %s; reconnecting in 3 seconds", self.last_error)
                self._stop_event.wait(3)
        self.connected.clear()
