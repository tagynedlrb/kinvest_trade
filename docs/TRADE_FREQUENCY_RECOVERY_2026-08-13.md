# Bounded Trade Frequency Recovery - 2026-08-13

## Decision

- Keep the domestic entry formulas, one-fire session breaker, and position
  sizing unchanged. The August 13 KRX session already produced 10 filled
  entries and 10 exits; its late inactivity followed a deliberate loss stop,
  not a lack of candidates for the full day.
- Expand only the overseas KIS paper probe for exact `VWAP+RSI`. Allow up to
  three effective entries per New York session at 10% of an ordinary slot
  each, with at most six accepted submissions. A pending or filled order uses
  an effective-entry slot; a finalized zero-fill order releases that slot but
  continues to consume the separate submission-attempt budget.
- Do not release the ordinary strategy guard or add another formula. Nasdaq
  must remain non-negative with a same-session observation no older than 600
  seconds, and every existing signal, liquidity, spread, position, circuit
  breaker, and loss control remains in force. Production remains ineligible.

## Why Activity Was Still Near Zero

- The August 13 US session generated one eligible paper probe. `TBBB` 17
  shares at 47.755 USD was accepted at 14:26 UTC, filled zero shares, and was
  canceled by the stale-order audit at 15:00 UTC. There was no rejection.
- The old policy counted accepted submissions as entries. Therefore this
  zero-fill cancellation exhausted the one-per-session quota and closed the
  only guarded-strategy route for the rest of the session.
- At review time, persistent usage should have been attempts 1, effective
  entries 0, fills 0, open entries 0, and finalized no-fill entries 1. The old
  single counter could not represent that state.

## Performance Boundary

- `VWAP+RSI` remains the least unfavorable confirmed US composite path, not a
  proven profitable strategy. Since July 27 it has 33 confirmed exits, 10 net
  winners, average modeled net return `-0.313%`, and total `-703.97 USD`.
- During the provisional August 13 Nasdaq `up|unknown|calm` session, blocked
  five-minute `VWAP+RSI` episodes had optimistic minimum-cost means of
  `-0.850%`, `-1.299%`, and `-1.206%` at 15, 30, and 60 minutes. This rejects
  a broad guard release or normal-size frequency expansion.
- Increasing from one to three 10% entries caps the aggregate probe allocation
  at 30% of one ordinary slot before independent portfolio limits. Six
  submissions permit bounded replacement after no-fill outcomes without
  turning broker acceptance or execution luck into an unlimited retry loop.

## Durable Accounting

- `get_strategy_guard_probe_usage` derives attempts from durable probe events
  and effective exposure from broker execution groups. Filled groups, pending
  groups, and virtual entries count; finalized real groups with zero fill do
  not count as exposure.
- Probe admission records submission attempts, effective entries, fills, open
  entries, virtual entries, no-fill finalizations, and both limits. `/lab_guard`
  reports the same dimensions instead of the ambiguous former `session` count.
- The original `count_strategy_guard_probe_submissions` API remains available
  for historical consumers, while admission prefers the new usage ledger and
  fails closed to the old count if the richer reader is unavailable.

## Validation and Reversal

- Keep the expanded paper path only while no session exceeds three effective
  entries or six submissions, a zero-fill cancellation releases exposure, and
  production stays blocked.
- Review after five completed probes across at least three final Nasdaq
  sessions. Disable the expansion if average net return is at or below
  `-0.50%`, there is no net winner, terminal order failures rise, or the larger
  sample confirms that additional probes merely reproduce guarded losses.
- Do not expand size or add `VWAP+VOL` from order count alone. Require positive
  after-cost forward evidence and completed-probe evidence before either move.
- The SEC Market Access guidance supports documented preset exposure limits
  and ongoing review of their appropriateness. FINRA's excessive-trading
  guidance is the reason submission attempts remain capped separately from
  exposure even though more observations are now permitted:
  https://www.sec.gov/rules-regulations/staff-guidance/trading-markets-frequently-asked-questions/divisionsmarketregfaq-0
  https://www.finra.org/investors/insights/3-ways-guard-against-excessive-trading-your-brokerage-account

## Verification

- Focused configuration, repository, admission, Telegram, and reason-format
  tests cover independent market settings, pending exposure, zero-fill slot
  release, and the split usage display. Fifteen focused tests and the full
  **836-test** suite passed in 212.75 seconds.
- Commit `9a59696` was pushed and deployed at `2026-08-13T18:04:31Z` as PID
  `1718366`. Four orderable US natural cycles completed with 252/252 API
  success, no terminal failure or rejection, and no new broker order. The
  live admission check returned attempts 1/6, effective entries 0/3, and one
  finalized no-fill entry, proving that the prior TBBB cancellation no longer
  consumes exposure. Current `VWAP+RSI` candidates remained below the volume
  floor, so absence of a forced order is expected signal behavior.
- Telegram deployment report `id=2322` succeeded. Evaluation `id=105` remains
  open for five completed probes across at least three final Nasdaq sessions.

## 2026-08-18 Forward Correction

- Three `VWAP+RSI` probes completed across the August 13-14 final sessions:
  PLGO `-0.568%`, BLSH `-0.652%`, and TSSI `+2.165%` modeled net, for one
  winner and `+0.315%` mean. The five-probe/three-session validation minimum
  is not met, so evaluation 105 remains open and size stays at 10%.
- Full KIS pagination later restored hidden `VOL+RSI` and `VWAP+VOL` fills.
  This does not invalidate the probe accounting, but it invalidates the old
  conclusion that ordinary US activity was absent. The system was submitting
  and filling orders while the local execution ledger was truncated.
- The probe list now includes every guarded overseas composite formula, still
  sharing the same three-entry/six-submission session budget. This preserves
  bounded learning after `VOL+RSI` was re-guarded; it does not increase the
  existing aggregate probe exposure.
