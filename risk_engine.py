"""Deterministic entry safety gate and durable order-reservation coordinator.

This module does not sign, submit, or manage transactions. Every strategy must
pass through this independent gate immediately before risk-increasing execution.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

RECONCILIATION_CONFIRMATION = "I_VERIFIED_THE_ONCHAIN_TRANSACTION_AND_WALLET_BALANCES"


class RiskEngine:
    """Fail-closed operator gate for entries, risk reservations, and recovery."""

    def __init__(self, store, settings):
        self.store = store
        self.settings = settings

    def engage_on_startup(self) -> None:
        """Require an explicit operator resume after every process startup."""
        self.store.set_entry_kill_switch(
            True,
            "Process startup: new entries remain halted until explicitly resumed from the local dashboard.",
        )
        self.store.recover_entry_reservations_on_startup(
            mode=str(getattr(self.settings, "mode", "paper"))
        )

    def kill_switch_active(self) -> bool:
        return self.store.get_state("entry_kill_switch_active", "1") != "0"

    def kill_switch_reason(self) -> str:
        return self.store.get_state(
            "entry_kill_switch_reason",
            "Entry kill switch is active; explicit operator resume is required.",
        )

    def gate_reason(self) -> str:
        """Return a rejection reason, or an empty string if entry admission is open."""
        try:
            if self.kill_switch_active():
                return "Global entry kill switch is active: " + self.kill_switch_reason()
            unresolved = self.store.list_state_prefix("unresolved_trade:", limit=1)
            if unresolved:
                return "An unresolved submitted transaction exists; reconcile it before resuming entries."
            active = self.store.active_entry_reservations()
            if active:
                first = active[0]
                return (
                    "An active order reservation requires reconciliation "
                    f"(mint {str(first.get('mint') or '')[:8]}, status {first.get('status') or 'unknown'})."
                )
        except Exception:
            return "Risk-control state could not be read; refusing new entries."
        return ""

    def activate_kill_switch(self, reason: str) -> None:
        clean = " ".join(str(reason or "Operator or safety gate activated").split())[:1000]
        self.store.set_entry_kill_switch(True, clean)

    def resume_entries(self) -> tuple[bool, str]:
        """Resume only after unresolved transactions and active reservations are gone."""
        try:
            unresolved = self.store.list_state_prefix("unresolved_trade:", limit=1000)
            if unresolved:
                return False, "Unresolved transactions remain. Reconcile each transaction first."
            active = self.store.active_entry_reservations()
            if active:
                return False, (
                    f"{len(active)} active order reservation(s) remain. Reconcile or resolve them first."
                )
            self.store.set_entry_kill_switch(
                False,
                "Entries explicitly resumed by the operator from the loopback dashboard.",
            )
            return True, "Entry gate opened by explicit operator action."
        except Exception:
            return False, "Risk state could not be verified; the entry gate remains halted."

    def reserve_entry(
        self,
        mint: str,
        strategy: str,
        amount_usd: float,
        *,
        exposure_usd: float | None = None,
    ) -> tuple[str | None, str]:
        """Atomically reserve position/exposure capacity before building or submitting."""
        reason = self.gate_reason()
        if reason:
            return None, reason
        try:
            amount = float(amount_usd)
            exposure = amount if exposure_usd is None else float(exposure_usd)
            if not math.isfinite(amount) or not math.isfinite(exposure) or amount <= 0 or exposure < amount:
                return None, "Entry amount or reserved exposure is invalid."
            if not mint or strategy not in {"leader_copy", "token_discovery", "trending_discovery"}:
                return None, "Unsupported strategy or missing mint."
            configured_cap = float(getattr(self.settings, "max_entry_usd", 0.50))
            if strategy == "token_discovery":
                configured_cap = min(configured_cap, 0.50, float(getattr(self.settings, "token_discovery_max_entry_usd", 0.50)))
            elif strategy == "trending_discovery":
                configured_cap = min(configured_cap, 0.50, float(getattr(self.settings, "trending_discovery_max_entry_usd", 0.50)))
            if amount > configured_cap + 1e-9:
                return None, f"Requested input USD {amount:.2f} exceeds the USD {configured_cap:.2f} strategy cap."
            reservation_id = uuid4().hex
            denied = self.store.try_reserve_entry_atomic(
                reservation_id=reservation_id,
                mint=mint,
                strategy=strategy,
                amount_usd=exposure,
                max_total_exposure_usd=float(getattr(self.settings, "max_total_exposure_usd", 1.00)),
                max_open_positions=int(getattr(self.settings, "max_open_positions", 1)),
                max_entries_per_day=int(getattr(self.settings, "max_entries_per_day", 4)),
            )
            if denied:
                return None, denied
            self.store.set_state(f"risk_reservation_for_mint:{mint}", reservation_id)
            return reservation_id, ""
        except Exception:
            return None, "Risk reservation failed; refusing to submit an order."

    def mark_submitting(self, reservation_id: str) -> tuple[bool, str]:
        """Last local admission check; fails if the kill switch was engaged meanwhile."""
        try:
            return self.store.mark_entry_reservation_submitting(reservation_id)
        except Exception:
            return False, "Could not persist pre-submission state; refusing order."

    def release_entry(self, reservation_id: str, *, status: str = "released", detail: str = "") -> None:
        """Release only known-resolved reservations; unresolved statuses remain active."""
        if not reservation_id:
            return
        try:
            row = self.store.get_entry_reservation(reservation_id)
            mint = str((row or {}).get("mint") or "")
            self.store.set_entry_reservation_status(reservation_id, status, detail)
            if mint and status not in {"reserved", "submitting", "uncertain"}:
                self.store.delete_state(f"risk_reservation_for_mint:{mint}")
        except Exception as exc:
            self.activate_kill_switch(
                f"Could not settle risk reservation {reservation_id[:8]} ({type(exc).__name__}); manual reconciliation required."
            )

    def mark_uncertain(self, mint: str, reason: str) -> None:
        reservation_id = self.store.get_state(f"risk_reservation_for_mint:{mint}", "")
        if reservation_id:
            try:
                self.store.set_entry_reservation_status(reservation_id, "uncertain", reason)
            except Exception:
                pass
        self.activate_kill_switch(reason)

    def acknowledge_reconciled_trade(
        self, mint: str, signature: str, confirmation: str
    ) -> tuple[bool, str]:
        """Clear an ambiguity only after a human explicitly confirms chain/balance review."""
        if confirmation != RECONCILIATION_CONFIRMATION:
            return False, "The exact reconciliation confirmation phrase was not supplied."
        key = f"unresolved_trade:{mint}"
        raw = self.store.get_state(key, "")
        if not raw:
            return False, "No unresolved trade record exists for that mint."
        try:
            record = json.loads(raw)
        except (TypeError, ValueError):
            return False, "Unresolved trade record is malformed; do not clear it automatically."
        expected = str(record.get("signature") or "")
        if not expected or signature != expected:
            return False, "The supplied signature does not match the recorded unresolved transaction."
        reservation_id = self.store.get_state(f"risk_reservation_for_mint:{mint}", "")
        if reservation_id:
            self.release_entry(
                reservation_id,
                status="reconciled",
                detail="Operator acknowledged chain status and wallet balances.",
            )
        self.store.set_state(
            f"reconciliation_ack:{mint}",
            json.dumps({
                "signature": expected,
                "acknowledged_at": datetime.now(timezone.utc).isoformat(),
                "operator_confirmed": True,
            }, sort_keys=True),
        )
        self.store.delete_state(key)
        self.store.delete_state(f"unresolved_trade_signature:{mint}")
        return True, "Reconciliation acknowledgement recorded. Entries remain halted until explicitly resumed."

    def acknowledge_uncertain_reservation(
        self, reservation_id: str, confirmation: str
    ) -> tuple[bool, str]:
        """Resolve a startup-interrupted reservation after operator chain/balance review."""
        if confirmation != RECONCILIATION_CONFIRMATION:
            return False, "The exact reconciliation confirmation phrase was not supplied."
        row = self.store.get_entry_reservation(reservation_id)
        if not row or row.get("status") != "uncertain":
            return False, "Only an uncertain reservation can be acknowledged here."
        mint = str(row.get("mint") or "")
        if self.store.get_state(f"unresolved_trade:{mint}", ""):
            return False, "A recorded unresolved transaction exists for this mint; acknowledge its exact signature instead."
        self.release_entry(
            reservation_id,
            status="reconciled",
            detail="Operator acknowledged transaction history and wallet balances.",
        )
        return True, "Reservation marked reconciled. Entries remain halted until explicitly resumed."

    def snapshot(self) -> dict[str, Any]:
        unresolved_rows = []
        for key, value in self.store.list_state_prefix("unresolved_trade:", limit=1000):
            mint = key[len("unresolved_trade:"):]
            try:
                record = json.loads(value)
            except (TypeError, ValueError):
                record = {"detail": "Malformed unresolved record; inspect the local database."}
            unresolved_rows.append({
                "mint": mint,
                "signature": str(record.get("signature") or ""),
                "action": str(record.get("action") or ""),
                "detail": str(record.get("detail") or ""),
                "recorded_at": str(record.get("recorded_at") or ""),
            })
        try:
            active = self.store.active_entry_reservations()
        except Exception:
            active = []
        return {
            "kill_switch_active": self.kill_switch_active(),
            "kill_switch_reason": self.kill_switch_reason(),
            "active_reservation_count": len(active),
            "active_reservations": active,
            "unresolved_trade_count": len(unresolved_rows),
            "unresolved_trades": unresolved_rows,
            "max_entry_usd": float(getattr(self.settings, "max_entry_usd", 0.50)),
            "max_total_exposure_usd": float(getattr(self.settings, "max_total_exposure_usd", 1.00)),
            "max_open_positions": int(getattr(self.settings, "max_open_positions", 1)),
            "max_entries_per_day": int(getattr(self.settings, "max_entries_per_day", 4)),
            "max_portfolio_drawdown_pct": float(getattr(self.settings, "max_portfolio_drawdown_pct", 0.15)),
        }
