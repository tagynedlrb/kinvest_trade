# Full Data and Settlement Audit - 2026-08-26

## Scope

This audit continues the domestic-frequency and service review after commit
`aba7bac`. It uses the live ledger through 2026-08-26 17:02 UTC, a SQLite
online backup, systemd/journald, KIS quote responses, and forward WAIT cohorts.
It does not infer fills from submissions: BUY_REAL and SELL_REAL samples require
filled broker execution groups.

## Integrity and Runtime Findings

- SQLite `quick_check=ok`; foreign-key violations are zero.
- Execution-event orphans, cycle-execution orphans, impossible execution
  quantities, nonpositive virtual positions/pending rows, negative lab holdings,
  and finalized filled groups without a cycle row are all zero.
- There are no unfinalized broker executions.
- The service has remained `active/running` with `NRestarts=0` since 14:20:52
  UTC. From that deployment through the audit cutoff, 8,183 API attempts had one
  recovered retry and zero terminal failures. Telegram sends in the recent
  seven-day window have 309 successes and zero failures.
- The only post-deployment warnings are intentional 50-cycle low-frequency
  diagnostics. They separate entry blockers from maintenance observations.
- Final market rows exist for 70 domestic and 120 overseas sessions. Session
  reviews exist for the same 70 domestic and 120 overseas final sessions.
  Older `unknown` regime fields are confined to the initial warm-up observations
  with insufficient prior samples.

## Settlement Attribution Defect

Legacy virtual exits recorded strategy PnL at the virtual decision time, then
later recorded the real account settlement without carrying the original
strategy metadata. This left:

- 21 confirmed `virtual_sell_settlement` cycle rows without strategy, entry
  owner, or entry time;
- 119 settlement execution rows and their 119 order events without attribution;
- one current NPAC pending row without complete attribution.

The migration now links settlements to the prior confirmed BUY_REAL by market,
symbol, time, and entry-price tolerance. Where an old imported holding has no
BUY_REAL row, it uses the persisted position state and labels the provenance
`legacy_position_state` instead of inventing an entry reason. A fresh production
snapshot migrated exactly 21 cycle rows, 119 executions, 119 events, and one
pending row; all corresponding blank counts became zero. Re-running the
migration makes no further changes.

New virtual exits persist `strategy_flag`, `entry_by`, `entry_reason`, and
`entry_time` from the pending row through submission, partial settlement, broker
execution, account cycle, and audit event.

## Probe Cohort Result

`/lab_guard` now links every filled strategy-guard probe to its confirmed exit,
including a later non-session account settlement. The 336-hour overseas cohort
contains nine filled and nine closed entries:

| Strategy | Closed | Wins | Mean net | Median net | Capital-weighted net | Net USD |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| VOL+RSI | 2 | 0 | -0.545% | -0.545% | -0.547% | -$7.69 |
| VWAP+RSI | 5 | 2 | +0.545% | -0.270% | +0.546% | +$21.82 |
| VWAP+VOL+RSI | 2 | 0 | -0.564% | -0.564% | -0.550% | -$7.94 |
| Total | 9 | 2 | +0.056% | -0.527% | +0.090% | +$6.19 |

Two approximately +2% trades account for the positive total; seven entries lost
after costs. The bounded probe remains useful for gathering evidence, but this
distribution does not justify general release or larger probe sizing.

## Frequency Decision and Market Context

At 17:02 UTC the temporary NASDAQ Composite regime was
`down|unknown|calm`, return -0.357%. For the last 336 hours, all large final
WAIT cohorts remain below the 0.50% minimum overseas round-trip cost after both
ordinary and 10% trimmed means. Examples:

- `trend_down`, final up/normal/calm, 516 episodes over four days: 60-minute
  gross +0.191%, trimmed +0.148%, minimum-cost net -0.309%/-0.352%;
- `volume_low`, same regime, 481 episodes: 60-minute gross -0.109%, trimmed
  -0.087%, minimum-cost net -0.609%/-0.587%;
- standalone VWAP blocked, same regime, 197 episodes: 60-minute gross +0.228%,
  trimmed +0.109%, minimum-cost net -0.272%/-0.391%;
- standalone VOL blocked, same regime, 182 episodes: 60-minute minimum-cost net
  -0.727%, trimmed net -0.711%.

All overseas WAIT episodes have zero positive spread observations because KIS
mock quotes omit bid and ask. The report now displays this coverage and both
trimmed gross/net means. Missing quotes no longer receive the one-point
"tight spread" activity bonus. The 0.50% fallback cost remains in force.

Broad threshold, volume-floor, strategy-guard, or position-limit loosening is
therefore rejected. It would increase trades but has negative measured net
expectancy. Existing small probes remain limited to nonnegative, fresh benchmark
conditions. The current negative benchmark correctly blocks them.

Domestic 2026-08-26 ended KOSPI +0.971%, `up|normal|calm`, with zero entries,
six exits, and +5,912.50 KRW net. The prior cross-session consecutive-loss
misattribution is fixed and replayed, but its forward proof requires the next KRX
session; no unverified threshold change is added meanwhile.

## NPAC Repeated No-Fill Handling

NPAC has 396 pending shares and eight failed settlement sessions. Ninety-four
orders were accepted, left open for the bounded stale interval, confirmed at zero
fills, canceled, and finalized. On 2026-08-26, three 50-basis-point aggressive
limits at $10.4136 also received no fills. The KIS quote showed last $10.4659,
no bid/ask, and current-session volume zero.

NPAC remains a Nasdaq-listed New Providence Acquisition Corp. III Class A share,
not a confirmed cash-merger case like CCRN. Nasdaq currently exposes the symbol
but no quote data, while the June 2026 SEC filing identifies NPAC as Nasdaq-listed.
The ledger and holding must therefore remain intact.

After at least two failed sessions, a live quote with zero session volume now
defers further same-session settlement submissions and emits one Telegram/event
notice. The system continues checking and automatically resumes the existing
bounded aggressive-limit path when volume becomes positive. It does not switch
to an unbounded market order without a bid or price discovery.

Sources: [Nasdaq NPAC](https://www.nasdaq.com/market-activity/stocks/npac),
[SEC Form 8-K](https://www.sec.gov/Archives/edgar/data/2048948/000121390026066354/ea0293890-8k_newpro3.htm).

## Forward Validation

- Next KRX session: verify that a prior-day loss cannot trigger the same-session
  three-loss entry stop and that eligible entries can reach order submission.
- Next three final NASDAQ sessions: retain final regime, WAIT trimmed/median net,
  spread coverage, guard-probe fills/exits, and order-submission frequency.
- NPAC: preserve the pending quantity while volume is zero; verify one defer event
  per session and automatic resumption only after positive volume.
- Do not promote inverse shadow policies: current closed results remain domestic
  8 trades at -0.622% mean net and overseas 5 trades at -0.926%.

## Verification Before Deployment

- Full test suite: 875 passed in 121.52 seconds.
- Affected repository, settlement, analysis, Telegram, and position modules:
  234 passed before the full run.
- `compileall`, configuration JSON parsing, and `git diff --check`: passed.
- Online backup:
  `data/trading_backup_20260826_170710_pre_settlement_attribution_deploy.db`,
  647,405,568 bytes, mode 600, `quick_check=ok`, zero foreign-key violations,
  SHA-256 `3ec1b4a532ffd71797f3f4636ec9b43eb99028959d258253a71af925a7609504`.
