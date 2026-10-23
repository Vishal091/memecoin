# Phase 5 — Paper and shadow trial protocol

## Purpose and guardrails

Phase 5 measures the existing strategies without authorizing real-money entries. It does not change the production entry approval policy. Use a separate SQLite database for this trial and keep `BOT_MODE=paper`, `LIVE_ENTRIES_APPROVED=false`, `TOKEN_DISCOVERY_LIVE_ENABLED=false`, and `TRENDING_DISCOVERY_LIVE_ENABLED=false`.

The persistent entry switch is engaged on startup. The operator can leave it engaged for shadow-only observation. To test the simulator's copy-trading order lifecycle, resume entries from the loopback dashboard while `BOT_MODE=paper`; paper executions use the simulated ledger. The global switch is not required for discovery's shadow observations and does not turn on either scanner's live-buy option.

## Suggested local profile

Copy these values into the local `.env` without committing credentials or private wallet material. Preserve your existing API credentials if needed by the data feeds; do not paste credentials into reports or issues.

```dotenv
BOT_MODE=paper
LIVE_ENTRIES_APPROVED=false
DATABASE_PATH=memecoin-phase5-paper.db
EXPERIMENT_DURATION_DAYS=30
TOKEN_DISCOVERY_ENABLED=true
TOKEN_DISCOVERY_LIVE_ENABLED=false
TOKEN_DISCOVERY_SHADOW_WINDOW_SECONDS=900
TRENDING_DISCOVERY_ENABLED=true
TRENDING_DISCOVERY_LIVE_ENABLED=false
MAX_ENTRY_USD=0.50
MAX_TOTAL_EXPOSURE_USD=1.00
MAX_OPEN_POSITIONS=1
MAX_ENTRIES_PER_DAY=4
DAILY_LOSS_LIMIT_USD=0.50
TOTAL_LOSS_LIMIT_USD=1.00
MAX_PORTFOLIO_DRAWDOWN_PCT=0.15
MIN_SOL_RESERVE=0.025
```

If you also want to observe copy-trading behavior, ensure your existing paper-mode feed/leaderboard setup works. Missing source data or credentials should be recorded as a limitation, not replaced with guessed values. Do not use the live database path.

## Starting and checking the trial

1. Stop other bot instances using the same database, confirm the database path is the dedicated paper path above, and keep live-entry options disabled.
2. Start the bot from the repository root with `python pumpfun_bot.py`.
3. Confirm the dashboard reports paper mode, live-entry approval OFF, discovery scanners enabled for analysis, both live-buy switches OFF, and the global entry switch state.
4. Leave the process collecting shadow marks. Token discovery records its observed candidate marks and 1/5/15-minute outcomes. Trending discovery records first-pass screened observations. A candidate qualifies for return calculations only when an exact pool match supplies a valid USD price with the candidate mint as the base token; screened rows without a valid mark remain visible as missing-price data.
5. After collection, run the read-only evaluator below. It does not alter the database and never signs or submits a transaction.

Observation storage is coalesced by candidate, minute and status, stores a bounded subset of source fields, and retains at most the 500,000 newest observation rows. If a busy trial reaches that cap, older rows are pruned; the report's actual observed span must still meet the coverage target before review readiness can be reported. Persistence failures are surfaced in the dashboard rather than silently treated as successful collection.

```powershell
python shadow_evaluation.py --db memecoin-phase5-paper.db --json-out reports/phase5-shadow-report.json --csv-out reports/phase5-shadow-trades.csv
```

The report can also be printed without writing files:

```powershell
python shadow_evaluation.py --db memecoin-phase5-paper.db
```

## Evaluation method

- Entry selection is chronological and uses the first qualifying shadow entry per mint and strategy, not the best retrospective price.
- Returns are measured at 1, 5 and 15 minutes from the reference entry, using the first later price observation at/after the horizon within the evaluator's bounded delay window. Missing marks are excluded from that horizon's sample.
- Trending candidates require the recorded shadow eligibility marker and a valid USD mark. A quote-side token price or a different pool is not treated as this token's price.
- Each cost scenario applies half the named round-trip cost percentage on the entry side and half on the exit side. The default 3.5%, 5% and 8% scenarios are sensitivity assumptions, not verified current route costs or simulated executable fills. Replace them with measured route/provider estimates before decision-making.
- The paper ledger's realized P&L is shown separately and reflects the application's simulated ledger accounting; it is not live performance.
- Readiness requires at least 30 elapsed days of strategy observations and at least 100 evaluable 15-minute samples for each strategy. This is an initial evidence gate, not a guarantee of statistical power or permission to go live.

## Interpretation and failure modes

Treat missing USD marks, absent candidate observations, stale prices, failed feed/RPC checks, market-source disagreement, and short samples as missing/weak evidence. Do not interpret the absence of candidates as proof of low or high strategy quality. Compare gross and scenario-adjusted results by strategy, win rate, median, tails and adverse excursion; review losses, fees, slippage, unfilled opportunities and exit-path behavior separately.

A report status of `READY_FOR_STRATEGY_REVIEW` only means that the initial coverage/sample targets are met. It does not mean the strategy has positive expected value. Review the actual data, model assumptions and “what could be wrong?” scenarios before proposing any strategy change.

## WHAT COULD I BE WRONG ABOUT?

The measured reference prices may not be executable at the observed size, price-impact and priority/transaction costs can exceed scenarios, and screened tokens may disappear before an exit. Pool/source marks may be sparse or biased, and 100 samples can still be statistically weak. Paper fills do not recreate RPC delays, failed transactions, MEV, transfer taxes, real wallet debits, or live sell liquidity. An insufficient or biased sample must not be treated as evidence of an edge.

No production activation, live-entry flag change, merge or deployment is authorized by this Phase 5 step.
