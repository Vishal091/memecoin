# Pump.fun / PumpSwap bot

This is the Pump.fun-specific entrypoint. It copies **only watched-wallet transactions that invoke the Pump bonding-curve program or PumpSwap program**. It does not route trades to Jupiter, Raydium or unrelated DEXes. After a token migrates, the transaction builder tries the PumpSwap venue if the bonding-curve venue is no longer available.

## Important cost and risk notes

- PumpPortal's Local Transaction API charges a 0.5% fee on each trade, in addition to Solana network and Pump protocol/creator fees. Your entry and exit can therefore incur multiple layers of cost.
- The bot uses PumpPortal to build a transaction and signs locally with your keypair. It sends through the configured Solana RPC and waits for confirmation before updating the ledger.
- Live trading has not been tested with your wallet. Paper mode only is the default. A sell route/stop cannot guarantee execution if liquidity disappears or a transaction fails.
- Public RPC may rate-limit. For a seven-day run, a dedicated RPC may improve reliability but can cost money.
- Watch wallets are curated manually. The bot does not automatically identify or certify profitable traders.
- New token prices/pair data can lag. The market filters can skip early opportunities; do not lower them blindly.

## Install (Windows PowerShell)

Use Python 3.12:

```powershell
git clone https://github.com/Vishal091/memecoin.git
cd memecoin
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If you already have a `.env`, keep a backup first, then copy the Pump.fun template:

```powershell
Copy-Item .env.pumpfun.example .env -Force
notepad .env
```

Add public trader wallet addresses to `WATCH_WALLETS`, separated by commas. **Do not paste a private key or seed phrase into `.env`**.

## Paper mode

Keep `BOT_MODE=paper`. Run:

```powershell
.\.venv\Scripts\python.exe pumpfun_bot.py --check
.\.venv\Scripts\python.exe pumpfun_bot.py --run
```

The first run records each watch wallet's latest signature as the starting cursor; it won't copy previous transactions. Paper mode uses market-price estimates and a simulated per-side cost. It does not sign/send a transaction.

Other commands:

```powershell
.\.venv\Scripts\python.exe pumpfun_bot.py --once
.\.venv\Scripts\python.exe pumpfun_bot.py --status
```

## Live mode setup — do not skip validation

1. Create a fresh dedicated Solana wallet. Fund it only with the experiment amount plus a small SOL fee reserve; accept that the whole balance can be lost.
2. Keep its Solana CLI JSON keypair file local and outside GitHub. Confirm `KEYPAIR_PATH` matches that wallet.
3. Use a separate live database by setting `DATABASE_PATH=memecoin-pumpfun-live.db`. The database intentionally locks to paper or live mode to prevent mixing simulated and real positions.
4. Review the source, run paper mode first, and check the wallet, market-data, and RPC behavior. Confirm the wallet has enough SOL for the entry, priority fee, and reserve.
5. Only then set `BOT_MODE=live` and `LIVE_CONFIRMATION=I_ACCEPT_THE_RISK_AND_UNDERSTAND_LIVE_TRADING` locally.

Never send the keypair or seed phrase in chat, GitHub, an issue, or a log. If the bot reports an ambiguous transaction/runtime error in live mode, it halts new entries and requires manual wallet/transaction reconciliation. Do not clear that halt or delete the database to force it back on.

## Strategy defaults

- Entry: at most $2, total open marked exposure at most $3, three positions and three entries per UTC day.
- Daily drawdown pause: $1.50. Total drawdown halt: $3; the bot attempts to exit tracked holdings, subject to live routes/liquidity.
- Exit: attempt to sell 40% of the initial quantity at +30%, another 30% at +60%, and trail the remainder 20% below the post-entry peak. A 25% marked decline triggers an exit attempt.
- Seven-day timer: begins on the first continuous `--run`, then disables entries and attempts to exit positions at expiry.

These are program rules, not guarantees of price or risk. Stop-loss and profit-taking triggers are evaluated only while the process and RPC are available.
