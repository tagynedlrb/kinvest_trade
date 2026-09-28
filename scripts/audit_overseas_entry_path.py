#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import fields
from pathlib import Path
from statistics import mean, median

from kinvest_trade.config import load_app_config
from kinvest_trade.market_sessions import is_us_orderable_session_for_env
from kinvest_trade.momentum_policy import evaluate_entry_setup
from kinvest_trade.strategy.manager import PriorityStrategyManager
from kinvest_trade.technical_signals import MovingAverageSnapshot
from kinvest_trade.time_utils import parse_datetime


def paper_orderable(timestamp: str) -> bool:
    observed_at = parse_datetime(timestamp)
    return observed_at is not None and is_us_orderable_session_for_env(observed_at, "vps")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only replay of recorded overseas entry signal routing."
    )
    parser.add_argument("db_path", type=Path)
    parser.add_argument("--since", required=True, help="Inclusive UTC ISO timestamp")
    parser.add_argument("--settings", default="config/fixed_config.json")
    args = parser.parse_args()
    config = load_app_config(args.settings).market_policies.overseas
    policy = config.auto_trade
    snapshot_fields = {field.name for field in fields(MovingAverageSnapshot)}
    counts: Counter[str] = Counter()
    examples: list[dict] = []
    invalid_snapshots = 0
    orderable_episodes = 0

    with sqlite3.connect(args.db_path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        # One entry episode has eight horizons; do not count it eight times.
        rows = conn.execute(
            """
            SELECT * FROM entry_horizon_shadows
            WHERE id IN (
                SELECT MIN(id) FROM entry_horizon_shadows
                WHERE market = 'overseas' AND opened_at >= ?
                GROUP BY group_id
            ) ORDER BY opened_at
            """,
            (args.since,),
        ).fetchall()
        for row in rows:
            if not paper_orderable(row["opened_at"]):
                counts["outside_paper_orderable_session"] += 1
                continue
            orderable_episodes += 1
            try:
                context = json.loads(row["context_json"])
                snapshot = MovingAverageSnapshot(**{
                    key: value
                    for key, value in context["signal_snapshot"].items()
                    if key in snapshot_fields
                })
            except (KeyError, TypeError, ValueError):
                invalid_snapshots += 1
                continue
            if row["symbol"] in policy.inverse_etf_symbols:
                counts["inverse_excluded_from_ordinary_replay"] += 1
                continue
            consensus = PriorityStrategyManager(policy).evaluate(
                row["symbol"], snapshot, commit=False,
            )
            formula = evaluate_entry_setup(policy, snapshot, symbol=row["symbol"])
            key = f"{consensus.signal}:{consensus.flag}|formula:{formula.reason}"
            counts[key] += 1
            if consensus.signal == "BUY" and consensus.flag == "VWAP+VOL+RSI":
                examples.append({
                    "symbol": row["symbol"],
                    "opened_at": row["opened_at"],
                    "recorded_policy_id": row["policy_id"],
                    "original_block_reason": row["block_reason"],
                    "formula_ready": formula.ready,
                    "formula_reason": formula.reason,
                    "v6_allowlist_pass": False,
                    "current_allowlist_pass": consensus.flag in policy.entry_strategy_allowlist,
                    "benchmark_return_pct": row["benchmark_return_pct"],
                    "volume_ratio": snapshot.volume_ratio,
                })
        horizon_rows = conn.execute(
            """
            SELECT cohort, horizon_minutes, entry_session_date,
                   opened_at, observed_at, estimated_net_pnl_pct
            FROM entry_horizon_shadows
            WHERE market = 'overseas' AND opened_at >= ? AND status = 'MATURED'
              AND horizon_minutes IN (15, 30, 60)
            ORDER BY cohort, horizon_minutes
            """,
            (args.since,),
        ).fetchall()

    buckets = defaultdict(list)
    for row in horizon_rows:
        if (
            paper_orderable(row["opened_at"])
            and paper_orderable(row["observed_at"] or "")
            and row["estimated_net_pnl_pct"] is not None
        ):
            buckets[(row["cohort"], row["horizon_minutes"])].append(row)
    horizons = []
    for (cohort, horizon), bucket in buckets.items():
        returns = sorted(row["estimated_net_pnl_pct"] for row in bucket)
        trim = len(returns) // 10
        horizons.append({
            "cohort": cohort,
            "horizon_minutes": horizon,
            "samples": len(returns),
            "sessions": len({row["entry_session_date"] for row in bucket}),
            "mean_net_return": mean(returns),
            "median_net_return": median(returns),
            "trimmed_mean_net_return": mean(returns[trim:-trim] if trim else returns),
            "positive_fraction": sum(value > 0 for value in returns) / len(returns),
        })

    print(json.dumps({
        "since": args.since,
        "replay_policy_id": config.policy_id,
        "episode_count": len(rows),
        "orderable_episode_count": orderable_episodes,
        "invalid_snapshots": invalid_snapshots,
        "signal_formula_counts": dict(counts.most_common()),
        "triple_consensus_count": len(examples),
        "triple_formula_ready_count": sum(row["formula_ready"] for row in examples),
        "triple_consensus_examples": examples[:20],
        "observed_cohort_horizons": horizons,
        "limitations": [
            "Entry horizon episodes are sampled observations, not independent trades.",
            "Replay checks signals and formula only; broker fills, market gates, session budgets and slippage are not replayed.",
            "Observed horizon returns belong to the original cohorts, not the proposed policy.",
        ],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
