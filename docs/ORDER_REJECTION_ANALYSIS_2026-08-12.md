# Order Rejection and Notification Review - 2026-08-12

## Decision

- Prevent a second sell when KIS no longer reports an open sell but the local
  execution ledger still has a recent accepted sell covering the apparent
  residual holding. Apply the same lifecycle rule to domestic and overseas
  order helpers; keep their submission and replacement formulas independent.
- Do not call policy WAIT or an internal pre-order guard a broker rejection.
  Keep those reasons in cycle/frequency logs, suppress per-cycle `watch:` trade
  notices, and reserve rejection wording for an actual rejected buy or sell.
- Keep the current market entry and exit policies unchanged. This is order
  lifecycle, notification, and API reliability work, not a frequency increase.

## Evidence

- The apparent current US rejection stream was 18 Telegram messages, ids
  `2239` through `2262`, with `watch:` reasons and `동작=주문거부`. None had a
  broker order event. They were policy WAIT results produced by the corrected
  low-frequency diagnostic and then misclassified by the generic skip summary.
- The true current broker rejection was domestic `360750` at
  `2026-08-12T04:37:06Z`: KIS returned `40240000` (no balance). A 35-share
  market sell had been accepted and fully filled 143 seconds earlier, but a
  transient balance row showed 9 held and 9 orderable shares before execution
  reconciliation completed. The bot submitted that stale 9-share balance.
- The same shape accounts for all three true sell rejections in the last seven
  days. Domestic `024840` retried 187 shares 63 seconds after its full sell;
  overseas `MDLN` retried 168 shares 66 seconds after the final replacement
  sell. Each returned the same no-balance code after the prior sell filled.
- There are no unfinalized execution rows now and no terminal API failure since
  the prior deployment. Fifteen `EGW00201` attempts all recovered, but the
  two-minute response-completion pacing window repeatedly expired during the
  same VPS session.

## Changes

- After the live open-order check reports no sell, both order helpers consult
  the execution ledger. A recent unfinalized sell covering the displayed
  holding suppresses resubmission for up to eight minutes; a recently confirmed
  full sell suppresses a stale balance for five minutes.
- A live open sell still follows the existing market-specific replacement
  rules. In particular, protective exits can still cancel and replace a
  broker-visible order after 45 seconds. The new guard only handles the gap
  where the broker-visible open order is gone but balance/reconciliation views
  have not converged.
- Suppressions preserve actual holding and orderable quantities in events and
  cycle logs. They do not register an order rejection, start a rejection
  circuit breaker, or create another broker order event.
- `watch:` remains available to the 50-cycle low-frequency aggregate but no
  longer emits a trade alert. Other internal skips display `매매미실행` and
  `미실행=N건`; actual buy and sell failures display `매수거부`/`매도거부`
  and `주문거부=N건`.
- After a VPS rate-limit response, the proven 950ms response-completion floor
  remains active for eight hours instead of two minutes. Request-start pacing,
  retry limits, production behavior, and POST non-replay rules are unchanged.

## Market and Probe Context

- Nasdaq Composite finalized `2026-08-12` at 26,588.49, `+0.5409%`, volume
  ratio `0.8968`, regime `up|normal|calm`.
- The first bounded `VWAP+RSI` VPS probe bought 75 `SVV` shares at $10.76 and
  sold 13.5 minutes later at $10.785 on `trend_filter_lost`. Gross return was
  `+0.2323%`, but modeled costs turned it into `-$2.18135` net
  (`-2,944.82 KRW`). This is one completed probe in one final session, so it
  neither releases the guard nor meets the five-session stop/review threshold.

## Validation

- Regression tests reproduce positive stale balance after an accepted sell in
  both markets and assert that no second API submission or rejected broker row
  is created.
- Notification tests cover silent policy WAIT, internal-skip wording, and
  explicit buy/sell rejection wording.
- The full suite passed 832 tests in 216.26 seconds; `compileall` and
  `git diff --check` also passed. Commit `e71a04c` was pushed before service
  restart.
- Two natural post-restart cycles completed with 107 successful API attempts
  for 107 logical requests, zero terminal failures, zero new broker orders or
  rejections, and zero false `watch:` trade notices. The service remained
  `active/running` as PID `1699600` with `NRestarts=0`; Telegram deployment
  report `id=2266` succeeded.
- Revert or narrow only if a legitimate sell remains blocked after both the
  broker open-order view and execution reconciliation show the preceding sell
  terminal without a full fill. Keep the eight-minute bound; do not turn it
  into an indefinite position lock.
