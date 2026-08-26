# Reliability, Frequency, and Weighted Guard Review - 2026-08-26

## Decision

- Keep domestic and overseas policy ownership separate. Domestic performance
  remains eligible under its existing guard, while overseas formulas now use
  an additional capital-weighted net-return threshold.
- Do not broadly increase trade frequency while overseas after-cost expectancy
  remains negative. Keep the paper-only 10% guard probes at three effective
  entries and six submissions shared per New York session.
- Preserve the 30-minute consecutive-loss circuit breaker, but apply the
  full-session one-fire stop only when the same local market session contains
  the configured three consecutive confirmed strategy losses.
- Retry KIS `EGW00300` gateway-routing failures only for idempotent VPS GETs.
  Never automatically replay order POSTs.
- Escalate a virtual-settlement limit after two failed local sessions to a
  marketable limit, capped at 50 basis points below the latest last/current
  price and no higher than the available bid.

## Current System State

- The user service was active as PID 1759845 with zero restarts since
  2026-08-17 21:18:20 UTC.
- The August 26 outage window produced 5,494 API attempts, 522 failed attempts,
  and 170 terminal failures. From 10:48 through 11:11 UTC, all 455 attempts
  succeeded with zero retry or terminal failure, so the broad transport outage
  had recovered before this deployment.
- Since August 24, the dominant terminal failures were 351 overseas-balance
  and 60 overseas-open-order `EGW00300` responses. Blank transport messages
  also affected quote, chart, balance, and regime endpoints. They will now
  retain the exception class, for example `transport_error: ReadTimeout`.
- There were zero unfinalized broker executions and zero terminal failed order
  POSTs after August 18. This was primarily a read-path reliability incident,
  not evidence that order bodies should be changed or replayed.

## Market-Conditioned Performance

The persisted final-session review ledger remains complete. All entries after
the market-context deployment have same-local-session benchmark context; the
latest reviews also include final index return, 20-day volume ratio, regime,
confirmed entries/exits, wins, and after-cost PnL.

| Session | Market | Final benchmark | Volume ratio | Entries/exits | Wins | Net |
|---|---|---:|---:|---:|---:|---:|
| 2026-08-18 | KOSPI | -1.549% | 1.145 | 7/7 | 2 | -23,198 KRW |
| 2026-08-19 | KOSPI | -5.803% | 0.863 | 0/0 | 0 | 0 KRW |
| 2026-08-20 | KOSPI | +5.894% | 0.870 | 12/12 | 6 | +53,718 KRW |
| 2026-08-21 | KOSPI | +0.881% | 1.177 | 5/5 | 2 | +8,688 KRW |
| 2026-08-24 | KOSPI | -3.124% | 0.771 | 0/0 | 0 | 0 KRW |
| 2026-08-25 | KOSPI | +0.684% | 0.842 | 8/2 | 0 | -980 KRW |
| 2026-08-26 | KOSPI | +0.971% | 0.947 | 0/6 | 2 | +5,912 KRW |
| 2026-08-19 | Nasdaq | +0.157% | 0.976 | 4/4 | 0 | -149.77 USD |
| 2026-08-20 | Nasdaq | -1.002% | 0.968 | 6/6 | 1 | -177.46 USD |
| 2026-08-21 | Nasdaq | +0.435% | 0.859 | 8/8 | 2 | -215.74 USD |
| 2026-08-24 | Nasdaq | -0.765% | 0.860 | 5/4 | 1 | +175.50 USD |
| 2026-08-25 | Nasdaq | +0.659% | 0.841 | 4/3 | 1 | -24.99 USD |

- Domestic no-entry sessions on August 19 and 24 were both severe KOSPI down
  days. The zero-floor behavior remains directionally useful and is not
  loosened from a request for more trades alone.
- Overseas losses occurred in sideways, down, and up final Nasdaq regimes.
  A Nasdaq direction floor would not isolate the failure and remains rejected.
- Domestic sector identity coverage since August 8 is 74/75 entries. It is not
  a breadth score and remains context-only. Overseas sector context is 62/62,
  with 44 evaluable and 20 supportive observations; supportive sessions still
  include material losses, so it is not promoted to a live gate.

## Why Trading Looked Inactive

### Domestic

- On August 26, the consecutive-loss breaker fired after the first current-day
  exit. Two losses had been carried from August 25, so the combined streak was
  three even though the same-session streak was only one.
- The 30-minute cooldown was reasonable, but `post_cb_max_fires_per_session=1`
  then treated that cross-session fire as a full-session loss budget and
  blocked every ordinary new entry. Later existing exits recovered session PnL
  from -2,773 KRW after three exits to +5,912 KRW at the close.
- Historical replay confirms the distinction: August 13 and 14 domestic fires
  each had three same-session consecutive strategy losses and remain full-day
  stops. The August 26 fire is now classified as cross-session and receives
  only the timed breaker plus KOSPI recovery gate.

### Overseas

- Recent dominant WAIT causes were low volume, standalone-formula hard blocks,
  post-breaker session stops, rolling underperformance, and 26,414 accumulated
  `virtual_sell_pending` observations in the 21-day analysis window.
- NPAC retained 396 orderable shares against a virtual pending sale from
  August 14. The bounded retry fix worked operationally: only three accepted
  attempts per session occurred from August 18 onward. It did not solve price
  selection: every attempt reused the broker balance current price rather than
  a marketable bid, so the same limit was canceled after five minutes.
- The new escalation uses persisted accepted-submission dates. NPAC has failed
  across seven UTC/New York order sessions and will immediately qualify for an
  aggressive marketable-limit attempt at the next orderable regular session.

## Capital-Weighted Strategy Guard

The old guard averaged trade percentages equally. A small profitable trade
could therefore offset a much larger losing position in the decision metric
even while cumulative dollars remained negative. The repository now records
both equal-trade average net return and `sum(net PnL) / sum(entry notional)`.

| Market/strategy, 336h | Exits | Equal average | Capital weighted | Net | Decision |
|---|---:|---:|---:|---:|---|
| Domestic VOL | 27 | +0.071% | +0.080% | +19,997 KRW | eligible |
| Domestic VWAP+RSI | 13 | +0.231% | +0.231% | +28,325 KRW | eligible |
| Domestic VWAP+VOL | 6 | +0.151% | +0.091% | +4,442 KRW | eligible |
| Overseas VOL+RSI | 11 | -0.519% | -0.484% | -321.58 USD | guarded |
| Overseas VWAP+RSI | 7 | +0.109% | -0.679% | -129.74 USD | guarded |
| Overseas VWAP+VOL | 28 | -0.077% | -0.131% | -228.09 USD | guarded |
| Overseas VWAP+VOL+RSI | 1 | -0.697% | -0.697% | -4.50 USD | observation hold |

- Domestic retains a -0.30% threshold for both metrics.
- Overseas retains the -0.25% equal-average threshold and adds a stricter
  -0.10% capital-weighted threshold. A breach of either metric guards entry.
- Overseas release requires three final sessions plus at least three
  post-activation confirmed exits with both metrics above 0%.
- Guard probes remain enabled for bounded activity. Eight completed probe
  exits across at least five New York dates produced two net winners and about
  +9.63 USD total; one August 25 UTZ probe remains open. This validates keeping
  the 10% learning path, not increasing full-size turnover.

## Rejected or Deferred Changes

- **Increase all entry limits:** rejected. Overseas gross and net expectancy
  remain negative across multiple regimes. More turnover would amplify the
  observed loss and approximately 0.50% round-trip cost gap.
- **Disable the circuit breaker:** rejected. Genuine same-session three-loss
  clusters on August 13, 14, and 24 remain valid stops. Only attribution of a
  cross-session/non-strategy fire to the full-session budget changes.
- **Retry order POST after `EGW00300`:** rejected because an accepted response
  can be lost, creating duplicate orders. The new retry is GET-only.
- **Promote inverse leveraged products:** deferred. Existing completed inverse
  shadows remain net negative; a falling benchmark alone is insufficient.
- **Promote sector alignment to a live gate:** deferred because coverage is
  mature but predictive outcome evidence is not.

## Validation Contract

- API: future VPS GET `EGW00300` attempts must log `gateway_routing`, recover
  within at most three attempts, and increment
  `gateway_routing_retry_count`; POST must remain single-attempt.
- Circuit breaker: a same-session confirmed strategy loss streak below three
  may clear the full-session stop after cooldown and benchmark recovery, while
  a streak of three must still produce `post_cb_session_loss_limit_reached`.
- Guard: the live audit must reactivate overseas `VWAP+RSI` and `VWAP+VOL`
  from capital-weighted evidence while preserving the three-entry shared probe
  budget.
- Settlement: NPAC must submit `aggressive_limit` with pricing evidence, then
  either fill and clear pending quantity or remain within the existing
  five-minute/15-minute/three-per-session retry contract.
- Ledger: continue recording final KOSPI/Nasdaq direction, volume ratio,
  activity/volatility regime, strategy results, sector context, and policy
  decisions. Do not change a formula from one unconditioned daily result.

## Reasoning Audit

- This review added value by joining final market regimes, broker-confirmed
  executions, entry notional, accepted settlement attempts, circuit-breaker
  event timing, and API lineage. It corrected both an apparently profitable US
  formula and an apparently valid domestic full-day stop.
- This does not establish that a higher-cost model is inherently superior.
  There was no controlled model or token-budget A/B comparison, so comparative
  value remains recorded as unverified.

## Deployment Record

- Pre-deploy implementation validation: 573 related tests passed, followed by
  852/852 full-suite tests in 226.97 seconds. Focused coverage includes gateway
  retries, typed transport errors, weighted guards, settlement pricing, and
  cross-session breaker attribution. `compileall` and `git diff --check`
  passed; Black was not installed.
- Online SQLite backup:
  `data/trading_backup_20260826_111720_pre_reliability_weighted_guard_deploy.db`,
  647,385,088 bytes, SHA-256
  `d676ea5fbf2a08889f85572a1a20d69ebe5334671726bdd009d1f6b1b1666eb3`.
  `quick_check` was `ok` with zero foreign-key violations.
- Implementation commit `80d6f49` was pushed and independently matched against
  remote `master` before restart. The user service restarted at
  2026-08-26 11:18:24 UTC as PID 1846564, active/running with zero restarts.
- The first live guard audit activated overseas `VWAP+RSI` and `VWAP+VOL` from
  their capital-weighted losses while retaining the existing `VOL+RSI` and
  `VWAP+VOL+RSI` states. Domestic formulas remained eligible.
- Natural post-restart cycles made 73/73 successful API attempts, with zero
  failed attempts, zero terminal failures, zero unfinalized executions, and no
  warning-level service journal entries.
- Policy evaluations 97, 98, 99, 104, 105, 106, and 107 were closed with
  observed outcomes. New forward evaluations are 108 (overseas weighted guard
  and settlement), 109 (same-session breaker attribution), and 110 (KIS GET
  routing retry). Comparative model value remains unverified without an A/B.
- Telegram deployment report 2875 succeeded. NPAC remains pending before the
  next VPS-orderable regular session; the first `aggressive_limit` submission
  and its fill/slippage are explicitly forward-validation evidence, not
  preclaimed success.
