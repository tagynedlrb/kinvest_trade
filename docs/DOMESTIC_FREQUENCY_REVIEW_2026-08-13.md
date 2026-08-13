# Domestic Frequency and Session-Stop Review - 2026-08-13

## Decision

- Keep the domestic one-fire local-session loss budget, KOSPI zero floor, and
  exact strategy guards. Do not increase frequency from today's late-session
  inactivity because the blocked paths did not show positive after-cost
  expectancy at the intended 30-60 minute holding horizons.
- Fix observability. A domestic cycle with no selected order must retain the
  binding market-level or dominant policy WAIT reason instead of collapsing to
  `no_action`. The cooldown-release notice must explicitly distinguish the
  expired 30-minute timer from the still-active local-session entry stop.
- Keep the domestic and overseas formulas independent. No overseas parameter
  or behavior changes in this checkpoint.

## What Actually Happened

- The KRX path was not inactive for the whole session. It submitted 12 buys;
  10 filled and two stale limit orders were canceled without a fill. All 10
  filled positions were later closed, with five net winners and total net PnL
  of `+8,050.97 KRW`.
- KOSPI closed at 6,813.34, `+3.5613%`, with 20-day volume ratio `1.1716` and
  final regime `strong_up|normal|normal`.
- After seven confirmed exits, the account was at `-4,458.52 KRW`. The last
  three outcomes were losses, so the domestic consecutive-loss breaker fired
  at 10:42 KST. Its 30-minute timer released at 11:12 KST, but the independently
  configured `post_cb_max_fires_per_session=1` correctly kept ordinary-long
  entries stopped for the remainder of the KRX session. Existing exits
  continued and added `+12,509.49 KRW`, producing the final positive result.
- The user-facing release notice said only that entry conditions would be
  rechecked. The later low-frequency notice then reported
  `skip:no_action 50회`. Both messages hid the binding same-session loss budget
  and reasonably made a deliberate risk stop look like a broken trader.

## Performance Evidence

- Over the latest 14-day confirmed account boundary, domestic performance is
  still negative: 118 exits, 30% net win rate, average net `-0.156%`, and
  cumulative `-168,830 KRW`. One positive day is not enough to expand risk.
- Today's post-CB optimistic counterfactuals, after only the minimum domestic
  round-trip cost floor, were negative at 60 minutes for the main blocked
  formulas: VWAP `-0.090%`, VWAP+VOL `-0.348%`, VOL `-0.413%`, and RSI
  `-0.421%`. Their 30-minute estimates were also negative. These estimates do
  not include spread or slippage, so they favor reopening rather than the stop.
- A generic `trend_down` WAIT bucket had positive 60-minute forward movement
  today, but it is one final session and is not equivalent to an executable
  strategy entry. It remains evidence to monitor, not a reason to remove the
  trend filter.
- Three confirmed standalone VOL entries with RSI above 90 across two sessions
  total `+51,906.82 KRW`. The generic momentum RSI ceiling and the independent
  VOL formula therefore remain deliberately separate for now. Add a dedicated
  strategy threshold only after enough multi-session evidence; do not infer
  that every high-RSI entry is safe from this small selected sample.

## Reliability Review

- There were zero broker rejections and zero unfinalized executions in the KRX
  session. The prior duplicate-sell guard suppressed two transient stale
  balances and both preceding sells later reconciled as full fills.
- Of 13,679 logical API requests during the KRX session, one terminal failure
  came from a Telegram open-order audit after three approximately ten-second
  transport timeouts. The fallback paper TR succeeded seven seconds later and
  the live trading cycle continued. This was not the cause of no trading.
- Two domestic strategy guards were released after their observation holds:
  `VWAP+VOL+RSI` before the close and `VWAP+RSI` after the final regime refresh.
  The next KRX session is not covered by today's post-CB stop. Only the
  independently negative `VOL+RSI` guard remains active domestically.

## Changes and Validation

- Domestic no-order results now preserve active CB, order-reject CB, post-CB
  session stop, dominant policy WAIT, ready-signal absence, or candidate
  absence as distinct reasons. Post-CB detail is attached to the order result.
- Low-frequency Telegram reasons are translated but the raw reason keys remain
  in the structured event ledger. Strategy labels are retained.
- A one-fire cooldown release now records
  `session_entry_stop_active=true` and tells the user that ordinary new buys
  remain stopped until the next local market session while exits and inverse
  monitoring continue.
- Targeted risk, notification, and liquidity-lab regression coverage passed
  316 tests. The full suite passed 835 tests in 208.13 seconds; deployment
  evidence is appended after rollout.

## Falsification and Follow-up

- Reconsider the one-fire domestic session stop only if at least three later
  final KRX intervention sessions show that blocked after-cost opportunity
  consistently exceeds avoided loss without another post-release loss cluster.
- Revert the diagnostic change if it attributes a market-level block when a
  broker order was actually attempted, hides a real rejection, affects an
  exit/inverse path, or carries today's stop into the next KRX session.
- Automated pre-trade exposure controls should be documented and monitored for
  continuing appropriateness rather than silently bypassed after a timer; see
  the SEC Market Access Rule FAQ:
  https://www.sec.gov/rules-regulations/staff-guidance/trading-markets-frequently-asked-questions/divisionsmarketregfaq-0
  FINRA also notes that more frequent activity can impair performance through
  costs even under an aggressive strategy:
  https://www.finra.org/investors/insights/3-ways-guard-against-excessive-trading-your-brokerage-account
