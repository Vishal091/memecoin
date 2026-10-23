# Risk policy and recovery

## Trial defaults

The loader enforces a conservative experiment ceiling: maximum $0.50 swap input, $1.00 tracked and pending exposure, one open position, up to four entries per UTC day, a $0.50 daily loss limit, a $1.00 total-loss limit, a minimum 0.025 SOL reserve, and a maximum 15% peak-to-trough equity drawdown gate. Local configuration may make entries smaller or caps stricter but cannot raise the stated ceilings.

These are operational caps, not evidence of a profitable strategy. Swap input is not identical to total wallet debit: network fees, provider/protocol fees, priority fees and token-account rent can increase the debit.

## Global entry kill switch

- A new process starts with new entries halted.
- An operator must explicitly resume entries in the loopback dashboard.
- The switch is checked before reserving capacity and again at the local submission boundary.
- Engaging the switch blocks new buys across copy trading, new-token discovery and trending discovery. Existing holdings remain managed by the normal position/exit paths.
- Engaging the switch does not automatically liquidate positions.
- Resume is refused while an unresolved transaction or active order reservation remains.

## Atomic reservations

The `risk_reservations` table records entry admission before execution. SQLite `BEGIN IMMEDIATE` admission accounts for tracked positions, active reservations, duplicate mints, total exposure, open-position count, daily entry count, loss pauses, unresolved transactions, and the global switch.

Reservation states:
- `reserved`: capacity reserved, prior to the submission boundary.
- `submitting`: execution call is about to be invoked.
- `uncertain`: outcome or post-submission reconciliation is unknown.
- `completed`, `failed`, `released`, `reconciled`: terminal/inactive statuses.

On restart, pre-submit `reserved` intents are released. In live mode, a `submitting` intent is moved to `uncertain` and remains a blocker until an operator reviews recent transaction history and actual wallet balances. Paper-mode reservations are released on restart because they do not submit real transactions.

## Manual reconciliation

For a recorded unresolved transaction, inspect the signature using a trusted explorer/RPC and reconcile wallet token/SOL balances before using **Acknowledge verified reconciliation**. The dashboard requires the recorded signature plus the exact confirmation phrase:

`I_VERIFIED_THE_ONCHAIN_TRANSACTION_AND_WALLET_BALANCES`

This is an operator attestation only. The application does not prove that the operator's review was correct and does not independently query the chain in response to that button. A startup-interrupted reservation without a recorded transaction signature is shown separately; acknowledge it only after reviewing recent wallet activity and current balances.

Acknowledgement does not resume entries. Use **Resume new entries** as a separate explicit action after reconciliation is complete.

## Incident behavior

- Invalid, stale or contradictory risk state refuses entries.
- A transaction with unknown outcome is never blindly retried.
- Daily/total loss caps block new entries; the peak-to-trough drawdown cap engages the persistent global entry switch.
- Normal exit management remains available while entries are halted.
- Do not share private keys, `.env` values or wallet recovery phrases.
