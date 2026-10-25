# Phase 6E — bounded live canary (draft; default off)

## Purpose and status

This change wires the existing `LIVE_ENTRIES_APPROVED` guard into configuration and adds a deliberately small first live-canary profile. It is for a single, supervised live software test, not a profitability claim or a general approval to run an unattended strategy.

This branch is stacked on the open Phase 6D draft and is not merged into `main`. Do not use it for real funds until GitHub Actions passes, the full tests pass on the target Windows machine, the code changes are reviewed, and the operator completes the preflight below.

## Hard live-canary limits

When `BOT_MODE=live`, configuration validation fails closed unless all of these are satisfied:

- `LIVE_ENTRIES_APPROVED=true` and the existing exact `LIVE_CONFIRMATION` phrase is set locally.
- `MAX_ENTRY_USD <= 0.50`.
- `MAX_TOTAL_EXPOSURE_USD <= 0.55`. The reservation includes the code's built-in $0.05 allowance for fees; actual fees/rent may be higher.
- `MAX_OPEN_POSITIONS=1`.
- `MAX_ENTRIES_PER_DAY=1`.
- `DAILY_LOSS_LIMIT_USD` and `TOTAL_LOSS_LIMIT_USD` are each greater than zero and no greater than $0.50.
- `MIN_SOL_RESERVE >= 0.025` SOL.
- `TOKEN_DISCOVERY_LIVE_ENABLED=false` and `TRENDING_DISCOVERY_LIVE_ENABLED=false`. The initial canary is leader-copy only.

The risk engine separately clamps live input to $0.50, total reserved exposure to $0.55, open positions to one and entries per day to one, even when a settings object is looser. These are ceilings, not guarantees about total wallet debit: network and route fees, token-account rent, slippage, and failed/unsellable tokens can cause additional costs or losses. The loss thresholds stop new entries; they are not a guarantee that an existing position will be liquidated at that loss.

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
        MAX_ENTRIES_PER_DAY=1
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

6. Start exactly one bot process. On every startup the independent global entry kill switch should engage by design. Before resuming it from the loopback dashboard, verify the wallet address, mode, live approval, all five limits, the fresh live database path, no unresolved transactions/reservations, and both discovery live switches off. Do not resume if any value is unclear.

7. For the canary, supervise the bot continuously and wait for at most one fresh leader-copy entry. The entry cap is the swap input, not a cap on total wallet debit. After the first confirmed entry, keep the process running so its position manager can manage the exit; the one-entry-per-day and one-open-position limits constrain further entries. Do not start a second bot against the same database.

8. If a transaction outcome is uncertain, a buy/sell fails unexpectedly, balances disagree, or the bot engages a manual-review/entry halt, do not clear the halt or retry the trade blindly. Keep new entries halted, inspect the exact signature and wallet/token balances on-chain, and reconcile according to the project procedure. Never infer that a timeout means a transaction did not land.

## Evidence limits

Passing unit tests only validates covered software behavior. Until there is collected paper-forward data and measured transaction costs, this canary does not establish positive expectancy. A $0.50 input can be uneconomic once entry/exit fees and rent are counted. Stop after a single observed live cycle and review actual debit, fill, exit, fees, and reconciliation before considering any increase to limits.
