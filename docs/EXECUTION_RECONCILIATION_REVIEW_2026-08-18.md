# Execution Reconciliation and Policy Review - 2026-08-18

## Decision

- Do not loosen broad entry thresholds. The system submitted enough orders;
  its 10-page overseas execution-history ceiling hid fills and made active
  trading look inactive.
- Raise the reconciliation ceiling to 50 pages, detect and stop on a still
  truncated response, and close prior-session standard overseas order
  remainders as expired DAY orders. Never infer a fill when KIS history is
  absent.
- Bound virtual settlement retries per symbol to a five-minute live-order
  window, a 15-minute retry cooldown, and three accepted submissions per New
  York session. Persist usage in `broker_order_events` so a restart cannot
  reset the limit.
- Re-guard loss-making overseas formulas while keeping controlled activity.
  Guarded `VWAP+RSI`, `VOL+RSI`, `VWAP+VOL`, and `VWAP+VOL+RSI` may submit
  10%-slot probes in the paper profile, with three effective entries and six
  attempts shared per session. An overseas guard now needs three final market
  sessions plus at least three post-activation exits averaging above 0% net
  before release. The domestic release rule remains separately owned and
  unchanged.

## Root Cause

- On August 14, `NPAC` virtual settlement produced 73 accepted sells and 72
  cancel requests. A 45-second stale threshold and unrestricted no-fill retry
  formed a deterministic resubmission loop.
- The resulting August 14-17 KIS history contained 208 rows over 14 pages.
  Reconciliation stopped at 10 pages without reporting truncation, so 51
  execution rows remained `PENDING` even though KIS retained all 51 order
  numbers.
- A read-only KIS query with 20 pages matched all 51. Validation against a
  SQLite backup reconciled 44 filled groups and six no-fill groups, created 19
  missing `SELL_REAL` records, and reduced unfinalized executions from 51 to
  zero.
- KIS's official examples use `tr_cont` values `M`/`F` and continuation keys
  to fetch subsequent pages. The implementation now treats a live `M`/`F`
  response at the configured ceiling as a reconciliation failure instead of
  silently accepting incomplete history:
  <https://github.com/koreainvestment/open-trading-api>

## Corrected Performance

### Domestic

- August 14 KOSPI: +2.4159%, volume ratio 0.9263,
  `strong_up|normal|calm`.
- Six confirmed entries/exits, one net winner, +3,357.14 KRW. `VOL` contributed
  +18,646.43 KRW; `VWAP+RSI` lost 14,159.89 KRW and `VWAP+VOL` lost 1,129.40
  KRW.
- August 17 was a substitute holiday, not a failed trading session. No
  domestic frequency change is justified from that date.

### Overseas

- August 14 Nasdaq: -0.2756%, volume ratio 0.8397,
  `sideways|normal|calm`. After missing fills were restored, eight strategy
  exits produced two winners and -210.45 USD, not the previously visible
  +145.73 USD.
- August 17 Nasdaq: -0.3152%, volume unavailable,
  `down|unknown|calm`. Fourteen confirmed account exits produced two winners
  and -411.25 USD. Strategy-owned exits lost 116.16 USD; delayed virtual
  settlements lost another 295.09 USD.
- The August 14-17 `VOL+RSI` forward set had nine exits, one winner, and
  -313.89 USD. Across the 336-hour policy window it had 11 exits, one winner,
  -0.5908% average net return, and -448.31 USD. Full-size continuation is
  rejected; small probes preserve learning and trade flow.
- `VWAP+VOL` had nine August 17 exits, two winners, and -30.13 USD, while its
  unweighted average net return remained +0.1813%. This is near-flat rather
  than a sufficient loss signal, so it remains tradable unless the normal
  rolling guard threshold is breached.

## Validation Contract

- Reconciliation: zero unfinalized August 14/17 rows after deployment, no
  `execution_history_pagination_truncated`, and no duplicate cycle log per
  execution group.
- Settlement: at most three accepted `virtual_sell_settlement` orders for one
  symbol/session, at least 15 minutes between accepted retries, and no stale
  cancel before five minutes.
- Overseas guard release: at least three final sessions, three confirmed
  post-activation exits, and average modeled net return greater than 0%.
- Frequency: count KIS-confirmed entries/exits, submissions, and no-fill
  attempts separately. Do not label submissions as completed trades.
- Reconsider `VOL+RSI` sizing only after at least five completed small probes
  across three final Nasdaq sessions. Stop or narrow probes if the first five
  average at or below -0.50% net or contain no net winner.

## Reasoning Audit

- This review corrected a materially wrong profitability conclusion because
  it combined full broker pagination, execution-group finality, session market
  regimes, and strategy ownership. That is evidence that the higher-context
  review added value over the earlier partial-ledger summary.
- It is not evidence that a more expensive model is inherently superior. No
  controlled model or token-budget comparison was run, so comparative value
  remains unverified.
