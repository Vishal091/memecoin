# Memecoin — Pump.fun Copy-Trading Bot

A local Windows/Python experiment for copying watched traders **only when their transactions invoke the Pump bonding-curve or PumpSwap programs**. Paper mode is the default. Live mode requires a dedicated local Solana keypair and an explicit arming phrase.

This is speculative software, not a profit guarantee. Memecoins can lose almost all value, transaction costs can dominate a $10 account, and a stop or profit target cannot guarantee execution.

## Pump.fun path (current)

Use **`pumpfun_bot.py`** and see [Pump.fun setup guide](PUMPFUN_SETUP.md).

- Pump.fun bonding-curve and PumpSwap signals only; unrelated DEX swaps are ignored.
- PumpPortal Local Transaction API creates Pump/PumpSwap transactions; the bot validates the requested venue, signs locally, submits through your Solana RPC, and waits for confirmation.
- The trade adapter tries the bonding-curve venue and then PumpSwap if the preferred venue cannot build a transaction, to handle token migration.
- Local SQLite ledger, duplicate-signal protection, position tracking, staged profit-taking, hard loss limits, and a seven-day run limit.
- Paper mode by default. Do not enable live mode before reviewing the source and running paper mode.

**Costs:** PumpPortal documents a 0.5% Local Transaction API fee on each trade, in addition to network and Pump protocol/creator fees. See [PumpPortal fee schedule](https://pumpportal.fun/fees/).

## Quick start (PowerShell)

Install Python 3.12 and then run:

```powershell
git clone https://github.com/Vishal091/memecoin.git
cd memecoin
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.pumpfun.example .env -Force
notepad .env
```

Add manually researched public wallet addresses to `WATCH_WALLETS`, comma-separated. Keep `BOT_MODE=paper` and run:

```powershell
.\.venv\Scripts\python.exe pumpfun_bot.py --check
.\.venv\Scripts\python.exe pumpfun_bot.py --run
```

The first run records each watch wallet's latest signature as its starting cursor; older activity is not replayed. The bot cannot discover and certify profitable traders automatically yet.

## Live trading security

Use a fresh wallet funded only with the amount you're prepared to lose. Keep its keypair JSON local and out of GitHub. Never share a seed phrase or private key in chat or a repo. Use a separate live ledger (`DATABASE_PATH=memecoin-pumpfun-live.db`) to avoid mixing paper and live positions.

Only after reviewing the code and validating paper mode should you set `BOT_MODE=live` and `LIVE_CONFIRMATION=I_ACCEPT_THE_RISK_AND_UNDERSTAND_LIVE_TRADING` locally. The program halts new entries on ambiguous runtime errors and requires manual reconciliation. Even then, it cannot guarantee fills, exits, or recovery of capital.

## Legacy prototype

`bot.py` and `jupiter.py` are the earlier generic/Jupiter prototype. For this Pump.fun-focused experiment, use `pumpfun_bot.py`, not `bot.py`.
