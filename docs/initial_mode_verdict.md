# Initial per-mode evidence verdict — 2026-10-25

## Decision summary

**No strategy is approved for live trading.** The evidence available in this workspace is an early paper/shadow operational snapshot, not a performance sample. It contains no evaluable per-mode return distribution or confidence interval. No profitability claim is supported.

| Mode | Evidence from latest supplied paper diagnostics | Initial verdict |
|---|---|---|
| A. Auto-copy leaderboard leaders | A later dashboard check reported `auto_copy=false`, `active_wallets=0`, and `account_trade_events=0`. An earlier feed snapshot counted 210 general trade events, but none were account trade events. No paper position/trade was present in the supplied state. | **KEEP PAPER — INSUFFICIENT EVIDENCE** |
| B. Trending-token discovery | A supplied snapshot reported scanner enabled in analysis-only mode, 3 scans, 19 pools checked, 9 screened, 6 rejected/filtered, and 0 shadow-observation write errors. The visible candidate sample had `shadow_eligible=false`; there is no demonstrated evaluable 15-minute outcome sample. | **KEEP PAPER — INSUFFICIENT EVIDENCE** |
| C. New-token discovery | A supplied snapshot reported scanner enabled with live disabled; 96 launch events, 115 safety scans, 221 tracked candidates, 59 safety rejections, 56 safety passes, 0 qualified candidates, 0 shadow entries and 0 live attempts. | **KEEP PAPER — INSUFFICIENT EVIDENCE** |

These are operational counters from the user-supplied local state and logs, not an independently reproduced run against the user's database. Exact timestamps vary across the supplied snapshots.

## Performance metrics

| Metric | Auto-copy | Trending | New-token |
|---|---:|---:|---:|
| Evaluable counterfactual 15-minute trades | Not available | Not available | Not available |
| After-cost expectancy | Not estimable | Not estimable | Not estimable |
| 95% expectancy confidence interval | Not estimable | Not estimable | Not estimable |
| Profit factor / hit rate / max drawdown | Not estimable | Not estimable | Not estimable |
| Out-of-sample evidence | None supplied | None supplied | None supplied |

“Not estimable” is intentional. An absent trade sample is not a zero-return observation and is not evidence of a negative edge. The initial data supports “not yet validated,” not “unprofitable.”

## Specific blockers and risks

### Auto-copy
The feed connection alone does not prove the provider accepted account subscriptions. The supplied diagnostics showed zero active watched wallets and zero account-trade events at the latest check. Until verified leader transactions are observed through the independent Solana logs path, copyability, entry drift, leader win rate and follower net expectancy cannot be evaluated.

### Trending
The scanner is running in analysis-only mode, but a scanned or screened candidate is not an evaluable paper trade. Candidates need a valid same-pool USD price, a qualifying entry mark and later horizon marks. A nonzero scanner count is not a strategy sample.

### New-token
The scanner processed many launches and rejected many for missing/unsupported quote mints or holder concentration. It had not produced a qualified shadow entry in the supplied snapshot. There are no counterfactual returns on which to set an evidence-based enable/disable verdict yet.

## Phase 1 implementation status (what this PR adds)

- A secret-safe SQLite research journal for normalized observed events, signal/entry decisions, rejection reasons, price-path marks, ledger fills and cost-estimate fields.
- Candidate-state recording for token discovery and trending, including rejects.
- Leader-copy eligibility is recorded only after the local filters and atomic entry reservation pass. Account-stream events are recorded as observations and are not actioned until independent signer/program verification arrives from the Solana logs path.
- Three separate virtual paper cash ledgers, individual mode entry kill switches, a global kill switch and a per-mode evidence-policy lock.
- A read-only per-mode evaluator with deterministic bootstrap expectancy intervals, after-cost sensitivity, separate ledger metrics and a maturity gate. Mature modes whose lower 95% expectancy bound is not positive are auto-disabled; a positive bound is review-only and never auto-enables a mode.
- A runbook for paper observation, report generation and verifying the difference between an observed signal, a qualified shadow entry and an actual paper-ledger position.

This is a Phase 1 foundation, **not the full strategy/replay system** requested. Creator history, wash/bundle/sniper detection, leader scoring, full market feature models, walk-forward out-of-sample replay, clustered/block bootstrap, stress tests, and a certified end-to-end paper signal-to-position test remain open work.

## Operating verdict

1. Keep all three strategies in paper mode.
2. Continue data capture until the predeclared maturity gate is met for each mode.
3. Inspect rejection reason and missing-price coverage, then use the report's net results under multiple cost assumptions—not raw price peaks—to compare modes.
4. Do not treat “CANDIDATE_FOR_HUMAN_REVIEW_NOT_LIVE_ENABLEMENT” as an approval to trade real money.

## WHAT COULD I BE WRONG ABOUT?

- The supplied API snapshot may not reflect current local database contents; the bot wasn't running in this environment.
- Feed counters can overcount duplicates or undercount unobserved events; RPC/provider outages can bias candidate selection.
- Prices and counterfactual exits may be stale or non-executable for the intended size.
- The 5% round-trip cost default is a sensitivity assumption. Real fees, price impact, failed transaction cost and latency can be higher.
- A naïve trade-level bootstrap does not correct selection bias, nonstationarity or correlations among coins sharing a creator/narrative/time window.
- 300 observations or two weeks of activity is a minimum screening gate, not a guarantee of statistical power or a proven edge.

**Final verdict from available evidence: keep all three modes in paper. No strategy has demonstrated positive after-cost expectancy, and no strategy has sufficient evidence for a live enablement decision.**
