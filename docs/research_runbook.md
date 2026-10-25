# Research-first strategy trial runbook

## Scope and safety invariant

This branch adds the Phase 1 telemetry foundation and the first per-mode evidence gates. It does **not** prove profitability, enable live trading, or authorize changing any live flags. Keep paper mode as the default, keep all live opt-ins false, and do not store credentials or wallet material in reports, tests, commits, or issue comments.

The three strategies are independent at the paper-account and entry-control level:

- `leader_copy`: verified watched-wallet Pump/PumpSwap signals.
- `trending_discovery`: existing trending-pool scanner.
- `token_discovery`: new-token launch scanner.

Each paper strategy receives one third of `STARTING_PAPER_CASH_USD` as its virtual starting budget. With the example $12 paper cash, each account starts at $4. The configured per-strategy exposure ceiling defaults to $0.75; the global exposure ceiling remains $1.00, maximum intended entry remains $0.50, and only one tracked position may be open overall. The strategy ledger is separate from the existing global cash ledger for research comparisons; it does not represent a new wallet or capital allocation on-chain.

The global entry kill switch and the per-strategy kill switches block new entries only; existing positions continue through their normal management path. Startup continues to engage the global switch. The evidence policy can latch a strategy disabled; the local dashboard cannot clear that evidence lock.

## 1. Use a dedicated paper database

Stop any other bot process using the same database. Preserve any live database unchanged. In a local working clone, use a dedicated paper database path, for example:

```powershell
# Run from the repository root after dependencies are installed.
python pumpfun_bot.py --check
```

Set these local, non-secret values in `.env` (never commit `.env`):

```dotenv
BOT_MODE=paper
LIVE_ENTRIES_APPROVED=false
DATABASE_PATH=memecoin-phase1-research.db
AUTO_COPY_ON_START=false
TOKEN_DISCOVERY_ENABLED=true
TOKEN_DISCOVERY_LIVE_ENABLED=false
TRENDING_DISCOVERY_ENABLED=true
TRENDING_DISCOVERY_LIVE_ENABLED=false
MAX_ENTRY_USD=0.50
MAX_TOTAL_EXPOSURE_USD=1.00
MAX_STRATEGY_EXPOSURE_USD=0.75
MAX_OPEN_POSITIONS=1
MAX_ENTRIES_PER_DAY=4
DAILY_LOSS_LIMIT_USD=0.50
TOTAL_LOSS_LIMIT_USD=1.00
MAX_PORTFOLIO_DRAWDOWN_PCT=0.15
MIN_SOL_RESERVE=0.025
RESEARCH_ROUND_TRIP_COST_PCT=5.0
RESEARCH_PATH_WINDOW_SECONDS=1200
RESEARCH_AUTO_DISABLE_ENABLED=true
```

The journal does not need any API key values to be printed. Existing provider credentials, if used for market collection, stay only in the local file and are never included in research payloads. If the relevant feeds are unavailable, record the gap; do not treat the absence of events as a zero-cost or losing strategy result.

## 2. Start and inspect data collection

```powershell
python pumpfun_bot.py
```

Open the loopback dashboard (normally `http://127.0.0.1:8765`). Leave the global entry kill switch engaged for shadow-only collection. To collect leader observations, choose the leaderboard and activate the leader subscription; this is now separate from order authorization. The bot will refuse new entries while the global gate is engaged. Leader signals that pass the market/strategy filters are still recorded as counterfactual would-copy signals, while the separate entry-decision record shows that the actual order was blocked by the gate. This lets the trial score strategy selection without submitting a paper/live order. Do not turn on any live discovery toggle.

The API state now includes:

- `research_journal`: counts for normalized events, entry decisions, rejection decisions, price marks and cost estimates missing components.
- `strategy_accounts`: each strategy's paper starting cash, remaining virtual cash and realized P&L.
- `risk_control.mode_policy`: strategy kill-switch and evidence-policy state.

A WebSocket `connected=true` only confirms a connection, not that the provider accepted account subscriptions. Look for account-trade events and signer/program-verified RPC signals. Live copy signals must have independent signer/program verification; a PumpPortal payload alone is journaled as an observation, not treated as authorization to trade.

## 3. Research journal contents

The SQLite tables are created in the mode-specific database:

- `research_events`: normalized public market/leader observations and recorded ledger fills. Provider payloads are allowlisted; the journal does not copy arbitrary request objects or credentials.
- `research_signals`: entry decisions, including rejected candidates and reasons.
- `research_marks`: per-minute price-path marks attached to each signal for up to the configured observation window.
- `research_cost_estimates`: PumpPortal's 0.5% fee assumption plus separately recorded protocol/creator fee, priority fee, tip, slippage, failed-transaction cost and latency fields. The all-in cost total stays unknown until required components are supplied. Missing components must not be replaced with zero.

This is an initial capture layer, not proof every provider/chain event or every rejection path is captured perfectly. Provider timestamps may be missing, prices may be stale or non-executable, and local RPC visibility is not a complete data vendor.

## 4. Generate reports

After data has accumulated, run the shadow horizon evaluator:

```powershell
python shadow_evaluation.py --db memecoin-phase1-research.db --json-out reports/phase1-shadow.json --csv-out reports/phase1-shadow.csv
```

Then run the per-mode evaluator:

```powershell
python mode_evaluation.py --db memecoin-phase1-research.db --mode paper --json-out reports/phase1-mode-evaluation.json
```

The mode evaluator is read-only unless its explicit `--apply-auto-disable` option is supplied; the CLI only permits applying policy flags to a paper-mode database. The bot's `RESEARCH_AUTO_DISABLE_ENABLED=true` option runs the disable-only check periodically. Neither route can enable a strategy, modify live approval, sign a transaction or submit an order.

The report includes counterfactual 15-minute mark-to-mark return metrics after a configurable round-trip cost assumption: sample count, expectancy, median, hit rate, average win/loss, profit factor, maximum drawdown on an ordered return curve, worst losing streak, p10/p90, and a deterministic percentile-bootstrap confidence interval for expectancy. It reports ledger exit-fill accounting separately. These are not the same measure: mark-to-mark candidate outcomes are not executable fills, while ledger rows may include partial exits and strategy-specific paper accounting.

The default 5% round-trip cost is a **sensitivity assumption**, not a measured route quote. The journal records the PumpPortal 0.5% fee as an assumption where that path applies, but protocol/creator fees, impact, network/priority/tip costs, failed transaction costs and latency remain unknown unless measured. A missing-cost total is never presented as complete.

## 5. Evidence gates and auto-disable behavior

Initial maturity gate: at least 300 evaluable 15-minute counterfactual outcomes, **or** at least 14 elapsed observation days and 30 evaluable outcomes. The second path is an early review gate, not equivalent statistical power to 300 samples. For a mature mode, a 95% expectancy bootstrap CI lower bound at or below zero latches its mode-policy disable flag and its independent entry kill switch. A non-positive conclusion before maturity stays `INSUFFICIENT_EVIDENCE / KEEP_PAPER`, not an automatic negative-edge claim. A positive lower bound produces `CANDIDATE_FOR_HUMAN_REVIEW_NOT_LIVE_ENABLEMENT`; it never auto-enables the mode.

When a strategy is auto-disabled, new entries for that strategy stay blocked until separate human research and code-policy review; the dashboard intentionally does not provide a bypass. Global risk gates still apply independently. Disabling a strategy is not a command to force-sell its existing position.

## 6. Verify that a real signal becomes a paper position

Use a paper database and live flags off.

1. Confirm in the dashboard that `mode=PAPER`, `LIVE_ENTRIES_APPROVED=false`, both discovery live flags are off, and no unresolved reservations/transactions remain.
2. Confirm exactly one bot process uses that paper database.
3. Enable leader subscription for observations; wait for active public wallet subscriptions and leader signals. Account-stream messages by themselves should be recorded as unverified. A paper copy entry must pass the same signer/program checks, price/market/real-liquidity gates, strategy cash budget, global risk reservation, and order-state path as the implemented signal logic.
4. For new-token and trending modes, observe a candidate's status and price marks; discovery scanners can gather counterfactual paths while the global entry gate is engaged. A rejected item should carry a rejection reason. It is not necessarily an attempted paper buy.
5. Query the local dashboard state. Compare `research_journal.signals/rejected_signals/observed_marks`, `strategy_accounts`, the paper position list and trade ledger. A new row in the research journal alone means only that a signal or candidate was observed, not that a paper fill occurred.
6. Run both reports and examine sample coverage, costs, drawdown and exit behavior. Do not interpret screened/rejected tokens without usable price marks as zero-return outcomes.

No claim of a successful signal-to-position integration test is made until that sequence has actually run and been recorded in a report/test. Do not test this using a funded live wallet.

## 7. WHAT COULD I BE WRONG ABOUT?

- Marks can be delayed, biased, stale, or impossible to execute at the modelled size.
- The 5% round-trip sensitivity may understate real route costs; unknown costs stay unknown in the journal.
- Token returns are correlated by creator, market regime, narrative and launch window. A simple bootstrap over outcomes can overstate certainty.
- The capture code can miss a provider event or lose visibility across an RPC outage. Observed-feed counts are not proof of full market coverage.
- Paper exits do not reproduce MEV, failed landing, sell-route failure, account rent or live execution latency.
- A positive lower confidence bound on the observed sample is a research signal, not proof of future profitability and not permission to turn live switches on.

## Implementation boundary

This branch is the Phase 1 data/evidence foundation plus preliminary per-mode risk isolation. Leader selection scoring, wash/bundle/sniper heuristics, copyability model, advanced momentum/creator reputation features, replay engine with train/test walk-forward selection, block-bootstrap inference, stress-test suite, and live execution certification remain follow-on work. No live-mode activation, secrets, signing or real transaction was performed as part of this branch.
