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
- Operational diagnostics that may initiate a policy review are also
  market-specific. Exit-reason ratios, minimum-hold warnings, and their audit
  events must group by market and display that market's configured value;
  combining KRX and US exits can hide a local problem or falsely implicate the
  other formula.
- Domestic performance is joined to the final KOSPI session regime.
- Overseas performance is joined to the final NASDAQ Composite session regime.
- Provisional benchmark rows are stored for monitoring but excluded from policy
  evaluation until the market close is final.
- An approved domestic inverse symbol whose KIS product type is `ETF` or `ETN`
  is not rejected solely by the generic low-share-price filter. That filter is
  a speculative-stock proxy, not an ETF liquidity measure. Intraday turnover,
  recent volume, spread, same-session benchmark decline, product direction and
  volume confirmation remain mandatory. Unapproved symbols and an approved
  code returned as an ordinary stock receive no exemption.
- Domestic inverse entries use the independently versioned
  `domestic_momentum_v2` shadow formula. The ordinary long-side VWAP, RSI/MACD
  and 2x-volume consensus is not an adequate proxy after an inverse ETF has
  already moved far above VWAP during a broad-market decline. The dedicated
  formula requires a same-session KOSPI decline of at least 3%, an up-sloping
  inverse-product minute trend with both positive multi-bar momentum and a
  positive current-bar return, projected relative volume of at least 0.8,
  price within 0.5% below the recent breakout level, RSI no higher than 85,
  and the existing spread and extension limits.
- Before that domestic formula can produce a shadow entry, the official KIS
  ETF/ETN quote must confirm a negative tracking multiplier, positive NAV, and
  an absolute market-price/NAV deviation no greater than 1%. Missing or stale
  product metadata fails closed. The product endpoint and field definitions
  are documented by the
  [KIS ETF/ETN current-price sample](https://github.com/koreainvestment/open-trading-api/blob/b093e42ba32d1df5f5ddad7a71cb715cbc800832/examples_llm/etfetn/inquire_price/inquire_price.py)
  and its
  [official response mapping](https://github.com/koreainvestment/open-trading-api/blob/b093e42ba32d1df5f5ddad7a71cb715cbc800832/examples_llm/etfetn/inquire_price/chk_inquire_price.py).
  KRX explains that NAV disclosure, arbitrage and LP quotes support price/NAV
  convergence, but do not justify ignoring observed spread or deviation
  ([KRX ETF price and liquidity](https://open.krx.co.kr/contents/OPN/01/01030204/OPN01030204T8.jsp)).
- The domestic dedicated formula remains shadow-only even if an operator
  changes the inverse execution mode to `live`; a separate code and evidence
  change is required to remove that block. The overseas policy remains
  `overseas_momentum_v1/strategy_consensus_v1` until US-specific down-regime
  observations support its own revision. KODEX states that `114800` targets
  the inverse of the F-KOSPI200 **daily** return and that longer or more
  volatile holding periods can diverge
  ([KODEX Inverse](https://www.samsungfund.com/etf/product/view.do?id=2ETF20));
  `252670` targets -2x daily return and can suffer larger path dependence
  ([KODEX 200 Futures Inverse 2X](https://www.samsungfund.com/etf/product/view.do?id=2ETF70)).
  Therefore one entry per symbol/session, intraday exits, conservative spread
  simulation and no live promotion from one crash-day replay remain mandatory.
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

Domestic costs are product-aware. The domestic policy charges its configured
round-trip commission to every KRX fill and a 0.20% sell tax to ordinary KOSPI
and KOSDAQ shares. ETF, ETN, and ELW rows are exempt only when the structured
KIS current-price field `rprs_mrkt_kor_name` identifies that product class.
An unknown class is treated as taxable rather than silently exempt. Persist the
product class and `domestic_product_tax_v2` calculation version on confirmed
cycles. Historical correction requires a consistent SQLite online backup,
fresh KIS classification for every affected symbol, and an idempotent second
pass. The rate basis is the current
[Korea Investment domestic fee and tax schedule](https://securities.koreainvestment.com/main/customer/guide/_static/TF04ae010000.jsp?tab=1);
the overseas 0.25% online commission and sell-side SEC fee remain governed by
the separate
[overseas market schedule](https://securities.koreainvestment.com/main/bond/research/_static/TF03ca050000.jsp).

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

Fill confirmation and strategy ownership are separate boundaries. Strategy
performance, entry guards, exit-reason diagnostics, before/after comparisons,
and regime attribution accept a confirmed sell only when either the cycle is
marked `is_session_trade=1`, or a confirmed `is_session_trade=1` buy for the
same non-empty session, market, and symbol precedes the sell. The latter
recovers legacy sell flags without discarding genuine bot trades. A confirmed
exit from an imported holding with no such buy remains in account-wide PnL,
daily-loss and consecutive-loss controls, but cannot train or score a formula.

A virtual sell against a real overseas holding closes strategy exposure but
does not prove broker settlement. Preserve its `virtual_sell_pending` quantity
until the VPS regular session can reconcile it. When that pending quantity
fully covers the visible real holding, a zero `ord_psbl_qty` outside the
profile-orderable session is an expected account-profile boundary, not evidence
of T+2 failure. It must neither consume the no-orderable anomaly budget nor be
selected for another strategy exit. During a profile-orderable session, zero
quantity or a rejected settlement order is a genuine reconciliation stall:
retain the pending row, record a cause-specific diagnostic, and alert with
bounded backoff. An accepted settlement order must enter the same immutable
broker-execution ledger as every other real order. Preserve the entire pending
quantity and block duplicate submission until KIS order history reports a
terminal outcome. A full fill removes the matching quantity, a terminal partial
fill removes only its confirmed quantity, and a cancel or rejection removes
none. A stale open settlement may be canceled only when its broker order number
matches the tracked execution; retry starts after that cancellation becomes
terminal in order history. Submission alone is neither settlement nor
broker-confirmed performance.

The virtual exit already owns the strategy decision time and strategy PnL.
Later broker settlement is an execution-control event: record its actual fill,
virtual reference price, and settlement slippage. The confirmed account fill
must enter account-wide PnL and risk control exactly once, but its `SELL_REAL`
accounting row has an empty session ID and `is_session_trade=0` so it cannot
become a second strategy result. Use the actual settlement fill for account
costs and risk, while the virtual exit remains the formula-performance result.
Any fill beyond the still-pending quantity is an unmatched-fill incident and
must remain visible in the audit event rather than being silently attributed.

For a confirmed exit, `action_reason` is the canonical decision cause used in
reason-level performance and operator alerts. `exit_by` is the strategy
manager's exit-signal attribution and remains a separate diagnostic dimension.
Only when `action_reason` is empty may a report fall back to `exit_by`. A
strategy signal such as `VWAP` must never silently replace a protective or
profit-taking cause such as `momentum_loss_cut` or `take_profit`.

## Restart safety

- A process restart does not start a new risk session. Persist and restore
  confirmed net PnL, market-specific consecutive losses, halt/release times,
  daily-loss state, and order-rejection breaker state before any new order
  decision.
- At service startup, reconstruct current risk-day PnL from broker-confirmed
  sell rows after the 07:00 KST boundary, including confirmed exits from
  positions imported before the bot session. The session-ownership filter is
  appropriate for strategy performance, not account-loss protection.
  Reconstruct again after every newly confirmed sell so an out-of-order
  historical row cannot contaminate the current risk day. Persisted runtime
  state remains the fallback if this query fails; a stale calendar date or
  restart-local subtotal must not replace the confirmed ledger.
- Before the first order decision after restart, replay recent confirmed exits
  in execution-time order and restore each market's trailing net-loss streak.
  If the streak crossed its market threshold, anchor the remaining cooldown to
  the confirmed exit that first crossed it. Do not erase a live cooldown or
  restart an already expired cooldown from process startup time.
- A persisted market-specific `cb_released` event is a hard timeline boundary
  for consecutive-loss restoration. Confirmed outcomes at or before that
  release remain in account PnL but cannot reappear as a post-release streak.
  Outcomes after the release start again from zero. A later unexpired
  `cb_fired` still overrides inferred streak state with its recorded count and
  original timestamp.
- KIS aggregate order-history rows expose filled quantity and price but only an
  order timestamp, not a distinct fill timestamp. Treat that timestamp as the
  best available effective time, name the source in the audit event, and retain
  the full raw response. If confirmation crosses the 07:00 KST risk boundary,
  replay consecutive-loss and re-entry cooldown controls only within the
  declared 30-minute reconciliation grace. Anchor any remaining cooldown to the
  effective order time. An older confirmation corrects historical performance
  but must not create current-day PnL, reorder the live loss streak, or start a
  fresh expired cooldown.
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
  attribution uses the latest order in the execution group with positive
  confirmed fill quantity, because an unfilled predecessor did not execute the
  trade. Keep the earlier intent, replacement latency, and every raw execution
  row for audit.
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
- Unfinalized execution rows remain immutable while their market cannot fill.
  Poll KIS execution history during the active profile's orderable session and
  for 30 minutes after that session closes to catch delayed reports. Outside
  that window, defer API reconciliation until the next eligible session; do
  not infer a cancel, rejection, or no-fill from elapsed wall time. Explicit
  forced audits may bypass this scheduling guard.

## Session boundary safety

- A market-open flag is a short-lived observation, not permission for the
  entire cycle. Recompute KRX open state and US session/profile orderability
  after any full scan and before deriving executable targets.
- If the market or US session changes during a scan, discard that market's
  decisions for the cycle. A fresh cycle may act on fresh quotes; stale
  pre-transition quotes may not be carried across daytime, premarket, regular,
  aftermarket, or closed boundaries.
- The paper profile does not start a full US scan with 120 seconds or less
  remaining in the current open session. This threshold is an operational
  guard based on the observed 93-call, 99-second scan, not an entry-policy
  parameter and not evidence for changing trade frequency.
- Real-order helpers retain their final broker-session checks. Virtual buys and
  sells must independently verify that the real US market is still open before
  writing performance rows. A closed-session signal is diagnostic evidence,
  never a virtual fill.

## Frequency decisions

- Low order frequency is not itself a defect.
- Loosen entry gates only when blocked candidates show positive forward net
  expectancy after realistic costs and the result persists across regimes.
- Reject a frequency increase when the apparent gross edge is below costs,
  comes from one market day, or is concentrated in a hostile regime.
- Report both submission and confirmed-fill frequency by market, and only
  during sessions orderable by the active broker profile.
- A high share of near-flat, after-cost trend exits is evidence to inspect
  turnover, not automatic evidence to delay exits. Compare post-exit paths at
  declared horizons and require multiple final benchmark sessions before
  changing a market's hold hysteresis. Keep hard stops outside that delay.

## Strategy loss guards

- The initial lookback, sample, and loss thresholds may be copied across
  markets, but every blocked key is `(market, strategy_flag)`. A domestic loss
  never blocks the corresponding overseas strategy, or vice versa.
- Recheck the guard both while building executable watch targets and directly
  before broker submission. A stale target or cache transition must not bypass
  a newly active loss guard.
- A temporary guard is not a permanent formula verdict. Keep combinations and
  independently performing strategies open, and require final market-regime
  evidence across multiple sessions before converting it to a fixed block.

## Down-market inverse policy

- Domestic inverse symbols and US inverse symbols are separately configured;
  evidence from one market never activates the other.
- A KRX dynamic-rank row whose name identifies an inverse or leveraged product
  must match that product class's explicit domestic allowlist. Approved inverse
  products enter only through the local benchmark-regime path; unapproved
  structured products cannot fall through to the ordinary long formula.
- A currently held domestic product remains in quote and exit monitoring even
  when it is no longer in the dynamic rank or is ineligible for a new entry.
- A candidate is eligible only when the same local session's benchmark is down
  at least 1%, its benchmark trend is down, and the inverse product itself has
  a rising intraday trend, positive current momentum, and market-specific
  volume confirmation.
- During an open session, refresh the provisional benchmark regime every five
  minutes so a threshold crossing is not hidden behind the normal 30-minute
  history refresh. Provisional rows may activate shadow observation but remain
  excluded from performance-based policy changes until the close is final.
- An eligible inverse symbol or an already-open inverse shadow trade has
  priority inside the existing signal and unified-watch limits. It replaces a
  generic candidate rather than expanding the normal signal-call budget.
- Speculative liquidity filters still apply to every new inverse candidate.
  Only an already-open shadow trade bypasses those entry filters so its price,
  stop, benchmark recovery, and session rollover can continue to be observed.
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
  -10,389.32 KRW net. Do not loosen entry thresholds from this sample. Blocked
  candidates had materially negative forward returns, and each observed regime
  still covers only one final KOSPI session. On 2026-07-28, KOSPI closed
  -10.84% at 6,023.66 while two same-day exits lost 4,314.74 KRW net. This is
  not evidence for increasing long-entry frequency.
- Overseas: twenty broker-confirmed exits produced six after-cost wins and
  -325,904.33 KRW net. The final 2026-07-28 NASDAQ session was
  sideways/normal-activity/normal-volatility; its eleven exits produced three
  wins and -98,792.54 KRW net. `VWAP+RSI` was positive in four same-day exits,
  while `VWAP+VOL` was negative in six. Neither is eligible for a policy change
  because each bucket still spans fewer than three final sessions.
- Overseas `trend_filter_lost` review: ten confirmed exits span only two final
  NASDAQ sessions. Observable post-exit prices had mean returns of -0.066% at
  five minutes (10 rows), -0.115% at fifteen (7), +0.040% at thirty (6), and
  -0.049% at sixty (7), using a five-minute matching tolerance. Coverage is
  incomplete and direction is mixed, so retain the 30-cycle minimum hold and
  reject both a longer exit delay and a frequency increase for now.
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
