# Runtime, Performance, and Frequency Review - 2026-09-25

## Scope

- Observation cutoff: 2026-09-25 15:00 UTC (2026-09-26 00:00 KST)
- Runtime under review: commit `6caa388c2d24`, domestic policy v9,
  overseas policy v6
- Sources: broker-confirmed execution ledger, `cycle_log`, market regimes,
  entry-horizon shadows, API/Telegram logs, user-systemd journal, and a
  read-only TradingView scanner probe
- Policy rule: compare net returns after market-specific costs and require
  multiple sessions before changing a live formula

## Conclusion

The lack of new trades after the September 21 deployment is not an order-path
failure. No full v9/v6 entry formula reached `BUY`; there were no order events,
order rejections, unresolved executions, or non-GET API calls after deployment.
The recent losses are attributable to negative after-cost expectancy in the
previous probe cohort, not merely to a weak market.

The live entry thresholds remain unchanged. Loosening them would promote
cohorts whose observed 15-60 minute returns do not cover costs. The safe
frequency improvement is instead to widen overseas candidate discovery when
the scanner collapses, while preserving every v6 order gate. Market-specific
horizon observation is added so this decision can be revisited with forward
KRX and US evidence rather than repeated ad-hoc replay.

## Confirmed Performance

Last 14 days, including all broker-confirmed account exits:

| Market | Exits | Win rate | Mean gross | Mean net | Total net |
| --- | ---: | ---: | ---: | ---: | ---: |
| KRX | 15 | 20% | -0.032% | -0.142% | -3,119 KRW |
| US | 4 | 0% | -0.030% | -0.532% | -23,645 KRW / -17.52 USD probe ledger |

The prior KRX v8 probe had 15 exits across five sessions. The prior US v5
probe had four exits across four sessions. Positive-index sessions also lost
after costs, so market direction alone does not explain the result.

After the v9/v6 service restart at 2026-09-21 18:05:52 UTC:

- Confirmed new buys: 0
- Confirmed new sells: 0
- Domestic `READY/near_breakout`: three observations, two symbols
- Overseas `READY/near_breakout`: ten observations, six symbols
- A `READY` state is not a buy order; it remains below the complete formula

## Market Context

| Market date | Benchmark | Return | 20-day volume ratio | Final regime | New trades |
| --- | --- | ---: | ---: | --- | ---: |
| KRX 2026-09-21 | KOSPI | +1.646% | 0.816 | strong up / normal / calm | pre-restart only |
| KRX 2026-09-22 | KOSPI | +0.145% | 0.880 | sideways / normal / normal | 0 |
| KRX 2026-09-23 | KOSPI | +0.898% | 0.726 | up / normal / normal | 0 |
| US 2026-09-21 | Nasdaq Composite | +2.260% | 1.114 | strong up / normal / extreme | 0 |
| US 2026-09-22 | Nasdaq Composite | +0.451% | 1.165 | up / normal / calm | 0 |
| US 2026-09-23 | Nasdaq Composite | -1.131% | 1.057 | down / normal / normal | 0 |
| US 2026-09-24 | Nasdaq Composite | +0.012% | 0.983 | sideways / normal / normal | 0 |

The September 25 US regime was provisional at the cutoff and is excluded from
policy evaluation. No final KRX regime exists after September 23 because the
local market calendar marked the following sessions closed.

## Entry and Holding Evidence

### Domestic confirmation blocks

The latest five-day `strategy_confirmation_blocked` cohort has three sessions
and 26-33 matured samples per horizon. Every cost-adjusted horizon is adverse:

| Horizon | Samples | Mean net | Median net | Positive sessions |
| ---: | ---: | ---: | ---: | ---: |
| 5m | 32 | -0.166% | -0.125% | 0/3 |
| 15m | 33 | -0.236% | -0.071% | 0/3 |
| 30m | 29 | -0.352% | -0.054% | 0/3 |
| 60m | 29 | -0.325% | -0.136% | 0/3 |
| 120m | 26 | -0.424% | -0.164% | 0/3 |

This directly rejects increasing frequency by bypassing v9 confirmation.

The 30-day post-CB 45-minute cohort is the only mechanical promotion candidate:
20 samples, three sessions, mean +0.705%, trimmed mean +0.134%, median +0.122%,
and two positive sessions. It is not promoted because its session median is
only +0.001%, it mixes policy versions and regimes, and it measures a joint
entry/exit intervention rather than an isolated holding-time change.

### Overseas blocked candidates

The existing WAIT-forward ledger covers four final US regimes. For `volume_low`
episodes, minimum-cost net performance remains negative at every reported
horizon:

- Sideways session: 15m -0.406%, 30m -0.443%, 60m -0.440%
- Down session: 15m -0.528%, 30m -0.658%, 60m -0.812%
- Up session: 15m -0.500%, 30m -0.491%, 60m -0.575%
- Strong-up session: 15m -0.449%, 30m -0.418%, 60m -0.628%

These are optimistic because the 0.50% floor excludes spread and slippage.
Relaxing the US volume confirmation would therefore increase activity without
demonstrated positive expectancy.

The old ledger had 782 domestic horizon groups across 26 sessions and zero US
horizon groups. This asymmetry is fixed prospectively. Historical US decision
rows remain preserved in `cycle_log`; they are not rewritten into synthetic
live observations.

## Frequency Bottleneck

At the review cutoff, the TradingView overseas discovery scan returned only:

- Relative volume > 1.80: 2 symbols
- Existing fallback > 1.08: 3 symbols
- Discovery-only fallback > 0.60: 14 symbols

The production log showed the active pool falling from 30 symbols to one to
three symbols during the US regular session. This is a genuine discovery
bottleneck independent of order eligibility.

The new third scan runs only when both earlier scans remain below 30% of the
configured target. It changes candidate discovery only. The overseas v6
formula, strategy allowlist, near-close cutoff, market regime gate, cost gate,
and short-window volume ratio of 2.0 remain unchanged.

## Reliability Audit

- Correct service scope: `systemctl --user`; PID 2168268 was active since
  2026-09-21 18:05:52 UTC with zero restarts before this deployment.
- Runtime state: running, `last_error=null`, clean commit `6caa388c2d24`.
- API after deployment: 115,809 terminal GET requests, 100 failures (0.0863%).
  There were no POST requests. The largest bucket was overseas balance lookup,
  58 failures out of 16,041 attempts; cached symbols kept monitoring alive.
- Telegram polling: 12 short communication outages and 12 recoveries. Outbound
  reports were 62 successes and one timeout failure. No persistent 409 conflict.
- Execution ledger: 881 filled, 158 canceled, 12 partial-canceled; every row is
  finalized and aggregate remaining quantity is zero.
- Database: `quick_check=ok`, no foreign-key violations.

The low-rate GET timeouts are contained and do not justify increasing the
global timeout, which would lengthen cycle stalls. They remain an observation
item unless failure rate or stale-balance impact rises materially.

## Implemented Changes

1. Generalized fixed-horizon observation to KRX and US markets while retaining
   separate policy IDs and cost models.
2. US horizon costs use two commissions plus the SEC fee; KRX continues to use
   product-aware sell-tax exemption.
3. Added `near_breakout_wait` plus US-only `entry_volume_blocked` and
   `entry_trend_blocked` as non-trading experimental cohorts.
4. Kept open US horizon symbols inside the unified watch limit until maturity.
5. Added the discovery-only 0.60 relative-volume coverage fallback.
6. Changed zero-denominator review ratios from false `100%` to `n/a`; persisted
   quality values become JSON `null` under `market_session_review_v2`.

## Validation and Falsification

- Do not promote any entry or holding rule before at least 20 matured samples,
  three final sessions, positive mean/trimmed mean/median/session median, and at
  least two-thirds positive sessions.
- Revert or revise the 0.60 discovery fallback if it fails to restore at least
  30% of the configured pool in repeated regular-session scans, materially
  raises terminal API failures, or degrades cycle completion.
- Keep v9/v6 unchanged if broader discovery merely creates more blocked rows.
- Consider a narrowly separated post-CB experiment only after version- and
  regime-specific forward evidence confirms the 45-minute result.
- Inverse products remain shadow-only. Existing domestic and overseas inverse
  cohorts are negative after costs, so a falling index alone is insufficient
  for live short-leverage promotion.

## External Principles

- Nasdaq states that regular trading is 09:30-16:00 ET and warns that extended
  hours have lower liquidity and higher volatility:
  https://www.nasdaq.com/market-activity/stock-market-holiday-schedule
- The SEC warns that frequent trading has substantial expenses and that a
  trader must know the return required merely to cover costs:
  https://www.sec.gov/newsroom/press-releases/99-114-day-trading-your-dollars-risk-investor-alert
- KRX rules identify exchange holidays as closed sessions:
  https://global.krx.co.kr/contents/GLB/06/0606/0606030101/GLB0606030101T3.jsp
- KRX product tax treatment remains the basis for product-aware domestic cost
  estimation:
  https://regulation.krx.co.kr/contents/RGL/03/03060105/RGL03060105.jsp
