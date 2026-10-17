import unittest
from unittest.mock import Mock, patch

from pumpportal import PumpPortalExecutor


class PumpPortalRouteFailureTests(unittest.TestCase):
    def setUp(self):
        self.executor = PumpPortalExecutor(rpc=Mock())
        self.executor.token_balance_raw = Mock(return_value=0)
        self.executor.sol_balance_lamports = Mock(return_value=1_000_000_000)

    def test_http_build_error_includes_status_and_response_body(self):
        self.executor.http = Mock()
        self.executor.http.post.return_value = Mock(
            status_code=400,
            text='{"error":"bonding curve not found"}',
        )

        with self.assertRaisesRegex(
            RuntimeError, r"HTTP 400:.*bonding curve not found"
        ):
            self.executor._build(
                owner="owner",
                mint="mint",
                action="buy",
                amount=0.01,
                denominated_in_sol=True,
                slippage_bps=300,
                pool="pump",
                keypair=None,
            )

    def test_all_route_build_errors_are_preserved_and_no_transaction_is_sent(self):
        def fail_build(owner, mint, action, amount, denominated_in_sol,
                       slippage_bps, pool, keypair):
            if pool == "pump-amm":
                raise RuntimeError("PumpPortal pump-amm build failed HTTP 400: pool missing")
            raise RuntimeError("PumpPortal pump build failed HTTP 400: curve unavailable")

        with patch.object(self.executor, "_build", side_effect=fail_build), \
             patch.object(self.executor, "_send_confirm") as send_confirm:
            with self.assertRaises(RuntimeError) as caught:
                self.executor.trade(
                    owner="owner",
                    keypair=object(),
                    mint="mint",
                    action="buy",
                    amount=0.01,
                    denominated_in_sol=True,
                    slippage_bps=300,
                    preferred_pool="pump-amm",
                )

        message = str(caught.exception)
        self.assertIn("No Pump ecosystem route for mint", message)
        self.assertIn("no transaction was submitted", message)
        self.assertIn("pump-amm: RuntimeError: PumpPortal pump-amm build failed HTTP 400: pool missing", message)
        self.assertIn("pump: RuntimeError: PumpPortal pump build failed HTTP 400: curve unavailable", message)
        send_confirm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
