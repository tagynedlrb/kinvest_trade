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

## API reliability measurement

- KIS API health separates transport/API attempts from logical requests. Every
  new attempt records one logical request ID, its attempt number, whether a
  retry is scheduled and why, and whether the row is the terminal outcome.
  A failed attempt followed by a terminal success is a recovered request, not
  a terminal API failure. Raw failed attempts remain visible as broker-pressure
  evidence.
- Legacy rows with no logical request ID remain available for historical raw
  attempt analysis but are excluded from tracked terminal-request metrics.
  One logical request must produce exactly one terminal row.
- KIS VPS returned `90020000` with an explicit request to retry the delayed
  mock service six times on the overseas-balance GET between July 15 and
  July 29. Every case was followed by a successful same-endpoint read within
  37.7 to 70.2 seconds. Treat this code as a transient response only for
  idempotent VPS GETs: retry after two and four seconds, persist
  `service_delay` on each nonterminal attempt, and retain one final logical
  outcome.
- Never apply the `90020000` rule to POST. A delayed order response does not
  establish whether the broker accepted the first request, so replay could
  duplicate an order. Production GETs also remain unchanged until that
  environment supplies its own evidence.
- KIS's official flow notice states that the virtual REST environment allows
  one request per second per account as of April 20, 2026
  ([KIS API flow notice](https://apiportal.koreainvestment.com/community/10000000-0000-0011-0000-000000000001/post/d0d1a83f-6f8d-4437-9700-6d26702fd989)).
  Do not globally slow every market scan from recovered attempts alone.
  Compare terminal failures, recovery latency, sustained retry ratio and the
  opportunity cost of slower scans first.
- Attribute API latency before changing pacing. For every new attempt, retain
  total elapsed time and also record the UTC time immediately before HTTP
  dispatch, time spent waiting for the shared host pacer, and HTTP network
  elapsed time. Completion timestamps do not establish request-start spacing.
  Wall-clock dispatch deltas can also include event-loop resumption order, so
  pair them with the failed attempt's logical lineage. Never persist query/body
  parameters, account numbers, credentials, authorization headers or tokens
  for this diagnosis.
- Every KIS client created inside the Telegram daemon must use the controller's
  telemetry factory. Persist a bounded, non-sensitive `client_source` such as
  `lab_cycle`, `telegram_open_orders_overseas`, or `telegram_portfolio` on
  every attempt so scheduler maintenance can be separated from the trading
  cycle. Legacy rows retain an empty source. A source label does not permit
  storing query/body parameters, symbols, account identifiers, credentials,
  authorization headers or tokens, and it must not change request, retry or
  pacing behavior.
- Do not use a fixed 1.10-second VPS overseas-balance request-start floor.
  A natural `VTTS3012R` pair reproduced `EGW00201` at a measured
  1.100416-second gap, then recovered on the same logical request without a
  terminal failure. The narrow-boundary hypothesis is falsified. Keep the
  shared 1.05-second floor and bounded retry until the existing sustained or
  terminal-failure thresholds are met. A response-completion delay must also
  justify its certain per-cycle opportunity cost against measured retry cost
  before deployment.
- A natural VPS `EGW00201` or `EGW00215` now activates a profile-local,
  120-second adaptive response-completion floor. During that window the next
  dispatch waits for both the existing 1.05-second request-start floor and
  0.95 seconds after the preceding response completed. Production profiles
  and an error-free VPS path remain unchanged. Persist whether the adaptive
  mode was active and only the incremental wait beyond the request-start
  floor. The 0.95-second value is an empirical boundary, not a broker
  guarantee: all 15 dispatch-timed rate-limit responses had less than 950ms
  of estimated post-response quiet time, while 4,084 first attempts at
  950-999ms had none. Reassess with natural active-window results.
- Consecutive VPS overseas-balance first attempts also use a narrowly scoped
  preventive response-completion floor of 850ms. It applies only when the
  immediately preceding completed request used the exact same balance path;
  production, domestic, quote/order paths, interrupted lineages, and retries
  keep their existing pacing. In 215 naturally observed balance pairs, all
  19 recovered rate-limit attempts occurred below 826ms of post-response
  quiet time. The 850ms counterfactual added about 10.55 seconds across all
  pairs, versus 23.69 seconds of observed retry recovery delay. This is a
  bounded VPS experiment, not a causal result or broker guarantee: 191 pairs
  below 950ms also succeeded. Persist pair activation and incremental wait
  separately from adaptive pacing, and do not count overlapping readiness
  boundaries twice.
- The first deployed natural window contained 27 consecutive balance pairs
  and 525 API attempts with no attempt or terminal failures. Twenty-six pairs
  needed no incremental wait. After one preceding balance response took
  236ms, the next pair added 36ms and dispatched 851.031ms after completion;
  it succeeded on the first attempt. This confirms selective activation and
  cost accounting at the intended boundary, but not the unobservable
  counterfactual that the same call would otherwise have rate-limited.
- Reconsider the current VPS pacing when a tracked rate-limit request ends in
  terminal failure, recovered rate-limit requests exceed 1% for three
  consecutive 30-minute windows, recovery p95 exceeds five seconds, or retry
  or adaptive delay causes an orderable policy bar to be missed. Also revert
  or shorten the adaptive window if its measured incremental wait persistently
  exceeds the avoided retry latency without reducing clustered responses.
- KIS overseas-balance exchange scope is environment-specific. The official
  sample defines demo `NASD`, `NYSE`, and `AMEX` as separate US exchanges,
  while production `NASD` means all US markets and production `NAS` means
  Nasdaq only
  ([KIS overseas-balance sample](https://github.com/koreainvestment/open-trading-api/blob/b093e42ba32d1df5f5ddad7a71cb715cbc800832/examples_llm/overseas_stock/inquire_balance/inquire_balance.py)).
  A VPS `NASD` response that happens to include NYSE rows is observational
  evidence, not permission to collapse the documented demo requests.
- Overseas balance must follow KIS continuation semantics. When response
  `tr_cont` is `M` or `F`, use the returned `CTX_AREA_FK200` and
  `CTX_AREA_NK200` with request header `tr_cont=N`; accumulate at most ten
  pages and stop on an empty or repeated context. The current one-page account
  incurs no extra call. Review the first natural multi-page response for row
  uniqueness, cycle latency, terminal failures, repeated contexts, and
  max-page truncation before changing the bound.
- Position completeness is an exit-safety contract, not a trading-frequency
  parameter. A balance transport or pagination fix does not justify changing
  entry formulas, market-specific limits, or candidate frequency.
- VPS current open-order maintenance follows the documented
  `inquire-ccnl` demo contract. Use the current local-market date, empty
  `PDNO/OVRS_EXCG_CD/ORD_DT/ORD_GNO_BRNO/ODNO`, `00` for side and fill,
  and KIS continuation keys; derive open rows locally from `nccs_qty > 0`
  ([KIS overseas order-history sample](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/overseas_stock/inquire_ccnl/inquire_ccnl.py)).
  The demo API does not document per-symbol, side-specific, fill-specific or
  exchange-specific lookup. One paginated snapshot is reused for the service
  cycle and invalidated after the bot records a real overseas submit or
  cancel. Production continues to use its dedicated open-order endpoint.
- Current orderability and historical execution accounting are distinct.
  The VPS snapshot uses the current New York date so prior-session rows that
  the broker can no longer cancel do not trigger maintenance. This does not
  prove whether an ambiguous old execution filled, expired or was canceled;
  its execution row remains unfinalized until independent order-history,
  balance or broker evidence resolves it. A snapshot lookup failure still
  fails closed and blocks a new order.
- A fully virtual-closed overseas holding needs live quotes, broker balance,
  and settlement reconciliation but no fresh strategy exit chart. When
  deduplicated real quantity is fully covered by `virtual_sell_pending` and no
  same-symbol virtual exposure remains, omit that symbol from daily/minute
  signal refreshes and do not refill its signal-budget slot with a new
  candidate. Partial pending quantities and active inverse observations remain
  eligible. Persist scope changes in
  `overseas_pending_signal_scan_scope`. KIS notes that VPS REST limits are
  lower and repeated parameter calls can trigger `EGW00201`
  ([KIS Open API repository](https://github.com/koreainvestment/open-trading-api));
  remove non-executable calls before slowing every safety-critical request.

## Telegram command channel reliability

- Telegram `getUpdates` is a positive-timeout long poll. Its HTTP read timeout
  must exceed the declared server poll timeout, while connect, write and pool
  timeouts remain independently bounded. Telegram documents that pending
  updates are retained for no more than 24 hours and that advancing
  `offset` prevents duplicate delivery
  ([Telegram Bot API](https://core.telegram.org/bots/api)).
- A transport failure in the command listener must not terminate the trading
  scheduler. Retry after 3, 6, 12, 24 and at most 30 seconds; reset the streak
  on the first successful response. This backoff affects command reception,
  never market scanning, position monitoring or exit execution.
- Persist one `telegram_poll_outage_started` event for the first failure and
  one `telegram_poll_outage_recovered` event after recovery. The pair records
  safe exception type, optional HTTP status, failure count and duration so
  outage frequency can be evaluated without counting every retry as an
  independent incident.
- Bot API URLs contain the bot token. Poll diagnostics must never persist or
  print the request URL, exception message, headers, traceback or credentials.
  Safe exception class names and HTTP status codes retain enough distinction
  for transient transport, authentication and duplicate-poller failures.
- Do not switch to a webhook from a small number of recovered long-poll
  timeouts alone. Reconsider the reception architecture only when outages
  cause a confirmed command to exceed the declared response objective,
  pending updates approach Telegram's retention boundary, or recovery events
  remain sustained across independent network windows.

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
- `market_regimes` remains the authoritative latest/final daily row. Intraday
  changes are append-only in `market_regime_observations`; only the market's
  current local session is appended, and historical recalculation rows are
  never presented as observations that happened in real time.
- Validate anomalous final benchmark moves against a date-bounded historical
  OHLC source before using them in a policy review. Match the session date,
  open, high, low and close rather than a search snippet or a dynamic current
  index page: the latter may display a pre-open/live value under the prior
  session's data label. Keep the original KIS row and raw payload immutable
  when sources disagree, record the discrepancy, and defer policy changes
  until the session attribution is resolved. External validation is an audit
  input, not a new runtime dependency.
- Every submitted BUY keeps a same-session entry-regime snapshot in the broker
  event and execution context. Missing current-session data is recorded as
  unavailable; a prior session must never be substituted. Final-session regime
  performance and entry-time regime performance are separate evaluation
  dimensions.
- Current regime context may accompany a strategy-guard audit, but it does not
  release the global emergency guard. Regime-specific loosening still requires
  at least five confirmed exits across three final benchmark sessions and
  positive net expectancy after costs.
- An approved domestic inverse symbol whose KIS product type is `ETF` or `ETN`
  is not rejected solely by the generic low-share-price filter. That filter is
  a speculative-stock proxy, not an ETF liquidity measure. Intraday turnover,
  recent volume, spread, same-session benchmark decline, product direction and
  volume confirmation remain mandatory. Unapproved symbols and an approved
  code returned as an ordinary stock receive no exemption.
- Domestic inverse entries use the independently versioned
  `domestic_momentum_v4` shadow formula. The ordinary long-side VWAP, RSI/MACD
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
  change is required to remove that block. KODEX states that `114800` targets
  the inverse of the F-KOSPI200 **daily** return and that longer or more
  volatile holding periods can diverge
  ([KODEX Inverse](https://www.samsungfund.com/etf/product/view.do?id=2ETF20));
  `252670` targets -2x daily return and can suffer larger path dependence
  ([KODEX 200 Futures Inverse 2X](https://www.samsungfund.com/etf/product/view.do?id=2ETF70)).
  Therefore one entry per symbol/session, intraday exits, conservative spread
  simulation and no live promotion from one crash-day replay remain mandatory.
- US inverse entries independently use
  `overseas_momentum_v2/us_regime_trend_breakout_v1`. The formula requires a
  same-session NASDAQ decline of at least 1%, an inverse-product price above
  its rising fast minute average, positive multi-bar and current-bar returns,
  projected relative volume of at least 1.3, price no more than 0.5% below
  the recent breakout level, RSI no higher than 85, and the existing spread
  and extension limits. It remains shadow-only and retains the dedicated
  `live` hard block.
- The US formula change is an observation-path correction, not a profitability
  claim. On 2026-07-29, `event_log.id=4556` recorded SOXS at 69.54 with NASDAQ
  at -1.135%, relative volume 1.70, positive momentum and a 1.05% breakout,
  yet the generic consensus returned `setup_not_ready`. Looking only at a later
  70.35 price implied about +0.66% after the configured round-trip cost, but
  full path replay invalidated that favorable snapshot: the next recorded
  SOXS quote was 69.1951, a modeled -0.99% net result that would have triggered
  the current shadow stop. The first later SQQQ setup also modeled a -1.21%
  hard stop, while SPXU remained open at about -0.33% net at the replay cutoff.
  None was an executed shadow trade. In the original snapshot, SQQQ and SPXU
  remained below the 1.3 volume floor, and a later SOXS reversal had negative
  current momentum. Preserve those selective controls and require completed
  out-of-sample shadow paths before any economic conclusion.
- The first deployed cycle supplied out-of-sample routing evidence, not
  performance evidence. With provisional NASDAQ return at -1.448%, SQQQ and
  SPXU opened shadow rows under `us_regime_trend_breakout_v1`; SOXS remained
  blocked because its projected relative volume was 1.19, below 1.3. No broker
  order or execution was created. Simultaneous inverse products share the same
  market shock and are correlated observations: retain each product path for
  execution analysis, but do not count them as independent market-session
  evidence when considering live promotion.
- Evaluate inverse exits with price-path attribution before changing their
  thresholds. For closed shadow rows, report maximum favorable excursion as
  `(peak-entry)/entry`, maximum adverse excursion as
  `(trough-entry)/entry`, and peak giveback as `(peak-exit)/entry`; open rows
  may show current MFE and MAE but must not enter closed averages. A trailing
  stop, take-profit or stop-loss change still requires at least five closed
  observations across three final benchmark sessions, positive after-cost
  evidence, and a repeated path mechanism. One domestic crash-session
  giveback is not evidence for the independently owned overseas inverse exit.
- The domestic approved leveraged-long list currently contains only `122630`.
  Under `domestic_momentum_v4`, every strategy signal for that list must pass
  both the daily and intraday uptrend properties before it can become a buy
  candidate, and the order helper must recheck the same boundary immediately
  before submission. This closes the path where the generic entry formula
  returned `leveraged_product_trend_down` while an independent RSI strategy
  still surfaced `BUY`. The overseas option remains disabled; a domestic
  counterexample does not modify `TQQQ/SOXL`.
- Product recognition is not product approval. `233740` is officially a
  KOSDAQ150 daily 2x ETF
  ([KODEX KOSDAQ150 Leverage](https://www.samsungfund.com/etf/product/view.do?id=2ETF56)),
  but it remains outside the approved list and must continue to be excluded as
  `unapproved_leveraged_product`. Adding a known leveraged code to an approval
  list requires separate final-session performance and product-risk evidence.
- Five broker-confirmed, session-owned domestic standalone-VWAP exits across
  only two sessions are insufficient for a permanent formula deletion or
  momentum threshold. A market-strategy guard activation is durable: keep it
  active while the rolling loss condition remains true, and after that
  condition clears, release it only when that market has recorded its
  separately configured minimum number of final benchmark sessions since
  activation. Lookback hours, minimum trades, average-net threshold, observed
  strategy labels, and minimum final sessions all belong to each market policy
  file. Their initial values are equal clones, but may diverge only on
  market-specific evidence. The global liquidity-lab settings control only
  whether the guard is enabled and which markets are in scope. Three sessions
  are a minimum observation hold, not proof of profitability or permission to
  loosen a formula. Retain legacy unconfirmed rows for diagnostics only and
  keep the existing five confirmed exits across three final sessions
  requirement for a permanent change. This avoids
  treating repeated scans from one market path as independent evidence, a
  known source of backtest-selection bias
  ([NBER Working Paper 21329](https://www.nber.org/papers/w21329)).
- A consecutive-loss breaker cooldown is a minimum time boundary, not evidence
  that the market state which produced the losses has changed. After the first
  overseas breaker released at 16:33:32 UTC on July 29, the latest provisional
  NASDAQ return was still -1.049%. LXFR entered four seconds later and closed
  at -0.502% net; FVRR entered while the benchmark was -1.224% and later closed
  at approximately -0.694% net. Both below-floor entries lost, but they remain
  retrospective, non-independent observations from the same provisional
  session. Once the benchmark recovered to -0.987%, later entries were
  admitted. MAS and EVR still lost and triggered a second breaker at 17:20:44,
  so the recovery gate is not a profitability claim or a substitute for the
  ordinary entry formula.
- For overseas only, a released consecutive-loss breaker now requires a fresh
  same-New-York-session NASDAQ observation with return at or above -1.0% before
  another ordinary long entry. The observation may be at most 600 seconds old;
  missing or stale data fails closed. The -1.0% value reuses the independently
  owned US inverse-regime boundary instead of fitting a new threshold to one
  path. It does not affect exits or inverse candidates. The domestic field is
  disabled because that session had no post-release domestic entry and its
  first stored same-day KOSPI observation arrived hours after the release.
  Research showing that reducing exposure when volatility is high can improve
  risk-adjusted outcomes supports the direction of a bounded risk reduction,
  but does not validate this exact threshold
  ([NBER Working Paper 22208](https://www.nber.org/papers/w22208)).
- The first deployed recovery path restored the actual 17:51:28 UTC overseas
  release and captured NASDAQ at -0.537% 47 seconds later. Ordinary-long WSO
  and TEVA orders passed the gate, while an ASH exit remained eligible. This
  confirms state restoration, fresh-data admission, and the exit exemption;
  it does not establish the admitted trades' profitability or the threshold's
  superiority.
- The same New York session then supplied a second, distinct consecutive-loss
  breaker fire. All eleven strategy exits confirmed after the first fire were
  after-cost losses, totaling USD -404.175456 and KRW -545,636.85, with
  individual net returns from -0.4050% to -1.3653%. These are correlated
  same-session observations and do not justify deleting an entry formula, but
  two separate three-loss trigger events are sufficient to stop adding
  ordinary-long exposure for the remainder of that session.
- The overseas policy therefore sets `post_cb_max_fires_per_session=2`.
  After the second same-session consecutive-loss breaker actually releases,
  ordinary-long entries remain blocked until the next New York session.
  Durable `cb_fired` events are re-counted by market and local session after a
  restart. Existing exits and inverse candidates remain exempt. The domestic
  field stays disabled because no domestic repeated-fire evidence exists.
  This is a bounded risk response, not a profitability claim, and it does not
  loosen or tighten normal-session frequency.
- During the deployment wait, all six ordinary-long entries opened after the
  second release closed at an after-cost loss: PAYO, FEIM, MOD, FER, ALOY and
  SITE totaled USD -270.404745 and KRW -365,046.41. A third overseas breaker
  consequently fired at 18:30:46 UTC. The new two-fire rule would have blocked
  these entries, but this remains a same-session retrospective counterfactual,
  not independent evidence of long-run profitability.
- Deployment restored the active third breaker and its 19:00:46 UTC release
  boundary. The first completed cycle made no order, had no failed API attempt,
  and naturally classified a PAYO ordinary-long candidate as
  `post_cb_session_loss_limit_reached`. This confirms wiring, durable replay,
  and the distinction from the NASDAQ-recovery-only baseline. It does not
  validate the exact threshold across sessions; the high-context reasoning
  comparison is confirmed only for finding and preventing the omitted
  repeated-fire risk.
- Reassess the two-fire limit over at least three final US sessions containing
  a repeated trigger. Record blocked-long counterfactual net outcomes and
  admitted inverse outcomes. Relax or return the rule to shadow if blocked
  longs have positive after-cost expectancy and their opportunity cost exceeds
  avoided losses, if event replay mixes markets or sessions, if the next
  session remains blocked, or if any exit or inverse candidate is affected.
- Re-evaluate the US post-breaker floor after at least five admitted and five
  blocked completed outcomes across three final US sessions. Revert or revise
  it if recovered entries systematically underperform blocked counterfactuals,
  if stale/missing benchmark data prevents otherwise valid entries for more
  than one refresh interval, if a new breaker release bypasses the gate in its
  first cycle, or if the gate touches an inverse candidate or any exit. Do not
  copy the threshold to the domestic policy without domestic evidence.
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

Overseas virtual performance must report both gross PnL and estimated net PnL
after the configured buy and sell commission plus the sell-side SEC fee.
Aggregate results must also be split by exit session so regular, premarket,
aftermarket, daytime, overnight, and unknown-session outcomes cannot hide one
another. The displayed session is the exit timestamp; it is not evidence of
the entry session by itself.

A virtual sell created to close an existing real holding during a
profile-unavailable session is reconciliation evidence, not a
strategy-originated virtual entry. Do not use that row to infer entry quality
unless a preceding virtual buy proves its origin. Reconsider an
aftermarket-entry restriction only after at least five virtual-origin exits
across three independent US entry sessions and a persistently negative
after-cost expectancy relative to regular or premarket observations. The SEC
identifies lower liquidity, wider or unavailable quotes, uncertain prices and
greater volatility as extended-hours risks
([SEC extended-hours bulletin](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins-42));
these risks justify separate measurement, not a conclusion from two sessions.

Circuit-breaker daily PnL and consecutive-loss direction use confirmed net PnL,
not submitted-price or pre-cost PnL. A positive gross move that fails to clear
round-trip costs is still a loss for risk control.

Once a market-specific consecutive-loss breaker fires, its cooldown is latched
to the original fire time. Later confirmation of a profitable exit may reset
the trailing loss count and must still update account PnL, but it cannot
silently reopen entries before that cooldown expires. Only the recorded
automatic expiry or an explicit operator reset may release the latch. This
rule is market-local: a KRX latch cannot block US entries and a US latch cannot
block KRX entries. KIS overseas aggregate history exposes an order time rather
than a distinct fill time, so delayed confirmation can make the exact economic
outcome order unknowable; that uncertainty favors preserving an already-fired
safety interval, not moving its start or declaring the trigger erroneous.

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
selected for another strategy exit. It must also remain absent from the active
strategy manager and appear in watch/cycle state as
`WAIT/SETTLEMENT_PENDING/virtual_sell_pending`, not as a repeated
`SELL_READY`; this separates completed strategy exposure from unfinished
broker reconciliation. A partial pending quantity or an independent positive
virtual position retains normal strategy monitoring. During a
profile-orderable session, zero quantity or a rejected settlement order is a
genuine reconciliation stall:
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
- An unexpired overseas signal-unavailable suppression is also restart state.
  Persist only its future expiry and associated failure count; discard expired
  or unrelated counters. A deployment restart must not turn a declared
  180-minute exclusion into three fresh broker chart calls, while held symbols
  remain exempt so exit monitoring continues.
- Per-symbol consecutive-loss streaks are also restart state. Persist
  market-qualified symbol keys, including a zero reset marker after a net win,
  so a restart cannot shorten the existing two-loss 60-minute or three-loss
  180-minute re-entry cooldown. For legacy state with no streak field, replay
  broker-confirmed real exits once in effective-time order, stop at the latest
  after-cost win, exclude virtual-settlement accounting rows, and anchor any
  still-live cooldown to the latest confirmed exit rather than process start.
  Never revive an already expired cooldown from historical losses.
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
- A VPS unfilled-order response can resurface an old row after the cancel API
  says the original order no longer exists. Once a later overseas cancel
  follow-up is terminal, exclude that normalized `(symbol, original order
  number)` from repeated automatic-cancel candidates. Compare timestamps so a
  later submission that reuses the same order number remains eligible. This
  tombstone stops duplicate broker calls only: an order-not-found response
  alone must not finalize or delete the execution ledger because it does not
  prove whether the order filled, expired, or was previously canceled.
- The first deployment passed both the startup scheduler check and the next
  ten-minute check without adding to the six predeployment FSUN/HUBB duplicate
  cancel events. Both ambiguous execution rows remained unfinalized, while an
  unrelated GLW take-profit order submitted and filled normally. Continue
  reconciliation independently; suppression success is not no-fill evidence.

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
- In a VPS US session that cannot submit broker orders, refresh the complete
  overseas candidate universe once per independently configured US policy bar.
  Between those refreshes, continue every-cycle quote and exit monitoring for
  real holdings, virtual holdings, pending settlements and open inverse shadow
  trades. This cadence applies whether KRX is open or closed; an orderable US
  profile still receives a complete scan every cycle.
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
- Raw WAIT rows are repeated scanner observations, not independent blocked
  opportunities. Start a new evaluation episode only when the same
  market/symbol/reason has been absent for at least five minutes.
- A chart-unavailable candidate is not an executable blocked opportunity.
  Overseas suppression evidence must distinguish a broker chart API error,
  insufficient daily history, and insufficient intraday bars, and must record
  actual versus required row counts. Do not lengthen the cooldown or blacklist
  a symbol until repeated reason-specific observations show that the current
  retry cadence has no recovery value.
- Evaluate 15/30/60-minute paths using the latest same-session price at or
  before the horizon, no more than five minutes stale. Exclude right-censored
  horizons and report observation coverage rather than filling them with a
  close or a later-session price.
- Include only sessions orderable by the active broker profile. Closed-session
  and unsupported extended-session stale prices are diagnostics, not
  hypothetical executable entries.
- The optimistic cost floor is 0.03% for domestic tax-exempt products and
  0.50% for overseas trades. Domestic stock tax, spread and slippage can only
  make results worse. Require at least three final sessions in the same regime,
  positive market-specific net expectancy, and no one-symbol/day concentration
  before loosening a particular gate.
- Do not label current activity as low frequency when three days already
  contain 11 confirmed domestic entries and 27 confirmed overseas entries.
  With after-cost account PnL negative in both markets and all ten overseas
  slots backed by real or virtual exposure, increasing scan aggressiveness or
  capacity has no current expectancy basis. Revisit only after positive
  market-specific net expectancy and confirmed profitable candidates blocked
  solely by capacity across at least three final benchmark sessions.

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

## Position lifecycle and capacity

- Position capacity is the unique union of positive real and virtual holdings
  by market and symbol. A virtual sell against a real holding removes strategy
  exposure but does not free broker capacity until KIS confirms settlement.
  Report `at capacity` separately from an actual overflow.
- Reaching the position cap explains blocked entry frequency; it is not by
  itself evidence that the cap should increase. Raise a market's cap only when
  released-slot candidates have positive net expectancy across at least three
  final benchmark sessions and the extra simultaneous exposure remains inside
  account risk limits.
- After `max_hold_cycles`, a nonnegative gross return below the market's
  conservative cost floor remains a hold with reason
  `time_exit_cost_floor_hold`. The floor is round-trip commission plus a
  0.3-percentage-point slippage buffer. Record the entry time, hold duration,
  hold cycles and virtual/real marker so this interval can be evaluated rather
  than hidden under a generic HOLD note.
- The conservative policy floor and the execution-time net estimate serve
  different purposes. A position slightly above estimated commissions but
  below the policy floor is not proof that immediate liquidation improves
  expectancy; spread, slippage and the path after exit still need observation.
- Do not force-close a position solely because it is old. The July 15 overseas
  long-duration cohort supplied a direct counterexample: WFC, BCC and UGP
  eventually closed at positive gross returns while CLM closed at a loss.
  Compare cost-adjusted alternatives at declared horizons and across final
  NASDAQ regimes before changing the overseas time-exit formula. Apply the
  same test independently to KRX data before changing the domestic formula.
- FINRA warns that frequent day trading can generate substantial commissions
  even when per-trade costs are low
  ([FINRA Rule 2270](https://www.finra.org/rules-guidance/rulebooks/finra-rules/2270)).
  The SEC's investor material likewise treats commissions as per-transaction
  fees that reduce portfolio returns and recommends calculating the increase
  required to break even
  ([Investor.gov fee bulletin](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins/updated),
  [ETF fee bulletin](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins/mutual-fund-and-etf-fees-and-expenses-investor-bulletin)).

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
- Eligibility and entry formulas are independently owned. Domestic
  `regime_trend_breakout_v1` uses its KOSPI crash threshold, projected-volume
  floor and KIS ETF/NAV validation. US `us_regime_trend_breakout_v1` uses its
  NASDAQ threshold and US product-volume floor. The shared evaluator is code
  reuse only; neither market reads the other market's parameters or evidence.
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
  `shadow`; the dedicated formulas also reject broker orders if an operator
  merely changes that setting to `live`. Removing the second block requires a
  separate reviewed code change.
- The shadow ledger charges both sides' commissions and half-spread on entry
  and exit. It closes on take-profit, stop, hard stop, time limit, benchmark
  recovery, or session rollover. Daily-reset inverse products are never carried
  into the next session by this policy.
- Report product paths and market-session evidence separately. Product open,
  close and win counts remain useful for comparing tracking and execution, but
  `observed_session_count` and `closed_session_count` count distinct
  market-scoped `entry_session_date` values. Multiple products from one shock
  may contribute multiple product paths but only one observed market session.
- Every inverse shadow entry freezes the same-session benchmark snapshot,
  including observation lineage, return, session-low return, rebound from that
  low, current position inside the intraday high-low range, and minutes until
  the regular close. A closed row also freezes the exit benchmark snapshot and
  signal snapshot. `/lab_performance` displays these path fields for the three
  most recent rows; final-session regime aggregation remains a separate
  evaluation dimension.
- A late-session loss after a large rebound from the session low is a
  `late_rebound_whipsaw` hypothesis, not proof that the RSI ceiling, entry
  cutoff, or benchmark decline gate is wrong. Segment later samples by
  rebound-from-low and minutes-to-close, then require the same five exits,
  three final sessions, and positive after-cost expectancy before changing a
  formula.
- Consider a small live pilot only after broker-quality shadow observations
  include at least five exits across three final benchmark sessions, net
  expectancy after costs is positive, and drawdown/tracking behavior remains
  within the predeclared risk limit. This is an evaluation floor, not automatic
  approval. The new US formula has a stricter initial floor of ten completed
  exits across at least three sessions.
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
- [FINRA non-traditional ETF FAQ](https://www.finra.org/rules-guidance/key-topics/etf/non-traditional-etf-faq)
- [Gao et al., Market intraday momentum](https://doi.org/10.1016/j.jfineco.2018.05.009)
- [Daniel and Moskowitz, Momentum Crashes](https://www.nber.org/papers/w20439)
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

- Domestic: the seven-day session-owned ledger has eleven confirmed entries
  and eleven exits; the account ledger has twelve confirmed exits, two
  after-cost wins and -83,147 KRW net. On 2026-07-29, KOSPI closed
  -5.98% at 5,663.24 in a strong-down/normal-activity/extreme-volatility
  regime. `volume_low` produced 178 deduplicated episodes and optimistic
  15/30/60-minute net means of -0.508%/-1.179%/-1.570%. The prior final
  strong-down day was also negative. Keep the domestic volume and trend gates.
- Overseas: twenty broker-confirmed exits produced six after-cost wins and
  -325,904.33 KRW net. The final 2026-07-28 NASDAQ session was
  sideways/normal-activity/normal-volatility; its eleven exits produced three
  wins and -98,792.54 KRW net. `VWAP+RSI` was positive in four same-day exits,
  while `VWAP+VOL` was negative in six. Neither is eligible for a policy change
  because each bucket still spans fewer than three final sessions.
- Overseas blocked-entry review: across two final
  sideways/normal-activity/normal-volatility sessions, `volume_low` has 247
  episodes and optimistic 15/30/60-minute net means of
  -0.375%/-0.318%/-0.056%; standalone VWAP and VOL blocks are also
  nonpositive at every horizon. `trend_down` is positive only at 60 minutes
  (+0.291%), while 15 and 30 minutes remain negative and 60-minute coverage is
  70%. Keep every current gate and retain the 60-minute result only as a
  hypothesis for the third final session.
- Overseas `trend_filter_lost` review: ten confirmed exits span only two final
  NASDAQ sessions. Observable post-exit prices had mean returns of -0.066% at
  five minutes (10 rows), -0.115% at fifteen (7), +0.040% at thirty (6), and
  -0.049% at sixty (7), using a five-minute matching tolerance. Coverage is
  incomplete and direction is mixed, so retain the 30-cycle minimum hold and
  reject both a longer exit delay and a frequency increase for now.
- Overseas capacity/lifecycle review: LFUS and FG have now closed virtually,
  leaving four strategy-open positions and four broker-settlement exposures,
  or eight of ten overseas and account-wide slots. The four pending virtual
  sells correctly retain capacity until KIS settlement. FG's
  `time_exit_profit` produced +0.958% gross and approximately +$16.89 after
  modeled costs; CPRX remains near flat after about 14.6 days. Natural entries
  may use the two released slots, but the cap and entry thresholds remain
  unchanged because recent three-day virtual performance is still negative
  after costs. Reassess after additional final NASDAQ sessions and completed
  outcomes.
- Overseas signal availability: direct KIS reads on 2026-07-29 returned 50
  daily and 40 intraday rows for recently listed `IPFX`, versus required
  60/21, while `KOYN` returned 100 daily but only 13 intraday rows. These are
  different deficiencies hidden by the old `signal_unavailable` label.
  Preserve the three-failure/180-minute policy, record the exact reason and
  row counts from the next suppression, and exclude these rows from blocked
  entry expectancy until a valid signal exists. The final NASDAQ regime is
  still sideways/normal-activity/normal-volatility; do not transfer this
  US-data observation to the domestic formula.
- Do not extend the aggregate standalone-strategy guard to combination labels
  from the current 48-hour average alone. Under True Range, the eleven
  `VWAP+VOL` exits belong to the same sideways/normal-activity/normal-volatility
  bucket but span only two final sessions; the minimum is three. Re-evaluate
  after the declared regime sample matures instead of converting one crash
  episode into a permanent entry block.
- Frequency: the seven-day confirmed ledger contains eleven domestic and
  thirty-five overseas entries, followed by eleven and twenty-four exits. The
  largest raw WAIT bucket shrinks from 1,251 to 178 domestic episodes and from
  5,143 to 247 overseas episodes after deduplication. This is not evidence of a
  system frequency ceiling. Do not loosen entry gates while after-cost
  expectancy remains negative and regime coverage is this narrow.
- Both markets: inverse trading remains shadow-only. Current evidence justifies
  testing separate down-market formulas, but not risking broker capital. The
  domestic formula opened and closed its first `114800` shadow observation
  during the final KOSPI strong-down/normal-activity/extreme-volatility
  session. It stopped after eight hold cycles at -0.899% gross and -0.928%
  after modeled costs. The benchmark had been observed at -11.72% at 13:22 KST
  and had recovered to -6.48% when the trade opened at 15:02 KST, about 28
  minutes before the regular close; this supports a late-rebound-whipsaw
  hypothesis but is only one path. Preserve entry/exit market-path context
  from the next observation onward and keep the formula, five-exit,
  three-final-session, positive-net gate unchanged. FINRA notes that most
  geared ETP objectives reset daily, can deviate over shorter or longer
  periods, and require close monitoring; that supports path measurement rather
  than threshold fitting from one loss. The final NASDAQ return of -0.22%
  remained above the overseas -1% gate, so no US inverse trade was expected in
  that session. On 2026-07-29 the provisional NASDAQ return crossed -1% and
  exposed a different zero-sample cause: the generic formula rejected a liquid,
  rising SOXS setup. Route future US observations through the separately named
  shadow formula. Same-session full-path replay then showed that the favorable
  later SOXS price was a cherry-picked endpoint: the current cost-aware stop
  would have closed on the immediately adverse next observation. There are
  now two completed US product paths but only one observed US market session.
  SQQQ and SPXU closed at -1.16% and -0.79% net; their MFE values of +0.28%
  and +0.25% never covered the modeled round-trip cost. This confirms the
  routing path and the prior replay warning, not a profitable formula. Durable
  regime and product-stage observations make each zero-sample reason auditable.
- In the same provisional `down|unknown|normal` NASDAQ session, four completed
  ordinary-long entries all lost after costs, averaging -0.85% net. Two more
  open positions were near flat gross but about -0.49% and -0.47% after modeled
  round-trip costs at review time. Reject a frequency increase. Do not install
  a permanent broad-market long block from this one correlated, non-final
  session; retain the existing consecutive-loss breaker and collect at least
  five confirmed exits across three final down-regime sessions first.
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
