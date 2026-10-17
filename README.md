# Memecoin — Solana Copy-Trading Bot

A local Python bot for a small, high-risk Solana copy-trading experiment. It monitors a **manually curated list of trader wallets**, detects likely swap signals, checks token liquidity, and manages staged exits. Paper mode is the default. Live mode requires a local keypair file and an explicit arming phrase.

This is an experimental trading tool, not a profit guarantee. Memecoins can go to zero, liquidity can disappear, quote/stop logic cannot ensure execution at a target price, and a copied entry can be much worse than the leader's entry.

## What's included

- Solana RPC polling for configured watch wallets.
- Swap-signal heuristics from parsed transaction token/SOL balance changes.
- Dexscreener market checks (liquidity, pair age, price).
- Jupiter Swap API V2 order/execute integration.
- SQLite ledger, position state, duplicate-signal protection, restart persistence.
- Staged profit-taking: sell 40% of the original amount at +30%, another 30% at +60%, trail the remainder.
- Risk controls: $2 maximum entry, $3 maximum marked exposure, 3 positions, 3 entries/day, $1.50 daily loss pause, $3 overall loss halt, SOL fee reserve, slippage and quote-deviation checks.
- Live trading disabled unless explicitly enabled in a local .env file.

## Important limitations

1. **This starter does not discover a public leaderboard automatically.** You must curate wallet addresses first and put them in WATCH_WALLETS. A wallet can look profitable because of insider access, transfers, token allocations, or unrealized marks; do your own analysis.
2. Transaction balance-delta parsing is a heuristic. It may miss swaps paid with other assets or misclassify unusual transactions. Review logs and use paper mode first.
3. The current public Solana RPC may rate-limit. Set SOLANA_RPC_URL to your own provider endpoint (for example, a free Helius endpoint). Don't put API keys in source code.
4. Small bankrolls are very sensitive to fees, rent, slippage and minimum practical trade size. The bot may correctly reject many trades.
5. A stop is not guaranteed. The program can only submit an exit if a quote and route are available, and an illiquid or frozen token may be impossible to sell.

## 1. Install on Windows

Install Python 3.11 or 3.12, then open PowerShell in the repo:

    py -3.12 -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install --upgrade pip
    pip install -r requirements.txt
    Copy-Item .env.example .env

If PowerShell blocks activation, run the venv interpreter directly instead: .\.venv\Scripts\python.exe

## 2. Configure paper mode first

Edit .env. Keep BOT_MODE=paper. Add public addresses of wallets you have independently researched to WATCH_WALLETS, separated by commas. **Paper mode does not need a Jupiter API key.** Live mode does; keep it local and never commit .env. Configure SOLANA_RPC_URL with your RPC provider endpoint if needed.

Run a configuration check:

    python bot.py --check

Start the monitor:

    python bot.py --run

The first launch stores each watch wallet's latest signature as a starting cursor, so it **does not copy old transactions**. It starts watching for new activity from that point onward. Leave it running to observe signals. Paper mode simulates trades at market-price estimates with a fixed configured cost per side and a $10 virtual starting balance; it never signs or broadcasts a transaction. Paper results are not a reliable substitute for real execution and slippage.

Stop with Ctrl+C. State is stored in `memecoin-paper.db`. Keep that file local. The ledger is locked to its first mode, so paper and live positions cannot accidentally be mixed.

## 3. Set up a dedicated wallet for live mode

Only use a new wallet dedicated to this experiment. Fund it with an amount you can lose completely, and maintain enough SOL for fees. Do not use your main wallet.

Create or export a Solana CLI keypair locally, stored in the usual JSON byte-array format. Set KEYPAIR_PATH to that local file path in .env. The keypair must match the funded wallet. Do not upload that file to GitHub, send it through chat, put it in screenshots, or paste the secret into .env.

Before enabling live trading:
- Complete the paper-mode trial and inspect the SQLite trade ledger and logs.
- Verify all configured wallet addresses yourself.
- Verify the dedicated wallet contains only the experiment funds.
- Confirm the RPC and Jupiter endpoints work.
- Review the risk settings and test the kill switch.

For live mode, first change `DATABASE_PATH=memecoin-live.db` so the live ledger starts separately from paper history. Then change `BOT_MODE=live`, set `KEYPAIR_PATH` to the fresh wallet's local keypair file, and set `LIVE_CONFIRMATION` exactly to:

    I_ACCEPT_THE_RISK_AND_UNDERSTAND_LIVE_TRADING

Live startup refuses to run if the keypair is missing or that confirmation does not match. Never send the keypair or seed phrase to an assistant or third party. The bot does not need custody of your wallet outside your own computer.

## Main settings

- WATCH_WALLETS: comma-separated trader wallet addresses.
- BOT_MODE: paper (default) or live. The database is locked to its first mode; use `memecoin-paper.db` for paper and `memecoin-live.db` for live.
- EXPERIMENT_DURATION_DAYS: continuous-run duration before entries stop and remaining positions are liquidated (default 7 days).
- DATABASE_PATH: local ledger path; keep separate files for paper and live mode.
- KEYPAIR_PATH: local Solana CLI keypair JSON, used only in live mode.
- JUPITER_API_KEY: local API key for Jupiter Swap API V2.
- SOLANA_RPC_URL: RPC endpoint.
- MAX_ENTRY_USD: default 2.00.
- MAX_TOTAL_EXPOSURE_USD: default 3.00.
- MAX_OPEN_POSITIONS: default 3.
- MAX_ENTRIES_PER_DAY: default 3.
- DAILY_LOSS_LIMIT_USD: default 1.50.
- TOTAL_LOSS_LIMIT_USD: default 3.00.
- MIN_SOL_RESERVE: default 0.008 SOL.
- MIN_LIQUIDITY_USD: default $5,000.
- MIN_PAIR_AGE_SECONDS: default 90.
- SLIPPAGE_BPS: default 150 (1.5%).
- MAX_QUOTE_SHORTFALL_PCT: default 0.04 (reject if quoted output is over 4% below its market-price estimate).

The overall loss halt is persistent in the local database. If it activates, the bot stops opening positions but continues trying to manage existing positions. The seven-day clock starts the first time you launch `python bot.py --run`; `--once` is for a single test cycle and does not start the clock. At expiry, the bot disables entries and attempts to sell remaining holdings, then shuts down when no tracked positions remain. This expiry liquidation cannot guarantee a sale if liquidity or routes disappear. Do not delete the database or clear the halt simply to resume trading; investigate first.

## Commands

    python bot.py --check   # configuration/API checks; never submits swaps
    python bot.py --once    # one polling cycle
    python bot.py --run     # continuous local monitor

## What the bot does when signals arrive

- A likely buy signal is considered only when another asset was spent and the token market meets the liquidity and age checks.
- A followed wallet selling a token triggers an attempted full exit of that token **only if this bot holds it**.
- It takes 40% of the initial token amount at +30%, another 30% of the initial amount at +60%, and trails the remainder 20% below its post-entry price peak.
- It exits the position if the marked price falls 25% below entry, if the trailing threshold is hit, or if the followed trader sells.
- Token prices and marked P&L are estimates. Actual proceeds are determined only after a confirmed swap. Exits can fail or slip.

## Safety

Never commit .env, keypair files, SQLite databases, logs, or private credentials. The .gitignore excludes common local files. Live mode makes real transactions and can lose the entire funded balance. There is no guaranteed way to secure profits at an ideal price.
