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
- Runtime-state replacement must be atomic. A partial JSON write must leave the
  prior valid state available rather than silently resetting safeguards.
- Session ownership is reconstructed from same-session broker-confirmed buys,
  not symbol identity alone. This keeps restarted bot positions attributable
  without claiming manually imported holdings.
- Position entry time is immutable execution state and must not be reconstructed
  from a watch cache's mutable update time. Persist it separately, fall back to
  a broker-confirmed buy for legacy state, and retain it across partial exits.
- A fill caused by a missing safety state still counts in account PnL and risk.
  It is incident evidence, not evidence for loosening the entry formula; its
  policy attribution must be called out in later regime reviews.

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
  still covers only one final KOSPI session.
- Overseas: fourteen broker-confirmed exits produced four after-cost wins and
  -353,126.79 KRW net. Do not loosen entry thresholds yet. The apparent
  `VWAP+VOL` edge in a sideways/normal-activity/high-volatility NASDAQ regime is
  five exits from one final session, while blocked-signal forward returns did
  not clear the cost hurdle.
- Both markets: inverse trading remains shadow-only. Current evidence justifies
  testing a separate down-market formula, but not risking broker capital. The
  first deployment occurred after the observed KOSPI crash session had closed,
  and the current provisional NASDAQ return is above the -1% gate, so zero
  shadow exits is currently "not observed", not evidence of failure or success.
  Durable regime and product-stage observations now make each zero-sample
  reason auditable.
- Performance now uses the broker execution ledger. Submission rows, canceled
  orders, and replacement attempts are excluded; partial/replacement fills in
  one execution group produce one confirmed trade.
- A temporary KIS balance row may remain after a complete sell fill. When
  `holding > 0` and `orderable = 0`, suppress another sell only if a broker
  execution group reached its full target within five minutes and its filled
  quantity covers the stale holding. Never apply this exception to a partial
  group fill.
- The 2026-07-28 restart lost an active overseas breaker and allowed an ARX
  481-share paper-account buy during the original 30-minute halt. The fill
  remains part of account/risk PnL, but it must not be interpreted as evidence
  that entry thresholds should be loosened. Breaker state is now durable and
  same-session ownership reporting is reconstructed from confirmed buys.
