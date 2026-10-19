import base64
import unittest
from types import SimpleNamespace

from solders.pubkey import Pubkey

from jupiter_ultra import (
    ADDRESS_LOOKUP_TABLE_PROGRAM_ID,
    JupiterUltraExecutor,
    WSOL_MINT,
)
from native_pump import TransactionBuildFailed


class JupiterOrderValidationTests(unittest.TestCase):
    def setUp(self):
        self.input_mint = WSOL_MINT
        self.output_mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    def _order(self, **overrides):
        row = {
            # Swap API V2 may omit echoed inAmount/inputMint/outputMint and the
            # legacy otherAmountThreshold; input is validated from our request
            # and the serialized transaction is inspected before signing.
            "outAmount": "1000000",
            "transaction": "nonempty-unsigned-transaction",
            "requestId": "request-1",
            "router": "metis",
        }
        row.update(overrides)
        return row

    def _validate(self, order, *, max_impact=None, expected_output=None, input_value=None):
        return JupiterUltraExecutor._validate_order_fields(
            order,
            input_mint=self.input_mint,
            output_mint=self.output_mint,
            amount_raw=5_000_000,
            max_price_impact_pct=max_impact,
            expected_output_value_usd=expected_output,
            input_value_usd=input_value,
            output_decimals=6,
        )

    def test_v2_order_without_legacy_echo_or_threshold_fields_is_accepted(self):
        self.assertEqual(self._validate(self._order()), 1_000_000)

    def test_order_rejects_wrong_mint_pair(self):
        with self.assertRaisesRegex(TransactionBuildFailed, "mint mismatch"):
            self._validate(self._order(outputMint=self.input_mint))

    def test_order_rejects_mismatched_echoed_input_amount(self):
        with self.assertRaisesRegex(TransactionBuildFailed, "hard cap"):
            self._validate(self._order(inAmount="5000001"))

    def test_order_rejects_invalid_minimum_output_when_supplied(self):
        with self.assertRaisesRegex(TransactionBuildFailed, "minimum output"):
            self._validate(self._order(otherAmountThreshold="0"))

    def test_order_rejects_api_price_impact_above_configured_maximum(self):
        with self.assertRaisesRegex(TransactionBuildFailed, "price impact"):
            self._validate(
                self._order(priceImpactPct="0.06"),
                max_impact=5.0,
                expected_output=0.50,
                input_value=0.50,
            )

    def test_order_rejects_quote_more_than_seven_percent_below_independent_mark(self):
        with self.assertRaisesRegex(TransactionBuildFailed, "below the independent market mark"):
            self._validate(
                self._order(),
                max_impact=5.0,
                expected_output=0.46,
                input_value=0.50,
            )

    def test_order_requires_transaction_and_request_id(self):
        with self.assertRaisesRegex(TransactionBuildFailed, "transaction or request ID"):
            self._validate(self._order(transaction=""))


class TransactionInstructionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.executor = JupiterUltraExecutor(rpc=object(), api_key="test-key")
        self.owner = "SysvarRent111111111111111111111111111111111"
        self.input_mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        self.output_mint = WSOL_MINT

    @staticmethod
    def _ix(program_index, data, accounts=b""):
        return SimpleNamespace(
            program_id_index=program_index,
            data=data,
            accounts=accounts,
        )

    def _validate(self, instructions, keys, allowed=None, input_mint=None, output_mint=None, amount=1000):
        allowed = set(allowed or keys)
        return self.executor._validate_transaction_instructions(
            instructions=instructions,
            keys=keys,
            owner=self.owner,
            input_mint=input_mint or self.input_mint,
            output_mint=output_mint or self.output_mint,
            amount_raw=amount,
            allowed_programs=allowed,
        )

    def test_transaction_without_swap_instruction_is_rejected(self):
        data = bytes([2]) + (500_000).to_bytes(4, "little")
        with self.assertRaisesRegex(TransactionBuildFailed, "no allowlisted swap route"):
            self._validate(
                [self._ix(0, data)],
                [ "ComputeBudget111111111111111111111111111111" ],
                allowed={"ComputeBudget111111111111111111111111111111"},
            )

    def test_priority_fee_above_one_millions_lamports_is_rejected(self):
        compute_budget = "ComputeBudget111111111111111111111111111111"
        cu_limit = self._ix(0, bytes([2]) + (1_400_000).to_bytes(4, "little"))
        cu_price = self._ix(0, bytes([3]) + (1_000_000).to_bytes(8, "little"))
        route = self._ix(1, b"\x01")
        with self.assertRaisesRegex(TransactionBuildFailed, "priority fee exceeds"):
            self._validate(
                [cu_limit, cu_price, route],
                [compute_budget, "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"],
                allowed={compute_budget, "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"},
            )

    def test_reasonable_priority_fee_and_allowlisted_route_are_accepted(self):
        compute_budget = "ComputeBudget111111111111111111111111111111"
        cu_limit = self._ix(0, bytes([2]) + (1_000_000).to_bytes(4, "little"))
        cu_price = self._ix(0, bytes([3]) + (100_000).to_bytes(8, "little"))
        route = self._ix(1, b"\x01")
        self._validate(
            [cu_limit, cu_price, route],
            [compute_budget, "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"],
            allowed={compute_budget, "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"},
        )

    def test_bounded_sol_input_rejects_transfer_above_exact_buy_amount(self):
        from native_pump import SYSTEM_PROGRAM_ID
        owner = self.owner
        wsol_ata = self.executor._expected_wsol_ata(owner)
        amount = 50_000
        transfer_data = (2).to_bytes(4, "little") + (amount + 1).to_bytes(8, "little")
        transfer = self._ix(2, transfer_data, accounts=bytes([0, 1]))
        with self.assertRaisesRegex(TransactionBuildFailed, "unexpected SOL transfer"):
            self._validate(
                [transfer],
                [owner, wsol_ata, SYSTEM_PROGRAM_ID],
                allowed={SYSTEM_PROGRAM_ID},
                input_mint=WSOL_MINT,
                output_mint=self.input_mint,
                amount=amount,
            )


class AddressLookupResolutionTests(unittest.TestCase):
    def test_v0_lookup_table_writable_and_readonly_keys_are_resolved(self):
        writable = Pubkey.from_string(WSOL_MINT)
        readonly = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
        table_bytes = b"\x00" * 56 + bytes(writable) + bytes(readonly)

        class FakeRPC:
            def call(self, method, params):
                if method != "getAccountInfo":
                    raise AssertionError(f"Unexpected RPC method: {method}")
                return {
                    "value": {
                        "owner": ADDRESS_LOOKUP_TABLE_PROGRAM_ID,
                        "data": [base64.b64encode(table_bytes).decode("ascii"), "base64"],
                    }
                }

        lookup = SimpleNamespace(
            account_key=Pubkey.from_string("11111111111111111111111111111111"),
            writable_indexes=[0],
            readonly_indexes=[1],
        )
        message = SimpleNamespace(
            account_keys=[Pubkey.from_string("SysvarRent111111111111111111111111111111111")],
            address_table_lookups=[lookup],
        )
        executor = JupiterUltraExecutor(FakeRPC(), api_key="test-key")
        resolved = executor._resolved_account_keys(message)
        self.assertEqual(resolved, [
            "SysvarRent111111111111111111111111111111111",
            WSOL_MINT,
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        ])

    def test_non_lookup_account_cannot_be_used_as_an_address_table(self):
        class FakeRPC:
            def call(self, method, params):
                return {
                    "value": {
                        "owner": "11111111111111111111111111111111",
                        "data": [base64.b64encode(b"\x00" * 88).decode("ascii"), "base64"],
                    }
                }

        lookup = SimpleNamespace(
            account_key=Pubkey.from_string("11111111111111111111111111111111"),
            writable_indexes=[0],
            readonly_indexes=[],
        )
        message = SimpleNamespace(
            account_keys=[Pubkey.from_string("SysvarRent111111111111111111111111111111111")],
            address_table_lookups=[lookup],
        )
        executor = JupiterUltraExecutor(FakeRPC(), api_key="test-key")
        with self.assertRaisesRegex(TransactionBuildFailed, "not an address lookup table"):
            executor._resolved_account_keys(message)


if __name__ == "__main__":
    unittest.main()
