# KR v12 Natural Execution Acceptance: 2026-10-02

## Why the previous result was still pending

KR v12 was first deployed on Oct 1 after the 14:30 KST new-entry cutoff. The
verified 229200 diagnostic roundtrip occurred earlier and deliberately bypassed
strategy selection. It proves account/order connectivity, not natural v12 entry.
Calling both natural execution and profitability merely "pending" omitted a
concrete next-session acceptance task. This change provides that task while
keeping execution acceptance separate from multi-session profitability evidence.

At this follow-up the market was already closed (Oct 1 18:32 KST). A fresh
paper-account balance and 229200 quote read succeeded; domestic positions were
zero. No after-hours mock order or forced strategy signal was submitted.

## Immediate repair and policy-path checks

- While KRX is open and the US profile cannot order, always use the existing
  monitored US scope. Previously a cold start or elapsed five-minute interval
  selected the full US universe before domestic decisions. This contributed to
  the observed 8m25s initial cycle. Held, pending and open-shadow monitoring stays
  active; US orderable sessions retain full scanning. Research resumes after KRX
  closes. Do not claim an observed warm-cycle speedup before the next KR session.
- Natural order events and execution contexts now carry the market policy ID,
  effective auto-trade parameter hash, account fingerprint and environment.
  These hashes contain no APP key, APP secret or account number in clear text.
- The existing domestic probe integration test now obtains BUY from the real
  RSI/MACD strategy and passes it through actual candidate selection and the
  order helper. It uses synthetic market state and a fixed entry-time gate with
  stubbed broker I/O; the BUY result itself is not manually injected.
  The prior synthetic fixture lacked MACD confirmation and correctly produced
  WAIT when this gap was exposed. Adding a qualifying synthetic MACD observation
  fixes the test fixture, not the production trading thresholds.
- The acceptance checker also replays each recorded order snapshot through the
  strategy engine, checks mandatory confirmation formulas, product restriction,
  recorded bounded-probe admission, benchmark session/freshness and entry cutoff.
  It verifies recorded evidence; it does not independently reconstruct every
  historical portfolio/risk state or establish expected profitability.

The numerical KR v12 policy, capital limits, two-entry/four-submission daily
limits, benchmark/volume gates and loss protections are unchanged. US remains v8.

## Installed schedule specification

Session: **2026-10-02**, checked against the existing XKRX calendar. The checker
also checks holidays when it runs. All user-facing times below are KST.

| Time | Action |
| --- | --- |
| 08:55 | Preopen deployment/account/policy/runtime check and report |
| 09:00 onward | Inspect natural strategy decisions and broker-ledger evidence each minute |
| 09:30 | Morning checkpoint, including why no order/fill exists if applicable |
| 12:00 | Midday checkpoint |
| 14:30 | Existing new-entry cutoff; report entry outcome and continue exit/reconciliation tracking |
| 15:40 | Final session acceptance report, including remaining unfilled/unverified orders |

The dated timer has no recurrence after Oct 2. It allows final-report delivery
retries until 15:59; after a final report succeeds, later invocations return the
saved result without restarting verification. This does not recreate any goal
or schedule indefinite model/token consumption. An offline host can miss the
session; a persistent catch-up reports missing evidence rather than inventing
execution. The checker never restarts a manually paused trading service.

Files: `systemd/kinvest-domestic-acceptance-20261002.{service,timer}`.
The normal `kinvest-telegram-control.service` remains the sole order submitter.
`execution_acceptance` has no broker client or order-submission path. It cannot
duplicate a buy to obtain a passing result. Timer activation and notification
evidence are retained in the event log and policy review #143 after deployment.

## Acceptance and failure criteria

A natural BUY must match the armed policy hash, account and VPS environment.
It must have matching broker order/date/symbol/side, positive raw history fill
quantity and price, and a linked nonvirtual BUY_REAL cycle with the same execution
group and sufficient executed quantity. Order acceptance alone is not a fill.
Old-account, diagnostic, virtual and unmatched-context records cannot pass.

Recorded strategy signals must reproduce BUY with the same strategy flag.
An actually filled order with invalid policy evidence is explicitly a policy
validation failure, not a successful policy test. Partial execution is reported
with remaining quantity. A full roundtrip additionally requires matching entry
timestamps and quantities on confirmed sells; unrelated preexisting-position
sales cannot complete the test. Missing cost data stays unknown, not zero profit.

Distinct outcomes include:

- No scan evidence / no BUY signal / pre-submission rule or risk block.
- BUY observed but no submission. A BUY observation is not final order eligibility.
- Broker rejection / accepted but not filled / broker fill awaiting ledger match.
- Confirmed entry / confirmed linked roundtrip / invalid policy evidence.
- Service paused or stale, policy/account/deployment changed, holiday, and
  terminal domestic API failures are reported separately.

Reports include repeated decision reasons, candidate funnel, contemporaneous
KOSPI context, order IDs, quantities/prices and available cost-adjusted exit P&L.
Intraday cumulative volume is not treated as a final or time-normalized volume.
No order at the deadline does **not** pass execution acceptance and does not
automatically imply an unprofitable strategy or justify weakening risk guards.

## Persistence and tests

- Immutable arm event: `domestic_execution_acceptance_armed`.
- Checkpoints and delivery status: `domestic_execution_acceptance_check`.
- Review #143 retains `execution_acceptance_plan` and `execution_acceptance_latest`.
  Updates merge evidence without clearing old outcomes or changing `reviewed_at`.
- Notifications deduplicate acknowledged checkpoint/status keys. Failed sends
  retry on the next timer invocation. An ambiguous network response can still
  produce a duplicate; no exactly-once delivery claim is made.
- Coverage includes real-account rejection, policy/account drift, old/virtual
  trades, unfilled/partial orders, mismatched broker history, missing cycle ledger,
  malformed policy evidence, unreproducible strategy signals, unrelated exits,
  missing costs, notification retry and completed-session non-restart.
- Final full suite: **1087 passed in 140.40s**. The strengthened real-policy path
  and checker subset passed 25 tests; final notification formatting was also
  checked with all 24 acceptance tests. User-manager `Linger=yes` is confirmed.
- New/changed-code Ruff F/E9 passes. Existing unrelated unused imports and one
  placeholder-free f-string in telegram_control remain outside this change;
  that module is also checked with F401/F541 ignored. Systemd unit and calendar
  syntax validation passes.
- Backup: `data/trading_backup_20261001_095205_pre_execution_acceptance.db`,
  647405568 bytes, mode 600, quick_check=ok, foreign-key errors=0, SHA-256
  `54cd7c066ff94c66f10df6c63d45027edbbabde0f390ad935ae43664ef6f742d`.

Natural v12 fills have not been claimed before the scheduled session. Reviews
#140/#141/#143 retain their separate profitability criteria: at least five
confirmed closes across three final regular sessions, net of costs and stratified
by market regime and holding time. A single successful execution cannot establish
a profitable strategy, and this task does not promote anything to real trading.
