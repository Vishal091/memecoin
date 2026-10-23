# Memecoin — Pump.fun Automatic Copy Trader

A local Windows/Python experiment for watching Pump.fun's trader leaderboard and copying the selected top 10. It supports Pump.fun bonding-curve and PumpSwap transactions only. It can show daily, weekly, monthly and a composite “overall” ranking, and includes a local dashboard with manual exit controls.

**Paper mode is the default.** Live mode needs credentials stored locally, a dedicated Solana wallet/keypair, and a deliberate arming phrase. No software can promise profit or an ideal exit.

## What it does

- Fetches Pump.fun leaderboard candidates for 1D, 1W and 1M periods, then computes an **overall composite rank** from those three lists. Pump.fun's visible leaderboard has 1D/1W/1M; the composite is not an official all-time list.
- Subscribes to trade activity from selected wallets through a single PumpPortal WebSocket connection for near-real-time signals; falls back to Solana RPC polling if the WebSocket feed is unavailable.
- Copies buys and sells from the selected top 10 and keeps watching a leader while a position copied from them remains open. Partial leader sells are approximated proportionally; full exits are attempted when the leader's observed token balance goes to zero.
- Runs Pump.fun/PumpSwap-only transaction building through PumpPortal's Local Transaction API, signing locally and submitting through your configured RPC.
- Provides a local dashboard: period selector, top-10 copy button, stop-copy button, position-by-position manual exit, manual exit all, recent trades, and estimated P&L.
- Tracks positions in a local SQLite ledger with staged take-profits, trailing exits, per-position downside stops, and explicit operator exit controls. The configured daily-loss and total-loss limits block new entries at the threshold; consecutive losing exits pause new entries for the rest of the UTC day after the configured streak. These entry circuit breakers do not automatically liquidate existing holdings, which remain under position management. An uncertain submitted transaction blocks duplicate attempts for that mint until reconciled. Watched-wallet signatures older than `MAX_COPY_SIGNAL_AGE_SECONDS` (default 30 seconds) are consumed without copying, preventing delayed RPC recovery from replaying stale trades. Manual dashboard stop controls, per-entry sizing, daily entry limits, and open-position/exposure caps remain active. Marked-loss and trailing stops attempt an exit immediately when triggered. Leader-triggered sells also initiate immediately, including when the position is below entry, so the bot does not intentionally remain exposed after the followed wallet has sold. Position management runs before discovery/live-entry scanning. Unmigrated SOL-quoted Pump.fun positions use bounded on-chain bonding-curve reserves for stop marks before relying on DEX indexing, with a stored/cached fallback if the quote is unavailable. These changes reduce avoidable software delay but cannot guarantee execution, price, or profit. Explicit manual exits and catastrophic drawdowns remain immediate too.

- Optional independent token discovery runs alongside leader copying in **shadow-only mode by default**. Live discovery remains separately opted in, with a $0.50 maximum swap input and at most one open discovery position.
- Entry filters now enforce at least $5,000 verified real bonding-curve liquidity, a score of 80/100, at least eight unique buyers, creator/top-holder concentration checks, and fresh on-chain rechecks before live entry. Regular leader-copy entries also require indexed market data and the hard $5,000 liquidity floor.
- The scanner persists a bounded queue and also pages Pump.fun's authenticated latest-coins catalogue for launch records with valid timestamps up to 24 hours old. Catalogue access is best-effort because the frontend API is not a stable public contract; if it fails, the live WebSocket stream continues and the dashboard/log records the backfill error. The queue holds at most 100 unscanned launches, so it can still drop opportunities during sustained overload. Queue age, gate failures, live attempts, confirmed buys and unfilled shadow upside are surfaced in the dashboard. These controls can reduce weak entries; they cannot guarantee profit or prevent every rug pull.

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

The scanner is an add-on, but entry gates for both discovery and leader-copy have been tightened. To test discovery alongside leader copying, add these lines to your **local** `.env` (the sample is `.env.pumpfun.example`):

```text
TOKEN_DISCOVERY_ENABLED=true
# Leave false until you explicitly decide to submit real-money discovery trades.
TOKEN_DISCOVERY_LIVE_ENABLED=false
# Hard code ceiling: $0.50 swap input per discovery buy; total wallet debit can be higher due to rent/fees.
TOKEN_DISCOVERY_MAX_ENTRY_USD=0.50
TOKEN_DISCOVERY_MAX_WATCHED_TOKENS=8
TOKEN_DISCOVERY_WATCH_SECONDS=300
TOKEN_DISCOVERY_MAX_LAUNCHES_PER_MINUTE=12
TOKEN_DISCOVERY_MIN_LIQUIDITY_USD=5000
TOKEN_DISCOVERY_MIN_SCORE=80
TOKEN_DISCOVERY_MIN_UNIQUE_BUYERS=8
TOKEN_DISCOVERY_MAX_TOKEN_AGE_HOURS=24
TOKEN_DISCOVERY_SHADOW_WINDOW_SECONDS=900
```

Restart the bot. The dashboard displays candidate status, launch-event age, verified real bonding-curve liquidity (minimum $5,000), entry/current market-cap estimates, holder concentration, unique buyers, buy/sell SOL flow, score, hypothetical returns, queue state, a stage-by-stage execution funnel, and any live-entry result. The scanner uses the live PumpPortal new-token stream plus a best-effort authenticated Pump.fun latest-coins backfill, then verifies each candidate on-chain before watching it. The 24-hour age check uses the API creation timestamp when available or the time the live creation event was observed; the API source is unstable and cannot guarantee a complete historical catalogue.

Live execution is deliberately a second opt-in. When you're ready to accept real-money risk, use a dedicated wallet and add `TOKEN_DISCOVERY_LIVE_ENABLED=true` to the local `.env` while keeping `TOKEN_DISCOVERY_ENABLED=true`, `BOT_MODE=live`, and the required live confirmation setting. Each swap input is capped in code at $0.50 even if `TOKEN_DISCOVERY_MAX_ENTRY_USD` is set higher; the actual wallet debit may be higher due to account rent and network/priority fees. P&L uses the all-in wallet debit, while the token price used for stops is based on swap input divided by tokens received. At most one position tagged `token_discovery` can be open. Old shadow entries from previous bot runs are marked stale and are not bought. Live entries repeat the mint authority, freeze authority, Token-2022 extension, holder concentration, quote-mint and canonical curve checks immediately before execution, then must pass the existing global exposure/drawdown gates. Existing position management is responsible for exits, but neither a stop nor any liquidity check guarantees a successful sale.

The minimum discovery liquidity is enforced at no less than $5,000, the score floor at 80/100, the unique-buyer floor at eight, and the live discovery watcher runs for at least five minutes before a shadow entry can qualify. The configured maximum launch-event age is capped at 24 hours. Leader-copy entries also require indexed market data and at least $5,000 observed/verified liquidity. These are stricter filters, not a guarantee of profit. Entry/current market-cap numbers are estimates when derived from token price × total supply; the dashboard labels their source and they should not be treated as guaranteed realizable value. A high shadow peak is not realized P&L because execution timing, fees, slippage and exit liquidity can differ.

This initial version only enters ungraduated SOL-quoted Pump bonding curves. It does not enter migrated PumpSwap pools whose reserves have not been independently verified. Token screens and score thresholds are hypotheses, not claims of profitability. Keep `TOKEN_DISCOVERY_LIVE_ENABLED=false` until you specifically choose to begin this live trial.

### Trending Solana tokens (research with separate live opt-in)

Trending scans are separate from new-launch scans. The trending panel ranks Solana pools, compares the same pool's market data between GeckoTerminal and DEX Screener, checks mint controls directly over RPC, and requests RugCheck. The dashboard reports momentum, liquidity, volume, price movement, holder/LP-risk data, live-entry status, and reasons for rejection or manual review.

Research is enabled separately:

```text
TRENDING_DISCOVERY_ENABLED=true
TRENDING_DISCOVERY_POLL_SECONDS=60
TRENDING_DISCOVERY_MAX_CANDIDATES=20
TRENDING_MIN_LIQUIDITY_USD=15000
TRENDING_MIN_VOLUME_1H_USD=10000
TRENDING_MIN_SCORE=75
```

**Live trending entries are still disabled by default.** After you have tested the scanner and are ready to authorize real-money trading, configure these additional settings locally in `.env`:

```text
TRENDING_DISCOVERY_LIVE_ENABLED=true
TRENDING_DISCOVERY_MAX_ENTRY_USD=0.50
TRENDING_MAX_PRICE_IMPACT_PCT=5.0
TRENDING_MAX_POOL_AGE_HOURS=720
JUPITER_API_KEY=your_jupiter_api_key
```

Live execution also requires the repo's existing live prerequisites: `BOT_MODE=live`, the exact `LIVE_CONFIRMATION` string, a valid local keypair, and the existing live provider credentials. The executor uses Jupiter Swap API V2's Order & Execute flow, excludes RFQ router transactions requiring another signer, validates the transaction program allowlist and any v0 address lookup tables before signing, simulates the signed transaction, then requires a successful execute response and fresh wallet-balance reconciliation. Buy input is capped at $0.50 (network fees and new-token-account rent can make total wallet debit higher); at most one position across the new-token and trending discovery strategies can be open. The same position manager can sell trending positions back to SOL through Jupiter, subject to route availability and fresh balances.

The scanner enforces at least $15,000 reported liquidity and $10,000 one-hour volume, plus a 75/100 momentum floor for live entries. Immediately before a buy it repeats the same-pool check, mint/authority checks, RugCheck, price/volume/flow screens, and portfolio budget checks. Missing or mismatched data is not treated as safe. A `SCREENED` result is not proof that a token cannot rug, that insider activity is absent, that the price feed is accurate, or that an exit will fill. Swap routes can disappear, prices can move, APIs can be rate-limited, and sold-token balance changes must be reconciled. The live path must pass CI and a local no-funds/API smoke test before using the live flags.

The first RPC fallback sync records each watch wallet's latest signature instead of replaying its historical trades. The WebSocket path follows new events after its subscription is active.

## Aggressive overnight tuning (still bounded)

For broader candidate coverage and faster trade management, merge these overrides into the existing local `.env` file rather than replacing it:

```text
TOKEN_DISCOVERY_MAX_WATCHED_TOKENS=8
TRENDING_DISCOVERY_POLL_SECONDS=60
TRENDING_DISCOVERY_MAX_CANDIDATES=20

MAX_MARKED_LOSS_PCT=0.18
TRAILING_STOP_PCT=0.12
TAKE_PROFIT_1_PCT=0.20
TAKE_PROFIT_2_PCT=0.45
TAKE_PROFIT_1_SELL_FRACTION=0.40
TAKE_PROFIT_2_SELL_FRACTION=0.30
MAX_ENTRIES_PER_DAY=4
```

This profile scans more trending candidates, watches more new tokens, starts taking partial profit sooner, tightens the downside trigger to 18%, and attempts algorithmic stop exits immediately rather than waiting out the hold grace period. Existing portfolio controls remain in force: keep a hard entry cap, exposure cap, maximum position count, and daily/total loss limits. The example configuration retains a $0.50 discovery swap-input ceiling and $1.00 total exposure cap; swap fees/rent can make the actual wallet debit higher. Do not interpret a stop trigger as a guaranteed fill: a rug pull, missing route, failed transaction, or insufficient exit liquidity can prevent a timely sale.

The live execution switches reset to OFF each time the process restarts, so explicitly re-enable the relevant live-buy controls in the dashboard after restarting. This tuning is more active, not a reliable way to make a fixed dollar target overnight.

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


## Phase 4: entry-risk controls

The risk-control branch adds a persistent global entry kill switch, atomic SQLite risk reservations, a reservation/submission lifecycle, unresolved-transaction visibility, and explicit reconciliation controls in the local dashboard. New process starts default to **new entries halted**; an operator must deliberately resume entry admission. This is separate from turning copying or scanners on.

The enforced experiment ceilings are maximum $0.50 swap input, $1.00 total tracked/pending exposure, one open position, at most four entries per UTC day, $0.50 daily loss, $1.00 total loss, at least 0.025 SOL reserved for fees, and a 15% peak-to-trough equity drawdown gate. These limits are operational caps, not evidence of a profitable strategy; actual wallet debits can exceed swap input due to fees and rent. The separate `LIVE_ENTRIES_APPROVED=false` setting defaults to off and prevents any live-mode entry from being authorized on this Phase 4 branch; changing it requires a separate rollout approval.

An uncertain submitted transaction blocks new entries and remains visible in the dashboard. Before acknowledging reconciliation, independently inspect transaction history and actual wallet token/SOL balances. The dashboard phrase is an operator attestation only; the software does not infer that on-chain reconciliation has occurred. Acknowledgement does not automatically resume entries, and existing position management/exits continue while entry admission is halted. See [docs/risk_policy.md](docs/risk_policy.md).

This work is not an authorization for live trading or deployment. Keep live discovery opt-ins disabled and validate tests, CI and a controlled end-to-end execution/reconciliation path before any separate production approval.

## Phase 5: paper/shadow evaluation

Phase 5 adds persistent minute/status-coalesced observations for token-discovery and trending candidates plus a read-only evaluator. Use a separate paper database and keep `BOT_MODE=paper`, `LIVE_ENTRIES_APPROVED=false`, `TOKEN_DISCOVERY_LIVE_ENABLED=false`, and `TRENDING_DISCOVERY_LIVE_ENABLED=false`. Read the full [Phase 5 trial protocol](docs/phase5_paper_shadow.md).

Run the bot locally in paper mode to collect real observations, then evaluate without changing the database:

```powershell
python shadow_evaluation.py --db memecoin-phase5-paper.db --json-out reports/phase5-shadow-report.json --csv-out reports/phase5-shadow-trades.csv
```

The evaluator uses first qualifying entries chronologically, measures 1/5/15-minute returns, reports adverse excursion and cost-sensitivity scenarios, keeps screened missing-price candidates visible but excludes them from returns, and separates paper-ledger P&L from shadow marks. It reports insufficient data honestly; review readiness is not live-trading authorization. Initial review targets are at least 30 elapsed days and 100 evaluable 15-minute samples per strategy. No live orders are submitted by the evaluator.
