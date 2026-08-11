# Loss Analysis and Market-Specific Remediation (2026-08-11)

## Scope and accounting boundary

- Source of truth: `data/trading.db`, KIS-filled broker executions, session-owned
  `SELL_REAL` rows, persisted entry contexts, final local-market regimes, service
  journal, API telemetry, and Telegram delivery log.
- The confirmed strategy ledger begins on 2026-07-27. The figures below describe
  that complete available period, not a lifetime profitability claim.
- Database `quick_check` is `ok`, foreign-key violations are zero, unfinalized
  executions are zero, and pending virtual sells are zero. Entry market/sector
  context coverage is 100% for both markets after the 2026-08-07 deployment.

## Persistent loss finding

| Market | Confirmed exits | Net wins | Avg gross | Avg net | Cumulative net |
|---|---:|---:|---:|---:|---:|
| Domestic | 133 | 30 (22.6%) | -0.199% | -0.305% | -367,498 KRW |
| Overseas | 83 | 18 (21.7%) | -0.073% | -0.575% | -3,299.78 USD / -4,454,699 KRW |

- This is persistent rather than a single-session problem. Domestic had one
  profitable local session among 12; overseas had none among 11.
- In the latest seven days, domestic recorded 32 exits, eight wins, and
  -95,317 KRW. Overseas recorded 21 exits, four wins, and -1,022.36 USD.
- The US gross-to-net gap is about 0.502 percentage points per completed trade.
  Gross performance is already slightly negative, and turnover makes it much
  worse. The SEC notes that transaction fees apply on each trade and reduce
  returns, while FINRA warns that frequent day trading can accumulate material
  commissions even when per-trade charges look small:
  [SEC fee bulletin](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins/updated),
  [FINRA day-trading disclosure](https://www.finra.org/rules-guidance/rulebooks/finra-rules/2270).

## Market context and recent sessions

| Local session | Market | Benchmark close | Return | Activity/volatility | Exits/wins | Net |
|---|---|---:|---:|---|---:|---:|
| 2026-08-07 | Domestic | KOSPI | -0.597% | normal/calm | 3/0 | -29,641 KRW |
| 2026-08-07 | Overseas | Nasdaq Composite | +1.299% | normal/normal | 4/0 | -212.69 USD |
| 2026-08-10 | Domestic | KOSPI | +0.653% | normal/calm | 7/3 | -4,597 KRW |
| 2026-08-10 | Overseas | Nasdaq Composite | -0.319% | normal/calm | 4/0 | -236.83 USD |
| 2026-08-11 | Domestic | KOSPI | +0.728% | normal/calm | 6/1 | -34,175 KRW |

- Losses occurred on both rising and falling final-index days. A final direction
  label alone is therefore not a universal strategy switch.
- Entry-time context is more useful domestically. In the matched period,
  ordinary longs entered below 0% KOSPI return produced 34 exits, only two wins,
  and about -128,882 KRW. Entries at 0% are kept eligible.
- On 2026-08-11, three ordinary longs were submitted in one cycle while KOSPI
  was about -1.03%, followed by another one minute later. The four correlated
  exits lost about -30,878 KRW. The earlier 005930 entry at 0% KOSPI was
  profitable and remains eligible under the new floor.
- US entry-time Nasdaq buckets were all similarly negative: below 0%, 0-1%,
  and above 1% averaged about -0.60% net. A US direction floor is not supported.

## Formula and exit findings

- Since 2026-07-30, domestic `VWAP+RSI` has 45 exits, 13% wins, and -0.341%
  average net; `VOL+RSI` has 13 exits and -0.910%. Exact composite labels were
  absent from the domestic guard scope.
- Since 2026-07-30, all active US composite formulas are negative:
  `VWAP+RSI` -0.592%, `VWAP+VOL` -0.459%, `VOL+RSI` -1.433%, and
  `VWAP+VOL+RSI` -0.693%. Exact `VWAP+RSI` and `VWAP+VOL+RSI` labels were
  absent from the US guard scope.
- Major recurring exit losses are domestic ATR hard stops and momentum cuts,
  and US trend-filter/momentum exits. Tightening exits alone is not selected:
  many US positions were nearly flat before fees, so the primary failure is
  accepting entries without enough expected movement to clear costs.
- Completed inverse shadows remain unfit for live promotion: domestic five
  exits average -0.614% net and overseas three exits average -0.990% net.
  Down-market inverse execution remains shadow-only.

## Implemented market-specific response

### Domestic

- Require a fresh same-session KOSPI observation, maximum age 600 seconds, for
  ordinary long entries.
- Require entry-time KOSPI return to be at least 0.0%. Domestic inverse products
  remain exempt and continue through their exact benchmark shadow policy.
- Expand the exact strategy-guard scope to all currently emitted standalone and
  composite labels. Use a 336-hour loss window, minimum three confirmed exits,
  and the existing -0.30% average-net threshold.
- A production-DB clone would currently guard `VWAP`, `VWAP+RSI`, `VOL+RSI`,
  and `VWAP+VOL+RSI`. `RSI`, `VOL`, and `VWAP+VOL` remain eligible when the
  KOSPI floor and their other confirmations pass.

### Overseas

- Do not add a Nasdaq direction floor because every observed direction bucket
  is negative and therefore non-discriminating.
- Expand exact guard scope to `VWAP+RSI` and `VWAP+VOL+RSI`, covering all
  emitted US formulas together with existing flags.
- Use a 336-hour loss window and -0.25% average-net threshold, half of the
  observed approximately 0.50% round-trip cost gap. The production-DB clone
  guards all four active composite formulas. Existing exits and inverse shadow
  evaluation remain active.

### Auditability and security

- Policy-generated buy skips now persist the contemporaneous market-regime
  snapshot in the `trade_skip` event. Future reviews can compare avoided losses
  and missed winners by direction, activity, and volatility.
- Telegram API exceptions are redacted at the API boundary before persistence
  or propagation. The one historical 502 row containing a credential-bearing
  URL must be scrubbed before the deployment backup is created.
- The 2026-08-11 KIS incident was broad transport failure rather than one broken
  endpoint. It produced 60 tracked terminal failures but no unfinalized order,
  no service restart, and later recovered. Existing bounded GET retry/cached
  balance behavior is retained; order POST replay is not introduced.

## Rejected changes

- **Increase trade frequency:** rejected. Both markets have negative gross/net
  expectancy and low win rates. More turnover increases expected loss and US
  costs rather than solving opportunity scarcity.
- **Promote inverse trading:** rejected. Eight closed shadow paths remain net
  negative. A falling benchmark is not sufficient evidence that a leveraged
  inverse product can be entered and exited profitably.
- **Apply one direction formula to both markets:** rejected. Domestic entry-time
  benchmark direction discriminates outcomes; the US buckets do not.
- **Turn sector shadow directly into a live gate:** deferred. Only one final US
  session has complete post-deployment sector evidence, below the existing
  five-session/20-entry evaluation requirement.
- **Change API pacing from the outage alone:** rejected. The errors were broad
  transport failures, not sustained endpoint rate limiting, and execution
  reconciliation is complete.

## Forward validation and falsification

- Evaluate each market independently after at least three new final local
  sessions. Do not count calendar days, holidays, or provisional regimes.
- For each guarded signal, compare 15/30/60-minute after-cost counterfactuals,
  grouped by market direction, activity, and volatility. Keep repeated scans in
  five-minute episodes so one signal is not mistaken for many observations.
- Narrow or revert the domestic 0% floor if, across at least three final KOSPI
  sessions, blocked after-cost winners and opportunity cost exceed avoided
  losses without reducing correlated losing entries.
- Narrow the 336-hour strategy guard if blocked after-cost opportunity exceeds
  avoided loss across three final sessions, or if any exit/inverse path is
  accidentally blocked. Retain it if avoided losses dominate.
- A frequency increase requires positive after-cost expectancy in at least
  three final sessions within the same market/regime family, plus no material
  increase in drawdown or API/order pressure.
- High-context reasoning found the exact-label gap, weekend-aging problem,
  entry-time KOSPI asymmetry, and credential-bearing error log. This value is
  recorded as operationally useful but not as superior to another model: no
  direct controlled model A/B was performed.

## Deployment result

- Implementation commit `2e1c6df` was pushed to remote `master`. The service
  restarted at 2026-08-11 13:31:30 UTC with PID 1675409, active/running and
  zero restarts.
- The first guard event activated the four expected domestic and four expected
  overseas formula guards. Two natural cycles made 116 API attempts with zero
  failed attempts, zero terminal failures, and zero new orders.
- Existing `CCRN` and `NPAC` positions continued through
  `time_exit_cost_floor_hold`; exit monitoring was not blocked.
- Telegram deployment report row 2171 succeeded. Policy evaluations 99 and 100
  retain `reviewed_at=NULL` until their three new final local sessions mature.
