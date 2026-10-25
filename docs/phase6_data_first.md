# Phase 6A — Data-first observation journal

## Scope and safety status

This is the first incremental implementation of the data-first phase. It adds a bounded SQLite event journal, best-effort instrumentation for the three strategy modes, basic route-cost assumptions, dashboard observation counters, and a read-only counterfactual report.

- Paper mode remains the default.
- This change does not read, print, or commit local .env values, API keys, private keys, keypairs, or recovery phrases.
- No transaction signing, transaction submission, live-mode change, or wallet operation is performed by the journal or report.
- This phase does not prove an edge and does not auto-enable, disable, or tune a trading mode.
- Event-journal write failures are logged and counted; instrumentation failure does not override an entry gate.
- Keep the Phase 4/5 global entry control active until you're deliberately running the paper-only validation procedure below.

## Captured evidence

The SQLite event_journal records a deduplicated timeline with UTC epoch timestamp, mode/strategy label, mint, public wallet/signature identifiers, source feed(s), side, status, decision, reason, reference price, observed liquidity, available event latency, estimated cost assumptions and a bounded payload.

Current integrations record:
- Leader/account-trade feed events, parser rejections, out-of-subscription events, duplicate signatures and processed signals.
- Solana log notifications and decoded Pump.fun/PumpSwap trade deltas that passed the existing transaction checks.
- New-token discovery candidate snapshots and their screening state, plus available reference-price marks.
- Trending pool candidate decisions, including rejected/unknown outcomes, and available same-pool price marks.
- Entry-gate allow/reject/uncertain/open events on the existing Pump.fun buy path and live-discovery status transitions.
- Paper/live ledger rows as explicitly labeled ledger events. Dashboard PnL summary is filtered to paper mode and is not substituted for counterfactual performance.

Event rows deduplicate on transaction identity where available. Different lifecycle decisions for a shared transaction remain separately queryable. Where multiple sources report the same trade, source labels are coalesced.

## Price-path integrity

The event timeline preserves observed marks as they arrive. The read-only strategy_evaluation.py report evaluates the first recorded decision for a new/trending mint and uses a future mark at or after 1, 5 and 15 minutes, bounded by the configured allowed delay. Missing or stale future marks are counted as missing; no interpolated or guessed prices are inserted.

Important limitation: scanner marks and trade-derived prices are reference prices, not guaranteed executable prices. The journal does not pretend to have a mark when none was received. Raw provider trade events are marked as observed; only the Solana-transaction parser path is labeled signer-verified after its existing checks.

## Cost model status

execution_costs.py provides versioned, configurable cost assumptions for:
- PumpPortal Local Transaction route fee when that execution route is actually used.
- Pump.fun bonding-curve or PumpSwap venue fee assumptions.
- Base network fees, configured priority fees and optional Jito tip.
- A conservative first-order slippage proxy based on observed real pool/curve liquidity (never virtual reserves).
- Measured latency slippage, failed-transaction probability/cost and allocated PumpPortal stream-message cost.

The estimate is intentionally incomplete if required observations are missing. Default failed-transaction probability, latency slippage, SOL/USD, stream-event allocation and exit-size executable quotes are not inferred. Configured fee percentages are assumptions, not verified on-chain fee schedules. The liquidity-based slippage proxy is not a quote or curve simulation. A record with measurement_basis=estimate_only must not satisfy the report's after-cost evidence gate.

No after-cost expectancy conclusion is available until measured fees, latency, failures and size-aware buy/sell results are recorded for all included samples.

## Fresh local paper-trial runbook

Use a separate working copy/database for observation, never the live database.

1. Fetch the feature branch and install the existing requirements:

        cd C:\Users\visha\memecoin
        git fetch origin
        git switch --track origin/phase6/data-first-evidence
        python -m pip install -r requirements.txt

2. In your local .env (do not paste it into chat or commit it), set these non-secret values for the observation run:

        BOT_MODE=paper
        LIVE_ENTRIES_APPROVED=false
        DATABASE_PATH=memecoin-phase6-observe.db
        TOKEN_DISCOVERY_LIVE_ENABLED=false
        TRENDING_DISCOVERY_LIVE_ENABLED=false

   Preserve all existing credentials locally. Do not set the live approval true or enable a discovery live switch. If the selected DB path is already used by a different mode, pick another fresh paper DB name. Avoid starting two copies of the bot against one database.

3. Start exactly one bot process:

        python pumpfun_bot.py

   Open http://127.0.0.1:8765. Verify the dashboard says paper, the global entry gate status is visible, both discovery scanners are enabled for observation if configured, and both live discovery switches are off.

4. To collect leader signals while the entry gate is halted, select the leaderboard window and choose Copy Top 10. On this Phase 6A branch, the selection means subscribe/observe leaders even when the entry gate is halted; the entry gate remains the final buy blocker. Verify active_wallets becomes non-zero and feed_stats reports events. A connected websocket alone is not proof that account-trade events are arriving.

5. For a controlled paper-only signal-to-position test, first confirm the dashboard still reports mode=paper, LIVE_ENTRIES_APPROVED=false, both discovery live switches false, and that the database is the fresh memecoin-phase6-observe.db. Then, and only for this paper test, use the dashboard to resume paper entries while auto-copy observes selected leaders. Wait for a newly observed leader buy that passes current filters. Expected evidence:
   - An event_journal leader_trade/signal_decision/entry_decision timeline for the signal.
   - A BUY row in trades with mode=paper and strategy=leader_copy.
   - The corresponding position appears on the dashboard.
   If no fresh signal passes, no position is expected; do not invent or replay a stale signal. Re-engage the global entry gate after the test. Never change to live mode for this procedure.

6. Generate the read-only evidence report:

        python strategy_evaluation.py --db memecoin-phase6-observe.db --json-out reports\phase6-data-first-report.json

   The report reads SQLite in read-only mode. It reports sample/mark coverage, rejected-signal reasons, 1/5/15-minute reference-price outcomes, cost sensitivity and paper-ledger PnL separately. Its verdict remains KEEP IN PAPER until the evidence gate can use measured costs and an untouched holdout.

## Test and report status

The added tests cover:
- Cross-feed transaction deduplication and preserving separate lifecycle decisions.
- Secret-key redaction and non-finite number sanitization.
- Separate paper-only PnL summary.
- Explicit/incomplete cost assumptions and round-trip accounting.
- Rejected-candidate counterfactual outcomes, missing marks and read-only reporting.
- Observing leader subscriptions while the global entry kill switch remains active.

The repository CI must pass before this change is treated as validated. Until the first real paper-forward DB is collected, there is no per-mode expectancy estimate or profitability claim. Do not use an empty report as evidence that a strategy works or fails.

## What remains for later phases

This phase is intentionally not an implementation of every requested strategy feature. Dedicated per-mode ledger budgets/switches, measured fill/exit-cost capture, more exhaustive early-return rejection coverage, leader reputation/anti-wash analysis, momentum/holder/creator features, robust walk-forward/bootstrap with dependence correction, auto-disable rules and alerts remain to be implemented and reviewed in later increments.
