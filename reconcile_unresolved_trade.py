"""Safely reconcile a verified non-landed live trade without replaying it."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from native_pump import NativePumpExecutor
from pumpfun_config import PumpFunSettings


PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
EXPECTED_MINT = "CVSUtRShLP8La3uCp7egWo4v8qsaYvC6Xpn3Me7Rpump"
EXPECTED_SIGNATURE = (
    "646pbqY3hFR8zoKuSyuihXKgxgyuxr47DGnXThZ8s68x4YEJz7Wn7RQSV6c9UFdHqPAY8Xbj81twQz9EjZG2cPqk"
)
EXPECTED_BALANCE_RAW = 166848492575


def rpc_call(url: str, method: str, params: list) -> object:
    response = requests.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=12,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(f"RPC {method} returned an error: {payload['error']}")
    if "result" not in payload:
        raise RuntimeError(f"RPC {method} response has no result field")
    return payload["result"]


def wallet_token_balance_raw(url: str, wallet: str, mint: str) -> int:
    result = rpc_call(
        url,
        "getTokenAccountsByOwner",
        [wallet, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
    )
    rows = (result or {}).get("value", [])
    total = 0
    for row in rows:
        total += int(
            row["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]
        )
    return total


def set_state(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO state(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile one expired, unlanded live trade. Dry-run by default; "
            "pass --apply only after the RPC and ledger checks pass."
        )
    )
    parser.add_argument("--mint", required=True)
    parser.add_argument("--signature", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write a backup and reconcile the verified transaction in SQLite.",
    )
    args = parser.parse_args()

    if args.mint != EXPECTED_MINT or args.signature != EXPECTED_SIGNATURE:
        raise SystemExit(
            "ABORT: this guarded script is restricted to the currently reviewed "
            "CVSU unresolved SELL. Review the source before using it for another trade."
        )

    settings = PumpFunSettings.load()
    db = settings.database_path
    if not db.is_absolute():
        db = Path(__file__).resolve().parent / db
    db = db.resolve()
    if db.name != "memecoin-pumpfun-live.db" or not db.is_file():
        raise SystemExit(f"ABORT: expected live database file was not found: {db}")

    wallet = str(NativePumpExecutor.load_keypair(settings.keypair_path).pubkey())
    target_key = f"unresolved_trade:{args.mint}"
    pending_key = f"exit_pending:{args.mint}"

    with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT value FROM state WHERE key=?", (target_key,)
        ).fetchone()
        if row is None:
            raise SystemExit(f"ABORT: no unresolved-trade record exists for {args.mint}")
        try:
            record = json.loads(row["value"])
        except (TypeError, ValueError) as exc:
            raise SystemExit("ABORT: unresolved-trade record is not valid JSON") from exc

        if (
            record.get("signature") != args.signature
            or record.get("action") != "sell"
            or record.get("mint") != args.mint
            or "confirmation timed out" not in str(record.get("detail", "")).lower()
        ):
            raise SystemExit("ABORT: unresolved record does not match the expected timed-out SELL")

        if record.get("source_wallet") != "29yFzeBZgxf5zqrAkKXwgZtQehRf4pL8WbV2nRJikbw8":
            raise SystemExit("ABORT: unexpected leader wallet in unresolved record")
        if record.get("source_signature") != (
            "kKeDv2u2dRnSzoHGgy43anDJXgjRS631j9BhRuviHrdSc6Qw7uX8HxRKYLVmX6xKY4iVX3fTBctmSeAEfFY9Ern"
        ):
            raise SystemExit("ABORT: unexpected leader source signature in unresolved record")

        recorded_at = datetime.fromisoformat(str(record.get("recorded_at", "")))
        if recorded_at.tzinfo is None:
            raise SystemExit("ABORT: unresolved record timestamp has no timezone")
        age_seconds = (datetime.now(timezone.utc) - recorded_at.astimezone(timezone.utc)).total_seconds()
        if age_seconds < 300:
            raise SystemExit("ABORT: timeout is less than five minutes old; wait and recheck")

        state = dict(con.execute(
            """SELECT key,value FROM state WHERE key IN (
                'manual_review_halt','manual_review_reason',
                'auto_copy_enabled','copy_paused'
            )"""
        ).fetchall())
        if state.get("manual_review_halt") != "1":
            raise SystemExit("ABORT: manual-review halt is not active")

        # The reason can be overwritten by a later generic loop exception in older
        # bot versions. The structured unresolved record above is authoritative for
        # the exact signature; accept only known generic replacement reasons, never
        # an unrelated transaction-specific reason.
        current_reason = state.get("manual_review_reason", "").strip()
        allowed_generic_reasons = {
            "Runtime exception; reconcile wallet and transactions before resuming",
            (
                f"RPC wallet monitoring error for {record['source_wallet']}; "
                "reconcile transactions before resuming"
            ),
        }
        if args.signature not in current_reason and current_reason not in allowed_generic_reasons:
            raise SystemExit(
                "ABORT: manual-review reason is neither this signature nor a known "
                "generic replacement reason; inspect it before reconciling."
            )

        positions = con.execute(
            "SELECT amount_raw FROM positions WHERE mint=?", (args.mint,)
        ).fetchone()
        if positions is None:
            raise SystemExit("ABORT: tracked position is missing; reconcile it manually")
        tracked_raw = int(positions["amount_raw"])
        if tracked_raw != EXPECTED_BALANCE_RAW:
            raise SystemExit(
                f"ABORT: tracked position is {tracked_raw} raw units, "
                f"expected {EXPECTED_BALANCE_RAW}; no database changes made"
            )

        all_unresolved = con.execute(
            "SELECT key,value FROM state WHERE key GLOB 'unresolved_trade:*'"
        ).fetchall()
        if len(all_unresolved) != 1 or all_unresolved[0]["key"] != target_key:
            raise SystemExit(
                "ABORT: another unresolved trade exists. Reconcile each signature "
                "before clearing the manual-review halt."
            )

        pending_row = con.execute(
            "SELECT value FROM state WHERE key=?", (pending_key,)
        ).fetchone()
        pending_value = str(pending_row["value"]) if pending_row else ""
        source_prefix = (
            f"followed wallet {record['source_wallet']} partial/full sell on "
        )
        if pending_value and not pending_value.startswith(source_prefix):
            raise SystemExit(
                f"ABORT: unrelated pending exit exists ({pending_value!r}); "
                "it was not cleared."
            )

    endpoints = []
    for name, url in (
        ("configured RPC", settings.rpc_url),
        ("Solana public RPC", PUBLIC_RPC),
    ):
        if url and all(url != item[1] for item in endpoints):
            endpoints.append((name, url))
    if len(endpoints) < 2:
        raise SystemExit(
            "ABORT: need two distinct RPC endpoints for this reconciliation."
        )

    checked = []
    for name, url in endpoints:
        try:
            status_result = rpc_call(
                url,
                "getSignatureStatuses",
                [[args.signature], {"searchTransactionHistory": True}],
            )
            statuses = (status_result or {}).get("value")
            if statuses is None or any(value is not None for value in statuses):
                raise RuntimeError("signature has a status or returned malformed status data")

            tx = rpc_call(
                url,
                "getTransaction",
                [
                    args.signature,
                    {
                        "encoding": "jsonParsed",
                        "maxSupportedTransactionVersion": 1,
                        "commitment": "confirmed",
                    },
                ],
            )
            if tx is not None:
                raise RuntimeError("transaction is present in history")

            balance = wallet_token_balance_raw(url, wallet, args.mint)
            if balance != tracked_raw:
                raise RuntimeError(
                    f"wallet balance {balance} does not match tracked position {tracked_raw}"
                )
            checked.append({"rpc": name, "balance_raw": balance, "status": None, "transaction": None})
            print(f"{name}: no signature status, no indexed transaction, balance={balance}")
        except Exception as exc:
            raise SystemExit(
                f"ABORT: {name} could not independently verify the non-landed SELL "
                f"({type(exc).__name__}: {exc}). No database changes made."
            ) from exc

    pending_prefix = f"followed wallet {record['source_wallet']} partial/full sell on "
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    audit_key = f"reconciled_not_landed_trade:{args.mint}:{args.signature}"
    audit_record = {
        **record,
        "resolution": "not_landed_verified",
        "resolution_detail": (
            "Old timeout; two RPCs report no status or indexed transaction, and both "
            "report the unchanged wallet token balance matching the tracked position."
        ),
        "wallet": wallet,
        "token_balance_raw": tracked_raw,
        "rpc_checks": checked,
        "resolved_at": now,
        "action_taken": "keep_position_unchanged_and_skip_old_source_signal",
    }

    if not args.apply:
        print("\nDRY RUN ONLY: all checks passed.")
        print("Would archive the unresolved record and clear its halt.")
        print("Would clear exit_pending only if it belongs to this exact source-driven sell.")
        print("Would leave the token position and trade ledger unchanged.")
        print("Auto-copy would remain OFF and copy_paused would remain ON.")
        print("Re-run with --apply to commit these narrowly scoped changes.")
        return 0

    backup = db.with_name(
        f"{db.stem}.before-reconcile-{datetime.now():%Y%m%d-%H%M%S}.db"
    )
    with sqlite3.connect(db) as src:
        with sqlite3.connect(backup) as dst:
            src.backup(dst)

    con = sqlite3.connect(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        latest = con.execute(
            "SELECT value FROM state WHERE key=?", (target_key,)
        ).fetchone()
        if latest is None or latest[0] != row["value"]:
            raise RuntimeError("unresolved record changed during reconciliation; aborting")
        if con.execute(
            "SELECT amount_raw FROM positions WHERE mint=?", (args.mint,)
        ).fetchone()[0] != str(tracked_raw):
            raise RuntimeError("tracked position changed during reconciliation; aborting")

        con.execute("DELETE FROM state WHERE key=?", (target_key,))
        set_state(con, audit_key, json.dumps(audit_record, sort_keys=True, separators=(",", ":")))
        pending_row = con.execute(
            "SELECT value FROM state WHERE key=?", (pending_key,)
        ).fetchone()
        pending_value = str(pending_row[0]) if pending_row else ""
        if pending_value.startswith(pending_prefix):
            set_state(con, pending_key, "")

        remaining = con.execute(
            "SELECT key,value FROM state WHERE key GLOB 'unresolved_trade:*' ORDER BY key"
        ).fetchall()
        if not remaining:
            set_state(con, "manual_review_halt", "0")
            set_state(con, "manual_review_reason", "")
        else:
            set_state(con, "manual_review_halt", "1")
            set_state(
                con,
                "manual_review_reason",
                "Other unresolved trades remain: " + ", ".join(row[0] for row in remaining),
            )

        # Keep copying disabled until the user explicitly rearms it in the dashboard.
        set_state(con, "auto_copy_enabled", "0")
        set_state(con, "copy_paused", "1")
        con.commit()
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()

    print("\nRECONCILED.")
    print("Backup:", backup)
    print("The tracked position and trade ledger were not changed.")
    print("The old leader sell signal will not be replayed.")
    print("auto_copy_enabled=0; copy_paused=1. Restart the bot paused, inspect the dashboard,")
    print("then explicitly re-enable copying for future signals only.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (requests.RequestException, sqlite3.Error) as exc:
        raise SystemExit(f"ABORT: {type(exc).__name__}: {exc}") from exc
