"""Low-latency Solana logsSubscribe monitor for watched-wallet transactions.

This is a push-based safety net beside PumpPortal's metered account-trade feed.
It only reports signatures; the bot fetches the confirmed transaction and
applies signer, Pump/PumpSwap program, and wallet-balance-delta checks before copying.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from websockets.sync.client import connect

logger = logging.getLogger(__name__)


class SolanaLogsFeed(threading.Thread):
    def __init__(self, rpc_url: str, output_queue: queue.Queue, timeout: int = 12):
        super().__init__(name="solana-logs-feed", daemon=True)
        self.websocket_url = self.to_websocket_url(rpc_url)
        self.output_queue = output_queue
        self.timeout = timeout
        self._lock = threading.Lock()
        self._desired_wallets: tuple[str, ...] = ()
        self._stop_event = threading.Event()
        self.connected = threading.Event()
        self.connected_at = 0.0
        self.last_message_at = 0.0
        self.last_notification_at = 0.0
        self.last_signature = ""
        self.messages_received = 0
        self.notifications_received = 0
        self.subscribed_wallet_count = 0
        self.subscription_errors = 0
        self.last_error = ""

    @staticmethod
    def to_websocket_url(rpc_url: str) -> str:
        parts = urlsplit(rpc_url.strip())
        schemes = {"https": "wss", "http": "ws", "wss": "wss", "ws": "ws"}
        scheme = schemes.get(parts.scheme.lower())
        if not scheme or not parts.netloc:
            raise ValueError("SOLANA_RPC_URL must be an HTTP(S) or WS(S) URL")
        return urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))

    def set_subscriptions(self, wallets) -> None:
        clean = tuple(dict.fromkeys(str(wallet).strip() for wallet in wallets if str(wallet).strip()))
        with self._lock:
            self._desired_wallets = clean

    def get_subscriptions(self) -> tuple[str, ...]:
        with self._lock:
            return self._desired_wallets

    def stop(self) -> None:
        self._stop_event.set()

    def _subscribe(self, ws, wallet: str, request_id: int, pending: dict[int, str]) -> int:
        ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [wallet]},
                {"commitment": "confirmed"},
            ],
        }))
        pending[request_id] = wallet
        return request_id + 1

    def run(self) -> None:
        while not self._stop_event.is_set():
            if not self.get_subscriptions():
                self._stop_event.wait(0.5)
                continue
            try:
                with connect(
                    self.websocket_url,
                    open_timeout=self.timeout,
                    close_timeout=3,
                    ping_interval=25,
                    max_size=2**20,
                ) as ws:
                    self.connected_at = time.time()
                    self.connected.set()
                    self.last_error = ""
                    logger.info("Connected to Solana logs WebSocket")
                    pending: dict[int, str] = {}
                    subscriptions: dict[str, int] = {}
                    request_id = 1

                    while not self._stop_event.is_set():
                        desired = set(self.get_subscriptions())

                        for wallet, subscription_id in list(subscriptions.items()):
                            if wallet not in desired:
                                ws.send(json.dumps({
                                    "jsonrpc": "2.0",
                                    "id": request_id,
                                    "method": "logsUnsubscribe",
                                    "params": [subscription_id],
                                }))
                                request_id += 1
                                del subscriptions[wallet]

                        pending_wallets = set(pending.values())
                        for wallet in sorted(desired - set(subscriptions) - pending_wallets):
                            request_id = self._subscribe(ws, wallet, request_id, pending)
                        self.subscribed_wallet_count = len(subscriptions)

                        try:
                            raw = ws.recv(timeout=1)
                        except TimeoutError:
                            continue
                        if not isinstance(raw, str):
                            continue

                        self.messages_received += 1
                        self.last_message_at = time.time()
                        try:
                            message = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(message, dict):
                            continue

                        if "id" in message and message.get("id") in pending:
                            wallet = pending.pop(message["id"])
                            if "error" in message:
                                self.subscription_errors += 1
                                self.last_error = "logsSubscribe request rejected"
                                logger.warning("Solana logs subscription rejected for %s", wallet[:8])
                                continue
                            subscription_id = message.get("result")
                            if isinstance(subscription_id, int):
                                subscriptions[wallet] = subscription_id
                                self.subscribed_wallet_count = len(subscriptions)
                                logger.info("Solana logs subscription active for wallet %s", wallet[:8])
                            continue

                        if message.get("method") != "logsNotification":
                            continue
                        params = message.get("params") or {}
                        subscription_id = params.get("subscription")
                        wallet = next(
                            (candidate for candidate, current_id in subscriptions.items()
                             if current_id == subscription_id),
                            None,
                        )
                        if not wallet:
                            continue
                        result = params.get("result") or {}
                        value = result.get("value") or {}
                        signature = value.get("signature")
                        if not signature or value.get("err") is not None:
                            continue
                        self.notifications_received += 1
                        self.last_notification_at = time.time()
                        self.last_signature = str(signature)
                        self.output_queue.put({
                            "wallet": wallet,
                            "signature": str(signature),
                            "slot": (result.get("context") or {}).get("slot"),
                            "source": "solana_logs",
                        })
            except Exception as exc:
                self.connected.clear()
                self.subscribed_wallet_count = 0
                # Avoid logging the full endpoint/exception because it could expose
                # a provider URL containing an API key.
                self.last_error = type(exc).__name__
                logger.warning(
                    "Solana logs WebSocket disconnected (%s); retrying in 3 seconds",
                    type(exc).__name__,
                )
                self._stop_event.wait(3)
        self.connected.clear()
        self.subscribed_wallet_count = 0
