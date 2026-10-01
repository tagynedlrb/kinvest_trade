# Domestic Execution and Broker Session Review: 2026-10-01

## Scope and evidence

- User requested domestic fill verification and current KIS trading hours for real
  and paper accounts. Only the enabled VPS paper account was used for orders.
  Real-money trading remains disabled. No strategy entry thresholds were relaxed.
- Baseline deployment: `4f5cec99e0485f054d150c29f7ab34f28a1454c3`.
- Existing policy reviews #140 (US v8) and #141 (KR v11) remain profitability
  experiments. Successful diagnostic orders do not establish profitability.

## Domestic roundtrip: confirmed

The scheduled 09:05 KST check stopped before POST because 069500 exceeded the
100,000 KRW one-share entry limit. At 10:18 KST its ask/bid were 109005/109000,
not malformed quotes. The diagnostic reported the misleading combined reason
`diagnostic_quote_out_of_bounds`. This is an unsuitable diagnostic candidate,
not evidence of expired credentials or a broken order API.

A nonleveraged ETF, 229200, was checked against the unchanged cap and spread
guard, then tested while the normal service was stopped with the existing lock:

| KST 2026-10-01 | Side | Order | Quantity | Confirmed fill (KRW) |
| --- | --- | --- | --- | --- |
| 10:21:18 | Buy | 0000015780 | 1 | 14780 |
| 10:21:35 | Sell | 0000015805 | 1 | 14790 |

Both broker history records were FILLED with remaining quantity zero. Balance
also returned zero; `ROUNDTRIP_CONFIRMED` was recorded at 10:21:45 KST. Service
restart and Telegram delivery were confirmed. The 10 KRW gross difference is
not a cost-adjusted strategy return. Evidence stays in `paper_execution_check_*`
events, excluded from strategy cycle P&L. No second diagnostic is necessary.

Diagnostic fixes distinguish invalid quotes from excessive notional, enforce
the US cap on the actual rounded buy limit, and do not impose entry caps on sell
liquidation. A preflight rejection no longer consumes a one-shot attempt key;
each actual submission still rechecks the quote, session and buying power.
Ambiguous POST outcomes still prohibit retries and automatic service restart.

## Official KIS hours, verified 2026-10-01

All hours below are Korean time, for normal trading days. Holidays, special
opening days and shortened sessions can override them. Order acceptance and
reservations are not evidence that an execution session is open.

| Venue/session | Real account | Paper account |
| --- | --- | --- |
| KRX regular | 09:00-15:30 | 09:00-15:30 |
| KRX pre-close-price / after-close-price | 08:30-08:40 / 15:40-16:00 | Not supported |
| KRX aftermarket, effective Sep 14 | 16:00-20:00, no ETPs; ORD_DVSN 41-47 | Not supported |
| NXT pre / main / after | 08:00-08:50 / 09:00:30-15:20 / 15:40-20:00 | Not supported |
| US daytime, DST / standard | 10:00-17:00 / 10:00-18:00; separate daytime API | Not supported |
| US premarket, DST / standard | 17:00-22:30 / 18:00-23:30 | Not supported |
| US regular, DST / standard | 22:30-next 05:00 / 23:30-next 06:00 | Same regular hours |
| US aftermarket, DST / standard | 05:00-07:00 / 06:00-07:00 | Not supported |
| US extended aftermarket | 07:00-09:00, prior account enrollment required | Not supported |

KRX old after-hours single-price trading was replaced by the new aftermarket.
The current ETF/ETN-focused KR strategy stays on KRX regular hours: simply
extending its clock would target an unsupported instrument/session combination.
NXT strategy routing is not enabled. US 07:00-09:00 is not enabled without
verified account enrollment. This work does not claim to validate real-account
permissions using the paper credentials, or to prove fills in extended sessions.

Sources, primary KIS publications:

1. [Domestic business hours](https://securities.koreainvestment.com/main/customer/guide/_static/TF04ad010000.jsp)
2. [KRX aftermarket and NXT changes, Sep 9 notice](https://apiportal.koreainvestment.com/community/10000000-0000-0011-0000-000000000001/post/26dfe350-eb72-48e5-8175-34eb27970f3e)
3. [Mock TR migration: NXT unsupported](https://apiportal.koreainvestment.com/community/10000000-0000-0011-0000-000000000001/post/dd2e7e20-51e8-45fd-b65a-24a128f6af34)
4. [US API-supported hours FAQ](https://apiportal.koreainvestment.com/community/10000000-0000-0011-0000-000000000002/post/db9b80d2-f2dd-492a-b3f3-02eb3ba9a566)
5. [US market guide](https://securities.koreainvestment.com/main/bond/research/_static/TF03ca050001.jsp)
6. [Paper investment rules](https://vts3.koreainvestment.com/vts/#/rule)

The dynamic portal bodies were also read through the official public
`/api/forums/{forum}/posts/{id}` API. Paper rules were read from the public VTS
application's domestic and overseas rule templates. The overseas table shows
current DST hours; the DST/standard conversion follows the US API FAQ and
America/New_York timezone. Do not use the old 2023 daytime 10:00-16:00 FAQ over
the current published hours. Paper fills require underlying market transactions;
an accepted or marketable order alone is not proof of execution.

## Software corrections

- Separate `is_us_regular_session` from the full `is_us_market_session` watch
  clock. Paper diagnostics now reject daytime/pre/after; strategy monitoring
  retains its prior extended-session behavior.
- Add dated KRX and NXT clock classification and environment-aware supported
  session reporting. Holiday checks remain at callers; classifications are not
  product eligibility or guarantees of order acceptance.
- Reject unsupported mock NXT/time-of-day order divisions before network POST.
  Invalid domestic side strings no longer default to SELL.
- Reject US closed-clock submissions even for production in the session-aware
  route. Preserve separate daytime routing and paper regular-only routing.
- Make the KRX new-order endpoint exclusive at 15:30 while retaining fill
  reconciliation at that boundary. Compute future US openings using that date's
  New York timezone, fixing DST transition weekend lookahead.
- Show account restrictions in Telegram status and correct outdated README claims
  of identical domestic real/paper hours and a 15:30-17:00 universal closure.

## Market and strategy observations

The first natural US v8 MOM trade, FNV, bought 4 at 240.60 and sold 4 at 240.63
on Sep 30 (17:03:38 / 17:56:10 UTC). Holding time: 52.54 minutes. Gross P&L was
USD +0.12; recorded roundtrip cost USD 4.832128; net P&L **USD -4.712128**
(KRW -6361.37 using the ledger's conversion). Confirmed cycle IDs 817441/818978,
broker orders 0000040028/0000040338. This demonstrates automatic entry and exit,
not a profitable policy. The cost-adjusted loss is about -0.4896% of entry value.
The time exit was based on the contemporaneous signal; a slightly better eventual
fill does not make `time_exit_loss` a logging error.

NASDAQ Sep 30 final: 26861.06, +0.23704%, volume/20-day average 1.04385,
`sideways|normal|normal`. This final close context must not be substituted for
the actual intraday benchmark used to authorize the entry.

KOSPI Sep 30 final: 6838.04, -0.47695%, volume ratio 0.98888. Oct 1 at
10:33 KST: 6837.02, -0.01492%, intraday volume ratio 0.41955, `is_final=0`.
Partial-day volume divided by full-day averages is NOT time-of-day-normalized
liquidity evidence. Do not conclude that the completed day was quiet from it.
At the audit cutoff, KR's leading repeated waits were volume_low 263,
VWAP benchmark-below-floor 62, leveraged trend-down 32 and trend_down 21.
These are repeated observations, not independent rejected orders or lost trades.

No broad volume/benchmark relaxation is justified by this evidence. Preserve
KR v11 / US v8 bounded paper probes and the existing evaluation requirements:
at least 5 confirmed closes across 3 final regular sessions; positive mean,
median, trimmed and capital-weighted net results, stratified by regime and hold
duration, before any promotion. Diagnostic fills are excluded. No claim is made
that a different model or higher token spend has been experimentally superior.

## Follow-up and limits

- #141's broker-execution subtask is complete; natural KR strategy profitability
  is still unverified. #140 has one new cost-negative close, not enough evidence
  to promote or declare a profitable holding horizon.
- Keep benchmark observations/final closes, confirmed broker fills, reasons and
  policy reviews linked for subsequent analysis. Do not overwrite failed checks.
- Future real KRX aftermarket support needs non-ETP universe selection, correct
  order types, position reconciliation and separate cost/risk evaluation.
- Account enrollment for US extended aftermarket and special-session overrides
  need independent validation before activating those routes. Neither is inferred
  from these normal-session paper tests.

## Verification and preservation

- Focused sessions/diagnostics: 97 passed; client/strategy/report suite: 581 passed.
- Full suite: **1050 passed in 135.62 seconds**. Changed files passed Ruff F/E9
  and `git diff --check`.
- Online SQLite backup: `data/trading_backup_20261001_013815_pre_session_hours.db`,
  647405568 bytes, mode 600, quick_check=ok, foreign-key violations=0.
  SHA-256: `c5d61e4a1acf20fae406bc6adf89de98a4e2dc9547a1cc9cab00e6f141362b65`.
- Review #142 links this audit and the retained pending reviews #140/#141.
  Their partial follow-up evidence is in `outcome_json`, with `reviewed_at` left
  NULL so profitability validation is not silently removed from future reviews.
- Diagnostic completion was followed by 559 successful terminal API requests,
  zero failed terminal requests and no new error/rejection event types at the
  predeployment checkpoint. This is an observation window, not a future guarantee.
