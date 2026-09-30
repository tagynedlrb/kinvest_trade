#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path
from statistics import mean, median

from kinvest_trade.config import load_app_config
from kinvest_trade.liquidity_lab import LiquidityLabService
from kinvest_trade.market_sessions import (
    is_krx_regular_session,
    is_us_regular_session,
    minutes_until_regular_session_close,
)
from kinvest_trade.momentum_policy import evaluate_entry_setup
from kinvest_trade.strategy.manager import PriorityStrategyManager
from kinvest_trade.technical_signals import MovingAverageSnapshot
from kinvest_trade.time_utils import parse_datetime


def regular(market, timestamp):
    value = parse_datetime(timestamp)
    if value is None:
        return False
    return (
        is_krx_regular_session(value)
        if market == "domestic"
        else is_us_regular_session(value)
    )


def main():
    parser = argparse.ArgumentParser(
        description="Read-only policy reachability replay; not a fills backtest."
    )
    parser.add_argument("db", type=Path)
    parser.add_argument("--since", default="2026-09-01T00:00:00+00:00")
    parser.add_argument("--baseline", default="94096b8")
    args = parser.parse_args()
    config = load_app_config()
    service = LiquidityLabService.__new__(LiquidityLabService)
    service.config = config
    old_policies = {}
    for market in ("domestic", "overseas"):
        raw = json.loads(
            subprocess.check_output(
                [
                    "git",
                    "show",
                    f"{args.baseline}:config/market_policies/{market}.json",
                ],
                text=True,
            )
        )
        current = getattr(config.market_policies, market).auto_trade
        old_policies[market] = replace(
            current,
            entry_momentum_fallback_enabled=False,
            entry_cost_guard_strategy_flags=[],
            **raw["parameters"],
        )
    counts = Counter()
    cohorts = defaultdict(list)
    decisions = {}
    examples = []
    with sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM entry_horizon_shadows WHERE opened_at>=? ORDER BY opened_at,id",
            (args.since,),
        ).fetchall()
        for row in rows:
            group = row["group_id"]
            if group not in decisions:
                market = row["market"]
                context = json.loads(row["context_json"])
                snapshot = MovingAverageSnapshot(**context["signal_snapshot"])
                current = getattr(config.market_policies, market).auto_trade
                opened = parse_datetime(row["opened_at"])
                regime = context.get("entry_market_regime", {})
                eligible = (
                    regular(market, row["opened_at"])
                    and (minutes_until_regular_session_close(market, opened) or 0) >= 60
                    and bool(regime.get("available"))
                    and regime.get("observation_age_sec") is not None
                    and regime["observation_age_sec"] <= 600
                    and row["benchmark_return_pct"] is not None
                    and row["benchmark_return_pct"] >= 0
                    and row["symbol"]
                    not in current.inverse_etf_symbols + current.leveraged_etf_symbols
                )
                if market == "domestic":
                    eligible = eligible and context.get("product_type") in {
                        "ETF",
                        "ETN",
                    }
                    eligible = (
                        eligible and (row["benchmark_range_position"] or 0) >= 0.5
                    )
                decisions[group] = {}
                for name, policy in (
                    ("baseline", old_policies[market]),
                    ("candidate", current),
                ):
                    result = PriorityStrategyManager(policy).evaluate(
                        row["symbol"], snapshot, commit=False
                    )
                    formula = evaluate_entry_setup(
                        policy, snapshot, symbol=row["symbol"]
                    )
                    formula_ready = formula.ready
                    if (
                        name == "baseline"
                        and snapshot.volume_ratio
                        < policy.volume_spike_ratio
                        * policy.volume_spike_ratio_prefilter_factor
                    ):
                        formula_ready = False
                    reason = "signal_not_buy" if result.signal != "BUY" else ""
                    if (
                        not reason
                        and result.flag not in policy.entry_strategy_allowlist
                    ):
                        reason = "strategy_not_allowed"
                    if (
                        not reason
                        and result.flag in policy.entry_confirmation_strategy_flags
                        and not formula_ready
                    ):
                        reason = "formula_not_ready"
                    if (
                        not reason
                        and result.flag in policy.entry_cost_guard_strategy_flags
                    ):
                        reason = service._entry_edge_block_reason(
                            market=market, signal_snapshot=snapshot
                        )
                    if not reason and not eligible:
                        reason = "session_market_or_product_gate"
                    counts[(market, name, reason or "eligible_signal")] += 1
                    decisions[group][name] = not reason
                    if name == "candidate" and not reason:
                        examples.append(
                            {
                                "market": market,
                                "symbol": row["symbol"],
                                "opened_at": row["opened_at"],
                                "strategy": result.flag,
                                "volume_ratio": snapshot.volume_ratio,
                                "atr_pct": snapshot.atr_pct,
                                "benchmark_return_pct": row["benchmark_return_pct"],
                            }
                        )
            if (
                row["status"] != "MATURED"
                or row["horizon_minutes"] not in {15, 30, 60}
                or row["observation_lag_sec"] is None
                or row["observation_lag_sec"] > 180
                or not regular(row["market"], row["observed_at"])
            ):
                continue
            for name, accepted in decisions[group].items():
                if accepted:
                    cohorts[(row["market"], name, row["horizon_minutes"])].append(row)
    outcomes = []
    for (market, version, horizon), values in sorted(cohorts.items()):
        net = sorted(float(row["estimated_net_pnl_pct"]) - 0.001 for row in values)
        trim = len(net) // 10
        outcomes.append(
            {
                "market": market,
                "version": version,
                "horizon_minutes": horizon,
                "samples": len(net),
                "sessions": len({r["entry_session_date"] for r in values}),
                "mean_net": mean(net),
                "median_net": median(net),
                "trimmed_mean_net": mean(net[trim:-trim] if trim else net),
                "positive_fraction": sum(v > 0 for v in net) / len(net),
            }
        )
    print(
        json.dumps(
            {
                "baseline_commit": args.baseline,
                "since": args.since,
                "episodes": len(decisions),
                "counts": [
                    {"market": k[0], "version": k[1], "reason": k[2], "count": v}
                    for k, v in sorted(counts.items())
                ],
                "candidate_examples": examples,
                "outcomes": outcomes,
                "limitations": [
                    "Sampled episodes, not independent trades; no budget or fills replay.",
                    "Costs from recorded cohort plus 0.1% slippage assumption, not realized P&L.",
                    "Domestic replay uses ETF/ETN and upper-half benchmark range, not full hysteresis replay.",
                    "Missing outcomes and observation lag over 180 seconds are excluded.",
                    "Core symbols absent from the old pool have no historical sample; no profitability claim.",
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
