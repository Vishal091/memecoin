import hashlib
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from solders.pubkey import Pubkey

from native_pump import (
    COMPUTE_BUDGET_ID,
    PUMP_PROGRAM_ID,
    PUMPPORTAL_ROUTER_ID,
    PUMPSWAP_PROGRAM_ID,
    PUMP_BUY_EXACT_QUOTE_IN_V3,
    PUMP_SELL_V3,
    PUMPSWAP_BUY_V2,
    NativePumpExecutor,
    TransactionBuildFailed,
    validate_native_transaction,
)


def ix(program_index, account_indexes=(), data=b""):
    return SimpleNamespace(
        program_id_index=program_index,
        accounts=bytes(account_indexes),
        data=bytes(data),
    )


def fake_tx(route_program, trade_data, route="pump", accounts=None, extra_programs=()):
    wallet = str(Pubkey.new_unique())
    mint = str(Pubkey.new_unique())
    keys = [
        wallet,
        COMPUTE_BUDGET_ID,
        route_program,
        mint,
        "global-config",
        "quote-mint",
        "token-program",
        "curve-or-pool",
        "vault-a",
        "vault-b",
        "user-volume",
        "fee-config",
        "buyback-fee-recipient",
        "system-program",
        "event-authority",
        "venue-program",
    ]
    for program in extra_programs:
        if program not in keys:
            keys.append(program)
    limit_data = b"\x02" + struct.pack("<I", 400_000)
    price_data = b"\x03" + struct.pack("<Q", 25)
    trade_accounts = accounts if accounts is not None else (
        [4, 3, 5, 6, 7, 8, 9, 10, 0] if route == "pump"
        else [7, 0, 4, 3, 5, 8, 9, 6, 6, 6, 6, 13, 10, 11, 12, 13, 14]
    )
    tx = SimpleNamespace(message=SimpleNamespace(
        account_keys=keys,
        header=SimpleNamespace(num_required_signatures=1),
        address_table_lookups=(),
        instructions=[
            ix(1, data=limit_data),
            ix(1, data=price_data),
            ix(2, account_indexes=trade_accounts, data=trade_data),
        ],
    ))
    return tx, wallet, mint




class NativeBuildFailureTests(unittest.TestCase):
    def test_nonzero_builder_exit_is_classified_as_pre_submission_failure(self):
        executor = object.__new__(NativePumpExecutor)
        executor.node = "node"
        executor.timeout = 12
        completed = SimpleNamespace(
            returncode=1,
            stderr="Error: Native executor only supports SOL-quoted Pump tokens; refusing a token-quoted coin.",
            stdout="",
        )
        with patch("native_pump.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(TransactionBuildFailed, "token-quoted coin"):
                executor._build_unsigned({"mint": "test-mint", "action": "buy"})

class NativeTransactionGuardTests(unittest.TestCase):
    def validate(self, tx, wallet, mint, action="buy", route="pump", budget=1_000, raw=100):
        validate_native_transaction(
            tx,
            wallet=wallet,
            mint=mint,
            action=action,
            route=route,
            amount_lamports=budget,
            amount_raw=raw,
            priority_fee_limit_lamports=100,
        )

    def test_accepts_direct_pump_exact_quote_buy_within_budget(self):
        data = PUMP_BUY_EXACT_QUOTE_IN_V3 + struct.pack("<QQ", 900, 123)
        tx, wallet, mint = fake_tx(PUMP_PROGRAM_ID, data)
        self.validate(tx, wallet, mint)

    def test_rejects_pump_buy_above_requested_budget(self):
        data = PUMP_BUY_EXACT_QUOTE_IN_V3 + struct.pack("<QQ", 1_001, 123)
        tx, wallet, mint = fake_tx(PUMP_PROGRAM_ID, data)
        with self.assertRaisesRegex(RuntimeError, "exceeds the requested SOL buy budget"):
            self.validate(tx, wallet, mint, budget=1_000)

    def test_rejects_pumpportal_router(self):
        data = PUMP_BUY_EXACT_QUOTE_IN_V3 + struct.pack("<QQ", 900, 123)
        tx, wallet, mint = fake_tx(PUMPPORTAL_ROUTER_ID, data, extra_programs=[PUMPPORTAL_ROUTER_ID])
        with self.assertRaisesRegex(RuntimeError, "PumpPortal router"):
            self.validate(tx, wallet, mint)

    def test_accepts_direct_pumpswap_buy_v2_within_budget(self):
        data = PUMPSWAP_BUY_V2 + struct.pack("<QQ", 123, 900)
        tx, wallet, mint = fake_tx(
            PUMPSWAP_PROGRAM_ID,
            data,
            route="pump-amm",
            accounts=[7, 0, 4, 3, 5, 8, 9, 6, 6, 6, 6, 13, 10, 11, 12, 13, 14],
        )
        self.validate(tx, wallet, mint, route="pump-amm")

    def test_rejects_pumpswap_buy_over_budget(self):
        data = PUMPSWAP_BUY_V2 + struct.pack("<QQ", 123, 1_001)
        tx, wallet, mint = fake_tx(
            PUMPSWAP_PROGRAM_ID,
            data,
            route="pump-amm",
            accounts=[7, 0, 4, 3, 5, 8, 9, 6, 6, 6, 6, 13, 10, 11, 12, 13, 14],
        )
        with self.assertRaisesRegex(RuntimeError, "worst-case quote exceeds"):
            self.validate(tx, wallet, mint, route="pump-amm", budget=1_000)

    def test_rejects_wrong_raw_sell_amount(self):
        data = PUMP_SELL_V3 + struct.pack("<QQ", 99, 1)
        tx, wallet, mint = fake_tx(PUMP_PROGRAM_ID, data)
        with self.assertRaisesRegex(RuntimeError, "differs from the requested token quantity"):
            self.validate(tx, wallet, mint, action="sell", raw=100)

    def test_rejects_unexpected_program(self):
        unknown = "UnknownProgram111111111111111111111111111111"
        data = PUMP_BUY_EXACT_QUOTE_IN_V3 + struct.pack("<QQ", 900, 123)
        tx, wallet, mint = fake_tx(PUMP_PROGRAM_ID, data, extra_programs=[unknown])
        tx.message.instructions.insert(2, ix(len(tx.message.account_keys) - 1))
        with self.assertRaisesRegex(RuntimeError, "unapproved top-level program"):
            self.validate(tx, wallet, mint)

    def test_rejects_address_lookup_tables(self):
        data = PUMP_BUY_EXACT_QUOTE_IN_V3 + struct.pack("<QQ", 900, 123)
        tx, wallet, mint = fake_tx(PUMP_PROGRAM_ID, data)
        tx.message.address_table_lookups = ["unresolved lookup"]
        with self.assertRaisesRegex(RuntimeError, "address lookup tables"):
            self.validate(tx, wallet, mint)


if __name__ == "__main__":
    unittest.main()
