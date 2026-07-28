# Policy Evaluation Protocol

## Active improvement goal

- The current long-running improvement goal ends when the user's current
  10,000-credit allocation is consumed or resets. It does not end merely
  because the calendar month changes.
- At reset, finish the active verification/deployment checkpoint, record the
  remaining risks, and close the goal. Do not start speculative work just to
  consume tokens.
- Every service-changing iteration follows this sequence:
  inspect evidence, document the decision, test, back up mutable production
  data, commit, push, restart, send a Telegram report, and recheck health.

## Market separation

- Domestic and overseas policies are independently owned and versioned.
- Shared policy values are an initial baseline only. A result from one market
  is not evidence for changing the other market.
- Domestic performance is joined to the final KOSPI session regime.
- Overseas performance is joined to the final NASDAQ Composite session regime.
- Provisional benchmark rows are stored for monitoring but excluded from policy
  evaluation until the market close is final.
- Volatility uses percentage True Range: the maximum of the session high-low
  range and each absolute high/low distance from the prior close. This captures
  close-to-open gaps while normalizing domestic and overseas markets against
  their own trailing history. Persist the calculation version and refresh an
  outdated backfill once before comparing regime buckets. Merge stored official
  source rows with a capped API response so an older row cannot retain the
  previous formula merely because it fell just outside the response limit.
- A final overseas close with zero reported cumulative volume is incomplete
  activity data, not a quiet session. Retry the same official KIS benchmark
  source until positive volume arrives; do not substitute an unrelated proxy.
- A confirmed fill with a provisional same-session benchmark is reported as
  `pending final`, not `missing`. `Missing` is reserved for a session with no
  benchmark row at all.

## Required evidence

Policy changes should use all of the following:

1. Broker-confirmed fill performance from the execution ledger.
2. Net performance after recorded costs, or a clearly labeled cost estimate.
3. The session's trend, volume activity, and volatility regime.
4. Multiple independent trading days, not repeated orders from one episode.
5. General market-microstructure and risk principles relevant to the decision.
6. A falsification criterion and a scheduled outcome review.

A market-regime/strategy bucket is exploratory until it has at least five
eligible exits across three distinct final sessions. Passing this minimum does
not force a change; it only permits evaluation. Higher-risk changes require a
larger sample.

Circuit-breaker daily PnL and consecutive-loss direction use confirmed net PnL,
not submitted-price or pre-cost PnL. A positive gross move that fails to clear
round-trip costs is still a loss for risk control.

The account-wide risk day rolls at 07:00 KST, after the US regular close in
both daylight-saving and standard time and before the KRX regular open. It
combines the local KRX session and the corresponding US session into one
account-loss interval. Market formulas and consecutive-loss breakers remain
separate, but an account daily-loss breach blocks new entries in both markets
until the next risk-day rollover. A cooldown must never erase daily PnL.

An action label is not fill evidence. A `BUY_REAL` or `SELL_REAL` cycle row is
eligible only when it has an execution-group ID and that group has a
KIS-confirmed fill in the matching direction with positive filled quantity.
This same boundary applies to entry frequency, session PnL, strategy
performance, strategy guards, exit-reason warnings, before/after comparisons,
and market-regime evaluation. Legacy rows remain available for audit but
cannot train or score policy.

## Restart safety

- A process restart does not start a new risk session. Persist and restore
  confirmed net PnL, market-specific consecutive losses, halt/release times,
  daily-loss state, and order-rejection breaker state before any new order
  decision.
- At service startup, reconstruct current risk-day PnL from broker-confirmed
  sell rows after the 07:00 KST boundary. Persisted runtime state remains the
  fallback if this query fails; a stale calendar date or restart-local subtotal
  must not replace the confirmed ledger.
- Runtime-state replacement must be atomic. A partial JSON write must leave the
  prior valid state available rather than silently resetting safeguards.
- Session ownership is reconstructed from same-session broker-confirmed buys,
  not symbol identity alone. This keeps restarted bot positions attributable
  without claiming manually imported holdings.
- Position entry time is immutable execution state and must not be reconstructed
  from a watch cache's mutable update time. Persist it separately, fall back to
  a broker-confirmed buy for legacy state, prefer broker average fill price over
  a submitted-price cache, and retain entry context across partial exits.
- `cycle_log.entry_time` and `hold_duration_min` are derived evaluation fields.
  Their canonical interval runs from the active broker-confirmed buy fill to
  the first sell submission in the confirmed sell execution group. A repair
  may update only these derived fields, only when no fully filled sell
  intervenes; raw execution rows remain immutable evidence.
- A replacement order may carry a newer, more severe exit signal. Performance
  attribution still uses the first sell submission's reason and signal
  snapshot because that decision initiated the exit. Replacement latency and
  its later signal remain available in the raw execution group for audit.
- A fill caused by a missing safety state still counts in account PnL and risk.
  It is incident evidence, not evidence for loosening the entry formula; its
  policy attribution must be called out in later regime reviews.

## Broker API reliability

- Rate limiting is shared by every client and process using one active account
  profile. Logical-request success and rejected attempts are reported
  separately: a successful retry preserves functionality but does not erase
  the rejected attempt as reliability evidence.
- KIS's official overseas `inquire_ccnl` example waits before requesting a
  continuation page. On the VPS profile, overseas order-history continuation
  pages use an additional one-second post-response delay because every observed
  one-second retry succeeded while request-start throttling alone still
  produced `EGW00201`.
- This delay is endpoint- and environment-specific. Do not slow production,
  domestic history, or the global request floor without matching evidence.
  Repeated continuation failures after deployment falsify the current delay
  hypothesis and require a new timing/cursor audit.

## Frequency decisions

- Low order frequency is not itself a defect.
- Loosen entry gates only when blocked candidates show positive forward net
  expectancy after realistic costs and the result persists across regimes.
- Reject a frequency increase when the apparent gross edge is below costs,
  comes from one market day, or is concentrated in a hostile regime.
- Report both submission and confirmed-fill frequency by market, and only
  during sessions orderable by the active broker profile.

## Down-market inverse policy

- Domestic inverse symbols and US inverse symbols are separately configured;
  evidence from one market never activates the other.
- A candidate is eligible only when the same local session's benchmark is down
  at least 1%, its benchmark trend is down, and the inverse product itself has
  a rising intraday trend, positive current momentum, and market-specific
  volume confirmation.
- Eligibility does not authorize a broker order. The current execution mode is
  `shadow`; defensive order checks reject inverse products unless that market's
  policy is explicitly changed to `live`.
- The shadow ledger charges both sides' commissions and half-spread on entry
  and exit. It closes on take-profit, stop, hard stop, time limit, benchmark
  recovery, or session rollover. Daily-reset inverse products are never carried
  into the next session by this policy.
- Consider a small live pilot only after broker-quality shadow observations
  include at least five exits across three final benchmark sessions, net
  expectancy after costs is positive, and drawdown/tracking behavior remains
  within the predeclared risk limit. This is an evaluation floor, not automatic
  approval.
- Reject live activation when product/benchmark tracking diverges materially,
  gains are concentrated in one shock session, or net expectancy is nonpositive.
  Daily leverage, compounding, volatility drag, and sharp bear-market rallies
  are first-class risks, not implementation noise.
- Zero inverse trades must remain explainable. Record one durable observation
  per local session, market, symbol, policy stage, and reason for regime
  rejection, quote failure/exclusion, product rejection, or product readiness.
  Restarts must not inflate this evidence. Observation counts are diagnostics,
  not trades and not performance samples.

Official product/risk references:

- [ProShares SQQQ](https://www.proshares.com/our-etfs/leveraged-and-inverse/sqqq?gad=1)
- [Direxion SOXS](https://www.direxion.com/product/daily-semiconductor-bull-bear-3x-etfs)
- [FINRA leveraged/inverse ETP guidance](https://www.finra.org/investors/insights/lowdown-leveraged-and-inverse-exchange-traded-products)
- [KODEX 200 Futures Inverse 2X](https://www.samsungfund.com/etf/product/view.do?id=2ETF70)
- [KODEX Inverse](https://www.samsungfund.com/etf/product/view.do?id=2ETF20)

## Reasoning value audit

`policy_evaluation_log` records the hypothesis, evidence, alternatives,
financial principles, confidence, falsification test, and later outcome.

High-cost reasoning is never marked superior by assertion. Its
`comparative_value_status` starts as `unverified` and may change only after a
measurable comparison:

- `confirmed`: it found a material issue or avoided a policy error that the
  declared baseline missed, and the result survived tests or later data.
- `no_gain`: it reached the same actionable result without additional verified
  value.
- `refuted`: its distinctive recommendation failed its stated validation test.

The baseline must be named, such as the unchanged policy, a fixed checklist, or
an actually executed lower-context/model review. Never invent another model's
result. Every later review should actively ask what evidence would show that
the current direction is wrong.

## Current decision checkpoint

- Domestic: eight broker-confirmed exits produced two after-cost wins and
  -2,380.92 KRW net. Do not loosen entry thresholds from this sample. Blocked
  candidates had materially negative forward returns, and each observed regime
  still covers only one final KOSPI session. On 2026-07-28, KOSPI closed
  -10.84% at 6,023.66 while two same-day exits lost 2,931.74 KRW net. This is
  not evidence for increasing long-entry frequency.
- Overseas: twenty broker-confirmed exits produced six after-cost wins and
  -325,904.33 KRW net. The final 2026-07-28 NASDAQ session was
  sideways/normal-activity/normal-volatility; its eleven exits produced three
  wins and -98,792.54 KRW net. `VWAP+RSI` was positive in four same-day exits,
  while `VWAP+VOL` was negative in six. Neither is eligible for a policy change
  because each bucket still spans fewer than three final sessions.
- Do not extend the aggregate standalone-strategy guard to combination labels
  from the current 48-hour average alone. Under True Range, the eleven
  `VWAP+VOL` exits belong to the same sideways/normal-activity/normal-volatility
  bucket but span only two final sessions; the minimum is three. Re-evaluate
  after the declared regime sample matures instead of converting one crash
  episode into a permanent entry block.
- Frequency: the seven-day confirmed ledger contains eight domestic and
  twenty-seven overseas entries, followed by eight and twenty exits. This is
  not evidence of a system frequency ceiling. Do not loosen entry gates while
  after-cost expectancy remains negative and regime coverage is this narrow.
- Both markets: inverse trading remains shadow-only. Current evidence justifies
  testing a separate down-market formula, but not risking broker capital. The
  first deployment occurred after the observed KOSPI crash session had closed,
  and the final NASDAQ return of -0.22% was above the -1% gate, so zero shadow
  exits is currently "not observed", not evidence of failure or success.
  Durable regime and product-stage observations now make each zero-sample
  reason auditable.
- Performance now uses the broker execution ledger. Submission rows, canceled
  orders, and replacement attempts are excluded; partial/replacement fills in
  one execution group produce one confirmed trade.
- A temporary KIS balance row may remain after a complete sell fill. When
  `holding > 0` and `orderable = 0`, defer another sell when an accepted,
  unfinalized sell from the last eight minutes covers the stale holding, or
  when a broker execution group reached its full target within five minutes.
  Never apply the accepted-order exception to a terminal canceled/rejected
  group or when its requested quantity is smaller than the holding.
- FSUN and HUBB limit buys accepted near the 2026-07-28 US close remain
  broker-reported as unfilled. Do not infer cancellation from the clock or
  block late-session entries from these two rows. Keep them out of performance,
  preserve their execution groups, and validate cancellation or unexpected
  carry at the next VPS regular session.
- The 2026-07-28 restart lost an active overseas breaker and allowed an ARX
  481-share paper-account buy during the original 30-minute halt. The fill
  remains part of account/risk PnL, but it must not be interpreted as evidence
  that entry thresholds should be loosened. Breaker state is now durable and
  same-session ownership reporting is reconstructed from confirmed buys.
