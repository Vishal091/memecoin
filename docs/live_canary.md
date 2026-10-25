# Phase 6E — bounded supervised live trial (draft; default off)

## Purpose and status

This change wires the existing `LIVE_ENTRIES_APPROVED` guard into configuration and adds a deliberately small first live-canary profile. It is for a supervised, tightly capped live software trial, not a profitability claim or approval to run an unattended strategy. The configured daily entry ceiling can be up to ten buys; it may halt earlier due to position, exposure, loss, or reconciliation safeguards.

This branch is stacked on the open Phase 6D draft and is not merged into `main`. Do not use it for real funds until GitHub Actions passes, the full tests pass on the target Windows machine, the code changes are reviewed, and the operator completes the preflight below.

## Hard live-canary limits

When `BOT_MODE=live`, configuration validation fails closed unless all of these are satisfied:

- `LIVE_ENTRIES_APPROVED=true` and the existing exact `LIVE_CONFIRMATION` phrase is set locally.
- `MAX_ENTRY_USD <= 0.50`.
- `MAX_TOTAL_EXPOSURE_USD <= 0.55`. The reservation includes the code's built-in $0.05 allowance for fees; actual fees/rent may be higher.
- `MAX_OPEN_POSITIONS=1`.
- `1 <= MAX_ENTRIES_PER_DAY <= 10` (the requested local profile uses `10`).
- `DAILY_LOSS_LIMIT_USD` and `TOTAL_LOSS_LIMIT_USD` are each greater than zero and no greater than $0.50.
- `MIN_SOL_RESERVE >= 0.025` SOL.
- `MAX_MARKED_LOSS_PCT <= 0.18`, `TRAILING_STOP_PCT <= 0.12`, and `MAX_QUOTE_SHORTFALL_PCT <= 0.04`.
- `TOKEN_DISCOVERY_LIVE_ENABLED=false` and `TRENDING_DISCOVERY_LIVE_ENABLED=false`. The initial canary is leader-copy only.

The risk engine separately clamps live input to $0.50, total reserved exposure to $0.55, open positions to one and entries per day to ten, even when a settings object is looser. At ten $0.50 buy inputs, daily swap notional could reach $5.00, before exit swaps, network fees, route fees and account rent; these limits do not cap total wallet debit or guarantee a maximum loss. The $0.50 daily and total loss circuit breakers remain in force and can halt entries earlier. These are ceilings, not guarantees about total wallet debit: network and route fees, token-account rent, slippage, and failed/unsellable tokens can cause additional costs or losses. The loss thresholds stop new entries; they are not a guarantee that an existing position will be liquidated at that loss.

## Preflight — do not skip

1. Use a **dedicated, disposable Solana wallet** funded only with the small amount you are willing to lose plus enough SOL for network fees and account rent. Do not use a wallet holding savings or other assets. Never share its seed phrase or keypair file.
2. Verify the wallet address and native/token balances directly from a trusted explorer/RPC before startup. Confirm there are no old positions or unresolved/ambiguous transactions to manage.
3. Set a new `DATABASE_PATH=memecoin-live-canary.db` that is not shared with any paper or previous live run. If that filename already exists or has data, choose a different fresh filename; never reuse a paper ledger for live mode.
4. Set these non-secret values locally in `.env` only after approving the limits:

        BOT_MODE=live
        LIVE_ENTRIES_APPROVED=true
        LIVE_CONFIRMATION=I_ACCEPT_THE_RISK_AND_UNDERSTAND_LIVE_TRADING
        MAX_ENTRY_USD=0.50
        MAX_TOTAL_EXPOSURE_USD=0.55
        MAX_OPEN_POSITIONS=1
        MAX_ENTRIES_PER_DAY=10
        DAILY_LOSS_LIMIT_USD=0.50
        TOTAL_LOSS_LIMIT_USD=0.50
        MIN_SOL_RESERVE=0.025
        TOKEN_DISCOVERY_LIVE_ENABLED=false
        TRENDING_DISCOVERY_LIVE_ENABLED=false
        DATABASE_PATH=memecoin-live-canary.db

   Preserve the local keypair and API credentials already required by the project. Do not paste any credential values into chat or commit them. Paper and live databases must remain separate.

5. In PowerShell, first run the validation and test suite on this branch:

        .\\.venv\\Scripts\\python.exe -m py_compile config.py risk_engine.py pumpfun_bot.py
        .\\.venv\\Scripts\\python.exe -m unittest discover -s tests -v
        .\\.venv\\Scripts\\python.exe pumpfun_bot.py --check

   The check command validates configuration/endpoints; it is not proof of a successful live fill. Do not continue if any command fails or the dashboard/console reports unresolved transactions, a missing wallet, an RPC issue, or an unexpected mode.

6. Start exactly one bot process. On every startup the independent global entry kill switch should engage by design. Before resuming it from the loopback dashboard, verify the wallet address, mode, live approval, the input/exposure caps, one-position/ten-entries-per-day limits, loss ceilings, SOL reserve, fresh live database path, no unresolved transactions/reservations, and both discovery live switches off. Verify values from the local configuration as well as the dashboard; do not resume if anything is unclear.

7. For this supervised trial, supervise the bot continuously and allow no more than ten confirmed fresh leader-copy entries in a UTC day. Keep at most one position open; do not allow a new buy until the prior position is confirmed closed and balances/ledger agree. After each fill and exit, verify the signature, actual SOL/token balances, fees, and recorded position before considering another entry. Stop earlier at any manual-review halt, unresolved transaction, stale/missed feed, inconsistent balance, unexpected exit, or loss circuit breaker. The $0.50 input cap is not a cap on total wallet debit. The daily and total loss limits remain $0.50 and may prevent the bot from reaching ten entries; do not disable or raise them to force the full count. Do not start a second bot against the same database.

8. If a transaction outcome is uncertain, a buy/sell fails unexpectedly, balances disagree, or the bot engages a manual-review/entry halt, do not clear the halt or retry the trade blindly. Keep new entries halted, inspect the exact signature and wallet/token balances on-chain, and reconcile according to the project procedure. Never infer that a timeout means a transaction did not land.

## Evidence limits

Passing unit tests only validates covered software behavior. Until there is collected paper-forward data and measured transaction costs, this canary does not establish positive expectancy. A $0.50 input can be uneconomic once entry/exit fees and rent are counted. Review actual debit, fills, exits, fees, and reconciliation after each cycle; stop the trial if actual costs or behavior differ from expectations. Ten $0.50 inputs are a ceiling, not a target or evidence of positive expectancy.
