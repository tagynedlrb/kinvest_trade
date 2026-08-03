# Session Log Reconciliation (2026-08-03)

This file is the durable handoff produced after comparing the project history,
production database, service logs, and the locally retained GPT/Codex and
Claude transcripts. It records material instructions and findings that were
missing from either `WORKLOG.md` or `policy_evaluation_log`; it is not a copy of
private credentials or full conversational transcripts.

## Sources Audited

- GPT/Codex session:
  `/home/ubuntu/.codex/sessions/2026/07/27/rollout-2026-07-27T11-06-25-019fa341-2b85-7321-881c-cdf655dde127.jsonl`
- Claude sessions:
  `/home/ubuntu/.claude/projects/-home-ubuntu/a3f1d604-af46-4d92-b81b-4f150efce4b9.jsonl`
  and
  `/home/ubuntu/.claude/projects/-home-ubuntu/8cfc88ef-166e-4c69-a4cb-88a8c6c81332.jsonl`
- Git history through `d0067e4`, `POLICY_EVALUATION.md`, `WORKLOG.md`,
  `data/trading.db`, and `kinvest-telegram-control.service` journal/state.
- No token, account number, authorization header, or Telegram credential was
  copied into this record.

## Standing User Instructions

1. Domestic and US trading formulas are independently owned. They started from
   a cloned baseline, but evidence from one market must not silently change the
   other market.
2. Every performance review must combine confirmed trade outcomes with the
   applicable final KOSPI or Nasdaq Composite session, including direction,
   range/volatility, and activity/volume quality. Provisional sessions stay
   visible but cannot drive final policy evaluation.
3. The system must preserve this history for later reviews rather than judge
   changing market environments against one context-free score.
4. Periodic review may use external financial knowledge, but every change needs
   measured evidence, alternatives, and a falsification or rollback condition.
   High-cost reasoning has no presumed advantage over a smaller review; its
   comparative value remains unverified without a real baseline comparison.
5. The long-running goal ends when the user's corrected 10,000-credit
   allocation resets or is consumed. A service-changing checkpoint still ends
   with tests, a database backup, Git push using `git_token.txt`, restart,
   health verification, and Telegram reporting.
6. Falling-market profit attempts use separately evaluated inverse products.
   Domestic and US inverse formulas require their product-appropriate
   benchmarks and remain shadow-only until after-cost evidence spans at least
   three final sessions. A broad-index crash does not by itself authorize a
   live inverse order.
7. Trade frequency can be increased only when after-cost expectancy and system
   capacity support it. Low frequency alone is not evidence of a defect.

## Material Gaps Recovered

### 2026-08-01 decision missing from the structured ledger

- `POLICY_EVALUATION.md` documented the change, but production
  `policy_evaluation_log` stopped at id 88 on 2026-07-29.
- US `VOL+RSI`: 12 confirmed session-owned exits over four sessions, one win,
  net `-$1,265.24`. Excluding RBLX still left about `-$434.09`.
- A blanket first-15-minute US entry ban was rejected because five recent
  opening entries included three winners.
- RBLX's protective stop was submitted in 2m36s, but the generic 480-second
  suppression hid it. Replacement was delayed about 11 minutes and the loss
  expanded by roughly 7.5 percentage points. Protective US exits therefore use
  the dedicated 45-second replacement window.
- Domestic execution 292/order `0000038515` remained unresolved. The close
  guard and terminal canonicalization were deployed in `af2ad02`; tests were
  813 passed. Deployment evidence was recorded by `d0067e4`, including backup,
  PID 1540887, and successful Telegram row 1704.
- This decision must be inserted into `policy_evaluation_log` during the next
  backed-up production mutation instead of being inferred only from Markdown.

### Daily market/trade review was recomputable but not durable

- `market_regimes` already retained final KOSPI/Nasdaq OHLC, close return,
  volume, 20-day volume ratio, range, and regime; append-only observations
  retained intraday market character.
- BUY execution context already retained the entry-time market observation and
  local session. Existing analysis could join confirmed exits to the final
  session regime and distinguish provisional finalization gaps.
- There was no durable daily review row combining those facts with confirmed
  entries, exits, wins, after-cost PnL, strategy/exit breakdown, context
  coverage, and local-session match quality. `market_session_reviews` closes
  that gap and is refreshed from final regimes at collection and startup.

### Prior critique not present in project records

- The Claude review found strong deployment and audit discipline but a repeated
  mismatch between infrastructure progress and economic performance. From an
  earlier comparison window, US confirmed exits grew from 20 to 52 while net
  performance worsened from approximately `-325,904 KRW` to `-864,172 KRW`.
  This is historical review evidence, not a newly recomputed current total.
- `src/kinvest_trade/liquidity_lab.py` is currently 10,532 lines, above both a
  reviewed 5,107-line refactored point and the cited 8,197-line pre-refactor
  size. New self-contained features should therefore live in helper modules;
  this iteration places daily-review calculation in `market_review.py`.
- Future work should prioritize after-cost expectancy and smaller ownership
  boundaries. More instrumentation is justified only when it resolves a named
  decision or safety gap.

## 2026-08-03 Evidence and Decisions

### Domestic

- Final KOSPI: 6,257.45, `-5.1247%`, 20-day volume ratio `0.6347`, range
  `5.64%`, regime `strong_down|quiet|calm`.
- The final market-local review contains 20 confirmed entries and 22 confirmed
  exits, one after-cost winner, and `-189,636.80 KRW`. Two exits were carry
  positions closed immediately after the open; the conditional policy evidence
  below starts after the first circuit-breaker event.
- The circuit breaker fired five times. After its first fire, 19 same-day long
  entries/exits produced one after-cost winner and about `-65,603.24 KRW` net.
  Every new domestic entry occurred with KOSPI around `-3.45%` to `-5.39%`.
- Add a domestic-only post-circuit-breaker KOSPI recovery floor of `-3.0%` and
  at most two fires per local session. The rule does not impose a blanket
  strong-down ban, does not affect approved inverse shadows, and does not alter
  US parameters.

### United States

- The 2026-08-03 Nasdaq session was still provisional during the review; KIS
  observed approximately `+2%` with no usable volume, so activity remained
  `unknown`. Final policy attribution waits for the final row.
- Four confirmed US exits at the checkpoint were all after-cost losses, about
  `-280,844 KRW` combined. The existing US two-fire circuit-breaker cap stopped
  further long entries.
- `VWAP+VOL` had been released after final-session aging while its 48-hour loss
  samples had already expired, then resumed losing. Increase only the US
  strategy-guard lookback from 48 to 168 hours. Domestic remains at 48 hours.

### Session-boundary correction found by the new ledger

- The prior 2026-08-01 Markdown checkpoint called 12 exits and
  `-1,738,865.34 KRW` the July 31 US session. That was a KST-calendar aggregate
  spanning the tail of the July 30 New York session and the start of July 31.
- Under the required New York-local boundary, the final July 31 Nasdaq session
  contains two confirmed exits, both losses, totaling `-1,459,481.84 KRW`.
  The 12-exit aggregate remains valid only as a KST operational-date statistic
  and must not be attributed to one final Nasdaq regime.
- This correction confirms the daily ledger has material value over the prior
  ad hoc date grouping. Regime policy uses market-local dates from this point.
- Entry-context quality for that session is `1/2`. RBLX explicitly recorded
  `same_session_regime_missing`; CVNA retained the matching July 31 observation.
  This is a genuine rollout-period data gap, not a backfill parsing failure.

### Rejected changes

- Do not increase long-entry frequency. Current 14-day confirmed account exits
  were 95 domestic at 21% wins and `-276,195 KRW`, and 69 US at 26% wins and
  `-2,844,012 KRW`. The frequency constraint is currently protecting capital,
  not suppressing verified positive expectancy.
- Do not promote inverse trading. Six closed inverse shadow paths were all
  negative; the 2026-08-03 domestic `114800` shadow lost about `-0.994%` net.
- Do not change API pacing. Since the 2026-08-01 deployment, 158 failed API
  attempts all recovered and tracked terminal failures remained zero.
- Do not add a global `VWAP+RSI` guard from this checkpoint. Its historical
  result varies by final regime and the current US session was not final.

## Required Forward Checks

- Domestic re-entry gate: compare blocked paths at 15/30/60 minutes. Narrow or
  revert after at least three later final domestic sessions if blocked positive
  opportunity exceeds avoided losses without reducing repeated post-CB loss.
- US 168-hour guard: retain blocked-path counterfactuals and require real
  after-cost recovery before release. Revert if positive blocked opportunity
  dominates avoided losses across at least three final US sessions.
- Daily review quality: target 100% entry-context coverage and 100% local-date
  match for new confirmed entries. Investigate any lower value; refresh the row
  after delayed execution reconciliation.
- Execution 292: two reads 16 seconds apart found zero domestic balance, zero
  current open orders, no `0162Z0`, and historical filled quantity zero. The
  old KIS VPS history still showed stale remaining quantity 73 and no broker
  cancellation flag. After an integrity-checked backup, finalize it as an
  audited session-expiry no-fill, preserving that discrepancy and never using
  a generic missing-history timeout as proof.
- Architecture: keep new bounded features out of `liquidity_lab.py` and track
  whether its line count and responsibility count fall rather than grow.

## Deployment Evidence

- Pre-change backup:
  `data/trading_backup_20260803_172408_pre_market_review_policy_deploy.db`,
  SHA-256
  `8c303c5c11ea17919a3264b9b2f6b8419a9ad105ea0f118fb142aa711b9168fd`,
  integrity `ok`, foreign-key violations 0.
- Implementation commit `3699b22` was pushed to remote `master` before the
  service restart. Full tests: 815 passed; `compileall` and `git diff --check`
  passed.
- Restart: `2026-08-03T17:28:57Z`, PID 1578133, `active/running`,
  `NRestarts=0`. Startup retained 157 unique daily review rows.
- The first natural strategy-guard audit loaded 48 hours for domestic and 168
  hours for overseas; US `VOL+RSI` and `VWAP+VOL` were blocked. The initial
  post-restart API window had 46 attempts, one recovered failed attempt, and
  zero terminal failures.
- Telegram control start row 1818 and explicit deployment report row 1820 both
  succeeded. Structured policy evaluations are ids 89 through 92.

## 2026-08-04 Follow-up

- A second timeline audit rejected the initial suspicion that ITGR entered
  after the second US circuit-breaker. US fires occurred at 13:50:19Z and
  15:21:10Z; no BUY followed the second fire. The existing two-fire gate worked
  exactly as configured.
- The first release still admitted two FERG and two ITGR entries. Three closed
  at `-276,410.64 KRW` after costs and the remaining ITGR position was below
  the round-trip cost floor. Domestic independently produced 19 post-first-fire
  exits, one winner, and `-65,603.24 KRW`.
- Each market now owns a one-fire local-session loss budget for ordinary longs.
  Existing exits, inverse shadows, pre-fire entries, and the next market-local
  session remain available. Structured evaluations 93 and 94 retain separate
  evidence and three-final-session rollback tests.
- Eleven overdue evaluation rows were resolved with explicit evidence. Rows
  lacking a natural cross-risk-day fill, natural multi-page balance, or mature
  inverse sample remain labeled `inconclusive`; the old 48-hour US guard window
  is labeled `superseded`, not silently confirmed.
- Commit `f6baed2` passed 816 tests and was pushed before restarting service PID
  1580482. Runtime audit confirmed domestic next-session reset and active US
  `post_cb_session_loss_limit_reached` from event ids 6971/7033. Telegram report
  row 1823 succeeded.
