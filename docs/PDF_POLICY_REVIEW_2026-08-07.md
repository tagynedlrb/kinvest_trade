# How to Pick Good Stocks: Policy Review (2026-08-07)

## Scope

- Reviewed all 35 pages of
  `/home/ubuntu/How_to_Pick_Good_Stocks_SNULife_Edition.pdf`.
- The document is educational material centered on swing trading, options,
  sector rotation, governance, and liquidity. Its examples are not treated as
  causal evidence for this intraday system.
- Each market keeps an independent policy. A US observation is not a reason to
  impose the same rule on Korea.

## Adopted now

### Market context before an ordinary long

- Pages 4-5 distinguish the path of a move from its endpoint. Pages 8-11 and
  18-23 put market and sector context ahead of an individual stock.
- The production ledger supplies direct US evidence: on 2026-08-06, the first
  same-session Nasdaq observation did not arrive until 09:35 ET. ZETA and SHOP
  entered at 09:31-09:32 with no same-session context and then closed for a
  combined `-$236.04` after costs.
- US ordinary longs therefore require an available same-New-York-session
  market observation no older than 600 seconds. The rule admits entries as
  soon as the observation exists; it is not a fixed opening ban.
- US inverse products are exempt because their dedicated benchmark regime is
  separately validated. Korea remains disabled for this gate because all 26
  post-deployment domestic entries had same-session context and no matching
  failure was observed.

### Sector context as a shadow feature

- Pages 19-23 and 27-30 motivate checking whether a stock is moving with its
  sector and whether capital is rotating. The TradingView candidate response
  already supports sector and industry fields, so those fields and the
  selected pool's change distribution are now retained at entry.
- The context records cohort size, average and median change, positive-member
  rate, target-relative change, and a provisional long-support flag.
- It does not block orders. The cohort is only the selected high-relative-
  volume pool, not a broad sector benchmark. Industry momentum research also
  supports studying a sector component, but it does not validate this
  intraday proxy or a live threshold
  ([Moskowitz and Grinblatt, 1999](https://doi.org/10.1111/0022-1082.00146)).
- Domestic KIS sector identity is retained, but breadth remains unevaluable
  until a suitable domestic sector benchmark or cohort source is added.

## Already covered

- Pages 31-33 emphasize exit liquidity and sizing relative to executable
  volume. Existing minimum price/volume/turnover, spread, orderable quantity,
  position cap, stop, and stale-order controls already cover the applicable
  intraday risk. No new duplicate filter is added.
- Pages 4-17 emphasize range, repeated behavior, and the path within the day.
  Existing ATR/range, intraday trend, VWAP, spread, session-range-position,
  stop, and forward-price logs retain the relevant measurable pieces.

## Not adopted

- CEO candor and incentives on pages 24-26 are fundamental and multi-period
  inputs with no validated intraday feed.
- The multi-month range boxes and option strike/expiry examples on pages
  10-17 do not match the system's holding period or instrument set.
- The document's fixed `$1B` market-cap and `$1` price filters on page 32 are
  not copied. The current US pool already uses `$300M`, `$5`, volume and spread
  filters. Raising the cap would reduce frequency without a same-system
  after-cost comparison.
- A blanket opening delay is rejected. The observed defect was missing market
  context, so the narrow rule waits for that data rather than an arbitrary
  number of minutes.

## Forward validation

- For the US market gate, collect at least three final Nasdaq sessions with a
  natural `entry_market_regime_unavailable` or stale intervention. Evaluate
  deduplicated blocked candidates at 15, 30 and 60 minutes after conservative
  round-trip costs. Revert or narrow the rule if valid same-session entries are
  blocked, if inverse entries are blocked, or if positive after-cost missed
  opportunities persist across three final sessions and outweigh avoided
  losses.
- For sector shadow context, require at least five final sessions and 20
  evaluable confirmed entries before considering a live threshold. Compare
  supportive and non-supportive groups within each market and regime. Do not
  pool domestic and US observations or infer causality from the selected
  candidate cohort.
- Trading frequency remains unchanged now. Existing WAIT evidence has no
  same-regime, after-cost positive bucket spanning the required three final
  sessions, so threshold loosening is not justified.
