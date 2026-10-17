# Native Pump.fun / PumpSwap execution

The live trading path builds transactions with the official `@pump-fun/pump-sdk` and
`@pump-fun/pump-swap-sdk` packages. It does **not** ask PumpPortal's Local Transaction
API to build trade transactions. PumpPortal may still be used by the separate account-trade
data feed; that feed does not sign or submit trades.

## Install

From the project folder, with the bot stopped:

```powershell
npm.cmd install
.\.venv\Scripts\python.exe -m compileall -q .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
npm.cmd run check
npm.cmd run check:sdk
```

Node.js is required only for the native live transaction builder. The SDK packages are pinned
in `package.json`. Keep `.env`, the keypair JSON file, and all API credentials local.

## Execution safeguards

- The Node helper reads on-chain curve/pool state and constructs an **unsigned** transaction with the official SDKs.
- The Python executor checks the fee payer, signer count, direct venue program, mint/action/amount, ComputeBudget instructions, SOL wrapping, ATA creation and close destinations.
- The transaction is simulated before the private key is used to sign it. If construction, validation, or simulation fails, it is not submitted.
- PumpPortal's `FAdo9…` router is explicitly refused.
- Native execution currently supports SOL-quoted Pump curves and canonical SOL/WSOL-quoted PumpSwap pools. Token-quoted coins and cashback curves fail closed instead of falling back to PumpPortal.
- Network fees and ATA/volume-account rent are additional to the configured trade notional.

## Live rollout checklist

Do not clear a manual-review halt just to install this change. Keep auto-copy OFF while testing. First run the build/tests above, then start the bot with copying disabled and verify it loads the native SDK. Before any live entry is re-armed, inspect the generated transaction path and wallet state. A successful Python test suite does not guarantee a trade will land, that the token can be sold, or that funds cannot be lost.
