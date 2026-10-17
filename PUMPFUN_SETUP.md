# Pump.fun automatic copy-trading setup

This is the Pump.fun-specific entrypoint. It can automatically fetch Pump.fun's 1D, 1W and 1M top-trader lists, compute an “overall” composite ranking across them, subscribe to selected wallets' new trades, copy buys/sells, and let you manually exit tracked positions from a local dashboard.

**Paper mode is the default.** Live mode has not been tested with your wallet and is not a promise of profitable trades. A stop/manual exit only attempts a transaction; it cannot make an illiquid token sellable.

## Important API and cost requirements

- **Pump.fun leaderboard token:** the front-end endpoint is authenticated and may change. You need a bearer token from your own logged-in Pump.fun browser session. The app checks that the three period requests return distinct rankings; if the endpoint ignores the period query, automatic copying is blocked.
- **PumpPortal API key:** needed for the near-real-time account-trade WebSocket feed. The feed is metered. Check current PumpPortal data API requirements and fees at [PumpPortal](https://pumpportal.fun/) and [PumpPortal fees](https://pumpportal.fun/fees/) before subscribing. Published conditions have included a 0.02 SOL linked-wallet minimum and 0.01 SOL for each 10,000 data events; verify current terms as they can change.
- **Swap costs:** PumpPortal's Local Transaction API has a 0.5% per-trade fee, in addition to Solana network and Pump protocol/creator fees. On $10 these costs can consume meaningful capital.
- The dashboard's “Overall” list is a composite rank across the 1D/1W/1M leaderboards, not an official all-time leaderboard.
- Top-trader ranking is a historic snapshot, not proof of future profitability. WebSocket data and transaction confirmations have latency; copied entries can be much worse than the leader's price.

## Install on Windows

Use Python 3.12, then run PowerShell:

    git clone https://github.com/Vishal091/memecoin.git
    cd memecoin
    py -3.12 -m venv .venv
    .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt
    Copy-Item .env.pumpfun.example .env -Force
    notepad .env

If you already have an important .env, back it up before replacing it.

## Configure automatic trader discovery

In your local .env, set:

- **PUMPFUN_BEARER_TOKEN:** the Authorization: Bearer token used by your own authenticated request to the Pump.fun leaderboard API at https://advanced-api-v2.pump.fun/callout/leaderboard. To find it, log into Pump.fun, open browser Developer Tools → Network, select the leaderboard request, and inspect the request headers. Copy it directly into your local .env; do not paste it into ChatGPT, GitHub, screenshots, or issues. The token may expire.
- **PUMPPORTAL_API_KEY:** your key for PumpPortal's data WebSocket. Keep it local and private.
- **BOT_MODE=paper.**
- **DATABASE_PATH=memecoin-pumpfun-paper.db.**

No trader addresses need to be entered in WATCH_WALLETS. Leave it blank for automatic Top 10 selection; you may add extra wallet addresses there if you separately want to follow additional public wallets.

The default period query settings in .env.pumpfun.example are configurable:

- PUMPFUN_LEADERBOARD_PERIOD_PARAM=period
- PUMPFUN_LEADERBOARD_DAY_VALUE=1D
- PUMPFUN_LEADERBOARD_WEEK_VALUE=1W
- PUMPFUN_LEADERBOARD_MONTH_VALUE=1M

These are frontend API settings, not a stable public API promise. If Pump.fun changes its endpoint, inspect the request made by the official leaderboard page and adjust the settings locally. The app deliberately fails closed if it cannot identify wallet addresses or if all three period lists come back identical.

## Run it

First check RPC and market data:

    .\\.venv\\Scripts\\python.exe pumpfun_bot.py --check

Then launch the local bot:

    .\\.venv\\Scripts\\python.exe pumpfun_bot.py --run

Open the local dashboard shown in the console, normally http://127.0.0.1:8765.

### Dashboard controls

- Select **Day (1D)**, **Week (1W)**, **Month (1M)** or **Overall composite**.
- Click **Copy this Top 10** to subscribe to the current top 10 for that period.
- Click **Stop copying** to block new copied entries. Existing positions continue to be managed.
- Use **Manual exit** on one position, or **Manual exit all** for all tracked positions. These queue exit attempts in the running bot; inspect the console and trade list to verify whether each sale confirmed.
- The dashboard binds to 127.0.0.1 only, so it is intended for the same PC. Keep the machine and its .env secure.

The bot refreshes the selected leaderboard at the configured interval and updates followed wallets while keeping watched leaders for any positions already opened. On a live WebSocket feed, copy signals are processed as events arrive. If the feed is unavailable, the bot falls back to RPC polling, which can be slower and may miss trades if the RPC is rate-limited.

Paper mode uses estimate-based token prices and a fixed simulated per-side cost; it cannot fully model launchpad slippage, priority fees, or PumpPortal's event-stream costs. Paper performance is not a guarantee of live performance.

The default ALLOW_UNINDEXED_PUMP_CURVE=true supports aggressive entries from a live Pump/PumpSwap event before a third-party market-data pair appears. This does **not** mean liquidity has been verified. The bot deliberately does not treat virtual bonding-curve reserves as real liquidity. Disable this option if you'd rather wait for indexed market data; leaving it enabled raises the risk of buying a token that becomes difficult or impossible to exit.

## Live mode setup — do not skip validation

1. Use a brand-new dedicated Solana wallet, not your main wallet. Fund it only with the amount you are willing to lose, plus a SOL fee reserve and any provider minimums that you have verified.
2. Keep the wallet's Solana CLI JSON keypair on your computer outside the repository. Never upload it or reveal its seed phrase.
3. Set DATABASE_PATH=memecoin-pumpfun-live.db so live and simulated positions do not mix.
4. Verify that the leaderboard produces distinct, plausible lists for all periods; confirm the WebSocket connection is established; test buys, partial sells, full sells, token migration and manual exits in paper mode.
5. Only then set BOT_MODE=live and LIVE_CONFIRMATION=I_ACCEPT_THE_RISK_AND_UNDERSTAND_LIVE_TRADING in your local .env.

If a live transaction or network error leaves its outcome ambiguous, the bot blocks new entries and requires you to reconcile signatures and wallet balances. Do not clear this state or delete the live database to force trading to resume.

## Default risk rules

- Maximum initial entry: $2.
- Maximum marked open exposure: $3.
- Maximum three open positions and three entries per UTC day.
- Daily drawdown pause: $1.50.
- Total experiment drawdown halt: $3, with exit attempts for tracked holdings.
- First profit target: +30%, attempt to sell 40% of original quantity.
- Second profit target: +60%, attempt to sell another 30% of original quantity.
- Trailing exit: 20% below the observed peak; downside exit trigger at −25%.
- Seven-day timer starts when continuous run starts. At expiry it disables new entries and attempts to liquidate tracked positions.

These are program rules, not guaranteed prices or hard exchange-enforced losses. A process that is offline cannot manage a position; a chain transaction may fail; market-price marks can be stale; and a token may become impossible to sell.
