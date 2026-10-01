# Domestic Candidates and Trade Notifications: 2026-10-01

## Scope and the 100,000 KRW cap

Baseline: `51bc72bf5cd757bff80e93b1ec53fe2532ef7bfc`, KR v11 / US v8.
Only the enabled VPS paper account is in scope; real-money trading stays disabled.

The 100,000 KRW limit is a one-share **diagnostic entry notional cap** in
`paper_execution_check.py`, not the account balance limit, a stock-price ceiling
for the strategy, or a general automatic-order budget. The previous 069500
diagnostic ask was 109005 KRW, so that check stopped before submitting an order.
It was subsequently replaced with 229200: one share bought at 14780 and sold at
14790 KRW, both confirmed, position zero. This did not establish profitability.
Normal entries use the market policy's buying-power/slot/probe budgets. This
change neither raises the diagnostic cap nor changes normal entry sizing.

## What actually traded and what was notified

At the 14:45 KST audit cutoff, Oct 1 KR natural strategy submissions and fills
were **zero**. The 10:21 KST diagnostic roundtrip is recorded separately and must
not be represented as a naturally selected strategy trade.

The latest US natural roundtrip was FNV, 4 shares, Sep 30 US session:

| Event | Oct 1 KST | Evidence |
| --- | --- | --- |
| Broker buy fill | 02:03:38 | 0000040028, USD 240.60 |
| Buy acceptance Telegram | 02:04:42 | telegram message log #3803, success |
| Buy fill Telegram | 02:08:25 | log #3804, success |
| Broker sell fill | 02:56:10 | 0000040338, USD 240.63 |
| Sell acceptance/fill Telegram | 02:57:26 | log #3806, success |

Broker-confirmed strategy cycle IDs are 817441/818978. Hold: 52.54 minutes.
Gross USD +0.12, recorded roundtrip cost USD 4.832128, net USD **-4.712128**.
The observed notifications were not lost, but the buy fill report arrived 4m47s
after the broker timestamp. Telegram server success is not a handset delivery or
read receipt. The audit found 31 successful outbound records on Sep 30 UTC and
21 on Oct 1 UTC, zero failures in that observation window.

## No-trade causes

1. Repeated KR wait observations at the cutoff included `volume_low` 750,
   VWAP confirmation-volume-low 116, leveraged trend-down 93, VWAP benchmark
   below-floor 91, and trend-down 39. These are repeated observations, not
   independent rejected orders or missed profitable trades.
2. Two BUY observations, 036540 at 12:46 KST and 005930 at 13:57 KST, were
   taxable ordinary stocks. The active bounded probe allowed only tax-exempt
   ETF/ETN products. The watch-stage guard omitted product type, while the final
   selector supplied it and excluded the stocks. Thus a recorded BUY did not
   imply an eligible order. Product type now reaches both watch-stage guards.
3. Volume discovery fetched only 20 rows; structured-product exclusions left
   roughly 13-14 symbols including ordinary stocks that could not be bought.
   Pool refreshes at 10:42, 11:23, 12:17, 13:12 and 14:07 KST show that a
   20-cycle refresh could take about 55 minutes.
4. The fluctuation rank URL/TR were wrong and the source was unconditionally
   disabled on paper accounts. No fluctuation request failed today because none
   was attempted by that deployment. The official corrected endpoint was
   successfully read on the current VPS account (`rt_cd=0`).
5. Rank percentage parsing converted `5.33` to `533`. This metadata bug is fixed
   locally with the existing decimal parser; it is not presented as proof of a
   direct order rejection. Currency parsing is unchanged.

KOSPI was not uniformly falling throughout the day: at 14:41 KST it was
6946.08, +1.5799849%, `is_final=0`, volume/20-day average 0.7967805. Earlier
morning benchmark waits cannot explain every afternoon non-entry. Sep 30 final
KOSPI was -0.476945%; NASDAQ final was +0.2370367%, volume ratio 1.0438523.
Intraday cumulative volume divided by full-day averages is not time-normalized
liquidity evidence. Preserve intraday and final observations separately.

## KR v12: repair coverage without forcing trades

- Fetch 30 volume-ranked rows plus 10 fluctuation rows, deduplicate and bound
  discovery to 30. Keep 069500, 102110 and 229200 within that bound as core
  domestic benchmark ETF candidates. Held and shadow positions retain monitoring.
- Refresh at the first scan after 600 seconds, or the cycle threshold, whichever
  comes first. Long-running scans can exceed ten minutes; this is not a hard
  real-time guarantee. A known empty result must not revive the static stock pool.
- Apply the active tax-exempt product restriction before expensive chart work;
  do not consume refinement capacity with stocks the final policy cannot buy.
- Retain successful volume results if fluctuation fails; record source degradation.
  Continue excluding unapproved leverage/inverse/foreign-underlying products.
  Newly encountered covered-call ETFs require a separate policy and are excluded.
- Record `domestic_candidate_funnel`: discovery, quote failures, eligible codes
  and each exclusion reason. Explicitly state that signal/risk checks remain.

Read-only broker validation, 15:05-15:08 KST: 30 volume rows + 10 fluctuation
rows, 11 structured exclusions, **29 discovered / 9 quote-eligible / 0 quote
failures**. Eligible codes: 069500, 102110, 229200, 122630, 0167A0, 232080,
396500, 455850, 475300. The last three were outside the old top-20 volume
sample. This demonstrates incremental candidate coverage, not nine BUY signals.
The event `domestic_candidate_audit_v12` retains the complete evidence.
The temporary audit script's output formatter used `name` instead of
`stock_name` after scanning; the persisted complete discovery/funnel records
were recovered and copied into the main audit log. No order was placed by it.

Entry formula, benchmark/volume confirmation, cost safeguards, daily loss limits,
probe sizing, maximum 2 entries/4 submissions per KR session and the 60-minute
entry-close buffer are unchanged. The new policy was prepared after 14:30 KST:
do not bypass the cutoff to manufacture a late-session strategy fill. Natural
post-change entry and profitability remain pending. US policy remains v8.

## Notification reliability

Previously `TradeNotifier` cleared its queue in `finally`, including failed sends.
Keep batches on exceptions or explicit failure; remove only acknowledged lines,
including when new lines arrive while awaiting the network. Catch/report errors
without aborting trading. Check Telegram JSON `ok=true`, not only HTTP status.
Flush reconciled fills before the long candidate scan and force the summary flush.

This fixes a latent loss path and avoidable batching delay; it does not establish
that past successful FNV notifications were missing. The retry queue remains
in-memory, so process crashes can still lose pending lines. An ambiguous network
timeout can cause duplicate retries. Do not claim durable exactly-once delivery.
Existing broker and Telegram ledgers remain the recovery/audit sources.

## Evaluation and preservation

Keep reviews #140 (US) and #141 (KR v11) and their original evidence. Link the
v12 follow-up with `reviewed_at=NULL`: at least five confirmed closes across
three final regular sessions, then positive mean, median, trimmed and
capital-weighted net metrics by benchmark/volume/volatility/holding-time cohort.
No promotion to real trading; no claim of profitability from diagnostic fills,
candidate counts, one US close, or extra reasoning spend. No controlled model
comparison was performed; comparative value remains unverified.

Reject blanket volume/trend guard relaxation: the retained Sep 30 replay did
not support it, and the FNV gross gain still failed to cover recorded costs.
Review quote-to-signal conversion, stale/slow scans, errors, ordinary-stock
exclusions and confirmed fill count under v12 before attributing low activity
to overly strict entry formulas alone.

Predeployment online backup: `data/trading_backup_20261001_060814_pre_candidate_v12.db`,
647405568 bytes, mode 600, quick_check=ok, foreign-key violations=0, SHA-256
`b0312988e41e3af5f30086aaba7e1c23dc08301b371f338612b06457398da88a`.
Final full regression: **1063 passed in 140.11 seconds**; changed-file Ruff F/E9
and `git diff --check` passed. Existing non-clock strategy tests were isolated
from the wall-clock entry cutoff; dedicated close-buffer tests remain unchanged.

## Postdeployment observation

Code commit `3982676e675c` was pushed and the paper service restarted at 15:12:35
KST. Runtime confirmed KR v12 / US v8, clean code tree and running mode. Its
first complete decision cycle ran 15:12:53-15:21:18 (8m25s), reproducing 29
discovered / 9 quote-eligible / 0 quote failures. KR's selected wait reason was
`volume_low`; US was `us_open_but_mock_session_not_supported`. The entry-close
buffer also remained enabled; no new strategy fill is claimed.

At 15:21:25 KST the API ledger showed 172 terminal successes and one terminal
domestic minute-chart timeout, logical request `a24d0fa720de4386b1d922e00d884ee4`.
The chart request exhausted three read attempts. One EGW00201 response, a
domestic order-history timeout and a US quote timeout recovered on retry.
Therefore this is NOT an error-free API observation window. Initial health
snapshots preceded these later failures. The service completed without a fatal
runtime error; it must not turn missing/stale chart data into an authorized BUY.
The unavailable/stale-signal regression subset passed 4 tests. Candidate quote
eligibility and successful chart/signal acquisition remain separate stages.

The first scan included both markets' cold caches and slow broker responses.
Record its 8m25s latency as a remaining bottleneck; do not claim faster full
cycles or instant notifications from this deployment. More aggressive retries
are not justified by a broker rate-limit observation. Follow-up should measure
warm-cycle latency and avoid non-orderable-market research delaying orderable
market decisions, while preserving held-position exits and signal freshness.
The short deployment probe timed out before the service cycle completed; an
extended probe confirmed completion. Neither probe placed orders.

Review #143 and `candidate_notification_deployment_verified` retain this
postdeployment evidence. Reviews #140/#141/#143 still have `reviewed_at=NULL`;
execution connectivity is not a substitute for the required profit sample.

## Primary references

- [KIS official fluctuation API example](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/fluctuation/fluctuation.py):
  `/uapi/domestic-stock/v1/ranking/fluctuation`, TR `FHPST01700000`, screen 20170.
- [KIS official volume rank example](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/volume_rank/volume_rank.py)
- [Telegram Bot API response contract](https://core.telegram.org/bots/api#making-requests)
- Prior execution evidence and broker sessions:
  `docs/DOMESTIC_EXECUTION_AND_SESSION_REVIEW_2026-10-01.md`.
