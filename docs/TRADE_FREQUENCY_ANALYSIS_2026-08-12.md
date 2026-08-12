# Trade Frequency and Recovery Review - 2026-08-12

## Decision

- Keep the domestic policy unchanged. Its first full forward session after the
  August 11 loss controls produced 14 confirmed entries and exits, six net
  winners, and +40,177.50 KRW after modeled costs while KOSPI closed +3.680%
  in `strong_up|normal|normal` conditions.
- Reject a broad US frequency increase. Every currently generated ordinary-long
  path is blocked for measured negative expectancy, and recent blocked-signal
  forward prices remain negative after the US cost floor.
- Permit one `VWAP+RSI` guard probe per New York session only in the KIS `vps`
  environment, only with a same-session Nasdaq observation no older than 600
  seconds and return at least 0.0%. Size it at 10% of the ordinary entry slot.
  Production accounts, all other formulas, stale/down-market observations, and
  every exit path retain their existing behavior.

## Current State

### Domestic

- The August 12 final review is complete and high quality: KOSPI 6,579.04,
  +3.6799%, volume ratio 0.9819, and regime
  `strong_up|normal|normal`. All 14 entries carry same-session market context.
- Strategy net results were `VOL` +43,785.54 KRW across nine exits,
  `VWAP+VOL` +5,434.92 KRW across four, and `RSI` -9,042.96 KRW in one.
  `partial_profit_lock`, `marginal_profit_exit`, and `time_exit_profit` supplied
  the positive result; six `trend_filter_lost` exits lost 21,672.91 KRW.
- This is one favorable final session, not proof of universal profitability.
  It does show that the KOSPI floor did not prevent trading when the market
  environment supported ordinary longs. Loosening domestic frequency now
  would confound a working first forward result with older negative sessions.

### Overseas

- There were no new US orders after the August 11 deployment. The current
  dynamic guard blocks `VWAP+RSI`, `VWAP+VOL`, `VOL+RSI`, and
  `VWAP+VOL+RSI`; static switches block standalone `VWAP`, `RSI`, and `VOL`.
- `no_overseas_candidate` in the low-frequency alert was a diagnostic error,
  not an empty scanner. During the current US session the TradingView pool
  grew from 4 to more than 20 symbols, while cycle logs accumulated 5,140
  `volume_low` waits and hundreds of exact strategy blocks. The cycle summary
  discarded those reasons when no order target was selected.
- The fix records a policy-blocked WAIT reason first, otherwise the dominant
  WAIT reason, plus ranked-candidate and watch-target counts. It changes no
  entry or exit decision.

## Historical Evidence

- The canonical broker-confirmed, session-owned ledger remains the policy PnL
  authority: since July 27 the US system has 83 exits, 18 net winners,
  -0.575% average net return, and -3,299.78 USD.
- A broader July 15+ `SELL_REAL` path review contains 179 rows, including nine
  legacy virtual-settlement rows that are not policy-authoritative. It is used
  only to inspect recurring entry and exit shapes.
- In that broader review, `VWAP+RSI` has the largest labeled sample and the
  most net winners: 66 exits, 11 winners, -0.567% average modeled net return,
  and -2,430.83 USD. `VWAP+VOL` is 58/6/-0.528%/-1,763.07 USD;
  `VOL+RSI` 22/3/-0.935%/-1,423.03 USD; `VWAP+VOL+RSI`
  15/4/-0.693%/-617.77 USD; standalone `VOL` 9/0/-1.014%/-2,469.04 USD.
- Positive outcomes cluster by exit, not by a stable entry threshold:
  `take_profit` is 7/7, +2.395% average net, +1,140.56 USD, with 23.1-minute
  average holds; `time_exit_profit` is 16/16, +0.581%, +610.67 USD, with
  151.4-minute average holds. In contrast, `trend_filter_lost` is 107 exits,
  one winner, -0.648%, and -5,730.27 USD. Entry RSI, volume ratio, and momentum
  did not reliably separate those winners from losers.
- Recent blocked `VWAP+RSI` episodes support the guard. In the final August 11
  `down|normal|calm` session, 42 five-minute episodes had minimum-cost net
  means of -0.584%/-0.772%/-0.794% at 15/30/60 minutes. In the provisional
  August 12 up/calm session, 12 episodes were still
  -0.357%/-0.138%/-0.664%. Other blocked composite and standalone paths were
  also negative at the measured horizons. A single positive standalone-RSI
  observation is not an adequate release sample.

## Probe Contract

- Scope: overseas market policy only, exact `VWAP+RSI`, `vps` only.
- Preconditions: the ordinary strategy must have generated a BUY that is
  blocked specifically by `recent_strategy_underperformance`; all formula,
  liquidity, spread, market-context, position, circuit-breaker, and order
  checks still apply.
- Market context: current New York session, Nasdaq return >= 0.0%, observation
  age <= 600 seconds.
- Exposure: at most one accepted real or virtual probe per New York session;
  quantity is floor(ordinary quantity * 0.10), with one share as the minimum
  when the ordinary slot can buy at least one share.
- Persistence: `strategy_guard_probe_submitted` stores session, strategy,
  quantity, virtual/real status, guard reason, and full entry market regime.
  The broker execution context and order reason carry the same probe marker.
  The repository-backed session counter survives service restarts.
- Visibility: `/lab_guard` reports environment eligibility, formula, session
  usage, slot multiplier, index floor, and maximum observation age.

## Validation and Rejection Rules

- Do not increase the one-per-session limit or 10% slot from a small number of
  favorable trades. Reconsider only after at least five completed probes in
  five final Nasdaq sessions, then require at least ten completed probes before
  any exposure expansion.
- Disable or narrow the probe if its first five completed outcomes average
  <= -0.50% net, if it records no net winner, if probe submissions bypass any
  non-strategy risk gate, or if session counting fails across a restart.
- Preserve the existing guard if blocked counterfactuals remain negative. A
  positive probe does not release `VWAP+RSI`; release still follows the normal
  guard's confirmed-exit and final-session policy.
- Keep current exits. Delayed-exit counterfactuals do not span three final
  sessions and are mixed; the profitable exit buckets show that deleting or
  delaying all exits would be an unsupported response to an entry problem.
- This high-context review is recorded as useful because it found the false
  `no_overseas_candidate` diagnosis and separated positive exit paths from
  negative entry expectancy. It is not evidence that expensive reasoning is
  intrinsically superior: no controlled model or token-budget A/B was run.

## Verification

- Targeted configuration, strategy-guard, order, frequency-diagnostic, and
  Telegram tests passed.
- Full repository suite after the final diagnostic patch: 827 passed in
  207.68 seconds. `compileall` and `git diff --check` also passed.
- Commit `6ba34f0` was pushed and deployed. PID 1694348 completed three natural
  cycles with the expected candidate and WAIT flow, no new order, no probe,
  and no terminal API failure. Telegram logs 2239 and 2240 preserve the first
  corrected frequency diagnosis and deployment report.
