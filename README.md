# Memecoin — Pump.fun Automatic Copy Trader

A local Windows/Python experiment for watching Pump.fun's trader leaderboard and copying the selected top 10. It supports Pump.fun bonding-curve and PumpSwap transactions only. It can show daily, weekly, monthly and a composite “overall” ranking, and includes a local dashboard with manual exit controls.

**Paper mode is the default.** Live mode needs credentials stored locally, a dedicated Solana wallet/keypair, and a deliberate arming phrase. No software can promise profit or an ideal exit.

## What it does

- Fetches Pump.fun leaderboard candidates for 1D, 1W and 1M periods, then computes an **overall composite rank** from those three lists. Pump.fun's visible leaderboard has 1D/1W/1M; the composite is not an official all-time list.
- Subscribes to trade activity from selected wallets through a single PumpPortal WebSocket connection for near-real-time signals; falls back to Solana RPC polling if the WebSocket feed is unavailable.
- Copies buys and sells from the selected top 10 and keeps watching a leader while a position copied from them remains open. Partial leader sells are approximated proportionally; full exits are attempted when the leader's observed token balance goes to zero.
- Runs Pump.fun/PumpSwap-only transaction building through PumpPortal's Local Transaction API, signing locally and submitting through your configured RPC.
- Provides a local dashboard: period selector, top-10 copy button, stop-copy button, position-by-position manual exit, manual exit all, recent trades, and estimated P&L.
- Tracks positions in a local SQLite ledger; staged take-profits, trailing exits, downside stops, daily pause, total-loss halt, and the seven-day experiment limit are included.

- Optional independent token discovery runs alongside leader copying in **shadow-only mode by default**. A separate local opt-in can route a fresh passing candidate through the existing live executor, after repeating all on-chain safety checks and global risk gates. Live discovery buys are hard-capped at $0.50 each and at one open discovery position; a screen is not a rug-pull guarantee.
- The discovery panel reports rejects and hypothetical returns beside the existing leader-copy positions/trades, and labels any live discovery entry separately. It is not a rug-pull guarantee.

## Important external dependencies

1. **Pump.fun leaderboard access:** the frontend leaderboard endpoint is authenticated and not a stable public API contract. You supply your own Pump.fun bearer token locally. Endpoint or period query changes can break discovery. The program fails closed if the API returns no recognizable wallet addresses or appears to ignore the selected period.
2. **PumpPortal trade feed:** the account-trade stream needs a PumpPortal API key. The stream is metered; PumpPortal's published data-feed conditions have included a linked wallet minimum and fees per events. Check the [current PumpPortal documentation and fees](https://pumpportal.fun/) before enabling it, especially with a $10 bankroll.
3. **Trading fees:** PumpPortal's Local Transaction API charges a per-trade fee in addition to Solana network and Pump protocol/creator fees. Entry and exit both cost money. Verify the current [PumpPortal fee schedule](https://pumpportal.fun/fees/).
4. **Independent discovery usage:** PumpPortal documents new-token subscription as free, but token-specific trade streams are metered. The shadow scanner caps active token-trade subscriptions (default five) and launch safety scans (default 12/minute). Review the current [PumpPortal data-feed terms](https://pumpportal.fun/data-api/real-time/) and [fee schedule](https://pumpportal.fun/fees/) before enabling it.
5. **Not truly instantaneous:** WebSocket notification and RPC confirmation still add latency. Your copy entry price can be materially worse than the leaderboard trader's price. A stopped token can be unsellable; manual exit and automatic stop buttons only attempt a sale.
6. **No guaranteed top traders:** leaderboard ranking shows the platform's selected metric/period, not independent proof of risk-adjusted future returns. Rankings can change and a winner over one window can lose money in the next.

## Quick start (PowerShell)

Install Python 3.12. In PowerShell:

```powershell
git clone https://github.com/Vishal091/memecoin.git
cd memecoin
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.pumpfun.example .env -Force
notepad .env
```

Set these values in your **local** `.env`:

- `PUMPFUN_AUTH_TOKEN`: the value of the `auth_token` cookie from your own successful Pump.fun leaderboard request. Copy only that cookie's value, never the full Cookie header. It may expire. Keep it in your local `.env`; never paste it into chat or commit it.
- `PUMPPORTAL_API_KEY`: your PumpPortal API key for the trader event stream. Keep it local.
- `BOT_MODE=paper` and `DATABASE_PATH=memecoin-pumpfun-paper.db`.

You do **not** need to put trader wallet addresses into `WATCH_WALLETS` for automatic leaderboard selection. Leave that variable blank unless you want to include extra wallets manually.

Run a connectivity/configuration check and start the bot:

```powershell
.\.venv\Scripts\python.exe pumpfun_bot.py --check
.\.venv\Scripts\python.exe pumpfun_bot.py --run
```

The console should print a local dashboard URL, normally `http://127.0.0.1:8765`. Open it in your browser. Choose Day, Week, Month, or Overall composite and click **Copy this Top 10**. Use **Stop copying** to prevent new copy entries; existing positions remain managed. Each open position has a **Manual exit** button, and **Manual exit all** submits exits for all tracked positions.

### Independent token discovery (shadow by default; live execution separately opt-in)

The scanner is an add-on; it does not replace or change leader-copy entry/exit behavior. To test both in the same run, add these lines to your **local** `.env` (the sample is `.env.pumpfun.example`):

```text
TOKEN_DISCOVERY_ENABLED=true
# Leave false until you explicitly decide to submit real-money discovery trades.
TOKEN_DISCOVERY_LIVE_ENABLED=false
# Hard code ceiling: $0.50 per discovery buy; only lower values are honored.
TOKEN_DISCOVERY_MAX_ENTRY_USD=0.50
TOKEN_DISCOVERY_MAX_WATCHED_TOKENS=5
TOKEN_DISCOVERY_WATCH_SECONDS=180
TOKEN_DISCOVERY_MAX_LAUNCHES_PER_MINUTE=12
TOKEN_DISCOVERY_MIN_LIQUIDITY_USD=1000
TOKEN_DISCOVERY_MIN_SCORE=75
TOKEN_DISCOVERY_MIN_UNIQUE_BUYERS=5
TOKEN_DISCOVERY_SHADOW_WINDOW_SECONDS=900
```

Restart the bot. The dashboard displays candidate status, verified real bonding-curve liquidity, holder concentration, unique buyers, buy/sell SOL flow, score, hypothetical returns, and any live-entry result. It uses the same PumpPortal WebSocket as account-trade copying and subscribes to a capped number of token streams; those token-trade messages are metered. New token events that cannot be checked on chain are rejected.

Live execution is deliberately a second opt-in. When you're ready to accept real-money risk, use a dedicated wallet and add `TOKEN_DISCOVERY_LIVE_ENABLED=true` to the local `.env` while keeping `TOKEN_DISCOVERY_ENABLED=true`, `BOT_MODE=live`, and the required live confirmation setting. Each buy is capped in code at $0.50 even if `TOKEN_DISCOVERY_MAX_ENTRY_USD` is set higher; at most one position tagged `token_discovery` can be open. Old shadow entries from previous bot runs are marked stale and are not bought. Live entries repeat the mint authority, freeze authority, Token-2022 extension, holder concentration, quote-mint and canonical curve checks immediately before execution, then must pass the existing global exposure/drawdown gates. Existing position management is responsible for exits, but neither a stop nor any liquidity check guarantees a successful sale.

This initial version only enters ungraduated SOL-quoted Pump bonding curves. It does not enter migrated PumpSwap pools whose reserves have not been independently verified. Token screens and score thresholds are hypotheses, not claims of profitability. Keep `TOKEN_DISCOVERY_LIVE_ENABLED=false` until you specifically choose to begin this live trial.

The first RPC fallback sync records each watch wallet's latest signature instead of replaying its historical trades. The WebSocket path follows new events after its subscription is active.

## About the leaderboard API settings

The UI shows 1D, 1W and 1M. Its frontend API can change. The current implementation uses configurable settings in `.env`:

- `PUMPFUN_LEADERBOARD_URL`
- `PUMPFUN_LEADERBOARD_PERIOD_PARAM`
- `PUMPFUN_LEADERBOARD_DAY_VALUE`
- `PUMPFUN_LEADERBOARD_WEEK_VALUE`
- `PUMPFUN_LEADERBOARD_MONTH_VALUE`

If the frontend API changes, discovery displays an error and does not claim a ranking is valid. Do not switch to live mode until all three windows return different, plausible lists and you have reviewed the selected wallets.

## Live mode — only after paper testing

Use a new dedicated wallet, keep its keypair JSON local and never upload or share its recovery phrase. Fund it only with an amount you are prepared to lose. Set `DATABASE_PATH=memecoin-pumpfun-live.db` so it cannot mix paper and real records. Keep `PUMPPORTAL_STREAM_TOKEN_PRICES=false` until you understand the extra metered event usage; account-trade signals are needed for copy trading regardless.

Only after checking the current API responses, trader list, event feed and exit behavior in paper mode should you set:

```text
BOT_MODE=live
LIVE_CONFIRMATION=I_ACCEPT_THE_RISK_AND_UNDERSTAND_LIVE_TRADING
```

The app blocks new live entries after ambiguous runtime errors and requires manual wallet/transaction reconciliation. It can still fail to exit if a token becomes illiquid or a transaction is rejected.

## Files

- `pumpfun_bot.py`: main Pump.fun runner and strategy/risk logic.
- `leaderboard.py`: leaderboard discovery and period checks.
- `pumpportal_feed.py`: single WebSocket event feed for the chosen traders.
- `pumpportal.py`: local-signing Pump.fun/PumpSwap transaction adapter.
- `dashboard.py`: loopback-only dashboard and manual exit controls.
- `pumpfun_config.py`: configuration.
- `PUMPFUN_SETUP.md`: step-by-step setup.
- `bot.py` and `jupiter.py`: earlier generic/Jupiter prototype; use `pumpfun_bot.py` for Pump.fun.

